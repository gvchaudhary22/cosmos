"""
Contextual Header Enricher — Anthropic's Contextual Retrieval technique.

Before embedding each chunk, prepends a 2-3 sentence contextual header generated
by Claude Opus that explains what the chunk is about within the broader document
and COSMOS knowledge base. Reduces retrieval failures by ~49% (Anthropic benchmark).

Uses enrichment cache (cosmos_enrichment_cache table) to avoid re-calling Opus on
unchanged documents.

Usage:
    enricher = ContextualHeaderEnricher()
    enriched_docs = await enricher.enrich_batch(documents)
"""

import asyncio
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

# Pillar descriptions for context
PILLAR_CONTEXT = {
    "pillar_1_schema": "Pillar 1 (Schema Intelligence) — database table schemas with columns, types, relationships, state machines, and validation rules for Shiprocket's e-commerce platform.",
    "pillar_3_api": "Pillar 3 (API/MCP Tools) — REST API endpoint documentation with request/response schemas, authentication, tool assignments, and agent routing for Shiprocket's MultiChannel API.",
    "pillar_4_pages": "Pillar 4 (Page Intelligence) — frontend page documentation for Shiprocket's ICRM admin panel and seller panel, including UI components, API bindings, and user roles.",
    "pillar_5_modules": "Pillar 5 (Module Documentation) — codebase module docs covering architecture, dependencies, and ownership.",
    "pillar_6_actions": "Pillar 6 (Action Contracts) — executable operations with full input/output schemas, preconditions, approval gates, and observability rules for Shiprocket operations.",
    "pillar_7_workflows": "Pillar 7 (Workflow Runbooks) — multi-step operational workflows that chain multiple actions in sequence with decision points.",
    "pillar_8_negative": "Pillar 8 (Negative Examples) — documented anti-patterns and mistakes to avoid when handling Shiprocket ICRM operations.",
}

CONTEXT_PROMPT = """<document_context>
This document is from {pillar_description}

Document entity: {entity_id}
Repository: {repo_id}
</document_context>

<full_document>
{whole_document}
</full_document>

Here is a chunk from this document that will be embedded for retrieval:
<chunk>
{chunk_content}
</chunk>

Write a short context (2-3 sentences) to situate this chunk within the COSMOS knowledge base for Shiprocket's ICRM AI assistant. Focus on:
1. What specific entity/concept this chunk describes
2. How it relates to ICRM operations (order management, shipment tracking, seller support, billing)
3. What type of operator query would need this information

Return ONLY the context text, nothing else."""


def _content_hash(content: str) -> str:
    """SHA-256 hash of content for cache key."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ContextualHeaderEnricher:
    """Generates contextual headers for KB chunks using Claude Opus 4.6."""

    def __init__(self, model: str = "claude-opus-4-6", max_concurrent: int = 3):
        self.model = model
        self.max_concurrent = max_concurrent
        self._cli = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cache_hits = 0
        self._cache_misses = 0
        self._api_calls = 0

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=120)
        return self._cli

    async def enrich_batch(
        self,
        documents: List[Dict[str, Any]],
        whole_document_text: str = "",
    ) -> List[Dict[str, Any]]:
        """Enrich a batch of chunked documents with contextual headers.

        Args:
            documents: List of IngestDocument-like dicts with 'content', 'entity_id', 'repo_id', 'metadata'
            whole_document_text: The full source document text (for context)

        Returns:
            Same documents with 'content' replaced by enriched text (context header prepended)
        """
        if not documents:
            return documents

        start = time.time()
        enriched = []

        # Check cache first for all documents
        cached, uncached = await self._check_cache_batch(documents)
        self._cache_hits += len(cached)
        self._cache_misses += len(uncached)

        # Apply cached enrichments
        for doc, cached_text in cached:
            doc = dict(doc)
            doc["content"] = cached_text
            doc.setdefault("metadata", {})["enrichment"] = "cached"
            enriched.append(doc)

        # Generate new enrichments for uncached (parallel with semaphore)
        if uncached:
            tasks = [
                self._enrich_single(doc, whole_document_text)
                for doc in uncached
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for doc, result in zip(uncached, results):
                if isinstance(result, Exception):
                    logger.warning("enrichment.failed", entity_id=doc.get("entity_id"), error=str(result))
                    doc = dict(doc)
                    doc.setdefault("metadata", {})["enrichment"] = "failed"
                    enriched.append(doc)
                else:
                    enriched.append(result)

        elapsed = (time.time() - start) * 1000
        logger.info(
            "enrichment.batch_complete",
            total=len(documents),
            cached=len(cached),
            generated=len(uncached),
            api_calls=self._api_calls,
            elapsed_ms=round(elapsed),
        )

        return enriched

    async def _enrich_single(self, doc: Dict, whole_doc_text: str) -> Dict:
        """Enrich a single document with contextual header."""
        async with self._semaphore:
            content = doc.get("content", "")
            entity_id = doc.get("entity_id", "unknown")
            repo_id = doc.get("repo_id", "")
            metadata = doc.get("metadata", {})

            # Determine pillar
            entity_type = doc.get("entity_type", "")
            pillar = self._detect_pillar(entity_type, entity_id)
            pillar_desc = PILLAR_CONTEXT.get(pillar, "COSMOS knowledge base for Shiprocket ICRM.")

            # Build prompt
            prompt = CONTEXT_PROMPT.format(
                pillar_description=pillar_desc,
                entity_id=entity_id,
                repo_id=repo_id,
                whole_document=whole_doc_text[:4000] if whole_doc_text else content[:2000],
                chunk_content=content[:3000],
            )

            try:
                cli = self._get_cli()
                context_header = await cli.prompt(prompt, model=self.model)
                self._api_calls += 1
                context_header = context_header.strip()
                if not context_header:
                    context_header = self._fallback_header(doc, pillar)
            except Exception as e:
                logger.warning("enrichment.opus_call_failed", entity_id=entity_id, error=str(e))
                # Fallback: generate a simple header from metadata
                context_header = self._fallback_header(doc, pillar)

            enriched_text = f"{context_header}\n\n{content}"

            # Cache the result
            await self._cache_put(content, enriched_text, pillar, entity_id)

            doc = dict(doc)
            doc["content"] = enriched_text
            doc.setdefault("metadata", {})["enrichment"] = "opus"
            doc["metadata"]["pillar"] = pillar
            return doc

    def _detect_pillar(self, entity_type: str, entity_id: str) -> str:
        """Detect which pillar a document belongs to."""
        et = entity_type.lower()
        eid = entity_id.lower()

        if et in ("schema", "table") or "table:" in eid:
            return "pillar_1_schema"
        if et in ("api_tool", "api") or "api:" in eid:
            return "pillar_3_api"
        if et in ("page", "page_intent", "cross_repo"):
            return "pillar_4_pages"
        if et.startswith("module"):
            return "pillar_5_modules"
        if et in ("action_contract",) or "action:" in eid or "action." in eid:
            return "pillar_6_actions"
        if et in ("workflow", "runbook"):
            return "pillar_7_workflows"
        if et in ("negative", "anti_pattern"):
            return "pillar_8_negative"
        return "pillar_3_api"  # default

    def _fallback_header(self, doc: Dict, pillar: str) -> str:
        """Generate a simple header without LLM when API call fails."""
        entity_id = doc.get("entity_id", "unknown")
        entity_type = doc.get("entity_type", "document")
        repo_id = doc.get("repo_id", "")
        domain = doc.get("metadata", {}).get("domain", "")

        parts = [f"[{pillar.replace('_', ' ').title()}]"]
        if entity_type:
            parts.append(f"Type: {entity_type}")
        if entity_id:
            parts.append(f"Entity: {entity_id}")
        if domain:
            parts.append(f"Domain: {domain}")
        if repo_id:
            parts.append(f"Repository: {repo_id}")

        return " | ".join(parts)

    # --- Cache operations ---

    async def _check_cache_batch(
        self, documents: List[Dict]
    ) -> tuple:
        """Check cache for all documents. Returns (cached, uncached) lists."""
        cached = []
        uncached = []

        try:
            async with AsyncSessionLocal() as session:
                for doc in documents:
                    content = doc.get("content", "")
                    ch = _content_hash(content)

                    result = await session.execute(
                        text("SELECT enriched_text FROM cosmos_enrichment_cache WHERE content_hash = :h"),
                        {"h": ch},
                    )
                    row = result.fetchone()
                    if row:
                        cached.append((doc, row[0]))
                    else:
                        uncached.append(doc)
        except Exception as e:
            logger.warning("enrichment.cache_check_failed", error=str(e))
            uncached = list(documents)

        return cached, uncached

    async def _cache_put(
        self, original_content: str, enriched_text: str, pillar: str, entity_id: str
    ):
        """Store enrichment result in cache."""
        try:
            ch = _content_hash(original_content)
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""INSERT INTO cosmos_enrichment_cache
                            (content_hash, enriched_text, pillar, entity_id, model_used)
                            VALUES (:h, :et, :p, :eid, :model)
                            ON DUPLICATE KEY UPDATE enriched_text = :et, updated_at = NOW()"""),
                    {"h": ch, "et": enriched_text, "p": pillar, "eid": entity_id, "model": self.model},
                )
                await session.commit()
        except Exception as e:
            logger.debug("enrichment.cache_put_failed", error=str(e))

    def get_stats(self) -> Dict:
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "api_calls": self._api_calls,
            "model": self.model,
        }

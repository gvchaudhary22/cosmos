"""
Synthetic Q&A Generator — Creates realistic ICRM operator queries for each KB chunk.

Generates 5 English queries per chunk using Claude Opus.
These are embedded alongside the original chunks, creating 5x more retrieval targets
that bridge the gap between operator language and technical documentation.

Uses enrichment cache to avoid regenerating for unchanged content.

Usage:
    generator = SyntheticQAGenerator()
    synthetic_docs = await generator.generate_batch(enriched_documents)
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

SYNTHETIC_QA_PROMPT = """You are generating realistic ICRM (Internal CRM) operator queries for Shiprocket, India's largest e-commerce shipping platform. ICRM operators handle 200+ tickets/day about orders, shipments, seller issues, and billing.

Given this knowledge base document, generate exactly 5 English queries that an ICRM operator would ask that this document helps answer.

REQUIREMENTS:
1. All 5 queries must be in English
2. Queries must reflect real operator language: direct, practical, sometimes with specific IDs
3. Include placeholders like "12345" for order IDs, "98765" for AWB numbers where relevant
4. Include common ICRM abbreviations: AWB, COD, NDR, RTO, OFD, WD, FM, LM
5. Range from simple lookups to troubleshooting to action requests
6. Cover different angles: status check, root cause investigation, action execution, policy clarification, escalation

<document>
{document_text}
</document>

<metadata>
Pillar: {pillar}
Entity: {entity_name}
Domain: {domain}
</metadata>

Return ONLY a JSON array, no other text:
[
  {{"query": "...", "language": "en", "complexity": "simple"}},
  {{"query": "...", "language": "en", "complexity": "simple"}},
  {{"query": "...", "language": "en", "complexity": "moderate"}},
  {{"query": "...", "language": "en", "complexity": "moderate"}},
  {{"query": "...", "language": "en", "complexity": "complex"}}
]"""


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class SyntheticQAGenerator:
    """Generates synthetic ICRM operator queries for KB chunks using Claude Opus 4.6."""

    def __init__(self, model: str = "claude-opus-4-6", max_concurrent: int = 3):
        self.model = model
        self.max_concurrent = max_concurrent
        self._cli = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._generated = 0
        self._cached = 0
        self._failed = 0

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=120)
        return self._cli

    async def generate_batch(
        self, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Generate synthetic Q&A for a batch of enriched documents.

        Args:
            documents: Enriched documents (output from ContextualHeaderEnricher)

        Returns:
            List of synthetic IngestDocument-like dicts (entity_type='synthetic_qa')
            ready for ingestion into Qdrant alongside originals.
        """
        if not documents:
            return []

        start = time.time()
        all_synthetic = []

        # Check cache first
        cached_results, uncached_docs = await self._check_cache_batch(documents)

        # Process cached
        for doc, qa_list in cached_results:
            self._cached += 1
            synthetic_docs = self._qa_to_ingest_docs(doc, qa_list)
            all_synthetic.extend(synthetic_docs)

        # Generate new for uncached
        if uncached_docs:
            tasks = [self._generate_single(doc) for doc in uncached_docs]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for doc, result in zip(uncached_docs, results):
                if isinstance(result, Exception):
                    logger.warning("synthetic_qa.failed", entity_id=doc.get("entity_id"), error=str(result))
                    self._failed += 1
                    continue

                qa_list, synthetic_docs = result
                all_synthetic.extend(synthetic_docs)

                # Cache the Q&A
                await self._cache_put(doc.get("content", ""), qa_list, doc)

        elapsed = (time.time() - start) * 1000
        logger.info(
            "synthetic_qa.batch_complete",
            input_docs=len(documents),
            synthetic_generated=len(all_synthetic),
            cached=self._cached,
            generated=self._generated,
            failed=self._failed,
            elapsed_ms=round(elapsed),
        )

        return all_synthetic

    async def _generate_single(self, doc: Dict) -> tuple:
        """Generate synthetic Q&A for a single document."""
        async with self._semaphore:
            content = doc.get("content", "")
            entity_id = doc.get("entity_id", "unknown")
            metadata = doc.get("metadata", {})
            pillar = metadata.get("pillar", "unknown")
            domain = metadata.get("domain", "")

            prompt = SYNTHETIC_QA_PROMPT.format(
                document_text=content[:3000],
                pillar=pillar,
                entity_name=entity_id,
                domain=domain,
            )

            try:
                cli = self._get_cli()
                raw_text = await cli.prompt(prompt, model=self.model)
                self._generated += 1

                raw_text = raw_text.strip()
                # Parse JSON from response (handle markdown code blocks)
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```")[1]
                    if raw_text.startswith("json"):
                        raw_text = raw_text[4:]
                qa_list = json.loads(raw_text)

            except json.JSONDecodeError:
                logger.warning("synthetic_qa.json_parse_failed", entity_id=entity_id)
                qa_list = self._fallback_qa(doc)
            except Exception as e:
                logger.warning("synthetic_qa.opus_failed", entity_id=entity_id, error=str(e))
                qa_list = self._fallback_qa(doc)

            synthetic_docs = self._qa_to_ingest_docs(doc, qa_list)
            return qa_list, synthetic_docs

    def _qa_to_ingest_docs(self, parent_doc: Dict, qa_list: List[Dict]) -> List[Dict]:
        """Convert Q&A pairs to IngestDocument-like dicts for Qdrant ingestion."""
        docs = []
        entity_id = parent_doc.get("entity_id", "unknown")
        repo_id = parent_doc.get("repo_id", "")
        metadata = parent_doc.get("metadata", {})

        for i, qa in enumerate(qa_list[:5]):
            query = qa.get("query", "")
            if not query or len(query) < 5:
                continue

            lang = qa.get("language", "en")
            complexity = qa.get("complexity", "simple")

            docs.append({
                "entity_type": "synthetic_qa",
                "entity_id": f"synqa:{entity_id}:{i}",
                "content": query,
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.85,
                "metadata": {
                    "parent_entity_id": entity_id,
                    "language": lang,
                    "complexity": complexity,
                    "synthetic": True,
                    "pillar": metadata.get("pillar", ""),
                    "domain": metadata.get("domain", ""),
                },
            })

        return docs

    def _fallback_qa(self, doc: Dict) -> List[Dict]:
        """Generate basic Q&A without LLM when API call fails."""
        entity_id = doc.get("entity_id", "")
        entity_type = doc.get("entity_type", "")
        domain = doc.get("metadata", {}).get("domain", "")

        parts = entity_id.replace(":", " ").replace("_", " ").split()
        base_query = " ".join(parts[-3:]) if len(parts) >= 3 else " ".join(parts)

        return [
            {"query": f"What is {base_query}?", "language": "en", "complexity": "simple"},
            {"query": f"How does {base_query} work in {domain}?", "language": "en", "complexity": "moderate"},
            {"query": f"Show me the details of {base_query}", "language": "en", "complexity": "simple"},
            {"query": f"What are the rules for {base_query} in {domain}?", "language": "en", "complexity": "moderate"},
            {"query": f"Troubleshoot {base_query} issue for a seller", "language": "en", "complexity": "complex"},
        ]

    # --- Cache operations ---

    async def _check_cache_batch(self, documents: List[Dict]) -> tuple:
        """Check cache for existing synthetic Q&A. Returns (cached, uncached)."""
        cached = []
        uncached = []

        try:
            async with AsyncSessionLocal() as session:
                for doc in documents:
                    content = doc.get("content", "")
                    ch = _content_hash(content)

                    result = await session.execute(
                        text("SELECT synthetic_qa FROM cosmos_enrichment_cache WHERE content_hash = :h AND synthetic_qa IS NOT NULL"),
                        {"h": ch},
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        try:
                            qa_list = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                            if qa_list and len(qa_list) > 0:
                                cached.append((doc, qa_list))
                                continue
                        except (json.JSONDecodeError, TypeError):
                            pass
                    uncached.append(doc)
        except Exception as e:
            logger.warning("synthetic_qa.cache_check_failed", error=str(e))
            uncached = list(documents)

        return cached, uncached

    async def _cache_put(self, original_content: str, qa_list: List[Dict], doc: Dict):
        """Store synthetic Q&A in enrichment cache."""
        try:
            ch = _content_hash(original_content)
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""UPDATE cosmos_enrichment_cache
                            SET synthetic_qa = :qa, updated_at = NOW()
                            WHERE content_hash = :h"""),
                    {"h": ch, "qa": json.dumps(qa_list)},
                )
                await session.commit()
        except Exception as e:
            logger.debug("synthetic_qa.cache_put_failed", error=str(e))

    def get_stats(self) -> Dict:
        return {
            "generated": self._generated,
            "cached": self._cached,
            "failed": self._failed,
            "model": self.model,
        }

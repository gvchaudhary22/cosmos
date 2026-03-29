"""
Codebase Intelligence — Tier 2 retrieval-driven code knowledge.

Pre-indexes .claude/docs/modules/ content into the vectorstore as
source_type='module_doc' at startup. At query time, retrieves relevant
module documentation via vector similarity search — no live filesystem access.

Flow:
  1. Startup: ingest all module docs into cosmos_embeddings (source_type=module_doc)
  2. Query: vector search for relevant module docs
  3. Extract: tables, APIs, code insights from retrieved chunks
  4. Refine: build better query for one brain retry
"""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class CodebaseContext:
    """Context extracted from pre-indexed module documentation."""
    relevant_chunks: List[Dict[str, Any]] = field(default_factory=list)
    db_tables: List[str] = field(default_factory=list)
    api_endpoints: List[str] = field(default_factory=list)
    code_insights: List[str] = field(default_factory=list)
    refined_query: str = ""
    modules_matched: List[str] = field(default_factory=list)
    suggested_db_template: Optional[str] = None


# Known DB tables in the Shiprocket codebase
_KNOWN_TABLES = {
    "orders", "shipments", "couriers", "ndr_requests", "wallets",
    "seller_wallets", "companies", "users", "tracking_events",
    "returns", "invoices", "transactions", "pickup_locations",
    "warehouses", "channels", "courier_rules", "rate_cards",
    "products", "skus", "manifest_items", "cod_remittances",
}

# DB query templates for Tier 3 (keyed by domain)
_DB_TEMPLATES = {
    "order_by_id": {
        "table": "orders",
        "query": "SELECT id, status, channel_order_id, company_id, courier_name, awb_code, updated_at FROM orders WHERE company_id = :company_id AND id = :entity_id LIMIT 1",
        "triggers": ["order", "order_id"],
    },
    "recent_orders": {
        "table": "orders",
        "query": "SELECT id, status, channel_order_id, courier_name, awb_code, created_at FROM orders WHERE company_id = :company_id ORDER BY created_at DESC LIMIT 10",
        "triggers": ["recent orders", "my orders", "latest orders"],
    },
    "shipment_by_awb": {
        "table": "shipments",
        "query": "SELECT id, order_id, awb_code, status, courier_id, pickup_date, delivered_date, updated_at FROM shipments WHERE company_id = :company_id AND awb_code = :entity_id LIMIT 1",
        "triggers": ["shipment", "awb", "tracking"],
    },
    "ndr_by_awb": {
        "table": "ndr_requests",
        "query": "SELECT id, awb_code, ndr_type, action_taken, reattempt_date, status, updated_at FROM ndr_requests WHERE company_id = :company_id AND awb_code = :entity_id LIMIT 5",
        "triggers": ["ndr", "non-delivery", "undelivered", "reattempt"],
    },
    "wallet_balance": {
        "table": "seller_wallets",
        "query": "SELECT id, balance, last_recharge_amount, last_recharge_date, updated_at FROM seller_wallets WHERE company_id = :company_id LIMIT 1",
        "triggers": ["wallet", "balance", "recharge"],
    },
    "tracking_events": {
        "table": "tracking_events",
        "query": "SELECT id, awb_code, status, location, scan_datetime, courier_status_code FROM tracking_events WHERE awb_code = :entity_id ORDER BY scan_datetime DESC LIMIT 20",
        "triggers": ["tracking", "scan", "milestone", "status update"],
    },
    "recent_returns": {
        "table": "returns",
        "query": "SELECT id, order_id, awb_code, reason, status, created_at FROM returns WHERE company_id = :company_id ORDER BY created_at DESC LIMIT 10",
        "triggers": ["return", "reverse", "exchange"],
    },
}


class CodebaseIntelligence:
    """
    Retrieval-driven code knowledge layer.

    At startup: ingests .claude/docs/modules/ into vectorstore as source_type='module_doc'.
    At query time: retrieves relevant chunks via vector similarity, no filesystem access.
    """

    SOURCE_TYPE = "module_doc"

    def __init__(self, repos_path: str, vectorstore=None):
        """
        Args:
            repos_path: Path to mars/repos/shiprocket/ containing the repos.
            vectorstore: VectorStoreService for storing/searching embeddings.
        """
        self.repos_path = Path(repos_path)
        self.vectorstore = vectorstore
        self._indexed = False
        self._stats = {"modules": 0, "chunks": 0, "repos": []}

    async def ingest(self) -> Dict[str, Any]:
        """
        One-time ingestion: read all .claude/docs/modules/ and store in vectorstore.
        Called at startup. Safe to call multiple times (idempotent via entity_id).
        """
        if not self.vectorstore:
            logger.warning("codebase_intelligence.no_vectorstore")
            return {"error": "vectorstore not available"}

        # All 8 repos (was 3 — missing MultiChannel_Web, shiprocket-channels,
        # SR_Sidebar, sr_login, helpdesk)
        repos = {
            "MultiChannel_API": self.repos_path / "MultiChannel_API",
            "SR_Web": self.repos_path / "SR_Web",
            "shiprocket-go": self.repos_path / "shiprocket-go",
            "MultiChannel_Web": self.repos_path / "MultiChannel_Web",
            "shiprocket-channels": self.repos_path / "shiprocket-channels",
            "SR_Sidebar": self.repos_path / "SR_Sidebar",
            "sr_login": self.repos_path / "sr_login",
            "helpdesk": self.repos_path / "helpdesk",
        }

        total_chunks = 0
        total_modules = 0
        total_skipped_draft = 0

        # Doc type → entity_type mapping (chunk by type, not mixed)
        _DOC_TYPE_MAP = {
            "CLAUDE": "module_overview",
            "api": "module_api",
            "database": "module_database",
            "business_rules": "module_rules",
            "debugging": "module_debug",
            "known_gaps": "module_gaps",
            "prd": "module_prd",
            "ssd": "module_ssd",
        }

        for repo_name, repo_path in repos.items():
            claude_docs = repo_path / ".claude" / "docs" / "modules"
            if not claude_docs.exists():
                claude_docs = repo_path / ".claude" / "docs"
                if not claude_docs.exists():
                    continue

            for module_dir in sorted(claude_docs.iterdir()):
                if not module_dir.is_dir():
                    continue

                module_name = module_dir.name

                # --- Quality gate: read module.yaml for trust scoring ---
                trust_score = 0.5  # default for modules without module.yaml
                module_status = "unknown"
                module_score = 0
                module_yaml_path = module_dir / "module.yaml"

                if module_yaml_path.exists():
                    try:
                        import yaml
                        with open(module_yaml_path) as f:
                            mod_meta = yaml.safe_load(f) or {}
                        module_status = mod_meta.get("status", "draft")
                        module_score = mod_meta.get("score", 0)

                        # Trust scoring based on status + score
                        if module_status == "draft" or module_score < 20:
                            trust_score = 0.3   # Tier D: retrieval-only
                        elif module_status in ("enriched", "active") and module_score >= 50:
                            trust_score = 0.7   # Tier C: train + retrieve
                        elif module_status in ("enriched", "active"):
                            trust_score = 0.5   # Tier C low
                        elif module_status == "infrastructure_only":
                            trust_score = 0.1   # Skip from training

                        # Ingest module.yaml itself as structured metadata
                        summary = mod_meta.get("summary", "")
                        tables = mod_meta.get("database", {}).get("tables", [])
                        deps = mod_meta.get("dependencies", {}).get("services", [])

                        if summary:
                            meta_content = (
                                f"[{repo_name}/{module_name}] Module: {module_name}. "
                                f"Status: {module_status}, Score: {module_score}. "
                                f"{summary}. "
                                f"Tables: {', '.join(tables[:10]) if isinstance(tables, list) else ''}. "
                                f"Dependencies: {', '.join(deps[:10]) if isinstance(deps, list) else ''}."
                            )
                            await self.vectorstore.store_embedding(
                                entity_type="module_meta",
                                entity_id=f"{repo_name}:{module_name}:module_yaml",
                                content=meta_content,
                                repo_id=repo_name,
                                metadata={
                                    "module": module_name,
                                    "file": "module.yaml",
                                    "status": module_status,
                                    "score": module_score,
                                    "trust_score": trust_score,
                                    "tables": tables[:10] if isinstance(tables, list) else [],
                                    "dependencies": deps[:10] if isinstance(deps, list) else [],
                                },
                            )
                            total_chunks += 1
                    except Exception:
                        pass

                # Skip infrastructure_only modules entirely
                if trust_score <= 0.1:
                    total_skipped_draft += 1
                    continue

                # --- Ingest evidence/index.yaml if exists ---
                evidence_path = module_dir / "evidence" / "index.yaml"
                if evidence_path.exists():
                    try:
                        import yaml
                        with open(evidence_path) as f:
                            evidence = yaml.safe_load(f) or {}
                        facts = evidence.get("facts", [])
                        for fact in facts[:20]:  # cap at 20 facts per module
                            claim = fact.get("claim", "")
                            confidence = fact.get("confidence", 0.5)
                            if claim:
                                await self.vectorstore.store_embedding(
                                    entity_type="module_evidence",
                                    entity_id=f"{repo_name}:{module_name}:evidence:{fact.get('id', '')}",
                                    content=f"[{repo_name}/{module_name}] Verified: {claim}",
                                    repo_id=repo_name,
                                    metadata={
                                        "module": module_name,
                                        "file": "evidence/index.yaml",
                                        "confidence": confidence,
                                        "trust_score": min(0.95, trust_score + 0.2),
                                        "source": fact.get("source", ""),
                                    },
                                )
                                total_chunks += 1
                    except Exception:
                        pass

                # --- Ingest markdown docs, chunked BY TYPE ---
                doc_files = list(module_dir.glob("*.md"))
                # Also get submodule docs
                submodule_files = list(module_dir.glob("submodules/**/*.md"))

                for doc_file in doc_files:
                    try:
                        content = doc_file.read_text(encoding="utf-8", errors="ignore")
                        if len(content.strip()) < 50:
                            continue

                        # Determine entity_type from filename
                        stem = doc_file.stem.lower()
                        entity_type = _DOC_TYPE_MAP.get(stem, self.SOURCE_TYPE)

                        chunks = self._chunk_content(content, max_chars=2000)

                        for i, chunk in enumerate(chunks):
                            entity_id = f"{repo_name}:{module_name}:{doc_file.stem}:chunk_{i}"
                            tables = [t for t in _KNOWN_TABLES if t in chunk.lower()]
                            apis = re.findall(r"(?:/v\d+/[\w/.-]+|/api/[\w/.-]+)", chunk)

                            await self.vectorstore.store_embedding(
                                entity_type=entity_type,
                                entity_id=entity_id,
                                content=f"[{repo_name}/{module_name}] {chunk}",
                                repo_id=repo_name,
                                metadata={
                                    "module": module_name,
                                    "file": doc_file.name,
                                    "chunk_index": i,
                                    "tables": tables,
                                    "apis": apis[:5],
                                    "trust_score": trust_score,
                                    "doc_type": stem,
                                },
                            )
                            total_chunks += 1

                    except Exception as e:
                        logger.warning("codebase_intelligence.ingest_error",
                                       file=str(doc_file), error=str(e))

                # --- Ingest submodule docs ---
                for sub_file in submodule_files:
                    try:
                        content = sub_file.read_text(encoding="utf-8", errors="ignore")
                        if len(content.strip()) < 50:
                            continue

                        sub_name = sub_file.parent.name
                        chunks = self._chunk_content(content, max_chars=2000)

                        for i, chunk in enumerate(chunks):
                            entity_id = f"{repo_name}:{module_name}:sub:{sub_name}:{sub_file.stem}:chunk_{i}"
                            tables = [t for t in _KNOWN_TABLES if t in chunk.lower()]

                            await self.vectorstore.store_embedding(
                                entity_type="module_submodule",
                                entity_id=entity_id,
                                content=f"[{repo_name}/{module_name}/{sub_name}] {chunk}",
                                repo_id=repo_name,
                                metadata={
                                    "module": module_name,
                                    "submodule": sub_name,
                                    "file": sub_file.name,
                                    "chunk_index": i,
                                    "tables": tables,
                                    "trust_score": max(0.3, trust_score - 0.1),
                                },
                            )
                            total_chunks += 1

                    except Exception:
                        pass

                total_modules += 1

        self._indexed = True
        self._stats = {
            "modules": total_modules,
            "chunks": total_chunks,
            "skipped_draft": total_skipped_draft,
            "repos": [r for r, p in repos.items() if p.exists()],
            "repos_total": len(repos),
        }
        logger.info("codebase_intelligence.ingested", **self._stats)
        return self._stats

    async def retrieve(
        self,
        query: str,
        intents: List[Dict] = None,
        repo_id: Optional[str] = None,
        top_k: int = 5,
    ) -> CodebaseContext:
        """
        Retrieve relevant code documentation for a query.
        Pure vector search — no filesystem access at query time.
        """
        result = CodebaseContext()
        intents = intents or []

        if not self.vectorstore:
            return result

        # Vector search for module docs
        chunks = await self.vectorstore.search_similar(
            query=query,
            limit=top_k,
            entity_type=self.SOURCE_TYPE,
            repo_id=repo_id,
            threshold=0.3,
        )

        result.relevant_chunks = chunks

        # Extract structured data from retrieved chunks
        all_tables = set()
        all_apis = set()
        modules_seen = set()

        for chunk in chunks:
            metadata = chunk.get("metadata", {})
            if isinstance(metadata, dict):
                all_tables.update(metadata.get("tables", []))
                all_apis.update(metadata.get("apis", []))
                module = metadata.get("module", "")
                if module:
                    modules_seen.add(module)

            # Also extract from content text
            content = chunk.get("content", "")
            all_tables.update(t for t in _KNOWN_TABLES if t in content.lower())

        result.db_tables = sorted(all_tables)
        result.api_endpoints = sorted(all_apis)
        result.modules_matched = sorted(modules_seen)

        # Extract insights (first 200 chars of each high-relevance chunk)
        for chunk in chunks[:3]:
            content = chunk.get("content", "")[:200].strip()
            similarity = chunk.get("similarity", 0)
            if content and similarity > 0.4:
                result.code_insights.append(content)

        # Build refined query
        if result.modules_matched:
            module_hint = ", ".join(result.modules_matched[:3])
            table_hint = ", ".join(result.db_tables[:5]) if result.db_tables else ""
            insights_hint = "; ".join(result.code_insights[:2])

            result.refined_query = (
                f"{query} "
                f"[Code context: modules={module_hint}"
                f"{f', tables={table_hint}' if table_hint else ''}"
                f"{f'. Insight: {insights_hint}' if insights_hint else ''}]"
            )
        else:
            result.refined_query = query

        # Suggest DB template for Tier 3
        result.suggested_db_template = self._match_db_template(query, intents)

        logger.info(
            "codebase_intelligence.retrieved",
            query=query[:60],
            chunks=len(chunks),
            modules=result.modules_matched,
            tables=result.db_tables[:5],
        )

        return result

    def _match_db_template(
        self, query: str, intents: List[Dict]
    ) -> Optional[str]:
        """Match query to a pre-defined parameterized DB template."""
        query_lower = query.lower()

        # Score each template
        best_template = None
        best_score = 0

        for template_name, template in _DB_TEMPLATES.items():
            score = sum(1 for trigger in template["triggers"] if trigger in query_lower)
            # Boost from intents
            for intent in intents:
                entity = intent.get("entity", "").lower()
                if entity in template["triggers"]:
                    score += 2
            if score > best_score:
                best_score = score
                best_template = template_name

        return best_template

    def get_db_template(self, template_name: str) -> Optional[Dict]:
        """Get a specific DB template by name."""
        return _DB_TEMPLATES.get(template_name)

    @staticmethod
    def _chunk_content(content: str, max_chars: int = 2000) -> List[str]:
        """Split content into chunks, preferring paragraph boundaries."""
        if len(content) <= max_chars:
            return [content]

        chunks = []
        paragraphs = content.split("\n\n")
        current = ""

        for para in paragraphs:
            if len(current) + len(para) + 2 > max_chars:
                if current:
                    chunks.append(current.strip())
                current = para
            else:
                current = current + "\n\n" + para if current else para

        if current.strip():
            chunks.append(current.strip())

        return chunks

    def get_stats(self) -> Dict[str, Any]:
        return {"indexed": self._indexed, **self._stats}

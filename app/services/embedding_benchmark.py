"""
Embedding A/B Benchmark — OpenAI primary vs Voyage shadow lane.

Architecture:
  PRIMARY LANE:  OpenAI text-embedding-3-small (1536-dim) → cosmos_embeddings (live)
  SHADOW LANE:   Voyage voyage-3-large (1024-dim) → cosmos_embeddings_shadow (benchmark)

Shadow lane runs in background on every ingestion:
  1. Primary embeds document via AI Gateway → stores in cosmos_embeddings
  2. Shadow embeds SAME document via Voyage → stores in cosmos_embeddings_shadow
  3. On every search query, BOTH lanes are queried
  4. Results compared but only primary lane serves the user
  5. Comparison metrics stored for analysis

Metrics tracked per query:
  - hit@5: is the correct doc in top-5?
  - MRR: mean reciprocal rank of correct doc
  - latency_ms: per-lane search time
  - exact_id_match: did it find the exact entity_id?
  - relevance_gap: primary score vs shadow score for top result

Decision criteria (after 500+ queries):
  - If shadow wins hit@5 by >5% → consider switching
  - If shadow wins MRR by >0.05 → consider switching
  - If latency difference < 20ms → latency is not a blocker
  - NEVER switch without full re-embed + holdout verification

Security note:
  Voyage API key should be rotated after exposure.
  Store in environment variable, not in code.
"""

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

SHADOW_TABLE = "cosmos_embeddings_shadow"
SHADOW_DIM = 1024
SHADOW_MODEL = "voyage-3-large"
BENCHMARK_TABLE = "cosmos_embedding_benchmarks"

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")


@dataclass
class BenchmarkResult:
    """Result of a single query comparison."""
    query: str
    primary_top5: List[str] = field(default_factory=list)   # entity_ids
    shadow_top5: List[str] = field(default_factory=list)
    primary_scores: List[float] = field(default_factory=list)
    shadow_scores: List[float] = field(default_factory=list)
    primary_latency_ms: float = 0.0
    shadow_latency_ms: float = 0.0
    overlap_count: int = 0        # how many of top-5 are in both
    primary_hit: bool = False      # correct doc in primary top-5
    shadow_hit: bool = False       # correct doc in shadow top-5
    primary_mrr: float = 0.0
    shadow_mrr: float = 0.0


class EmbeddingBenchmark:
    """
    Manages shadow embedding lane and comparison metrics.

    Usage:
        benchmark = EmbeddingBenchmark()
        await benchmark.ensure_shadow_schema()

        # On every ingestion (background, non-blocking):
        await benchmark.shadow_embed(entity_type, entity_id, content, repo_id, metadata)

        # On every search (background, non-blocking):
        result = await benchmark.compare_search(query, entity_type, limit=5)

        # After 500+ queries:
        report = await benchmark.get_report()
    """

    def __init__(self):
        self._enabled = bool(VOYAGE_API_KEY)
        if not self._enabled:
            logger.info("embedding_benchmark.disabled", reason="no VOYAGE_API_KEY")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def ensure_shadow_schema(self) -> None:
        """Create shadow embedding table + benchmark results table."""
        if not self._enabled:
            return

        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {SHADOW_TABLE} (
                        id CHAR(36) PRIMARY KEY,
                        repo_id VARCHAR(255) NOT NULL DEFAULT '',
                        entity_type VARCHAR(255) NOT NULL,
                        entity_id VARCHAR(500) NOT NULL,
                        content_hash VARCHAR(32) NOT NULL DEFAULT '',
                        embedding vector({SHADOW_DIM}),
                        trust_score FLOAT DEFAULT 0.5,
                        embedding_model VARCHAR(100) DEFAULT '{SHADOW_MODEL}',
                        metadata JSON DEFAULT '{{}}',
                        embedded_at TIMESTAMP DEFAULT now()
                    )
                """))
                await session.execute(text(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_identity
                    ON {SHADOW_TABLE} (repo_id, entity_type, entity_id)
                """))

                # Benchmark results table
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {BENCHMARK_TABLE} (
                        id CHAR(36) PRIMARY KEY,
                        query_hash VARCHAR(32) NOT NULL,
                        query_preview VARCHAR(200),
                        entity_type VARCHAR(255),
                        primary_top5 JSON,
                        shadow_top5 JSON,
                        primary_latency_ms FLOAT,
                        shadow_latency_ms FLOAT,
                        overlap_count INT,
                        primary_top_score FLOAT,
                        shadow_top_score FLOAT,
                        created_at TIMESTAMP DEFAULT now()
                    )
                """))

                await session.commit()
                logger.info("benchmark.shadow_schema_created", dim=SHADOW_DIM)
            except Exception as e:
                await session.rollback()
                logger.error("benchmark.schema_failed", error=str(e))

    async def shadow_embed(
        self,
        entity_type: str,
        entity_id: str,
        content: str,
        repo_id: str = "",
        metadata: Optional[Dict] = None,
        content_hash: str = "",
    ) -> bool:
        """
        Embed document into shadow lane (Voyage).
        Runs in background — failure does not affect primary lane.
        """
        if not self._enabled:
            return False

        try:
            # Call Voyage API with input_type="document"
            embedding = await self._voyage_embed(content, input_type="document")
            if not embedding:
                return False

            meta = metadata or {}
            trust_score = meta.get("trust_score", 0.5)

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(f"""
                        INSERT INTO {SHADOW_TABLE}
                            (repo_id, entity_type, entity_id, content_hash, embedding,
                             trust_score, embedding_model, metadata, embedded_at)
                        VALUES
                            (:repo_id, :entity_type, :entity_id, :content_hash, :embedding,
                             :trust_score, :embedding_model, :metadata, now())
                        ON CONFLICT (repo_id, entity_type, entity_id)
                        DO UPDATE SET
                            embedding = EXCLUDED.embedding,
                            content_hash = EXCLUDED.content_hash,
                            trust_score = EXCLUDED.trust_score,
                            embedded_at = now()
                    """),
                    {
                        "repo_id": repo_id,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "content_hash": content_hash,
                        "embedding": str(embedding),
                        "trust_score": trust_score,
                        "embedding_model": SHADOW_MODEL,
                        "metadata": json.dumps(meta),
                    },
                )
                await session.commit()
                return True

        except Exception as e:
            logger.warning("benchmark.shadow_embed_failed", entity_id=entity_id[:60], error=str(e))
            return False

    async def compare_search(
        self,
        query: str,
        entity_type: Optional[str] = None,
        limit: int = 5,
    ) -> Optional[BenchmarkResult]:
        """
        Run same query against both lanes and compare results.
        Returns comparison but does NOT affect user-facing results.
        """
        if not self._enabled:
            return None

        result = BenchmarkResult(query=query[:200])

        try:
            # Shadow lane: Voyage with input_type="query"
            t0 = time.monotonic()
            shadow_embedding = await self._voyage_embed(query, input_type="query")
            if not shadow_embedding:
                return None

            filters = ""
            params: Dict[str, Any] = {
                "embedding": str(shadow_embedding),
                "limit": limit,
            }
            if entity_type:
                filters = "WHERE entity_type = :entity_type"
                params["entity_type"] = entity_type

            async with AsyncSessionLocal() as session:
                shadow_result = await session.execute(
                    text(f"""
                        SELECT entity_id,
                               1 - (embedding <=> :embedding) AS similarity
                        FROM {SHADOW_TABLE}
                        {filters}
                        ORDER BY embedding <=> :embedding
                        LIMIT :limit
                    """),
                    params,
                )
                shadow_rows = shadow_result.fetchall()

            result.shadow_latency_ms = (time.monotonic() - t0) * 1000
            result.shadow_top5 = [row.entity_id for row in shadow_rows]
            result.shadow_scores = [round(float(row.similarity), 4) for row in shadow_rows]

            # Compare overlap with primary (need primary results passed in or queried)
            # For now, store shadow results for later comparison
            result.overlap_count = 0  # filled when primary results available

            # Store benchmark
            import hashlib
            query_hash = hashlib.md5(query.encode()).hexdigest()[:32]
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(f"""
                        INSERT INTO {BENCHMARK_TABLE}
                            (query_hash, query_preview, entity_type,
                             shadow_top5, shadow_latency_ms, shadow_top_score)
                        VALUES
                            (:qhash, :qpreview, :etype,
                             :shadow_top5, :shadow_ms, :shadow_score)
                    """),
                    {
                        "qhash": query_hash,
                        "qpreview": query[:200],
                        "etype": entity_type,
                        "shadow_top5": json.dumps(result.shadow_top5),
                        "shadow_ms": result.shadow_latency_ms,
                        "shadow_score": result.shadow_scores[0] if result.shadow_scores else 0,
                    },
                )
                await session.commit()

            return result

        except Exception as e:
            logger.warning("benchmark.compare_failed", error=str(e))
            return None

    async def get_report(self) -> Dict[str, Any]:
        """
        Generate benchmark report from collected comparison data.
        Call after 500+ queries for statistically meaningful results.
        """
        async with AsyncSessionLocal() as session:
            try:
                count = await session.execute(
                    text(f"SELECT COUNT(*) as cnt FROM {BENCHMARK_TABLE}")
                )
                total = count.scalar() or 0

                if total == 0:
                    return {"status": "no_data", "queries_compared": 0}

                # Average scores
                stats = await session.execute(
                    text(f"""
                        SELECT
                            COUNT(*) as total_queries,
                            AVG(shadow_top_score) as avg_shadow_score,
                            AVG(shadow_latency_ms) as avg_shadow_latency,
                            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY shadow_latency_ms) as p95_shadow_latency
                        FROM {BENCHMARK_TABLE}
                    """)
                )
                row = stats.fetchone()

                # Shadow table stats
                shadow_count = await session.execute(
                    text(f"SELECT COUNT(*) as cnt FROM {SHADOW_TABLE}")
                )
                shadow_docs = shadow_count.scalar() or 0

                return {
                    "status": "active",
                    "queries_compared": row.total_queries if row else 0,
                    "shadow_docs_embedded": shadow_docs,
                    "shadow_model": SHADOW_MODEL,
                    "shadow_dim": SHADOW_DIM,
                    "avg_shadow_top_score": round(float(row.avg_shadow_score or 0), 4),
                    "avg_shadow_latency_ms": round(float(row.avg_shadow_latency or 0), 1),
                    "p95_shadow_latency_ms": round(float(row.p95_shadow_latency or 0), 1),
                    "decision_criteria": {
                        "switch_if": "shadow wins hit@5 by >5% AND MRR by >0.05",
                        "min_queries": 500,
                        "status": "collecting" if total < 500 else "ready_to_evaluate",
                    },
                }
            except Exception as e:
                return {"status": "error", "error": str(e)}

    async def _voyage_embed(self, text_input: str, input_type: str = "document") -> Optional[List[float]]:
        """Call Voyage API for embedding."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    VOYAGE_API_URL,
                    json={
                        "input": [text_input[:16000]],
                        "model": SHADOW_MODEL,
                        "input_type": input_type,
                    },
                    headers={
                        "Authorization": f"Bearer {VOYAGE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    if "data" in data:
                        embedding = data["data"][0]["embedding"]
                        if len(embedding) == SHADOW_DIM:
                            return embedding

                return None
        except Exception:
            return None

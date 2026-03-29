"""
Canonical Ingestor — Single ingestion path for ALL training data sources.

Every document enters cosmos_embeddings through this ingestor, regardless of source.
This ensures: one upsert policy, one trust scoring, one schema contract.

Multi-model embedding:
  PRIMARY:  text-embedding-3-small (1536 dim) → cosmos_embeddings (live, serves queries)
  SHADOW:   voyage-3-large (1024 dim) → cosmos_embeddings_shadow (benchmark only)
  Shadow runs async in background — never blocks primary ingestion.
  Shadow is disabled if VOYAGE_API_KEY is not set.

Sources handled:
  - Pillar 1 schema YAML (entity_type='schema')
  - Pillar 3 API tool YAML (entity_type='api_tool')
  - Pillar 4 page/role YAML (entity_type='page', 'page_intent', 'cross_repo')
  - Generated artifacts (entity_type='domain_overview', 'symptom_fix', etc.)
  - Module docs from .claude/ (entity_type='module_*')
  - Runtime data (entity_type='knowledge', 'distillation')
  - Eval/seed data (entity_type='eval_seed')
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class IngestDocument:
    """Canonical document format for ingestion."""
    entity_type: str          # schema | api_tool | page | module_overview | knowledge | etc.
    entity_id: str            # unique ID within entity_type
    content: str              # text to embed
    repo_id: str = ""
    capability: str = "retrieval"  # retrieval | intent_seed | graph_edge | page_intel | code_intel
    trust_score: float = 0.5
    freshness: Optional[str] = None  # ISO timestamp
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Result of a batch ingestion."""
    total: int = 0
    ingested: int = 0
    skipped: int = 0
    errors: int = 0
    duration_ms: float = 0.0
    by_entity_type: Dict[str, int] = field(default_factory=dict)


class CanonicalIngestor:
    """
    Single entry point for all data → cosmos_embeddings.

    Features:
    - Upsert behavior (no duplicates)
    - Trust scoring enforcement
    - Capability tagging
    - Batch ingestion with progress logging
    """

    # Minimum trust to be ingested at all
    MIN_TRUST = 0.1

    def __init__(self, vectorstore):
        """
        Args:
            vectorstore: VectorStoreService instance
        """
        self.vectorstore = vectorstore
        self._stats = {"total_ingested": 0, "total_skipped": 0}

        # Kafka producer for shadow embedding lanes
        self._kafka_producer = None
        self._kafka_init = False

    def _get_kafka_producer(self):
        """Lazy-init Kafka producer for shadow lanes."""
        if not self._kafka_init:
            self._kafka_init = True
            try:
                from app.services.embedding_queue import EmbeddingProducer
                p = EmbeddingProducer()
                if p._get_producer():
                    self._kafka_producer = p
                    logger.info("ingestor.kafka_producer_enabled")
                else:
                    logger.info("ingestor.kafka_producer_disabled")
            except Exception as e:
                logger.debug("ingestor.kafka_init_failed", error=str(e))
        return self._kafka_producer

    async def ingest(self, documents: List[IngestDocument]) -> IngestResult:
        """Ingest a batch of documents into cosmos_embeddings with upsert.

        Primary: text-embedding-3-small → cosmos_embeddings (synchronous, blocks)
        Shadow:  voyage-3-large → cosmos_embeddings_shadow (async background, non-blocking)
        """
        t0 = time.monotonic()
        result = IngestResult(total=len(documents))
        kafka_producer = self._get_kafka_producer()
        kafka_published = 0

        for doc in documents:
            # Skip below minimum trust
            if doc.trust_score < self.MIN_TRUST:
                result.skipped += 1
                continue

            # Skip empty content
            if not doc.content or len(doc.content.strip()) < 10:
                result.skipped += 1
                continue

            meta = {
                **doc.metadata,
                "trust_score": doc.trust_score,
                "capability": doc.capability,
                "freshness": doc.freshness,
                "ingested_at": time.time(),
            }

            try:
                # PRIMARY LANE: text-embedding-3-small → cosmos_embeddings
                await self.vectorstore.store_embedding(
                    entity_type=doc.entity_type,
                    entity_id=doc.entity_id,
                    content=doc.content,
                    repo_id=doc.repo_id or None,
                    metadata=meta,
                )
                result.ingested += 1
                result.by_entity_type[doc.entity_type] = result.by_entity_type.get(doc.entity_type, 0) + 1

                # Mark small_done in tracker (so re-runs skip this doc)
                if kafka_producer:
                    kafka_producer._track_small_done(
                        doc.repo_id or "", doc.entity_type, doc.entity_id,
                        meta.get("content_hash", "")
                    )

                # SHADOW LANES: publish to Kafka → consumer embeds with 3-large + Voyage in parallel
                if kafka_producer:
                    ok = kafka_producer.publish(
                        entity_type=doc.entity_type,
                        entity_id=doc.entity_id,
                        content=doc.content,
                        repo_id=doc.repo_id or "",
                        metadata=meta,
                        trust_score=doc.trust_score,
                    )
                    if ok:
                        kafka_published += 1

            except Exception as e:
                result.errors += 1
                logger.warning("ingestor.error", entity_id=doc.entity_id, error=str(e))

        # Flush Kafka producer to ensure all messages are sent
        if kafka_producer and kafka_published > 0:
            kafka_producer.flush()

        result.duration_ms = (time.monotonic() - t0) * 1000
        self._stats["total_ingested"] += result.ingested
        self._stats["total_skipped"] += result.skipped

        logger.info(
            "ingestor.batch_complete",
            total=result.total,
            ingested=result.ingested,
            skipped=result.skipped,
            errors=result.errors,
            ms=round(result.duration_ms, 1),
            by_type=result.by_entity_type,
            kafka_published=kafka_published,
        )

        return result

    async def ingest_one(self, doc: IngestDocument) -> bool:
        """Ingest a single document. Returns True if ingested."""
        result = await self.ingest([doc])
        return result.ingested > 0

    def get_stats(self) -> Dict:
        return dict(self._stats)

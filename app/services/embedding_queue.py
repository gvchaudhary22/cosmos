"""
Embedding Queue — Kafka-based async embedding for shadow lanes.

Architecture:
  Producer: CanonicalIngestor publishes doc to Kafka after primary embed
  Consumer: Background worker consumes and embeds with shadow models

Shadow models:
  1. text-embedding-3-large (3072 dim) → cosmos_embeddings_large
     - Via AI Gateway (same provider, just different model)
     - No rate limit — runs in parallel with primary
  2. voyage-3-large (1024 dim) → cosmos_embeddings_shadow
     - Via Voyage API (3 RPM limit)
     - Batched: 50 docs per API call → 150 docs/min → ~2.7h for full KB

Topic: "cosmos.embedding.shadow"
Message format: JSON {entity_type, entity_id, content, repo_id, metadata, trust_score}

Run consumer:
  python -m app.services.embedding_queue consume
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "kafka-inhouse.shiprocket-stage-internal.in:9094")
KAFKA_USERNAME = os.environ.get("KAFKA_CLUSTER_USERNAME", "nishant-user")
KAFKA_PASSWORD = os.environ.get("KAFKA_CLUSTER_PASSWORD", "T1YgArGNrvZ7yKwS5KgZxPeVSdQCTMwR")
KAFKA_TOPIC = os.environ.get("KAFKA_EMBEDDING_TOPIC", "sc_webhook_orders_wc")

# Shadow model configs
LARGE_MODEL = "text-embedding-3-large"
LARGE_DIM = 3072
LARGE_TABLE = "cosmos_embeddings_large"

VOYAGE_MODEL = "voyage-3-large"
VOYAGE_DIM = 1024
VOYAGE_TABLE = "cosmos_embeddings_shadow"
VOYAGE_BATCH_SIZE = 50  # texts per API call
VOYAGE_RPM = 3          # max 3 calls per minute → 1 call per 20 seconds


# ===================================================================
# PRODUCER — called during ingestion, non-blocking
# ===================================================================

TRACKING_TABLE = "cosmos_embedding_queue_tracker"


class EmbeddingProducer:
    """Publishes embedding jobs to Kafka for shadow lane processing.

    Non-blocking, fire-and-forget. If Kafka is down, silently skips.
    Tracks published content_hashes in DB to avoid re-publishing unchanged docs.
    """

    def __init__(self):
        self._producer = None
        self._enabled = True
        self._init_attempted = False
        self._tracker_ready = False

    def _get_producer(self):
        """Lazy-init Kafka producer."""
        if self._init_attempted:
            return self._producer
        self._init_attempted = True

        try:
            from kafka import KafkaProducer
            self._producer = KafkaProducer(
                bootstrap_servers=KAFKA_BROKERS.split(","),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                security_protocol="SASL_PLAINTEXT",
                sasl_mechanism="SCRAM-SHA-512",
                sasl_plain_username=KAFKA_USERNAME,
                sasl_plain_password=KAFKA_PASSWORD,
                acks="all",
                retries=2,
                max_block_ms=5000,  # don't block ingestion for more than 5s
                linger_ms=100,      # batch messages for 100ms before sending
                batch_size=65536,   # 64KB batch
            )
            logger.info("embedding_queue.producer_connected", brokers=KAFKA_BROKERS)
        except Exception as e:
            logger.warning("embedding_queue.producer_init_failed", error=str(e))
            self._enabled = False
        return self._producer

    def publish(self, entity_type: str, entity_id: str, content: str,
                repo_id: str = "", metadata: Optional[Dict] = None,
                trust_score: float = 0.5) -> bool:
        """Publish a doc for shadow embedding. Non-blocking.

        Checks DB tracker to skip docs with unchanged content_hash.
        Includes content_hash so the consumer can also skip unchanged docs.
        """
        if not self._enabled:
            return False

        producer = self._get_producer()
        if not producer:
            return False

        try:
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]

            # Check tracker: skip if already published with same content_hash
            if self._is_already_published(repo_id, entity_type, entity_id, content_hash):
                return False

            message = {
                "msg_type": "cosmos_embedding",  # filter key for shared topic
                "entity_type": entity_type,
                "entity_id": entity_id,
                "content": content,
                "repo_id": repo_id,
                "metadata": metadata or {},
                "trust_score": trust_score,
                "content_hash": content_hash,
                "published_at": time.time(),
            }
            producer.send(
                KAFKA_TOPIC,
                value=message,
                key=entity_id.encode("utf-8"),
            )

            # Track in DB
            self._track_published(repo_id, entity_type, entity_id, content_hash)
            return True
        except Exception as e:
            logger.debug("embedding_queue.publish_failed", error=str(e))
            return False

    def _ensure_tracker_table(self):
        """Create tracker table if needed (sync, called once)."""
        if self._tracker_ready:
            return
        self._tracker_ready = True
        try:
            import psycopg2
            db_url = os.environ.get("DATABASE_URL", "postgresql://cosmos:cosmos@localhost:5433/cosmos")
            # Parse async URL to sync
            sync_url = db_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
            conn = psycopg2.connect(sync_url)
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                    repo_id VARCHAR(255) NOT NULL DEFAULT '',
                    entity_type VARCHAR(255) NOT NULL,
                    entity_id VARCHAR(500) NOT NULL,
                    content_hash VARCHAR(64) NOT NULL DEFAULT '',
                    published_at TIMESTAMPTZ DEFAULT now(),
                    small_done BOOLEAN DEFAULT false,
                    large_done BOOLEAN DEFAULT false,
                    voyage_done BOOLEAN DEFAULT false,
                    PRIMARY KEY (repo_id, entity_type, entity_id)
                )
            """)
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug("embedding_queue.tracker_table_failed", error=str(e))

    def _is_already_published(self, repo_id: str, entity_type: str,
                               entity_id: str, content_hash: str) -> bool:
        """Check if doc was already published with same content_hash."""
        self._ensure_tracker_table()
        try:
            import psycopg2
            db_url = os.environ.get("DATABASE_URL", "postgresql://cosmos:cosmos@localhost:5433/cosmos")
            sync_url = db_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
            conn = psycopg2.connect(sync_url)
            cur = conn.cursor()
            cur.execute(f"""
                SELECT content_hash FROM {TRACKING_TABLE}
                WHERE repo_id = %s AND entity_type = %s AND entity_id = %s
            """, (repo_id or "", entity_type, entity_id))
            row = cur.fetchone()
            cur.close()
            conn.close()
            return row is not None and row[0] == content_hash
        except Exception:
            return False

    def _track_published(self, repo_id: str, entity_type: str,
                          entity_id: str, content_hash: str):
        """Record that a doc was published to Kafka."""
        try:
            import psycopg2
            db_url = os.environ.get("DATABASE_URL", "postgresql://cosmos:cosmos@localhost:5433/cosmos")
            sync_url = db_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
            conn = psycopg2.connect(sync_url)
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO {TRACKING_TABLE} (repo_id, entity_type, entity_id, content_hash, published_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (repo_id, entity_type, entity_id)
                DO UPDATE SET content_hash = EXCLUDED.content_hash, published_at = now(),
                              small_done = false, large_done = false, voyage_done = false
            """, (repo_id or "", entity_type, entity_id, content_hash))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug("embedding_queue.track_failed", error=str(e))

    def _track_small_done(self, repo_id: str, entity_type: str,
                          entity_id: str, content_hash: str):
        """Mark primary (small) embedding as done in tracker."""
        self._ensure_tracker_table()
        try:
            import psycopg2
            db_url = os.environ.get("DATABASE_URL", "postgresql://cosmos:cosmos@localhost:5433/cosmos")
            sync_url = db_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
            conn = psycopg2.connect(sync_url)
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO {TRACKING_TABLE} (repo_id, entity_type, entity_id, content_hash, small_done)
                VALUES (%s, %s, %s, %s, true)
                ON CONFLICT (repo_id, entity_type, entity_id)
                DO UPDATE SET small_done = true
            """, (repo_id or "", entity_type, entity_id, content_hash or ""))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug("embedding_queue.track_small_failed", error=str(e))

    def flush(self):
        """Flush any pending messages."""
        if self._producer:
            try:
                self._producer.flush(timeout=10)
            except Exception:
                pass

    def close(self):
        if self._producer:
            try:
                self._producer.flush(timeout=5)
                self._producer.close(timeout=5)
            except Exception:
                pass


# ===================================================================
# CONSUMER — runs as background worker
# ===================================================================

class EmbeddingConsumer:
    """Consumes from Kafka and embeds with shadow models.

    text-embedding-3-large: runs immediately (no rate limit)
    voyage-3-large: rate-limited to 3 RPM, batched 50 docs/call

    Usage:
        consumer = EmbeddingConsumer()
        await consumer.run()  # blocks forever, processing messages
    """

    def __init__(self):
        self._voyage_api_key = os.environ.get("VOYAGE_API_KEY", "")
        self._aigateway_url = os.environ.get("AIGATEWAY_URL", "https://aigateway.shiprocket.in")
        self._aigateway_key = os.environ.get("AIGATEWAY_API_KEY", "")
        self._voyage_batch: List[Dict] = []
        self._last_voyage_call = 0.0
        self._stats = {"large_ok": 0, "large_err": 0, "voyage_ok": 0, "voyage_err": 0}

    async def run(self):
        """Start consuming. Blocks forever."""
        from kafka import KafkaConsumer

        await self._ensure_tables()

        consumer = KafkaConsumer(
            KAFKA_TOPIC,
            bootstrap_servers=KAFKA_BROKERS.split(","),
            security_protocol="SASL_PLAINTEXT",
            sasl_mechanism="SCRAM-SHA-512",
            sasl_plain_username=KAFKA_USERNAME,
            sasl_plain_password=KAFKA_PASSWORD,
            group_id="cosmos-embedding-shadow-consumer",  # separate from WooCommerce consumers
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            max_poll_records=VOYAGE_BATCH_SIZE,
            consumer_timeout_ms=30000,  # 30s timeout for batch collection
        )

        logger.info("embedding_consumer.started",
                     topic=KAFKA_TOPIC,
                     large_enabled=bool(self._aigateway_key),
                     voyage_enabled=bool(self._voyage_api_key))

        try:
            while True:
                # Poll for messages (collect up to VOYAGE_BATCH_SIZE)
                records = consumer.poll(timeout_ms=5000, max_records=VOYAGE_BATCH_SIZE)

                if not records:
                    # No new messages — flush any remaining voyage batch
                    if self._voyage_batch:
                        await self._flush_voyage_batch()
                    continue

                for tp, messages in records.items():
                    for msg in messages:
                        doc = msg.value
                        # Filter: only process COSMOS embedding messages, skip WooCommerce webhooks
                        if not isinstance(doc, dict) or doc.get("msg_type") != "cosmos_embedding":
                            continue
                        await self._process_one(doc)

                # Log progress periodically
                total = sum(self._stats.values())
                if total % 100 == 0 and total > 0:
                    logger.info("embedding_consumer.progress", **self._stats)

        except KeyboardInterrupt:
            logger.info("embedding_consumer.stopping")
        finally:
            # Flush remaining
            if self._voyage_batch:
                await self._flush_voyage_batch()
            consumer.close()

    async def _process_one(self, doc: Dict):
        """Process a single doc through both shadow models."""
        tasks = []

        # text-embedding-3-large: embed immediately (no rate limit)
        if self._aigateway_key:
            tasks.append(self._embed_large(doc))

        # voyage-3-large: collect into batch, flush when full or rate-limited
        if self._voyage_api_key:
            self._voyage_batch.append(doc)
            if len(self._voyage_batch) >= VOYAGE_BATCH_SIZE:
                tasks.append(self._flush_voyage_batch())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # text-embedding-3-large (via AI Gateway, no rate limit)
    # ------------------------------------------------------------------

    async def _embed_large(self, doc: Dict):
        """Embed one doc with text-embedding-3-large. Skips if content unchanged."""
        try:
            import httpx

            # Check if already embedded with same content_hash
            if await self._already_embedded(LARGE_TABLE, doc):
                return

            content = doc["content"][:8000]
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._aigateway_url}/api/v1/embedding",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self._aigateway_key,
                    },
                    json={
                        "model": LARGE_MODEL,
                        "provider": "openai",
                        "input": content,
                    },
                )
                if resp.status_code != 200:
                    self._stats["large_err"] += 1
                    logger.debug("consumer.large_api_error", status=resp.status_code, body=resp.text[:200])
                    return
                resp_data = resp.json()
                if not resp_data.get("success"):
                    self._stats["large_err"] += 1
                    return
                output = resp_data.get("data", {})
                embedding = output.get("embedding") or output.get("output", {}).get("embedding")
                if not embedding:
                    self._stats["large_err"] += 1
                    return

            stored = await self._store_embedding(
                LARGE_TABLE, doc, embedding, LARGE_MODEL
            )
            if stored:
                await self._mark_done(doc, "large_done")
            self._stats["large_ok"] += 1

        except Exception as e:
            self._stats["large_err"] += 1
            logger.debug("consumer.large_failed",
                         entity_id=doc.get("entity_id", "")[:50], error=str(e))

    # ------------------------------------------------------------------
    # voyage-3-large (3 RPM, batched 50/call)
    # ------------------------------------------------------------------

    async def _flush_voyage_batch(self):
        """Send collected batch to Voyage API with rate limiting. Skips unchanged docs."""
        if not self._voyage_batch:
            return

        batch = list(self._voyage_batch)
        self._voyage_batch.clear()

        # Filter out already-embedded docs (don't waste Voyage RPM)
        filtered = []
        for doc in batch:
            if not await self._already_embedded(VOYAGE_TABLE, doc):
                filtered.append(doc)

        if not filtered:
            logger.debug("consumer.voyage_batch_all_skipped", original=len(batch))
            return

        batch = filtered

        # Rate limit: 3 RPM = 1 call per 20 seconds
        now = time.time()
        wait = 20.0 - (now - self._last_voyage_call)
        if wait > 0:
            logger.debug("consumer.voyage_rate_wait", seconds=round(wait, 1))
            await asyncio.sleep(wait)

        try:
            import httpx

            texts = [doc["content"][:4000] for doc in batch]

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._voyage_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": VOYAGE_MODEL,
                        "input": texts,
                        "input_type": "document",
                    },
                )
                self._last_voyage_call = time.time()

                if resp.status_code != 200:
                    self._stats["voyage_err"] += len(batch)
                    logger.warning("consumer.voyage_api_error",
                                   status=resp.status_code, body=resp.text[:200])
                    return

                embeddings = [d["embedding"] for d in resp.json()["data"]]

            # Store all embeddings
            for doc, embedding in zip(batch, embeddings):
                stored = await self._store_embedding(
                    VOYAGE_TABLE, doc, embedding, VOYAGE_MODEL
                )
                if stored:
                    await self._mark_done(doc, "voyage_done")
            self._stats["voyage_ok"] += len(batch)

            logger.info("consumer.voyage_batch_done", docs=len(batch))

        except Exception as e:
            self._stats["voyage_err"] += len(batch)
            logger.warning("consumer.voyage_failed", error=str(e))

    # ------------------------------------------------------------------
    # Shared: store embedding in postgres
    # ------------------------------------------------------------------

    async def _already_embedded(self, table: str, doc: Dict) -> bool:
        """Check if doc with same content_hash already exists in shadow table."""
        from sqlalchemy import text as sql_text
        from app.db.session import AsyncSessionLocal

        content_hash = doc.get("content_hash") or hashlib.sha256(
            doc.get("content", "").encode()
        ).hexdigest()[:32]

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(sql_text(f"""
                    SELECT content_hash FROM {table}
                    WHERE repo_id = :repo_id
                      AND entity_type = :entity_type
                      AND entity_id = :entity_id
                    LIMIT 1
                """), {
                    "repo_id": doc.get("repo_id", ""),
                    "entity_type": doc["entity_type"],
                    "entity_id": doc["entity_id"],
                })
                row = result.fetchone()
                return row is not None and row.content_hash == content_hash
        except Exception:
            return False  # on error, re-embed to be safe

    async def _store_embedding(self, table: str, doc: Dict,
                                embedding: List[float], model: str) -> bool:
        """Upsert embedding into a shadow table. Returns False if skipped (unchanged)."""
        from sqlalchemy import text as sql_text
        from app.db.session import AsyncSessionLocal

        content = doc.get("content", "")
        content_hash = doc.get("content_hash") or hashlib.sha256(content.encode()).hexdigest()[:32]
        repo_id = doc.get("repo_id", "")
        entity_type = doc["entity_type"]
        entity_id = doc["entity_id"]

        async with AsyncSessionLocal() as session:
            # Check if content unchanged — skip re-embedding
            existing = await session.execute(sql_text(f"""
                SELECT content_hash FROM {table}
                WHERE repo_id = :repo_id AND entity_type = :entity_type AND entity_id = :entity_id
                LIMIT 1
            """), {"repo_id": repo_id, "entity_type": entity_type, "entity_id": entity_id})
            row = existing.fetchone()

            if row and row.content_hash == content_hash:
                return False  # unchanged, skip

            await session.execute(sql_text(f"""
                INSERT INTO {table}
                    (repo_id, entity_type, entity_id, content, content_hash,
                     embedding, trust_score, embedding_model, metadata, embedded_at)
                VALUES
                    (:repo_id, :entity_type, :entity_id, :content, :content_hash,
                     CAST(:embedding AS vector), :trust_score, :model, CAST(:metadata AS jsonb), now())
                ON CONFLICT (repo_id, entity_type, entity_id)
                DO UPDATE SET
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding,
                    trust_score = EXCLUDED.trust_score,
                    metadata = EXCLUDED.metadata,
                    embedded_at = now()
            """), {
                "repo_id": repo_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "content": content[:5000],
                "content_hash": content_hash,
                "embedding": str(embedding),
                "trust_score": doc.get("trust_score", 0.5),
                "model": model,
                "metadata": json.dumps(doc.get("metadata", {})),
            })
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Table creation
    # ------------------------------------------------------------------

    async def _mark_done(self, doc: Dict, column: str):
        """Mark a doc as done for a shadow model in the tracker table."""
        from sqlalchemy import text as sql_text
        from app.db.session import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(sql_text(f"""
                    UPDATE {TRACKING_TABLE}
                    SET {column} = true
                    WHERE repo_id = :repo_id
                      AND entity_type = :entity_type
                      AND entity_id = :entity_id
                """), {
                    "repo_id": doc.get("repo_id", ""),
                    "entity_type": doc["entity_type"],
                    "entity_id": doc["entity_id"],
                })
                await session.commit()
        except Exception:
            pass  # non-critical

    async def _ensure_tables(self):
        """Create shadow embedding tables if they don't exist."""
        from sqlalchemy import text as sql_text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            try:
                for table, dim, model in [
                    (LARGE_TABLE, LARGE_DIM, LARGE_MODEL),
                    (VOYAGE_TABLE, VOYAGE_DIM, VOYAGE_MODEL),
                ]:
                    await session.execute(sql_text(f"""
                        CREATE TABLE IF NOT EXISTS {table} (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            repo_id VARCHAR(255) NOT NULL DEFAULT '',
                            entity_type VARCHAR(255) NOT NULL,
                            entity_id VARCHAR(500) NOT NULL,
                            content TEXT,
                            content_hash VARCHAR(64) NOT NULL DEFAULT '',
                            embedding vector({dim}),
                            trust_score FLOAT DEFAULT 0.5,
                            embedding_model VARCHAR(100) DEFAULT '{model}',
                            metadata JSONB DEFAULT '{{}}'::jsonb,
                            embedded_at TIMESTAMPTZ DEFAULT now()
                        )
                    """))
                    await session.execute(sql_text(f"""
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_identity
                        ON {table} (repo_id, entity_type, entity_id)
                    """))

                # Tracker table (shared with producer)
                await session.execute(sql_text(f"""
                    CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                        repo_id VARCHAR(255) NOT NULL DEFAULT '',
                        entity_type VARCHAR(255) NOT NULL,
                        entity_id VARCHAR(500) NOT NULL,
                        content_hash VARCHAR(64) NOT NULL DEFAULT '',
                        published_at TIMESTAMPTZ DEFAULT now(),
                        large_done BOOLEAN DEFAULT false,
                        voyage_done BOOLEAN DEFAULT false,
                        PRIMARY KEY (repo_id, entity_type, entity_id)
                    )
                """))

                await session.commit()
                logger.info("embedding_consumer.tables_ensured")
            except Exception as e:
                await session.rollback()
                logger.error("embedding_consumer.tables_failed", error=str(e))


# ===================================================================
# CLI entry point: python -m app.services.embedding_queue consume
# ===================================================================

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "consume":
        consumer = EmbeddingConsumer()
        asyncio.run(consumer.run())
    else:
        print("Usage: python -m app.services.embedding_queue consume")

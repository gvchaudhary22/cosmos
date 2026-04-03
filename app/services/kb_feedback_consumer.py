"""
RALPH→KB Online Feedback Loop (Phase 5e).

Kafka consumer that listens for RALPH quality-verdict events and updates
the knowledge base in real time:

  - Negative feedback (quality_score < NEGATIVE_THRESHOLD):
      • Decrements trust_score on cosmos_embeddings rows that contributed
        to the bad response (by entity_id + entity_type match).
      • If trust_score falls below EVICTION_THRESHOLD, the row is flagged
        with low_quality=true in metadata JSON (soft delete — not evicted
        immediately, excluded from retrieval by vectorstore threshold).

  - Positive feedback (quality_score >= POSITIVE_THRESHOLD):
      • Appends a new row to dev_set.jsonl for future eval seed generation.
      • Increments trust_score on contributing rows (capped at 1.0).

Kafka topic: RALPH_FEEDBACK_TOPIC (default: "cosmos.ralph.feedback")

Message schema:
  {
    "session_id": str,
    "query": str,
    "response": str,
    "quality_score": float (0.0–1.0),
    "verdict": "positive" | "negative" | "neutral",
    "contributing_chunks": [
      {"entity_id": str, "entity_type": str, "chunk_type": str}
    ],
    "timestamp": str (ISO-8601)
  }
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Kafka configuration
RALPH_FEEDBACK_TOPIC = os.environ.get("RALPH_FEEDBACK_TOPIC", "cosmos.ralph.feedback")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_GROUP_ID = os.environ.get("KAFKA_FEEDBACK_GROUP_ID", "cosmos-kb-feedback")

# Feedback thresholds
NEGATIVE_THRESHOLD = float(os.environ.get("KB_FEEDBACK_NEGATIVE_THRESHOLD", "0.4"))
POSITIVE_THRESHOLD = float(os.environ.get("KB_FEEDBACK_POSITIVE_THRESHOLD", "0.75"))

# Trust score adjustments
NEGATIVE_TRUST_DELTA = float(os.environ.get("KB_FEEDBACK_NEGATIVE_DELTA", "-0.05"))
POSITIVE_TRUST_DELTA = float(os.environ.get("KB_FEEDBACK_POSITIVE_DELTA", "0.02"))

# Below this trust_score: mark low_quality=true in metadata JSON
EVICTION_THRESHOLD = float(os.environ.get("KB_FEEDBACK_EVICTION_THRESHOLD", "0.2"))

# Dev set output path (relative to app root, can be overridden)
DEV_SET_PATH = os.environ.get("COSMOS_DEV_SET_PATH", "cosmos/data/dev_set.jsonl")


# ---------------------------------------------------------------------------
# KBFeedbackConsumer
# ---------------------------------------------------------------------------

class KBFeedbackConsumer:
    """
    Consumes RALPH feedback events from Kafka and updates cosmos_embeddings
    trust scores + dev_set.jsonl in real time.

    Usage (in app startup):
        consumer = KBFeedbackConsumer()
        asyncio.create_task(consumer.run())
    """

    def __init__(
        self,
        topic: str = RALPH_FEEDBACK_TOPIC,
        bootstrap_servers: str = KAFKA_BOOTSTRAP_SERVERS,
        group_id: str = KAFKA_GROUP_ID,
        dev_set_path: str = DEV_SET_PATH,
    ) -> None:
        self.topic = topic
        self.bootstrap_servers = bootstrap_servers
        self.group_id = group_id
        self.dev_set_path = Path(dev_set_path)
        self._running = False
        self._consumer = None

    async def run(self) -> None:
        """Start consuming Kafka messages. Runs until stop() is called."""
        try:
            from aiokafka import AIOKafkaConsumer  # type: ignore
        except ImportError:
            logger.warning(
                "kb_feedback_consumer.aiokafka_missing",
                msg="aiokafka not installed — RALPH→KB feedback loop disabled. "
                    "Install with: pip install aiokafka",
            )
            return

        self._running = True
        logger.info("kb_feedback_consumer.starting",
                    topic=self.topic, servers=self.bootstrap_servers)

        try:
            self._consumer = AIOKafkaConsumer(
                self.topic,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_id,
                auto_offset_reset="latest",
                enable_auto_commit=True,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            )
            await self._consumer.start()
            logger.info("kb_feedback_consumer.started")

            async for msg in self._consumer:
                if not self._running:
                    break
                try:
                    await self._handle_event(msg.value)
                except Exception as exc:
                    logger.warning("kb_feedback_consumer.handle_error",
                                   error=str(exc), offset=msg.offset)

        except Exception as exc:
            logger.error("kb_feedback_consumer.fatal", error=str(exc))
        finally:
            if self._consumer:
                await self._consumer.stop()
            logger.info("kb_feedback_consumer.stopped")

    async def stop(self) -> None:
        """Gracefully stop the consumer."""
        self._running = False

    # -----------------------------------------------------------------------
    # Event handler
    # -----------------------------------------------------------------------

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """Process a single RALPH feedback event."""
        query: str = event.get("query", "")
        response: str = event.get("response", "")
        quality_score: float = float(event.get("quality_score", 0.5))
        verdict: str = event.get("verdict", "neutral")
        chunks: List[Dict] = event.get("contributing_chunks", [])
        session_id: str = event.get("session_id", "")

        logger.debug("kb_feedback_consumer.event",
                     verdict=verdict, quality=quality_score,
                     chunks=len(chunks), session=session_id[:8])

        if verdict == "negative" or quality_score < NEGATIVE_THRESHOLD:
            await self._apply_negative_feedback(chunks, query, quality_score)

        elif verdict == "positive" or quality_score >= POSITIVE_THRESHOLD:
            await self._apply_positive_feedback(chunks, query, response, quality_score)

        # neutral: no action — don't accumulate noise

    # -----------------------------------------------------------------------
    # Negative feedback: degrade trust_score
    # -----------------------------------------------------------------------

    async def _apply_negative_feedback(
        self,
        chunks: List[Dict],
        query: str,
        quality_score: float,
    ) -> None:
        """Decrement trust_score on contributing chunks. Flag low-quality rows."""
        if not chunks:
            return

        from app.db.session import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            for chunk in chunks:
                entity_id = chunk.get("entity_id", "")
                entity_type = chunk.get("entity_type", "")
                if not entity_id:
                    continue

                # Decrement trust_score, floor at 0.0
                # Also set low_quality flag if below eviction threshold
                sql = text("""
                    UPDATE cosmos_embeddings
                    SET trust_score = GREATEST(0.0, trust_score + :delta),
                        metadata = metadata || CASE
                            WHEN (trust_score + :delta) < :evict_threshold
                            THEN jsonb_build_object('low_quality', true, 'low_quality_since', now()::text)
                            ELSE '{}'
                        END
                    WHERE entity_id = :entity_id
                      AND (:entity_type = '' OR entity_type = :entity_type)
                    RETURNING id, trust_score
                """)
                result = await session.execute(sql, {
                    "delta": NEGATIVE_TRUST_DELTA,
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "evict_threshold": EVICTION_THRESHOLD,
                })
                updated = result.fetchall()
                if updated:
                    min_trust = min(r.trust_score for r in updated)
                    logger.info("kb_feedback_consumer.trust_degraded",
                                entity_id=entity_id, new_min_trust=round(min_trust, 3),
                                low_quality=(min_trust < EVICTION_THRESHOLD))

            await session.commit()

    # -----------------------------------------------------------------------
    # Positive feedback: boost trust_score + append to dev_set.jsonl
    # -----------------------------------------------------------------------

    async def _apply_positive_feedback(
        self,
        chunks: List[Dict],
        query: str,
        response: str,
        quality_score: float,
    ) -> None:
        """Increment trust_score on contributing chunks. Append to dev_set.jsonl."""
        if chunks:
            from app.db.session import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                for chunk in chunks:
                    entity_id = chunk.get("entity_id", "")
                    entity_type = chunk.get("entity_type", "")
                    if not entity_id:
                        continue

                    sql = text("""
                        UPDATE cosmos_embeddings
                        SET trust_score = LEAST(1.0, trust_score + :delta),
                            metadata = metadata - 'low_quality' - 'low_quality_since'
                        WHERE entity_id = :entity_id
                          AND (:entity_type = '' OR entity_type = :entity_type)
                    """)
                    await session.execute(sql, {
                        "delta": POSITIVE_TRUST_DELTA,
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                    })

                await session.commit()

        # Append to dev_set.jsonl for future eval seed generation
        # Only append if we have a non-trivial query (skip short/noise queries)
        if len(query.strip()) >= 10:
            try:
                # Extract expected tool/entity from chunks if available
                expected_tool = ""
                expected_entity_type = ""
                for chunk in chunks:
                    if chunk.get("chunk_type") in ("api_tool_tags", "api_overview"):
                        expected_tool = chunk.get("entity_id", "")
                        break
                if chunks:
                    expected_entity_type = chunks[0].get("entity_type", "")

                seed = {
                    "query": query,
                    "expected_tool": expected_tool,
                    "expected_entity_type": expected_entity_type,
                    "quality_score": round(quality_score, 3),
                    "source": "ralph_positive",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

                self.dev_set_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.dev_set_path, "a") as f:
                    f.write(json.dumps(seed) + "\n")

                logger.debug("kb_feedback_consumer.dev_set_appended",
                             query=query[:60], tool=expected_tool)

            except Exception as exc:
                logger.warning("kb_feedback_consumer.dev_set_write_error", error=str(exc))


# ---------------------------------------------------------------------------
# Singleton for use by app startup
# ---------------------------------------------------------------------------

_consumer_instance: Optional[KBFeedbackConsumer] = None


def get_kb_feedback_consumer() -> KBFeedbackConsumer:
    """Return (or create) the singleton KBFeedbackConsumer."""
    global _consumer_instance
    if _consumer_instance is None:
        _consumer_instance = KBFeedbackConsumer()
    return _consumer_instance

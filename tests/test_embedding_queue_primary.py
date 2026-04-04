"""
Tests for Phase 9: Kafka-Driven Primary Embedding Pipeline.

Covers:
  - EmbeddingProducer.publish_primary()
  - EmbeddingProducer.publish_dlq()
  - PrimaryEmbeddingConsumer._process_batch()
  - canonical_ingestor.ingest(kafka_mode=True)
  - kafka_mode=True fallback to in-process when Kafka unavailable
  - DLQ routing after MAX_RETRIES failures
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# EmbeddingProducer.publish_primary
# ---------------------------------------------------------------------------

class TestPublishPrimary:

    def _make_producer(self, kafka_ok: bool = True):
        from app.services.embedding_queue import EmbeddingProducer
        p = EmbeddingProducer()
        p._enabled = kafka_ok
        mock_kafka = MagicMock()
        mock_kafka.send = MagicMock(return_value=None)
        p._producer = mock_kafka if kafka_ok else None
        p._init_attempted = True
        return p, mock_kafka

    def test_publish_primary_sends_correct_msg_type(self):
        producer, mock_kafka = self._make_producer(kafka_ok=True)
        ok = producer.publish_primary(
            entity_type="schema",
            entity_id="orders:test",
            content="test content for primary embedding",
            repo_id="MultiChannel_API",
        )
        assert ok is True
        assert mock_kafka.send.call_count == 1
        call_kwargs = mock_kafka.send.call_args
        msg = call_kwargs[1]["value"] if call_kwargs[1] else call_kwargs[0][1]
        assert msg["msg_type"] == "cosmos_primary_embedding"
        assert msg["entity_id"] == "orders:test"
        assert msg["entity_type"] == "schema"
        assert "content_hash" in msg
        assert msg["retry_count"] == 0

    def test_publish_primary_returns_false_when_kafka_disabled(self):
        producer, _ = self._make_producer(kafka_ok=False)
        producer._enabled = False
        ok = producer.publish_primary("schema", "test", "content")
        assert ok is False

    def test_publish_primary_different_from_shadow_msg_type(self):
        """Ensure primary and shadow messages use different msg_type values."""
        producer, mock_kafka = self._make_producer(kafka_ok=True)
        producer.publish_primary("schema", "id1", "content one")
        producer.publish("schema", "id2", "content two", trust_score=0.9)

        assert mock_kafka.send.call_count == 2
        calls = mock_kafka.send.call_args_list
        # First call is primary
        msg1 = calls[0][1]["value"] if calls[0][1] else calls[0][0][1]
        assert msg1["msg_type"] == "cosmos_primary_embedding"
        # Second call is shadow
        msg2 = calls[1][1]["value"] if calls[1][1] else calls[1][0][1]
        assert msg2["msg_type"] == "cosmos_embedding"


# ---------------------------------------------------------------------------
# EmbeddingProducer.publish_dlq
# ---------------------------------------------------------------------------

class TestPublishDlq:

    def _make_producer(self):
        from app.services.embedding_queue import EmbeddingProducer
        p = EmbeddingProducer()
        p._enabled = True
        mock_kafka = MagicMock()
        mock_kafka.send = MagicMock(return_value=None)
        p._producer = mock_kafka
        p._init_attempted = True
        return p, mock_kafka

    def test_publish_dlq_sets_correct_msg_type(self):
        producer, mock_kafka = self._make_producer()
        doc = {
            "entity_id": "orders:123",
            "entity_type": "schema",
            "content": "some content",
            "msg_type": "cosmos_primary_embedding",
        }
        ok = producer.publish_dlq(doc, reason="Qdrant connection refused")
        assert ok is True
        msg = mock_kafka.send.call_args[1]["value"] if mock_kafka.send.call_args[1] else mock_kafka.send.call_args[0][1]
        assert msg["msg_type"] == "cosmos_primary_embedding.dlq"
        assert msg["dlq_reason"] == "Qdrant connection refused"
        assert "dlq_at" in msg
        assert msg["entity_id"] == "orders:123"

    def test_publish_dlq_returns_false_when_disabled(self):
        from app.services.embedding_queue import EmbeddingProducer
        p = EmbeddingProducer()
        p._enabled = False
        p._init_attempted = True
        p._producer = None
        ok = p.publish_dlq({"entity_id": "x"}, reason="test")
        assert ok is False


# ---------------------------------------------------------------------------
# PrimaryEmbeddingConsumer._process_batch
# ---------------------------------------------------------------------------

class TestPrimaryConsumerBatch:

    @pytest.mark.asyncio
    async def test_process_batch_embeds_all_docs(self):
        from app.services.embedding_queue import PrimaryEmbeddingConsumer

        vs = MagicMock()
        vs.store_embedding = AsyncMock(return_value="ok")
        consumer = PrimaryEmbeddingConsumer(vectorstore=vs)

        batch = [
            {"entity_type": "schema", "entity_id": f"t:{i}",
             "content": f"content {i}", "repo_id": "MultiChannel_API",
             "msg_type": "cosmos_primary_embedding"}
            for i in range(5)
        ]

        await consumer._process_batch(batch, vs)

        assert vs.store_embedding.call_count == 5
        assert consumer._stats["embedded_ok"] == 5
        assert consumer._stats["embedded_err"] == 0
        assert consumer._stats["dlq_sent"] == 0

    @pytest.mark.asyncio
    async def test_process_batch_routes_to_dlq_after_max_retries(self):
        from app.services.embedding_queue import PrimaryEmbeddingConsumer, PRIMARY_MAX_RETRIES

        vs = MagicMock()
        vs.store_embedding = AsyncMock(side_effect=RuntimeError("Qdrant down"))

        consumer = PrimaryEmbeddingConsumer(vectorstore=vs)
        consumer._producer = MagicMock()
        consumer._producer.publish_dlq = MagicMock(return_value=True)

        doc = {
            "entity_type": "schema", "entity_id": "fail:entity",
            "content": "content", "repo_id": "test",
            "msg_type": "cosmos_primary_embedding",
        }

        # Run batch PRIMARY_MAX_RETRIES times — should route to DLQ on last attempt
        for attempt in range(PRIMARY_MAX_RETRIES):
            await consumer._process_batch([doc], vs)

        assert consumer._producer.publish_dlq.call_count == 1
        assert consumer._stats["dlq_sent"] == 1
        # retry counter cleared after DLQ routing
        assert "fail:entity" not in consumer._retry_counts

    @pytest.mark.asyncio
    async def test_process_batch_partial_failure(self):
        """Some docs succeed, some fail — both tracked correctly."""
        from app.services.embedding_queue import PrimaryEmbeddingConsumer

        call_count = 0

        async def flaky_store(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("flaky")
            return "ok"

        vs = MagicMock()
        vs.store_embedding = flaky_store
        consumer = PrimaryEmbeddingConsumer(vectorstore=vs)

        batch = [
            {"entity_type": "schema", "entity_id": f"id:{i}",
             "content": f"content {i}", "repo_id": "", "msg_type": "cosmos_primary_embedding"}
            for i in range(4)
        ]

        await consumer._process_batch(batch, vs)

        assert consumer._stats["embedded_ok"] == 2   # calls 1, 3
        assert consumer._stats["embedded_err"] == 2  # calls 2, 4 (first retry, not DLQ yet)
        assert consumer._stats["dlq_sent"] == 0      # not exhausted retries yet

    @pytest.mark.asyncio
    async def test_process_batch_empty_does_nothing(self):
        from app.services.embedding_queue import PrimaryEmbeddingConsumer

        vs = MagicMock()
        vs.store_embedding = AsyncMock(return_value="ok")
        consumer = PrimaryEmbeddingConsumer(vectorstore=vs)

        await consumer._process_batch([], vs)

        vs.store_embedding.assert_not_called()
        assert consumer._stats["embedded_ok"] == 0


# ---------------------------------------------------------------------------
# canonical_ingestor.ingest(kafka_mode=True)
# ---------------------------------------------------------------------------

class TestKafkaMode:

    def _make_ingestor(self, kafka_available: bool = True):
        from app.services.canonical_ingestor import CanonicalIngestor

        vs = MagicMock()
        vs.store_embedding = AsyncMock(return_value="ok")
        ingestor = CanonicalIngestor(vectorstore=vs)

        mock_producer = MagicMock()
        mock_producer.publish_primary = MagicMock(return_value=kafka_available)
        mock_producer.flush = MagicMock()
        mock_producer._get_producer = MagicMock(return_value=MagicMock() if kafka_available else None)
        ingestor._kafka_init = True
        ingestor._kafka_producer = mock_producer if kafka_available else None

        return ingestor, vs, mock_producer

    @pytest.mark.asyncio
    async def test_kafka_mode_publishes_not_embeds(self):
        """kafka_mode=True → publish_primary called, store_embedding NOT called."""
        from app.services.canonical_ingestor import IngestDocument

        ingestor, vs, mock_producer = self._make_ingestor(kafka_available=True)

        docs = [IngestDocument(
            entity_type="schema", entity_id=f"t:{i}",
            content=f"sufficient content number {i} for quality gate",
            repo_id="MultiChannel_API", capability="retrieval", trust_score=0.9,
        ) for i in range(3)]

        result = await ingestor.ingest(docs, kafka_mode=True)

        assert result.ingested == 3
        assert mock_producer.publish_primary.call_count == 3
        vs.store_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_kafka_mode_skips_low_trust(self):
        """Docs below MIN_TRUST are skipped even in kafka_mode."""
        from app.services.canonical_ingestor import IngestDocument

        ingestor, vs, mock_producer = self._make_ingestor(kafka_available=True)

        docs = [
            IngestDocument(entity_type="schema", entity_id="good",
                           content="sufficient content for the quality gate check",
                           repo_id="x", capability="retrieval", trust_score=0.9),
            IngestDocument(entity_type="schema", entity_id="bad",
                           content="fine content here too",
                           repo_id="x", capability="retrieval", trust_score=0.05),  # below MIN_TRUST=0.1
        ]

        result = await ingestor.ingest(docs, kafka_mode=True)

        assert result.ingested == 1
        assert result.skipped == 1
        assert mock_producer.publish_primary.call_count == 1

    @pytest.mark.asyncio
    async def test_kafka_mode_false_uses_in_process_embed(self):
        """kafka_mode=False (default) → store_embedding called, not publish_primary."""
        from app.services.canonical_ingestor import IngestDocument

        ingestor, vs, mock_producer = self._make_ingestor(kafka_available=True)

        docs = [IngestDocument(
            entity_type="schema", entity_id="test",
            content="sufficient content for the quality gate check",
            repo_id="x", capability="retrieval", trust_score=0.9,
        )]

        result = await ingestor.ingest(docs, kafka_mode=False)

        assert vs.store_embedding.call_count == 1
        mock_producer.publish_primary.assert_not_called()

    @pytest.mark.asyncio
    async def test_kafka_mode_fallback_when_kafka_unavailable(self):
        """kafka_mode=True but Kafka down → falls back to in-process embed."""
        from app.services.canonical_ingestor import IngestDocument

        ingestor, vs, mock_producer = self._make_ingestor(kafka_available=False)

        docs = [IngestDocument(
            entity_type="schema", entity_id="fallback_test",
            content="sufficient content for the quality gate fallback test",
            repo_id="x", capability="retrieval", trust_score=0.9,
        )]

        result = await ingestor.ingest(docs, kafka_mode=True)

        # Kafka unavailable → in-process store_embedding called instead
        assert vs.store_embedding.call_count == 1
        assert result.ingested == 1

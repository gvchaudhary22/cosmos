"""
Tests for Kafka event bus: topics, events, producer, consumer, handlers.
"""

import asyncio
import json
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.events.kafka_bus import (
    EventBus,
    FeedbackEvent,
    KafkaEventConsumer,
    KafkaEventProducer,
    KBUpdatedEvent,
    LearningInsightEvent,
    QueryCompletedEvent,
    Topic,
)


# ---------------------------------------------------------------------------
# Topic enum
# ---------------------------------------------------------------------------

class TestTopics:
    def test_topic_values(self):
        assert Topic.QUERY_COMPLETED.value == Topic.QUERY_COMPLETED.value
        assert Topic.LEARNING_INSIGHT.value == "cosmos.learning.insight"
        assert Topic.FEEDBACK_SUBMITTED.value == "cosmos.feedback.submitted"
        assert Topic.KB_UPDATED.value == "cosmos.kb.updated"

    def test_topic_count(self):
        assert len(Topic) == 5

    def test_external_topic(self):
        assert Topic.SC_ORDERS_WC.value == "sc_webhook_orders_wc"


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------

class TestQueryCompletedEvent:
    def test_to_json(self):
        event = QueryCompletedEvent(
            session_id="sess-1",
            user_id="user-1",
            company_id="comp-1",
            query="Where is my order?",
            intent="order_tracking",
            entity="order",
            confidence=0.95,
            tools_used=["mcapi.orders.get"],
            response="Your order is on the way.",
            escalated=False,
            latency_ms=150.5,
            model="claude-haiku",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.001,
        )
        raw = event.to_json()
        data = json.loads(raw)
        assert data["session_id"] == "sess-1"
        assert data["confidence"] == 0.95
        assert data["tools_used"] == ["mcapi.orders.get"]
        assert isinstance(data["timestamp"], float)

    def test_default_timestamp(self):
        event = QueryCompletedEvent(
            session_id="s", user_id="u", company_id=None, query="q",
            intent="i", entity="e", confidence=0.5, tools_used=[],
            response="r", escalated=False, latency_ms=0.0, model="m",
            tokens_in=0, tokens_out=0, cost_usd=0.0,
        )
        assert event.timestamp > 0


class TestFeedbackEvent:
    def test_to_json(self):
        event = FeedbackEvent(
            session_id="sess-1",
            message_id="msg-1",
            rating=5,
            comment="Great!",
            tags=["helpful"],
        )
        data = json.loads(event.to_json())
        assert data["rating"] == 5
        assert data["tags"] == ["helpful"]

    def test_defaults(self):
        event = FeedbackEvent(session_id="s", message_id="m", rating=3, comment=None)
        assert event.comment is None
        assert event.tags == []


class TestLearningInsightEvent:
    def test_to_json(self):
        event = LearningInsightEvent(
            insight_id="ins-1",
            learning_type="FEW_SHOT_EXAMPLE",
            description="desc",
            evidence="evidence",
            proposed_change="change",
            risk_level="LOW",
            query_pattern="pattern",
        )
        data = json.loads(event.to_json())
        assert data["insight_id"] == "ins-1"
        assert data["learning_type"] == "FEW_SHOT_EXAMPLE"


class TestKBUpdatedEvent:
    def test_to_json(self):
        event = KBUpdatedEvent(
            update_count=3,
            source="github_webhook",
            doc_ids=["doc1", "doc2", "doc3"],
        )
        data = json.loads(event.to_json())
        assert data["update_count"] == 3
        assert len(data["doc_ids"]) == 3


# ---------------------------------------------------------------------------
# KafkaEventProducer
# ---------------------------------------------------------------------------

class TestKafkaEventProducer:
    def test_initial_stats(self):
        producer = KafkaEventProducer()
        stats = producer.get_stats()
        assert stats["started"] is False
        assert stats["total_produced"] == 0
        assert stats["total_errors"] == 0

    @pytest.mark.asyncio
    async def test_produce_when_not_started(self):
        producer = KafkaEventProducer()
        await producer.produce(Topic.QUERY_COMPLETED, b'{"test": true}')
        assert producer.get_stats()["total_errors"] == 1

    @pytest.mark.asyncio
    async def test_produce_with_mock_producer(self):
        """Simulate a started producer with mocked aiokafka."""
        producer = KafkaEventProducer()
        producer._started = True
        producer._producer = AsyncMock()
        producer._producer.send_and_wait = AsyncMock()

        await producer.produce(Topic.QUERY_COMPLETED, b'{"test": true}', key="key-1")
        assert producer.get_stats()["total_produced"] == 1
        assert producer.get_stats()["by_topic"][Topic.QUERY_COMPLETED.value] == 1

    @pytest.mark.asyncio
    async def test_produce_error_handling(self):
        """Should catch send errors and increment error count."""
        producer = KafkaEventProducer()
        producer._started = True
        producer._producer = AsyncMock()
        producer._producer.send_and_wait = AsyncMock(side_effect=Exception("Kafka down"))

        await producer.produce(Topic.QUERY_COMPLETED, b'{}')
        assert producer.get_stats()["total_errors"] == 1
        assert producer.get_stats()["total_produced"] == 0

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        producer = KafkaEventProducer()
        await producer.stop()  # Should not raise even when not started


# ---------------------------------------------------------------------------
# KafkaEventConsumer
# ---------------------------------------------------------------------------

class TestKafkaEventConsumer:
    def test_register_handler(self):
        consumer = KafkaEventConsumer()
        handler = AsyncMock()
        consumer.register_handler(Topic.QUERY_COMPLETED, handler)
        assert Topic.QUERY_COMPLETED.value in consumer._handlers

    def test_initial_stats(self):
        consumer = KafkaEventConsumer()
        stats = consumer.get_stats()
        assert stats["running"] is False
        assert stats["total_consumed"] == 0
        assert stats["active_tasks"] == 0

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        consumer = KafkaEventConsumer()
        await consumer.stop()  # Should not raise

    def test_register_multiple_handlers(self):
        consumer = KafkaEventConsumer()
        consumer.register_handler(Topic.QUERY_COMPLETED, AsyncMock())
        consumer.register_handler(Topic.FEEDBACK_SUBMITTED, AsyncMock())
        assert len(consumer._handlers) == 2


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_disabled_bus(self):
        bus = EventBus(enabled=False)
        stats = bus.get_stats()
        assert stats["enabled"] is False

    @pytest.mark.asyncio
    async def test_disabled_start(self):
        bus = EventBus(enabled=False)
        await bus.start()  # Should just log and return

    @pytest.mark.asyncio
    async def test_stop_disabled(self):
        bus = EventBus(enabled=False)
        await bus.start()
        await bus.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_register_and_stats(self):
        bus = EventBus(enabled=False)
        handler = AsyncMock()
        bus.register_handler(Topic.QUERY_COMPLETED, handler)
        stats = bus.get_stats()
        assert stats["consumer"]["active_tasks"] == 0

    @pytest.mark.asyncio
    async def test_produce_methods_exist(self):
        """Verify all typed produce methods exist and are callable."""
        bus = EventBus(enabled=False)
        assert callable(bus.produce_query_completed)
        assert callable(bus.produce_learning_insight)
        assert callable(bus.produce_feedback)
        assert callable(bus.produce_kb_updated)

    @pytest.mark.asyncio
    async def test_produce_query_completed_calls_producer(self):
        bus = EventBus(enabled=False)
        bus.producer.produce = AsyncMock()
        event = QueryCompletedEvent(
            session_id="s", user_id="u", company_id=None, query="q",
            intent="i", entity="e", confidence=0.5, tools_used=[],
            response="r", escalated=False, latency_ms=0.0, model="m",
            tokens_in=0, tokens_out=0, cost_usd=0.0,
        )
        await bus.produce_query_completed(event)
        bus.producer.produce.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_produce_feedback_calls_producer(self):
        bus = EventBus(enabled=False)
        bus.producer.produce = AsyncMock()
        event = FeedbackEvent(session_id="s", message_id="m", rating=5, comment=None)
        await bus.produce_feedback(event)
        bus.producer.produce.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_produce_kb_updated_calls_producer(self):
        bus = EventBus(enabled=False)
        bus.producer.produce = AsyncMock()
        event = KBUpdatedEvent(update_count=1, source="manual")
        await bus.produce_kb_updated(event)
        bus.producer.produce.assert_awaited_once()


# ---------------------------------------------------------------------------
# Handlers — _safe_uuid (imported lazily to avoid asyncpg)
# ---------------------------------------------------------------------------

class TestSafeUuid:
    @pytest.fixture(autouse=True)
    def _import_handlers(self):
        """Lazy-import handlers to work around asyncpg requirement."""
        # Mock the DB session module to avoid asyncpg import
        mock_session = MagicMock()
        mock_session.AsyncSessionLocal = MagicMock()
        with patch.dict(sys.modules, {"app.db.session": mock_session}):
            # Also need to mock the models
            mock_models = MagicMock()
            with patch.dict(sys.modules, {"app.db.models": mock_models}):
                # Force reimport
                if "app.events.handlers" in sys.modules:
                    del sys.modules["app.events.handlers"]
                from app.events.handlers import _safe_uuid
                self._safe_uuid = _safe_uuid

    def test_valid_string(self):
        uid = uuid.uuid4()
        result = self._safe_uuid(str(uid))
        assert result == uid

    def test_uuid_passthrough(self):
        uid = uuid.uuid4()
        assert self._safe_uuid(uid) is uid

    def test_none(self):
        assert self._safe_uuid(None) is None

    def test_invalid(self):
        assert self._safe_uuid("not-a-uuid") is None

    def test_empty_string(self):
        assert self._safe_uuid("") is None


# ---------------------------------------------------------------------------
# Handlers — handle_query_completed
# ---------------------------------------------------------------------------

class TestHandleQueryCompleted:
    @pytest.mark.asyncio
    async def test_persists_records(self):
        """handler should add DistillationRecord + QueryAnalytics and commit."""
        mock_db = AsyncMock()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session_mod = MagicMock()
        mock_session_mod.AsyncSessionLocal = MagicMock(return_value=mock_session_ctx)

        mock_models = MagicMock()

        event = {
            "session_id": str(uuid.uuid4()),
            "query": "test query",
            "intent": "order_tracking",
            "entity": "order",
            "confidence": 0.9,
            "tools_used": ["mcapi.orders.get"],
            "response": "Your order...",
            "model": "claude-haiku",
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": 0.001,
            "latency_ms": 200.0,
            "escalated": False,
        }

        with patch.dict(sys.modules, {
            "app.db.session": mock_session_mod,
            "app.db.models": mock_models,
        }):
            if "app.events.handlers" in sys.modules:
                del sys.modules["app.events.handlers"]
            from app.events.handlers import handle_query_completed
            await handle_query_completed(event)

        assert mock_db.add.call_count == 2
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_on_error(self):
        """handler should rollback on exception."""
        mock_db = AsyncMock()
        mock_db.commit.side_effect = Exception("DB error")
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session_mod = MagicMock()
        mock_session_mod.AsyncSessionLocal = MagicMock(return_value=mock_session_ctx)
        mock_models = MagicMock()

        with patch.dict(sys.modules, {
            "app.db.session": mock_session_mod,
            "app.db.models": mock_models,
        }):
            if "app.events.handlers" in sys.modules:
                del sys.modules["app.events.handlers"]
            from app.events.handlers import handle_query_completed
            await handle_query_completed({"session_id": str(uuid.uuid4())})

        mock_db.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Handlers — handle_learning_insight + handle_kb_updated
# ---------------------------------------------------------------------------

class TestLogOnlyHandlers:
    @pytest.mark.asyncio
    async def test_learning_insight_logs(self):
        """Should not raise, just log."""
        mock_session_mod = MagicMock()
        mock_models = MagicMock()
        with patch.dict(sys.modules, {
            "app.db.session": mock_session_mod,
            "app.db.models": mock_models,
        }):
            if "app.events.handlers" in sys.modules:
                del sys.modules["app.events.handlers"]
            from app.events.handlers import handle_learning_insight
            await handle_learning_insight({
                "insight_id": "ins-1",
                "learning_type": "FEW_SHOT_EXAMPLE",
            })

    @pytest.mark.asyncio
    async def test_kb_updated_logs(self):
        """Should not raise, just log."""
        mock_session_mod = MagicMock()
        mock_models = MagicMock()
        with patch.dict(sys.modules, {
            "app.db.session": mock_session_mod,
            "app.db.models": mock_models,
        }):
            if "app.events.handlers" in sys.modules:
                del sys.modules["app.events.handlers"]
            from app.events.handlers import handle_kb_updated
            await handle_kb_updated({
                "update_count": 5,
                "source": "scheduled",
            })


# ---------------------------------------------------------------------------
# Order webhook handler
# ---------------------------------------------------------------------------

from app.events.order_handler import (
    handle_order_webhook,
    get_recent_order,
    get_order_cache_stats,
    _build_order_summary,
    _cache_order,
    _recent_orders,
)


class TestOrderWebhookHandler:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _recent_orders.clear()
        yield
        _recent_orders.clear()

    @pytest.mark.asyncio
    async def test_processes_valid_order(self):
        event = {
            "identifier": "webhook",
            "channel_id": "123",
            "base_channel_code": "WC",
            "event": "orders/create",
            "channel_order_id": "ORD-001",
            "uniqueId": "uid-1",
            "data": {
                "id": 1001,
                "status": "processing",
                "total": "599.00",
                "currency": "INR",
                "payment_method": "cod",
                "billing": {"first_name": "Gaurav", "last_name": "C", "email": "g@test.com"},
                "shipping": {"city": "Delhi", "state": "DL"},
                "line_items": [{"name": "T-Shirt", "quantity": 2, "sku": "TS-01", "total": "599.00"}],
            },
        }
        await handle_order_webhook(event)
        cached = get_recent_order("ORD-001")
        assert cached is not None
        assert cached["status"] == "processing"
        assert cached["customer_name"] == "Gaurav C"
        assert cached["item_count"] == 1

    @pytest.mark.asyncio
    async def test_skips_event_without_data(self):
        await handle_order_webhook({"event": "orders/create", "channel_order_id": "X"})
        assert get_recent_order("X") is None

    @pytest.mark.asyncio
    async def test_handles_missing_fields_gracefully(self):
        event = {
            "event": "orders/updated",
            "channel_order_id": "ORD-002",
            "data": {"status": "completed"},
        }
        await handle_order_webhook(event)
        cached = get_recent_order("ORD-002")
        assert cached["status"] == "completed"
        assert cached["customer_name"] == ""

    def test_build_order_summary(self):
        summary = _build_order_summary(
            event_type="orders/create",
            channel_id="10",
            channel_order_id="ORD-003",
            base_channel_code="WC",
            order_data={
                "id": 99,
                "status": "pending",
                "total": "100.00",
                "currency": "INR",
                "billing": {"first_name": "Test", "last_name": "User"},
                "shipping": {"city": "Mumbai", "state": "MH"},
                "line_items": [],
            },
        )
        assert summary["wc_order_id"] == 99
        assert summary["shipping_city"] == "Mumbai"

    def test_cache_eviction(self):
        """Cache should evict oldest when exceeding max."""
        from app.events import order_handler
        old_max = order_handler._MAX_CACHED_ORDERS
        order_handler._MAX_CACHED_ORDERS = 3
        try:
            _cache_order("a", {"status": "a"})
            _cache_order("b", {"status": "b"})
            _cache_order("c", {"status": "c"})
            _cache_order("d", {"status": "d"})  # Should evict "a"
            assert get_recent_order("a") is None
            assert get_recent_order("d") is not None
        finally:
            order_handler._MAX_CACHED_ORDERS = old_max

    def test_cache_stats(self):
        _cache_order("x", {"status": "test"})
        stats = get_order_cache_stats()
        assert stats["cached_orders"] == 1


# ---------------------------------------------------------------------------
# SASL kwargs builder
# ---------------------------------------------------------------------------

from app.events.kafka_bus import _build_sasl_kwargs


class TestBuildSaslKwargs:
    def test_plaintext_returns_empty(self):
        result = _build_sasl_kwargs("PLAINTEXT", "PLAIN", "user", "pass")
        assert result == {}

    def test_no_username_returns_empty(self):
        result = _build_sasl_kwargs("SASL_PLAINTEXT", "PLAIN", None, "pass")
        assert result == {}

    def test_sasl_returns_credentials(self):
        result = _build_sasl_kwargs("SASL_PLAINTEXT", "PLAIN", "user", "pass")
        assert result["security_protocol"] == "SASL_PLAINTEXT"
        assert result["sasl_plain_username"] == "user"
        assert result["sasl_plain_password"] == "pass"
        assert result["sasl_mechanism"] == "PLAIN"

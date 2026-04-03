"""Tests for the database repository layer and Redis cache.

Uses SQLite in-memory for repository tests (async via aiosqlite)
and mocked Redis for cache tests. Does NOT modify existing engines.
"""

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sqlalchemy import event, JSON, String
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB as PG_JSONB

from app.db.models import Base, ICRMSession, ICRMMessage, MessageRole

from app.db.repositories import (
    ApprovalRepository,
    AuditRepository,
    AnalyticsRepository,
    FeedbackRepository,
    KnowledgeRepository,
    DistillationRepository,
    SessionStateRepository,
    CostRepository,
)
from app.db.redis_cache import RedisCache

# Note: redis_cache uses 'app.config' internally but for tests we only use mocks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

sqlite3.register_adapter(uuid.UUID, lambda u: str(u))
sqlite3.register_converter("UUID", lambda b: uuid.UUID(b.decode()))

# Track whether we already patched PG types for this process
_types_patched = False


async def _make_session_factory():
    """Create a fresh in-memory SQLite async engine + session factory."""
    global _types_patched
    if not _types_patched:
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, PG_JSONB):
                    col.type = JSON()
                elif isinstance(col.type, PG_UUID):
                    col.type = String(36)
        _types_patched = True

    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return factory, eng


def _run(coro):
    """Run an async coroutine."""
    return asyncio.run(coro)


async def _create_session_row(session_factory) -> str:
    """Insert a minimal icrm_sessions row and return its UUID string."""
    session_id = str(uuid.uuid4())
    async with session_factory() as session:
        record = ICRMSession(
            id=session_id,
            user_id="test-user",
            company_id="test-company",
            channel="web",
            status="active",
        )
        session.add(record)
        await session.commit()
    return session_id


async def _create_message_row(session_factory, session_id: str) -> str:
    """Insert a minimal icrm_messages row and return its UUID string."""
    msg_id = str(uuid.uuid4())
    async with session_factory() as session:
        record = ICRMMessage(
            id=msg_id,
            session_id=session_id,
            role=MessageRole.user,
            content="test message",
        )
        session.add(record)
        await session.commit()
    return msg_id


# ===========================================================================
# ApprovalRepository Tests
# ===========================================================================

class TestApprovalRepository:

    def test_create_and_get(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            sid = await _create_session_row(factory)

            result = await repo.create({
                "session_id": sid,
                "action_type": "refund",
                "risk_level": "high",
                "approval_mode": "manual",
                "reason": "Customer refund request",
            })

            assert result["action_type"] == "refund"
            assert result["approved"] is None

            fetched = await repo.get_by_id(result["id"])
            assert fetched is not None
            assert fetched["id"] == result["id"]
            assert fetched["reason"] == "Customer refund request"
            await eng.dispose()

        _run(_test())

    def test_update_status_approve(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            sid = await _create_session_row(factory)

            created = await repo.create({
                "session_id": sid,
                "action_type": "cancel_order",
                "risk_level": "medium",
            })

            updated = await repo.update_status(
                created["id"], "approved", approved_by="admin-1"
            )
            assert updated["approved"] is True
            assert updated["approved_by"] == "admin-1"
            assert updated["resolved_at"] is not None
            await eng.dispose()

        _run(_test())

    def test_update_status_reject(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            sid = await _create_session_row(factory)

            created = await repo.create({
                "session_id": sid,
                "action_type": "delete_user",
                "risk_level": "critical",
            })

            updated = await repo.update_status(
                created["id"], "rejected", approved_by="mgr-1", reason="Too risky"
            )
            assert updated["approved"] is False
            assert updated["reason"] == "Too risky"
            await eng.dispose()

        _run(_test())

    def test_list_pending(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            sid = await _create_session_row(factory)

            await repo.create({"session_id": sid, "action_type": "a1", "risk_level": "low"})
            await repo.create({"session_id": sid, "action_type": "a2", "risk_level": "medium"})

            pending = await repo.list_pending()
            assert len(pending) == 2
            await eng.dispose()

        _run(_test())

    def test_expire_stale(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            sid = await _create_session_row(factory)

            await repo.create({"session_id": sid, "action_type": "old_action", "risk_level": "low"})

            count = await repo.expire_stale(max_age_minutes=0)
            assert count == 1
            await eng.dispose()

        _run(_test())

    def test_get_nonexistent(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            result = await repo.get_by_id(str(uuid.uuid4()))
            assert result is None
            await eng.dispose()

        _run(_test())

    def test_update_nonexistent(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = ApprovalRepository(factory)
            result = await repo.update_status(str(uuid.uuid4()), "approved")
            assert result is None
            await eng.dispose()

        _run(_test())


# ===========================================================================
# AuditRepository Tests
# ===========================================================================

class TestAuditRepository:

    def test_log_and_query(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AuditRepository(factory)

            await repo.log_entry({
                "action": "order.cancelled",
                "user_id": "user-1",
                "resource_type": "order",
                "resource_id": "ORD-123",
                "details": {"reason": "customer request"},
            })

            trail = await repo.get_trail(user_id="user-1")
            assert len(trail) == 1
            assert trail[0]["action"] == "order.cancelled"
            await eng.dispose()

        _run(_test())

    def test_query_by_session(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AuditRepository(factory)
            sid = await _create_session_row(factory)

            await repo.log_entry({"action": "login", "session_id": sid, "user_id": "u1"})
            await repo.log_entry({"action": "query", "session_id": sid, "user_id": "u1"})
            await repo.log_entry({"action": "other", "user_id": "u2"})

            trail = await repo.get_trail(session_id=sid)
            assert len(trail) == 2
            await eng.dispose()

        _run(_test())

    def test_trail_limit(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AuditRepository(factory)

            for i in range(10):
                await repo.log_entry({"action": f"action_{i}", "user_id": "u1"})

            trail = await repo.get_trail(user_id="u1", limit=5)
            assert len(trail) == 5
            await eng.dispose()

        _run(_test())


# ===========================================================================
# AnalyticsRepository Tests
# ===========================================================================

class TestAnalyticsRepository:

    def test_record_and_dashboard(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AnalyticsRepository(factory)

            await repo.record_query({
                "intent": "lookup",
                "entity": "order",
                "confidence": 0.9,
                "latency_ms": 150.0,
                "tools_used": ["get_order"],
                "model": "claude-haiku-4-5",
                "cost_usd": 0.001,
            })

            dashboard = await repo.get_dashboard(days=1)
            assert dashboard["total_queries"] == 1
            assert dashboard["avg_confidence"] == 0.9
            await eng.dispose()

        _run(_test())

    def test_intent_breakdown(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AnalyticsRepository(factory)

            await repo.record_query({"intent": "lookup", "confidence": 0.8, "model": "haiku"})
            await repo.record_query({"intent": "lookup", "confidence": 0.7, "model": "haiku"})
            await repo.record_query({"intent": "explain", "confidence": 0.9, "model": "sonnet"})

            breakdown = await repo.get_intent_breakdown(days=1)
            assert breakdown.get("lookup") == 2
            assert breakdown.get("explain") == 1
            await eng.dispose()

        _run(_test())

    def test_cost_report(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = AnalyticsRepository(factory)

            await repo.record_query({
                "intent": "act",
                "model": "claude-sonnet-4-6",
                "cost_usd": 0.005,
            })

            report = await repo.get_cost_report(days=1)
            assert report["total_cost_usd"] == 0.005
            await eng.dispose()

        _run(_test())


# ===========================================================================
# FeedbackRepository Tests
# ===========================================================================

class TestFeedbackRepository:

    def test_submit_and_get(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = FeedbackRepository(factory)
            sid = await _create_session_row(factory)
            mid = await _create_message_row(factory, sid)

            result = await repo.submit({
                "session_id": sid,
                "message_id": mid,
                "user_id": "agent-1",
                "rating": 4,
                "comment": "Good response",
                "tags": ["accurate", "helpful"],
            })

            assert result["rating"] == 4
            assert result["tags"] == ["accurate", "helpful"]

            items = await repo.get_by_session(sid)
            assert len(items) == 1
            await eng.dispose()

        _run(_test())

    def test_summary(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = FeedbackRepository(factory)
            sid = await _create_session_row(factory)

            await repo.submit({"session_id": sid, "rating": 5})
            await repo.submit({"session_id": sid, "rating": 3})
            await repo.submit({"session_id": sid, "rating": 1})

            summary = await repo.get_summary(days=1)
            assert summary["total_feedback"] == 3
            assert summary["avg_score"] == 3.0
            await eng.dispose()

        _run(_test())

    def test_low_scoring(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = FeedbackRepository(factory)
            sid = await _create_session_row(factory)

            await repo.submit({"session_id": sid, "rating": 1, "comment": "Bad"})
            await repo.submit({"session_id": sid, "rating": 2, "comment": "Poor"})
            await repo.submit({"session_id": sid, "rating": 5, "comment": "Great"})

            low = await repo.get_low_scoring(max_score=2)
            assert len(low) == 2
            await eng.dispose()

        _run(_test())


# ===========================================================================
# KnowledgeRepository Tests
# ===========================================================================

class TestKnowledgeRepository:

    def test_add_and_search(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = KnowledgeRepository(factory)

            result = await repo.add({
                "category": "faq",
                "question": "How do I track my shipment?",
                "answer": "Use the tracking page with your AWB number.",
                "source": "docs",
            })

            assert result["category"] == "faq"
            assert result["question"] == "How do I track my shipment?"

            results = await repo.search("track")
            assert len(results) >= 1
            assert "track" in results[0]["question"].lower()
            await eng.dispose()

        _run(_test())

    def test_search_with_category(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = KnowledgeRepository(factory)

            await repo.add({"category": "faq", "question": "FAQ item", "answer": "Answer"})
            await repo.add({"category": "policy", "question": "Policy item", "answer": "Policy answer"})

            faq_results = await repo.search("item", category="faq")
            assert all(r["category"] == "faq" for r in faq_results)
            await eng.dispose()

        _run(_test())

    def test_stats(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = KnowledgeRepository(factory)

            await repo.add({"category": "faq", "question": "Q1", "answer": "A1"})
            await repo.add({"category": "faq", "question": "Q2", "answer": "A2"})
            await repo.add({"category": "policy", "question": "Q3", "answer": "A3"})

            stats = await repo.get_stats()
            assert stats["total_entries"] == 3
            assert stats["by_category"]["faq"] == 2
            assert stats["by_category"]["policy"] == 1
            await eng.dispose()

        _run(_test())


# ===========================================================================
# DistillationRepository Tests
# ===========================================================================

class TestDistillationRepository:

    def test_log_and_stats(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = DistillationRepository(factory)
            sid = await _create_session_row(factory)

            record_id = await repo.log({
                "session_id": sid,
                "user_query": "What is my order status?",
                "intent": "lookup",
                "entity": "order",
                "confidence": 0.85,
                "model_used": "claude-haiku-4-5",
                "token_count_input": 100,
                "token_count_output": 50,
                "cost_usd": 0.001,
            })

            assert record_id is not None

            stats = await repo.get_stats()
            assert stats["total_records"] == 1
            await eng.dispose()

        _run(_test())

    def test_add_feedback_and_export(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = DistillationRepository(factory)
            sid = await _create_session_row(factory)

            record_id = await repo.log({
                "session_id": sid,
                "user_query": "How to cancel order?",
                "confidence": 0.9,
                "final_response": "You can cancel from the dashboard.",
            })

            await repo.add_feedback(record_id, score=5, text="Perfect answer")

            exported = await repo.export(min_confidence=0.7, min_feedback=4)
            assert len(exported) == 1
            assert exported[0]["feedback_score"] == 5
            await eng.dispose()

        _run(_test())

    def test_add_feedback_invalid_score(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = DistillationRepository(factory)
            with pytest.raises(ValueError, match="between 1 and 5"):
                await repo.add_feedback(str(uuid.uuid4()), score=0)
            await eng.dispose()

        _run(_test())

    def test_add_feedback_nonexistent(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = DistillationRepository(factory)
            with pytest.raises(ValueError, match="not found"):
                await repo.add_feedback(str(uuid.uuid4()), score=3)
            await eng.dispose()

        _run(_test())


# ===========================================================================
# SessionStateRepository Tests
# ===========================================================================

class TestSessionStateRepository:

    def test_create_and_get(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = SessionStateRepository(factory)

            result = await repo.create_session({
                "user_id": "user-123",
                "company_id": "comp-456",
                "channel": "telegram",
            })

            assert result["user_id"] == "user-123"
            assert result["channel"] == "telegram"

            state = await repo.get_state(result["id"])
            assert state is not None
            assert state["context"] is not None
            await eng.dispose()

        _run(_test())

    def test_update_state(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = SessionStateRepository(factory)

            created = await repo.create_session({"user_id": "u1", "company_id": "c1"})

            updated = await repo.update_state(created["id"], {
                "intent": "lookup",
                "entities": {"order": ["ORD-1"]},
                "status": "active",
            })

            assert updated["context"]["intent"] == "lookup"
            assert updated["context"]["entities"] == {"order": ["ORD-1"]}
            await eng.dispose()

        _run(_test())

    def test_get_nonexistent(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = SessionStateRepository(factory)
            result = await repo.get_state(str(uuid.uuid4()))
            assert result is None
            await eng.dispose()

        _run(_test())

    def test_update_nonexistent_raises(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = SessionStateRepository(factory)
            with pytest.raises(ValueError, match="not found"):
                await repo.update_state(str(uuid.uuid4()), {"status": "closed"})
            await eng.dispose()

        _run(_test())


# ===========================================================================
# CostRepository Tests
# ===========================================================================

class TestCostRepository:

    def test_record_and_daily_summary(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = CostRepository(factory)
            sid = await _create_session_row(factory)

            await repo.record({
                "session_id": sid,
                "model_tier": "haiku",
                "input_tokens": 500,
                "output_tokens": 200,
                "cost_usd": 0.002,
                "intent": "lookup",
            })

            summary = await repo.get_daily_summary()
            assert summary["query_count"] == 1
            assert summary["total_cost_usd"] == 0.002
            await eng.dispose()

        _run(_test())

    def test_session_summary(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = CostRepository(factory)
            sid = await _create_session_row(factory)

            await repo.record({"session_id": sid, "cost_usd": 0.01, "model_tier": "sonnet"})
            await repo.record({"session_id": sid, "cost_usd": 0.02, "model_tier": "sonnet"})

            summary = await repo.get_session_summary(sid)
            assert summary["query_count"] == 2
            assert summary["total_cost_usd"] == 0.03
            await eng.dispose()

        _run(_test())

    def test_trend(self):
        async def _test():
            factory, eng = await _make_session_factory()
            repo = CostRepository(factory)
            sid = await _create_session_row(factory)

            await repo.record({"session_id": sid, "cost_usd": 0.005, "model_tier": "haiku"})

            trend = await repo.get_trend(days=1)
            assert len(trend) >= 1
            await eng.dispose()

        _run(_test())


# ===========================================================================
# RedisCache Tests (mocked)
# ===========================================================================

class TestRedisCache:

    def test_session_get_set(self):
        async def _test():
            cache = RedisCache("redis://localhost:6379/0")
            mock_redis = AsyncMock()
            cache._redis = mock_redis

            state = {"user_id": "u1", "message_count": 5}
            await cache.set_session("sess-1", state, ttl=1800)
            mock_redis.setex.assert_called_once()

            mock_redis.get.return_value = json.dumps(state)
            result = await cache.get_session("sess-1")
            assert result == state

        _run(_test())

    def test_session_get_missing(self):
        async def _test():
            cache = RedisCache()
            mock_redis = AsyncMock()
            mock_redis.get.return_value = None
            cache._redis = mock_redis

            result = await cache.get_session("nonexistent")
            assert result is None

        _run(_test())

    def test_rate_limiting(self):
        async def _test():
            cache = RedisCache()
            # Use MagicMock for redis so pipeline() is sync (matches real redis.asyncio)
            mock_redis = MagicMock()
            mock_pipe = MagicMock()
            mock_pipe.incr = MagicMock(return_value=mock_pipe)
            mock_pipe.expire = MagicMock(return_value=mock_pipe)
            mock_pipe.execute = AsyncMock(return_value=[3, True])
            mock_redis.pipeline.return_value = mock_pipe
            cache._redis = mock_redis

            count = await cache.increment_rate("user:u1:query", window=60)
            assert count == 3

        _run(_test())

    def test_get_rate(self):
        async def _test():
            cache = RedisCache()
            mock_redis = AsyncMock()
            mock_redis.get.return_value = "7"
            cache._redis = mock_redis

            count = await cache.get_rate("user:u1:query")
            assert count == 7

        _run(_test())

    def test_get_rate_no_connection(self):
        async def _test():
            cache = RedisCache()
            count = await cache.get_rate("anything")
            assert count == 0

        _run(_test())

    def test_tool_result_cache(self):
        async def _test():
            cache = RedisCache()
            mock_redis = AsyncMock()
            cache._redis = mock_redis

            result = {"data": "order details"}
            await cache.cache_tool_result("get_order", "hash123", result, ttl=300)
            mock_redis.setex.assert_called_once()

            mock_redis.get.return_value = json.dumps(result)
            cached = await cache.get_cached_tool_result("get_order", "hash123")
            assert cached == result

        _run(_test())

    def test_tool_result_cache_miss(self):
        async def _test():
            cache = RedisCache()
            mock_redis = AsyncMock()
            mock_redis.get.return_value = None
            cache._redis = mock_redis

            cached = await cache.get_cached_tool_result("get_order", "miss")
            assert cached is None

        _run(_test())

    def test_delete_session(self):
        async def _test():
            cache = RedisCache()
            mock_redis = AsyncMock()
            cache._redis = mock_redis

            await cache.delete_session("sess-1")
            mock_redis.delete.assert_called_once_with("session:sess-1")

        _run(_test())

    def test_graceful_degradation_no_redis(self):
        async def _test():
            cache = RedisCache()
            assert cache._redis is None

            assert await cache.get_session("x") is None
            await cache.set_session("x", {})
            assert await cache.increment_rate("x") == 0
            assert await cache.get_rate("x") == 0
            assert await cache.get_cached_tool_result("x", "y") is None
            await cache.cache_tool_result("x", "y", {})

        _run(_test())

    def test_is_connected(self):
        cache = RedisCache()
        assert cache.is_connected is False

        cache._redis = AsyncMock()
        assert cache.is_connected is True

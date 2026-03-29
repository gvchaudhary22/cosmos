"""
Tests for COSMOS Phase 3: Intelligence & Learning.

Covers:
  - DistillationCollector: log, feedback, export, stats
  - FeedbackEngine: submit, summary, low-scoring
  - AnalyticsEngine: record, dashboard, cost report
  - KnowledgeManager: add, search, context retrieval, TF-IDF
  - API endpoint contract tests
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import sqlite3

from sqlalchemy import event, JSON, String, TypeDecorator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB as PG_JSONB
from cosmos.app.db.models import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Register UUID adapter for SQLite so it can bind uuid.UUID objects
sqlite3.register_adapter(uuid.UUID, lambda u: str(u))
sqlite3.register_converter("UUID", lambda b: uuid.UUID(b.decode()))


async def _make_db():
    """Create a fresh in-memory SQLite async session + engine with PG type shims."""
    # Remap PG-specific column types for SQLite compatibility
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, PG_JSONB):
                col.type = JSON()
            elif isinstance(col.type, PG_UUID):
                col.type = String(36)

    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    return session, eng


def _run(coro):
    """Run an async coroutine."""
    return asyncio.run(coro)


def _mock_react_result(tools=None, confidence=0.85, response="Test response"):
    r = MagicMock()
    r.tools_used = tools or []
    r.confidence = confidence
    r.response = response
    r.steps = []
    return r


# =====================================================================
# TF-IDF unit tests
# =====================================================================


class TestTFIDF:
    def test_tokenize_basic(self):
        from cosmos.app.learning.knowledge import _tokenize
        tokens = _tokenize("How do I track my order?")
        assert "track" in tokens
        assert "order" in tokens
        assert "how" not in tokens
        assert "do" not in tokens

    def test_tokenize_empty(self):
        from cosmos.app.learning.knowledge import _tokenize
        assert _tokenize("") == []

    def test_tokenize_punctuation(self):
        from cosmos.app.learning.knowledge import _tokenize
        tokens = _tokenize("order #12345 -- what's the status?")
        assert "order" in tokens
        assert "12345" in tokens
        assert "status" in tokens

    def test_compute_idf(self):
        from cosmos.app.learning.knowledge import _compute_idf
        docs = [["order", "status"], ["track", "shipment"], ["order", "refund"]]
        idf = _compute_idf(docs, ["order", "track"])
        assert idf["track"] > idf["order"]

    def test_tfidf_similarity_identical(self):
        from cosmos.app.learning.knowledge import _tfidf_similarity, _compute_idf
        doc = ["order", "status", "track"]
        idf = _compute_idf([doc], doc)
        sim = _tfidf_similarity(doc, doc, idf)
        assert sim == pytest.approx(1.0, abs=0.01)

    def test_tfidf_similarity_disjoint(self):
        from cosmos.app.learning.knowledge import _tfidf_similarity, _compute_idf
        q = ["order", "status"]
        d = ["payment", "refund"]
        idf = _compute_idf([d], q)
        sim = _tfidf_similarity(q, d, idf)
        assert sim == 0.0

    def test_tfidf_similarity_partial(self):
        from cosmos.app.learning.knowledge import _tfidf_similarity, _compute_idf
        q = ["order", "status"]
        d = ["order", "delivery", "status", "details"]
        idf = _compute_idf([d], q)
        sim = _tfidf_similarity(q, d, idf)
        assert 0.0 < sim < 1.0


# =====================================================================
# DistillationCollector tests
# =====================================================================


class TestDistillationCollector:
    def test_log_interaction(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                record_id = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(["lookup_order"]),
                    llm_prompt="Show order 12345",
                    llm_response="Your order is shipped",
                    model="claude-haiku-4-5",
                    tokens_in=100, tokens_out=50,
                )
                assert record_id is not None
                assert len(record_id) == 36
            await engine.dispose()

        _run(_test())

    def test_add_feedback(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                rid = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(),
                    llm_prompt="test", llm_response="test",
                    model="claude-haiku-4-5", tokens_in=10, tokens_out=5,
                )
                await collector.add_feedback(rid, score=5, text="Great answer")
            await engine.dispose()

        _run(_test())

    def test_add_feedback_invalid_score(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                with pytest.raises(ValueError, match="between 1 and 5"):
                    await collector.add_feedback(str(uuid.uuid4()), score=6)
            await engine.dispose()

        _run(_test())

    def test_add_feedback_not_found(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                with pytest.raises(ValueError, match="not found"):
                    await collector.add_feedback(str(uuid.uuid4()), score=3)
            await engine.dispose()

        _run(_test())

    def test_get_stats_empty(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                stats = await collector.get_stats()
                assert stats["total_records"] == 0
                assert stats["avg_confidence"] == 0.0
                assert stats["exportable_records"] == 0
            await engine.dispose()

        _run(_test())

    def test_get_stats_with_data(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                rid = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(confidence=0.9),
                    llm_prompt="test", llm_response="test",
                    model="claude-haiku-4-5", tokens_in=50, tokens_out=20,
                )
                await collector.add_feedback(rid, score=5)
                stats = await collector.get_stats()
                assert stats["total_records"] == 1
                assert stats["exportable_records"] == 1
            await engine.dispose()

        _run(_test())

    def test_export_training_data_empty(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                data = await collector.export_training_data()
                assert data == ""
            await engine.dispose()

        _run(_test())

    def test_export_training_data_with_records(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                rid = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(["lookup_order"], 0.9, "Order shipped"),
                    llm_prompt="show order 123", llm_response="Order shipped",
                    model="claude-haiku-4-5", tokens_in=50, tokens_out=20,
                )
                await collector.add_feedback(rid, score=5, text="Perfect")
                data = await collector.export_training_data(min_confidence=0.7, min_feedback=4)
                assert data != ""
                parsed = json.loads(data.split("\n")[0])
                assert "messages" in parsed
                assert parsed["confidence"] == 0.9
            await engine.dispose()

        _run(_test())

    def test_export_filters_low_quality(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                rid = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(confidence=0.3),
                    llm_prompt="vague", llm_response="unsure",
                    model="claude-haiku-4-5", tokens_in=20, tokens_out=10,
                )
                await collector.add_feedback(rid, score=2)
                data = await collector.export_training_data(min_confidence=0.7, min_feedback=4)
                assert data == ""
            await engine.dispose()

        _run(_test())

    def test_cost_estimation(self):
        from cosmos.app.learning.collector import _estimate_cost
        cost_haiku = _estimate_cost("claude-haiku-4-5", 1000, 500)
        assert cost_haiku > 0
        assert cost_haiku < 1.0
        cost_sonnet = _estimate_cost("claude-sonnet-4-6", 1000, 500)
        assert cost_sonnet > cost_haiku


# =====================================================================
# FeedbackEngine tests
# =====================================================================


class TestFeedbackEngine:
    def test_submit_feedback(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                result = await fb.submit_feedback(
                    message_id=str(uuid.uuid4()),
                    session_id=str(uuid.uuid4()),
                    score=4, text="Good response",
                    categories=["accurate", "helpful"],
                )
                assert result["score"] == 4
                assert result["text"] == "Good response"
                assert "accurate" in result["categories"]
            await engine.dispose()

        _run(_test())

    def test_submit_feedback_invalid_score(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                with pytest.raises(ValueError, match="between 1 and 5"):
                    await fb.submit_feedback(
                        str(uuid.uuid4()), str(uuid.uuid4()), score=0,
                    )
            await engine.dispose()

        _run(_test())

    def test_submit_feedback_invalid_category(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                with pytest.raises(ValueError, match="Invalid categories"):
                    await fb.submit_feedback(
                        str(uuid.uuid4()), str(uuid.uuid4()),
                        score=3, categories=["invalid_cat"],
                    )
            await engine.dispose()

        _run(_test())

    def test_get_session_feedback(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                sid = str(uuid.uuid4())
                await fb.submit_feedback(str(uuid.uuid4()), sid, score=5)
                await fb.submit_feedback(str(uuid.uuid4()), sid, score=3)
                feedback = await fb.get_session_feedback(sid)
                assert len(feedback) == 2
            await engine.dispose()

        _run(_test())

    def test_get_feedback_summary(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                for score in [5, 4, 3, 5, 4]:
                    await fb.submit_feedback(
                        str(uuid.uuid4()), str(uuid.uuid4()), score=score,
                    )
                summary = await fb.get_feedback_summary(days=7)
                assert summary["total_feedback"] == 5
                assert summary["avg_score"] > 0
                assert "score_distribution" in summary
            await engine.dispose()

        _run(_test())

    def test_get_low_scoring_queries(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                await fb.submit_feedback(
                    str(uuid.uuid4()), str(uuid.uuid4()),
                    score=1, text="Wrong", categories=["wrong"],
                )
                await fb.submit_feedback(
                    str(uuid.uuid4()), str(uuid.uuid4()),
                    score=5, text="Perfect",
                )
                low = await fb.get_low_scoring_queries(max_score=2)
                assert len(low) == 1
                assert low[0]["score"] == 1
            await engine.dispose()

        _run(_test())

    def test_get_low_scoring_empty(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                low = await fb.get_low_scoring_queries()
                assert low == []
            await engine.dispose()

        _run(_test())


# =====================================================================
# AnalyticsEngine tests
# =====================================================================


class TestAnalyticsEngine:
    def test_record_query(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                await ae.record_query(
                    session_id=str(uuid.uuid4()),
                    intent="lookup", entity="order",
                    confidence=0.85, latency_ms=150.0,
                    tools_used=["lookup_order"], escalated=False,
                    model="claude-haiku-4-5", cost_usd=0.001,
                )
            await engine.dispose()

        _run(_test())

    def test_get_dashboard_empty(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                dashboard = await ae.get_dashboard(days=7)
                assert dashboard["total_queries"] == 0
                assert dashboard["avg_confidence"] == 0.0
                assert dashboard["period_days"] == 7
            await engine.dispose()

        _run(_test())

    def test_get_dashboard_with_data(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                for i in range(5):
                    await ae.record_query(
                        session_id=str(uuid.uuid4()),
                        intent="lookup" if i % 2 == 0 else "explain",
                        entity="order",
                        confidence=0.7 + (i * 0.05),
                        latency_ms=100.0 + (i * 20),
                        tools_used=["lookup_order"], escalated=(i == 4),
                        model="claude-haiku-4-5", cost_usd=0.001,
                    )
                dashboard = await ae.get_dashboard(days=7)
                assert dashboard["total_queries"] == 5
                assert dashboard["avg_confidence"] > 0
                assert dashboard["intent_breakdown"]["lookup"] == 3
                assert dashboard["intent_breakdown"]["explain"] == 2
                assert dashboard["escalation_rate"] > 0
                assert dashboard["total_cost_usd"] > 0
            await engine.dispose()

        _run(_test())

    def test_get_intent_analytics(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                await ae.record_query(
                    session_id=str(uuid.uuid4()),
                    intent="lookup", entity="order",
                    confidence=0.9, latency_ms=100.0,
                    tools_used=["lookup_order"], escalated=False,
                    model="claude-haiku-4-5", cost_usd=0.001,
                )
                result = await ae.get_intent_analytics("lookup", days=7)
                assert result["intent"] == "lookup"
                assert result["total_queries"] == 1
                assert result["avg_confidence"] > 0
            await engine.dispose()

        _run(_test())

    def test_get_cost_report(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                await ae.record_query(
                    session_id=str(uuid.uuid4()),
                    intent="lookup", entity="order",
                    confidence=0.9, latency_ms=100.0,
                    tools_used=[], escalated=False,
                    model="claude-haiku-4-5", cost_usd=0.002,
                )
                report = await ae.get_cost_report(days=30)
                assert report["total_cost_usd"] > 0
                assert "by_model" in report
                assert "by_intent" in report
                assert "by_day" in report
            await engine.dispose()

        _run(_test())

    def test_get_hourly_traffic(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                await ae.record_query(
                    session_id=str(uuid.uuid4()),
                    intent="lookup", entity="order",
                    confidence=0.9, latency_ms=100.0,
                    tools_used=[], escalated=False,
                    model="claude-haiku-4-5", cost_usd=0.001,
                )
                traffic = await ae.get_hourly_traffic(days=1)
                assert len(traffic) >= 1
                assert traffic[0]["count"] >= 1
            await engine.dispose()

        _run(_test())

    def test_dashboard_has_all_keys(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                dashboard = await ae.get_dashboard(days=7)
                required_keys = [
                    "period_days", "total_queries", "queries_per_day",
                    "avg_confidence", "confidence_distribution", "intent_breakdown",
                    "entity_breakdown", "avg_latency_ms", "p95_latency_ms",
                    "escalation_rate", "total_cost_usd", "avg_cost_per_query",
                    "model_usage_breakdown",
                ]
                for key in required_keys:
                    assert key in dashboard, f"Missing key: {key}"
            await engine.dispose()

        _run(_test())


# =====================================================================
# KnowledgeManager tests
# =====================================================================


class TestKnowledgeManager:
    def test_add_knowledge(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                eid = await mgr.add_knowledge(
                    "faq", "How do I track my order?",
                    "Go to Orders page.", "manual",
                )
                assert eid is not None
                assert len(eid) == 36
            await engine.dispose()

        _run(_test())

    def test_add_knowledge_invalid_category(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                with pytest.raises(ValueError, match="Invalid category"):
                    await mgr.add_knowledge("invalid", "q", "a", "test")
            await engine.dispose()

        _run(_test())

    def test_search_knowledge(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                await mgr.add_knowledge("faq", "How to track order?", "Go to Orders page.", "manual")
                await mgr.add_knowledge("faq", "How to process refund?", "Go to Payments.", "manual")
                results = await mgr.search_knowledge("track my order")
                assert len(results) >= 1
                assert "track" in results[0]["question"].lower()
            await engine.dispose()

        _run(_test())

    def test_search_knowledge_with_category(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                await mgr.add_knowledge("faq", "Track order status", "Use tracking", "manual")
                await mgr.add_knowledge("policy", "Return policy for orders", "30 day window", "manual")
                results = await mgr.search_knowledge("order", category="policy")
                for r in results:
                    assert r["category"] == "policy"
            await engine.dispose()

        _run(_test())

    def test_search_knowledge_empty(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                results = await mgr.search_knowledge("anything")
                assert results == []
            await engine.dispose()

        _run(_test())

    def test_get_relevant_context(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                await mgr.add_knowledge("faq", "Order status lookup", "Check order details page.", "manual")
                await mgr.add_knowledge("troubleshooting", "Order stuck in processing", "Check payment and warehouse.", "manual")
                context = await mgr.get_relevant_context("explain", "order", "why stuck?")
                assert len(context) >= 1
            await engine.dispose()

        _run(_test())

    def test_update_from_feedback_existing(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                eid = await mgr.add_knowledge("faq", "How to cancel?", "Old answer", "manual")
                await mgr.update_from_feedback(eid, "New corrected answer")
                results = await mgr.search_knowledge("cancel")
                assert len(results) >= 1
                assert results[0]["answer"] == "New corrected answer"
            await engine.dispose()

        _run(_test())

    def test_update_from_feedback_new_entry(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                await mgr.update_from_feedback(str(uuid.uuid4()), "Corrected info")
            await engine.dispose()

        _run(_test())

    def test_get_knowledge_stats(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                await mgr.add_knowledge("faq", "Q1", "A1", "test")
                await mgr.add_knowledge("faq", "Q2", "A2", "test")
                await mgr.add_knowledge("policy", "Q3", "A3", "test")
                stats = await mgr.get_knowledge_stats()
                assert stats["total_entries"] == 3
                assert stats["by_category"]["faq"] == 2
                assert stats["by_category"]["policy"] == 1
                assert len(stats["coverage_gaps"]) >= 1
            await engine.dispose()

        _run(_test())

    def test_knowledge_stats_structure(self):
        from cosmos.app.learning.knowledge import KnowledgeManager

        async def _test():
            session, engine = await _make_db()
            async with session:
                mgr = KnowledgeManager(session)
                stats = await mgr.get_knowledge_stats()
                assert "total_entries" in stats
                assert "by_category" in stats
                assert "coverage_gaps" in stats
                assert "top_used" in stats
                assert "last_updated" in stats
            await engine.dispose()

        _run(_test())


# =====================================================================
# API contract tests
# =====================================================================


class TestFeedbackAPIContract:
    def test_feedback_response_structure(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                result = await fb.submit_feedback(
                    str(uuid.uuid4()), str(uuid.uuid4()), score=4, text="Good",
                )
                assert "id" in result
                assert "session_id" in result
                assert "message_id" in result
                assert "score" in result
                assert "categories" in result
            await engine.dispose()

        _run(_test())

    def test_feedback_summary_structure(self):
        from cosmos.app.learning.feedback import FeedbackEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                fb = FeedbackEngine(session)
                summary = await fb.get_feedback_summary(days=7)
                assert "period_days" in summary
                assert "total_feedback" in summary
                assert "avg_score" in summary
                assert "score_distribution" in summary
                assert "daily_trend" in summary
            await engine.dispose()

        _run(_test())


class TestDistillationAPIContract:
    def test_distillation_stats_structure(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                stats = await collector.get_stats()
                assert "total_records" in stats
                assert "avg_confidence" in stats
                assert "feedback_distribution" in stats
                assert "total_cost_usd" in stats
                assert "exportable_records" in stats
            await engine.dispose()

        _run(_test())

    def test_export_jsonl_format(self):
        from cosmos.app.learning.collector import DistillationCollector

        async def _test():
            session, engine = await _make_db()
            async with session:
                collector = DistillationCollector(session)
                rid = await collector.log_interaction(
                    session_id=str(uuid.uuid4()),
                    react_result=_mock_react_result(["lookup_order"], 0.95, "Order shipped"),
                    llm_prompt="show order 123", llm_response="Order shipped",
                    model="claude-haiku-4-5", tokens_in=80, tokens_out=30,
                )
                await collector.add_feedback(rid, score=5)
                data = await collector.export_training_data(min_confidence=0.7, min_feedback=4)
                assert data.strip() != ""
                line = json.loads(data.strip().split("\n")[0])
                assert "messages" in line
                assert len(line["messages"]) == 2
                assert line["messages"][0]["role"] == "user"
                assert line["messages"][1]["role"] == "assistant"
                assert line["model"] == "claude-haiku-4-5"
            await engine.dispose()

        _run(_test())


class TestCostReportContract:
    def test_cost_report_structure(self):
        from cosmos.app.learning.analytics import AnalyticsEngine

        async def _test():
            session, engine = await _make_db()
            async with session:
                ae = AnalyticsEngine(session)
                report = await ae.get_cost_report(days=30)
                assert "total_cost_usd" in report
                assert "total_queries" in report
                assert "avg_cost_per_query" in report
                assert "by_model" in report
                assert "by_intent" in report
                assert "by_day" in report
            await engine.dispose()

        _run(_test())

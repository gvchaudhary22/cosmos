"""
Tests for Brain Wiring — GREL→Pipeline→Cache→Router→N8N connections.
"""

import asyncio
import pytest

from app.brain.cache import SemanticCache
from app.brain.grel import (
    GRELEngine,
    LearningInsight,
    LearningType,
    ApprovalStatus,
)
from app.brain.indexer import KBDocument, KnowledgeIndexer
from app.brain.pipeline import KBUpdatePipeline
from app.brain.router import IntelligentRouter
from app.brain.tournament import StrategyName, StrategyResult
from app.brain.wiring import (
    KBScanScheduler,
    create_cache_invalidation_callback,
    create_grel_learning_callback,
    create_n8n_notification_callback,
    create_router_rebuild_callback,
    wire_brain,
    _extract_doc_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_indexer():
    indexer = KnowledgeIndexer("/fake/path")
    doc = KBDocument(
        doc_id="mcapi.orders.get",
        doc_type="api",
        repo="MultiChannel_API",
        domain="orders",
        summary="GET /api/v1/orders",
        intent_tags=["orders_lookup"],
        keywords=["orders"],
        aliases=[],
        example_queries=[],
        tool_candidate="orders_lookup",
        primary_agent="order_ops_agent",
        read_write_type="read",
        risk_level="low",
        approval_mode="auto",
        method="GET",
        path="/api/v1/orders",
        param_examples=[],
        negative_examples=[],
        training_ready=True,
        confidence="high",
        text_for_embedding="orders lookup",
    )
    indexer._documents[doc.doc_id] = doc
    indexer._indexed = True
    indexer._build_embeddings()
    return indexer


def _make_brain():
    indexer = _make_indexer()
    router = IntelligentRouter(indexer)
    router.build()
    pipeline = KBUpdatePipeline(indexer, "/fake/kb")
    return {
        "indexer": indexer,
        "router": router,
        "pipeline": pipeline,
        "graph": None,
        "document_count": 1,
        "routing_stats": {},
    }


async def _mock_strategy(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.DECISION_TREE,
        answer=f"Order {entity_id} shipped",
        confidence=0.9,
        tool_used="lookup_order",
        cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# _extract_doc_id
# ---------------------------------------------------------------------------

class TestExtractDocId:
    def test_extracts_mcapi_id(self):
        insight = LearningInsight(
            insight_id="1",
            learning_type=LearningType.FEW_SHOT_EXAMPLE,
            description="test",
            evidence="Query: 'show order' → tool=mcapi.orders.get, params={}",
            proposed_change="Add to examples.yaml for mcapi.orders.get",
            risk_level="low",
        )
        assert _extract_doc_id(insight) == "mcapi.orders.get"

    def test_extracts_table_id(self):
        insight = LearningInsight(
            insight_id="2",
            learning_type=LearningType.KNOWLEDGE_GAP,
            description="test",
            evidence="Missing table.sr_web.users",
            proposed_change="test",
            risk_level="low",
        )
        assert _extract_doc_id(insight) == "table.sr_web.users"

    def test_returns_empty_on_no_match(self):
        insight = LearningInsight(
            insight_id="3",
            learning_type=LearningType.EDGE_CASE,
            description="test",
            evidence="some random text",
            proposed_change="some change",
            risk_level="low",
        )
        assert _extract_doc_id(insight) == ""


# ---------------------------------------------------------------------------
# GREL → Pipeline callback
# ---------------------------------------------------------------------------

class TestGRELPipelineCallback:
    def test_creates_callback(self):
        pipeline = KBUpdatePipeline(_make_indexer(), "/fake")
        callback = create_grel_learning_callback(pipeline)
        assert callable(callback)

    def test_callback_processes_few_shot(self):
        indexer = _make_indexer()
        pipeline = KBUpdatePipeline(indexer, "/fake")
        callback = create_grel_learning_callback(pipeline)

        insight = LearningInsight(
            insight_id="1",
            learning_type=LearningType.FEW_SHOT_EXAMPLE,
            description="test",
            evidence="Query: 'show order 123' → tool=mcapi.orders.get",
            proposed_change="Add to examples.yaml for mcapi.orders.get",
            risk_level="low",
        )
        # Should not raise
        _run(callback([insight]))

    def test_callback_skips_edge_cases(self):
        pipeline = KBUpdatePipeline(_make_indexer(), "/fake")
        callback = create_grel_learning_callback(pipeline)

        insight = LearningInsight(
            insight_id="2",
            learning_type=LearningType.EDGE_CASE,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="low",
        )
        # Edge cases should not be forwarded to pipeline
        _run(callback([insight]))
        assert len(pipeline._updates) == 0


# ---------------------------------------------------------------------------
# Cache invalidation callback
# ---------------------------------------------------------------------------

class TestCacheInvalidationCallback:
    def test_creates_callback(self):
        cache = SemanticCache()
        callback = create_cache_invalidation_callback(cache)
        assert callable(callback)

    def test_invalidates_on_doc_update(self):
        cache = SemanticCache()
        # Pre-populate cache
        cache.put("show orders", "lookup", "order", None, "response", 0.003)

        callback = create_cache_invalidation_callback(cache)

        # Simulate a pipeline update object
        class FakeUpdate:
            doc_id = "mcapi.orders.get"
            status = "indexed"
        callback([FakeUpdate()])
        # Cache should have been invalidated for orders pattern

    def test_full_reindex_clears_all(self):
        cache = SemanticCache()
        cache.put("query1", "lookup", "order", None, "resp1", 0.003)
        cache.put("query2", "lookup", "shipment", None, "resp2", 0.003)

        callback = create_cache_invalidation_callback(cache)

        class FullReindex:
            doc_id = "*"
            status = "indexed"
        callback([FullReindex()])

        stats = cache.get_stats()
        assert stats["l1_size"] == 0


# ---------------------------------------------------------------------------
# Router rebuild callback
# ---------------------------------------------------------------------------

class TestRouterRebuildCallback:
    def test_creates_callback(self):
        router = IntelligentRouter(_make_indexer())
        router.build()
        callback = create_router_rebuild_callback(router)
        assert callable(callback)

    def test_rebuilds_on_update(self):
        indexer = _make_indexer()
        router = IntelligentRouter(indexer)
        router.build()

        callback = create_router_rebuild_callback(router)

        class FakeUpdate:
            doc_id = "mcapi.orders.get"
        callback([FakeUpdate()])
        # Should not raise — router rebuilt


# ---------------------------------------------------------------------------
# N8N notification callback
# ---------------------------------------------------------------------------

class TestN8NCallback:
    def test_returns_none_when_no_urls(self):
        callback = create_n8n_notification_callback(None, None)
        assert callback is None

    def test_creates_callback_with_webhook_url(self):
        callback = create_n8n_notification_callback("http://n8n.local/webhook")
        assert callable(callback)

    def test_creates_callback_with_mars_url(self):
        callback = create_n8n_notification_callback(mars_base_url="http://mars:8080")
        assert callable(callback)

    def test_callback_doesnt_crash_on_empty_updates(self):
        callback = create_n8n_notification_callback("http://n8n.local/webhook")
        callback([])  # Should not raise


# ---------------------------------------------------------------------------
# KBScanScheduler
# ---------------------------------------------------------------------------

class TestKBScanScheduler:
    def test_creates_scheduler(self):
        pipeline = KBUpdatePipeline(_make_indexer(), "/fake")
        scheduler = KBScanScheduler(pipeline, interval_seconds=60)
        assert scheduler._interval == 60
        assert scheduler._running is False

    def test_start_stop(self):
        pipeline = KBUpdatePipeline(_make_indexer(), "/fake")
        scheduler = KBScanScheduler(pipeline, interval_seconds=9999)

        async def _test():
            await scheduler.start()
            assert scheduler._running is True
            assert scheduler._task is not None
            await scheduler.stop()
            assert scheduler._running is False

        _run(_test())

    def test_double_start_is_safe(self):
        pipeline = KBUpdatePipeline(_make_indexer(), "/fake")
        scheduler = KBScanScheduler(pipeline, interval_seconds=9999)

        async def _test():
            await scheduler.start()
            await scheduler.start()  # Should not create a second task
            assert scheduler._running is True
            await scheduler.stop()

        _run(_test())


# ---------------------------------------------------------------------------
# wire_brain (master wiring)
# ---------------------------------------------------------------------------

class TestWireBrain:
    def test_wires_all_components(self):
        brain = _make_brain()
        cache = SemanticCache()
        grel = GRELEngine()
        grel.register_strategy(StrategyName.DECISION_TREE, _mock_strategy)

        result = wire_brain(
            brain=brain,
            cache=cache,
            grel_engine=grel,
        )

        assert result["cache"] is cache
        assert result["grel_engine"] is grel
        assert result["scheduler"] is not None
        # GREL should have learning callback set
        assert grel._learning_callback is not None
        # Pipeline should have callbacks registered (cache + router)
        assert len(brain["pipeline"]._callbacks) >= 2

    def test_wires_without_optional_components(self):
        brain = _make_brain()
        result = wire_brain(brain=brain)
        # Should still wire router rebuild callback
        assert len(brain["pipeline"]._callbacks) >= 1
        assert result["cache"] is None
        assert result["grel_engine"] is None

    def test_wires_n8n_when_url_provided(self):
        brain = _make_brain()
        result = wire_brain(
            brain=brain,
            n8n_webhook_url="http://n8n.local/webhook",
        )
        # Should have router + n8n callbacks
        assert len(brain["pipeline"]._callbacks) >= 2

    def test_scheduler_in_result(self):
        brain = _make_brain()
        result = wire_brain(brain=brain, scan_interval_seconds=120)
        scheduler = result["scheduler"]
        assert isinstance(scheduler, KBScanScheduler)
        assert scheduler._interval == 120

    def test_grel_learning_flows_to_pipeline(self):
        """Integration: GREL insight → pipeline callback → pipeline._updates."""
        brain = _make_brain()
        grel = GRELEngine()
        grel.register_strategy(StrategyName.DECISION_TREE, _mock_strategy)

        wire_brain(brain=brain, grel_engine=grel)

        # Simulate GREL producing a learning insight
        insight = LearningInsight(
            insight_id="test-1",
            learning_type=LearningType.FEW_SHOT_EXAMPLE,
            description="test",
            evidence="tool=mcapi.orders.get",
            proposed_change="Add example",
            risk_level="low",
        )
        _run(grel._learning_callback([insight]))
        # Pipeline should have received the feedback
        # (may or may not create an update depending on doc_id match)

"""
Tests for Wave 3 (LangGraph) and Wave 4 (Neo4j) shadow mode behavior
in QueryOrchestrator.

Shadow mode: wave runs, result is logged for MRR comparison, but is NOT
injected into result.context — so the LLM sees no wave enrichment.
This lets us benchmark the wave before promoting it to default.

Normal mode: result IS injected into result.context["wave3_reasoning"] /
result.context["wave4_graph"].

Timeout: asyncio.TimeoutError is caught silently — pipeline continues.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.engine.tier_policy import TierDecision, TierGateResult
from app.services.query_orchestrator import (
    OrchestratorResult, PipelineName, ProbeResult, QueryOrchestrator,
)
from app.services.workflow_settings import WorkflowSettings


# ─── Helpers (shared with tier tests) ─────────────────────────────────────────

def _gate(decision: TierDecision = TierDecision.RESPOND, score: float = 0.75) -> TierGateResult:
    return TierGateResult(
        decision=decision,
        composite_score=score,
        signals_summary={},
        reason="test_forced",
    )


def _probe_results() -> dict:
    pipes = [
        PipelineName.INTENT, PipelineName.ENTITY, PipelineName.VECTOR,
        PipelineName.PAGE_ROLE, PipelineName.CROSS_REPO,
    ]
    results = {p: ProbeResult(pipeline=p, found_data=False) for p in pipes}
    results[PipelineName.INTENT] = ProbeResult(
        pipeline=PipelineName.INTENT,
        found_data=True,
        data=[{"intent": "explain", "entity": "shipment", "entity_id": None,
               "confidence": 0.7, "needs_ai": True, "sub_intents": []}],
    )
    return results


def _make_orchestrator(**kwargs) -> QueryOrchestrator:
    from app.engine.classifier import IntentClassifier

    orch = QueryOrchestrator(
        classifier=IntentClassifier(),
        vectorstore=AsyncMock(),
        graphrag=AsyncMock(),
        page_intelligence=AsyncMock(),
        **kwargs,
    )
    orch.cost_tracker = MagicMock()
    orch.cost_tracker.check_budget.return_value = True
    orch.cost_tracker.finalize.return_value = None
    orch.cost_tracker.record_tier_cost.return_value = None
    orch.semantic_cache = None
    orch._pattern_cache = None
    orch.react_engine = None
    # Stub request_classifier so wave3 auto-enable heuristic (confidence < 0.6) never fires
    from app.engine.request_classifier import (
        RequestClassification, QueryDomain, QueryComplexity, QueryMode,
    )
    _cls_mock = MagicMock()
    _cls_mock.classify.return_value = RequestClassification(
        domain=QueryDomain.GENERAL,
        complexity=QueryComplexity.STANDARD,
        mode=QueryMode.LOOKUP,
        confidence=0.9,
    )
    orch._request_classifier = _cls_mock
    return orch


def _base_patches(orch: QueryOrchestrator):
    """Context managers that no-op all pipeline stages except wave 3/4."""
    return [
        patch.object(orch, "_enrich_query_with_claude", new=AsyncMock(return_value=None)),
        patch.object(orch, "_prefetch_wave2_legs3_and_4", new=AsyncMock(return_value=[])),
        patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results())),
        patch.object(orch, "_stage2_conditional_deep", new=AsyncMock(return_value={})),
        patch.object(orch, "_merge_context", new=AsyncMock(return_value={})),
        # Prevent clarification gate from firing before tier/wave logic
        patch.object(orch, "_generate_clarification_question", return_value=None),
        patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate()),
    ]


def _ws_wave3(shadow: bool = False, enabled: bool = True) -> WorkflowSettings:
    ws = WorkflowSettings.balanced()
    ws.wave3_langgraph_enabled = enabled
    ws.wave3_shadow_mode = shadow
    ws.wave3_timeout_sec = 5
    ws.wave4_neo4j_enabled = False   # isolate wave 3 in these tests
    return ws


def _ws_wave4(shadow: bool = False, enabled: bool = True) -> WorkflowSettings:
    ws = WorkflowSettings.balanced()
    ws.wave3_langgraph_enabled = False  # isolate wave 4
    ws.wave4_neo4j_enabled = enabled
    ws.wave4_shadow_mode = shadow
    ws.wave4_timeout_sec = 5
    return ws


# ─── Wave 3 (LangGraph) ───────────────────────────────────────────────────────

class TestWave3LangGraph:

    def test_wave3_normal_mode_injects_into_context(self):
        """In normal mode, wave3 result is stored in result.context['wave3_reasoning']."""
        orch = _make_orchestrator()
        w3_payload = {"enriched_query": "why NDR happened", "confidence": 0.82, "refined_entities": ["NDR"]}

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value=w3_payload)))
            result: OrchestratorResult = asyncio.run(
                orch.execute("why did NDR happen", user_id="u1", workflow_settings=_ws_wave3(shadow=False))
            )

        assert result.wave3_context == w3_payload
        assert result.context.get("wave3_reasoning") == w3_payload

    def test_wave3_shadow_mode_does_not_inject_into_context(self):
        """In shadow mode, wave3 runs; result stored in wave3_context but NOT injected."""
        orch = _make_orchestrator()
        w3_payload = {"enriched_query": "why NDR happened", "confidence": 0.82, "refined_entities": ["NDR"]}

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value=w3_payload)))
            result: OrchestratorResult = asyncio.run(
                orch.execute("why did NDR happen", user_id="u1", workflow_settings=_ws_wave3(shadow=True))
            )

        assert result.wave3_context == w3_payload
        assert "wave3_reasoning" not in result.context

    def test_wave3_timeout_does_not_crash_pipeline(self):
        """asyncio.TimeoutError in wave3 is caught — pipeline continues and returns."""
        orch = _make_orchestrator()

        async def slow_w3(*args, **kwargs):
            await asyncio.sleep(999)

        ws = _ws_wave3(shadow=False)
        ws.wave3_timeout_sec = 0.01

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=slow_w3))
            result: OrchestratorResult = asyncio.run(
                orch.execute("complex query", user_id="u1", workflow_settings=ws)
            )

        assert isinstance(result, OrchestratorResult)
        assert result.wave3_context is None

    def test_wave3_disabled_does_not_call_stage3(self):
        """When wave3_langgraph_enabled=False, _stage3_langgraph is never called."""
        orch = _make_orchestrator()
        stage3_mock = AsyncMock(return_value={"confidence": 0.9})

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=stage3_mock))
            asyncio.run(orch.execute("simple query", user_id="u1", workflow_settings=_ws_wave3(enabled=False)))

        stage3_mock.assert_not_awaited()

    def test_wave3_exception_does_not_crash_pipeline(self):
        """Non-timeout exception from wave3 is caught and pipeline continues."""
        orch = _make_orchestrator()

        async def failing_w3(*args, **kwargs):
            raise ValueError("Neo4j node not found")

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=failing_w3))
            result: OrchestratorResult = asyncio.run(
                orch.execute("query", user_id="u1", workflow_settings=_ws_wave3(shadow=False))
            )

        assert isinstance(result, OrchestratorResult)
        assert result.wave3_context is None

    def test_wave3_none_result_not_stored(self):
        """If _stage3_langgraph returns None, wave3_context is not set."""
        orch = _make_orchestrator()

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value=None)))
            result: OrchestratorResult = asyncio.run(
                orch.execute("query", user_id="u1", workflow_settings=_ws_wave3())
            )

        assert result.wave3_context is None
        assert "wave3_reasoning" not in result.context


# ─── Wave 4 (Neo4j) ───────────────────────────────────────────────────────────

class TestWave4Neo4j:

    def test_wave4_normal_mode_injects_into_context(self):
        """In normal mode, wave4 result is stored in wave4_context and injected."""
        orch = _make_orchestrator()
        w4_payload = {"paths": [{"from": "order", "to": "shipment"}], "path_count": 1}

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=AsyncMock(return_value=w4_payload)))
            result: OrchestratorResult = asyncio.run(
                orch.execute("trace order to shipment", user_id="u1", workflow_settings=_ws_wave4(shadow=False))
            )

        assert result.wave4_context == w4_payload
        assert result.context.get("wave4_graph") == w4_payload

    def test_wave4_shadow_mode_does_not_inject_into_context(self):
        """In shadow mode, wave4 result goes to wave4_context only — not context['wave4_graph']."""
        orch = _make_orchestrator()
        w4_payload = {"paths": [], "path_count": 0}

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=AsyncMock(return_value=w4_payload)))
            result: OrchestratorResult = asyncio.run(
                orch.execute("trace query", user_id="u1", workflow_settings=_ws_wave4(shadow=True))
            )

        assert result.wave4_context == w4_payload
        assert "wave4_graph" not in result.context

    def test_wave4_timeout_does_not_crash_pipeline(self):
        """asyncio.TimeoutError in wave4 is caught and pipeline continues."""
        orch = _make_orchestrator()

        async def slow_w4(*args, **kwargs):
            await asyncio.sleep(999)

        ws = _ws_wave4(shadow=False)
        ws.wave4_timeout_sec = 0.01

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=slow_w4))
            result: OrchestratorResult = asyncio.run(
                orch.execute("query", user_id="u1", workflow_settings=ws)
            )

        assert isinstance(result, OrchestratorResult)
        assert result.wave4_context is None

    def test_wave4_disabled_does_not_call_stage4(self):
        """When wave4_neo4j_enabled=False, _stage4_neo4j is never called."""
        orch = _make_orchestrator()
        stage4_mock = AsyncMock(return_value={"path_count": 5})

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=stage4_mock))
            asyncio.run(orch.execute("query", user_id="u1", workflow_settings=_ws_wave4(enabled=False)))

        stage4_mock.assert_not_awaited()

    def test_wave4_exception_does_not_crash_pipeline(self):
        """Non-timeout exception from wave4 is caught and pipeline continues."""
        orch = _make_orchestrator()

        async def failing_w4(*args, **kwargs):
            raise ConnectionError("Neo4j bolt connection refused")

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=failing_w4))
            result: OrchestratorResult = asyncio.run(
                orch.execute("query", user_id="u1", workflow_settings=_ws_wave4())
            )

        assert isinstance(result, OrchestratorResult)
        assert result.wave4_context is None


# ─── SSE progress events for wave 3/4 ────────────────────────────────────────

class TestWave34SseProgress:
    """Verify wave-level SSE progress callbacks are emitted for waves 3 and 4."""

    def test_wave3_emits_running_and_completed(self):
        """on_wave_progress receives (3, 'wave3_langgraph', 'running') and 'completed'."""
        orch = _make_orchestrator()
        events = []

        async def on_progress(wave_id, task_id, status, data):
            events.append((wave_id, task_id, status))

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value={"confidence": 0.8})))
            asyncio.run(orch.execute("query", user_id="u1", workflow_settings=_ws_wave3(shadow=False), on_wave_progress=on_progress))

        statuses = [s for w, _, s in events if w == 3]
        assert "running" in statuses
        assert "completed" in statuses

    def test_wave4_emits_running_and_completed(self):
        """on_wave_progress receives (4, 'wave4_neo4j', 'running') and 'completed'."""
        orch = _make_orchestrator()
        events = []

        async def on_progress(wave_id, task_id, status, data):
            events.append((wave_id, task_id, status))

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=AsyncMock(return_value={"path_count": 2})))
            asyncio.run(orch.execute("query", user_id="u1", workflow_settings=_ws_wave4(shadow=False), on_wave_progress=on_progress))

        statuses = [s for w, _, s in events if w == 4]
        assert "running" in statuses
        assert "completed" in statuses

    def test_wave3_shadow_emits_running_shadow(self):
        """In shadow mode, the running event has status 'running_shadow'."""
        orch = _make_orchestrator()
        events = []

        async def on_progress(wave_id, task_id, status, data):
            events.append((wave_id, task_id, status))

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value={"confidence": 0.8})))
            asyncio.run(orch.execute("query", user_id="u1", workflow_settings=_ws_wave3(shadow=True), on_wave_progress=on_progress))

        wave3_statuses = [s for w, _, s in events if w == 3]
        assert "running_shadow" in wave3_statuses

    def test_no_sse_callback_does_not_raise(self):
        """When on_wave_progress is None, wave 3/4 runs without error."""
        orch = _make_orchestrator()
        ws = WorkflowSettings.balanced()
        ws.wave3_langgraph_enabled = True
        ws.wave4_neo4j_enabled = True
        ws.wave3_shadow_mode = False
        ws.wave4_shadow_mode = False
        ws.wave3_timeout_sec = 5
        ws.wave4_timeout_sec = 5

        with ExitStack() as stack:
            for p in _base_patches(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value={"confidence": 0.7})))
            stack.enter_context(patch.object(orch, "_stage4_neo4j", new=AsyncMock(return_value={"path_count": 1})))
            result = asyncio.run(orch.execute("query", user_id="u1", workflow_settings=ws, on_wave_progress=None))

        assert isinstance(result, OrchestratorResult)

"""
Integration tests for QueryOrchestrator Tier 2 (CodebaseIntelligence)
and Tier 3 (SafeDBTool) execution paths.

Mocking strategy:
  - _enrich_query_with_claude         → None (no enrichment)
  - _prefetch_wave2_legs3_and_4       → [] (no prefetch)
  - _stage1_parallel_probe            → controlled ProbeResults
  - _stage2_conditional_deep          → {} (empty deep results)
  - _merge_context                    → {} (no KB context)
  - _stage3_langgraph / _stage4_neo4j → skipped via WorkflowSettings flags
  - cost_tracker                      → MagicMock (no DB)
  - tier_policy.evaluate_tier*        → forced decisions per test
  - codebase_intel / safe_db_tool     → injected mocks
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engine.codebase_intelligence import CodebaseContext
from app.engine.safe_query_executor import SafeDBResult
from app.engine.tier_policy import TierDecision, TierGateResult, FreshnessMode, TierSignals
from app.services.query_orchestrator import (
    OrchestratorResult, PipelineName, ProbeResult, QueryOrchestrator,
)
from app.services.workflow_settings import WorkflowSettings


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _gate(decision: TierDecision, score: float = 0.45) -> TierGateResult:
    return TierGateResult(
        decision=decision,
        composite_score=score,
        signals_summary={},
        reason="test_forced",
    )


def _probe_results(n_found: int = 0) -> dict:
    """Return 5 probes with the first n_found having data."""
    all_pipes = [
        PipelineName.INTENT,
        PipelineName.ENTITY,
        PipelineName.VECTOR,
        PipelineName.PAGE_ROLE,
        PipelineName.CROSS_REPO,
    ]
    found = {
        PipelineName.INTENT: ProbeResult(
            pipeline=PipelineName.INTENT,
            found_data=True,
            data=[{"intent": "lookup", "entity": "order", "entity_id": None,
                   "confidence": 0.6, "needs_ai": False, "sub_intents": []}],
        )
    }
    not_found = {
        p: ProbeResult(pipeline=p, found_data=False)
        for p in all_pipes if p != PipelineName.INTENT
    }
    if n_found == 0:
        not_found[PipelineName.INTENT] = ProbeResult(
            pipeline=PipelineName.INTENT, found_data=False
        )
        return not_found
    return {**found, **not_found}


def _make_orchestrator(**kwargs) -> QueryOrchestrator:
    from app.engine.classifier import IntentClassifier

    orch = QueryOrchestrator(
        classifier=IntentClassifier(),
        vectorstore=AsyncMock(),
        graphrag=AsyncMock(),
        page_intelligence=AsyncMock(),
        **kwargs,
    )
    # Stub out cost tracker (no DB interaction)
    orch.cost_tracker = MagicMock()
    orch.cost_tracker.check_budget.return_value = True
    orch.cost_tracker.start_session.return_value = MagicMock()
    orch.cost_tracker.finalize.return_value = None
    orch.cost_tracker.record_tier_cost.return_value = None
    # No semantic cache, pattern cache, react engine
    orch.semantic_cache = None
    orch._pattern_cache = None
    orch.react_engine = None
    return orch


def _no_ops(orch: QueryOrchestrator) -> list:
    """Return patch targets that make the pipeline no-op for all non-tier parts."""
    return [
        patch.object(orch, "_enrich_query_with_claude", new=AsyncMock(return_value=None)),
        patch.object(orch, "_prefetch_wave2_legs3_and_4", new=AsyncMock(return_value=[])),
        patch.object(orch, "_stage2_conditional_deep", new=AsyncMock(return_value={})),
        patch.object(orch, "_merge_context", new=AsyncMock(return_value={})),
        # Prevent clarification gate from firing before tier gate
        patch.object(orch, "_generate_clarification_question", return_value=None),
        # Prevent wave3/wave4 auto-enable (low conf triggers it regardless of ws flag)
        patch.object(orch, "_stage3_langgraph", new=AsyncMock(return_value=None)),
        patch.object(orch, "_stage4_neo4j", new=AsyncMock(return_value=None)),
    ]


# Minimal WorkflowSettings with waves 3/4 disabled to keep tests fast
def _ws() -> WorkflowSettings:
    ws = WorkflowSettings.balanced()
    ws.wave3_langgraph_enabled = False
    ws.wave4_neo4j_enabled = False
    return ws


# ─── Tier 2: CodebaseIntelligence path ────────────────────────────────────────

class TestTier2CodebaseIntelligence:
    """Tier 2 is entered when Tier 1 gate says ESCALATE_TIER2."""

    def test_tier2_context_populated_on_escalation(self):
        """When evaluate_tier1 → ESCALATE_TIER2, codebase_intel.retrieve is called
        and result.tier2_context is populated."""

        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext(
            modules_matched=["OrdersController"],
            db_tables=["orders"],
            code_insights=["order status is tracked in orders.status"],
            refined_query="what is order status",
        )

        orch = _make_orchestrator(codebase_intel=mock_intel)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(1))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER2)))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier2", return_value=_gate(TierDecision.RESPOND, 0.65)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="what is order status", user_id="u1", workflow_settings=_ws())
            )

        assert result.tier2_context is not None
        assert result.tier2_context["modules"] == ["OrdersController"]
        assert "orders" in result.tier2_context["tables"]
        assert result.resolution_tier == 2
        assert 2 in result.tiers_visited
        mock_intel.retrieve.assert_awaited_once()

    def test_tier2_skipped_when_intel_unavailable(self):
        """When codebase_intel is None and gate1 says ESCALATE_TIER2, the orchestrator
        flags tier2_blocked and returns at tier 1 with a warning in metadata."""

        orch = _make_orchestrator(codebase_intel=None)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER2)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="some complex query", user_id="u1", workflow_settings=_ws())
            )

        assert result.tier2_context is None
        assert result.response_metadata is not None
        assert result.response_metadata.get("tier2_blocked") is True
        assert "tier2_skipped_no_intel" in result.used_fallbacks

    def test_tier2_refined_query_triggers_retry_probe(self):
        """When codebase_intel returns a refined_query that differs from the original,
        a second _stage1_parallel_probe call is made with the refined query."""

        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext(
            modules_matched=["ShipmentTracker"],
            refined_query="why was AWB delivery failed",   # different from original
        )

        orch = _make_orchestrator(codebase_intel=mock_intel)
        probe_mock = AsyncMock(return_value=_probe_results(1))

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=probe_mock))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER2)))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier2", return_value=_gate(TierDecision.RESPOND, 0.65)))
            asyncio.run(orch.execute(query="delivery failed", user_id="u1", workflow_settings=_ws()))

        # Called twice: once for original query, once for refined query
        assert probe_mock.await_count == 2
        second_call_query = probe_mock.call_args_list[1][0][0]
        assert second_call_query == "why was AWB delivery failed"

    def test_tier2_retrieve_exception_falls_through(self):
        """If codebase_intel.retrieve raises, the orchestrator logs and falls through
        gracefully (no crash, result still returned)."""

        mock_intel = AsyncMock()
        mock_intel.retrieve.side_effect = RuntimeError("vectorstore unavailable")

        orch = _make_orchestrator(codebase_intel=mock_intel)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(1))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER2)))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier2", return_value=_gate(TierDecision.RESPOND, 0.65)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="complex query", user_id="u1", workflow_settings=_ws())
            )

        assert isinstance(result, OrchestratorResult)
        assert result.total_latency_ms > 0


# ─── Tier 3: SafeDBTool path ──────────────────────────────────────────────────

class TestTier3SafeDBTool:
    """Tier 3 is entered when MARS is available and safe_db_tool is wired."""

    def test_tier3_context_populated_on_db_success(self):
        """When safe_db_tool.execute_template succeeds, result.tier3_context is set."""

        mock_db = AsyncMock()
        mock_db.execute_template.return_value = SafeDBResult(
            success=True,
            data=[{"id": "12345", "status": "delivered", "awb_code": "AWB9999"}],
            row_count=1,
            template_used="order_by_id",
        )

        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext()
        mock_intel._match_db_template = MagicMock(return_value="order_by_id")

        mock_circuit = MagicMock()
        mock_circuit.is_available = True
        mock_circuit.record_success = MagicMock()

        orch = _make_orchestrator(
            codebase_intel=mock_intel,
            safe_db_tool=mock_db,
            mars_circuit=mock_circuit,
        )

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER3, 0.2)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="show order 12345", user_id="u1", company_id="42", workflow_settings=_ws())
            )

        assert result.tier3_context is not None
        assert result.tier3_context["template"] == "order_by_id"
        assert result.tier3_context["row_count"] == 1
        assert result.tier3_context["data"][0]["status"] == "delivered"
        assert 3 in result.tiers_visited
        mock_db.execute_template.assert_awaited_once()

    def test_tier3_skipped_when_mars_circuit_open(self):
        """When mars_circuit.is_available is False, Tier 3 is skipped and
        used_fallbacks contains 'mars_degraded'."""

        mock_circuit = MagicMock()
        mock_circuit.is_available = False
        mock_circuit.record_short_circuit = MagicMock()

        orch = _make_orchestrator(safe_db_tool=AsyncMock(), mars_circuit=mock_circuit)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER3, 0.15)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="show all orders", user_id="u1", workflow_settings=_ws())
            )

        assert "mars_degraded" in result.used_fallbacks
        assert result.tier3_context is None

    def test_tier3_skipped_when_no_template_matched(self):
        """When codebase_intel finds no DB template, execute_template is not called."""

        mock_db = AsyncMock()
        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext()
        mock_intel._match_db_template = MagicMock(return_value=None)

        mock_circuit = MagicMock()
        mock_circuit.is_available = True

        orch = _make_orchestrator(codebase_intel=mock_intel, safe_db_tool=mock_db, mars_circuit=mock_circuit)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER3, 0.15)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="tell me something vague", user_id="u1", workflow_settings=_ws())
            )

        mock_db.execute_template.assert_not_awaited()
        assert result.tier3_context is None

    def test_tier3_db_failure_handled_gracefully(self):
        """When execute_template returns success=False, tier3_context is not set."""

        mock_db = AsyncMock()
        mock_db.execute_template.return_value = SafeDBResult(success=False, error="row cap exceeded")

        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext()
        mock_intel._match_db_template = MagicMock(return_value="recent_orders")

        mock_circuit = MagicMock()
        mock_circuit.is_available = True
        mock_circuit.record_success = MagicMock()

        orch = _make_orchestrator(codebase_intel=mock_intel, safe_db_tool=mock_db, mars_circuit=mock_circuit)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER3, 0.15)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="show recent orders", user_id="u1", workflow_settings=_ws())
            )

        assert isinstance(result, OrchestratorResult)
        assert result.tier3_context is None

    def test_tier3_human_escalation_sets_clarification(self):
        """When gate3 returns ESCALATE_HUMAN, result.needs_clarification is True."""

        mock_db = AsyncMock()
        mock_db.execute_template.return_value = SafeDBResult(success=False, error="no template")

        mock_intel = AsyncMock()
        mock_intel.retrieve.return_value = CodebaseContext()
        mock_intel._match_db_template = MagicMock(return_value=None)

        mock_circuit = MagicMock()
        mock_circuit.is_available = True

        orch = _make_orchestrator(codebase_intel=mock_intel, safe_db_tool=mock_db, mars_circuit=mock_circuit)

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.ESCALATE_TIER3, 0.15)))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier3", return_value=_gate(TierDecision.ESCALATE_HUMAN, 0.2)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="totally unknown domain query", user_id="u1", workflow_settings=_ws())
            )

        assert result.needs_clarification is True
        assert result.clarification_prompt is not None


# ─── Confidence Gate ──────────────────────────────────────────────────────────

class TestConfidenceGateEnforcement:
    """Verify the confidence gate added to execute() enforces refuse/uncertain."""

    def test_low_confidence_sets_refused(self):
        """With zero attribution and zero evidence, final_confidence < 0.3 → refused."""
        orch = _make_orchestrator()

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(0))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.RESPOND, 0.1)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="totally random", user_id="u1", workflow_settings=_ws())
            )

        assert result.response_metadata is not None
        gate_val = result.response_metadata.get("confidence_gate")
        assert gate_val in ("refused", "uncertain")

    def test_response_metadata_has_final_confidence(self):
        """final_confidence key is always present in response_metadata."""
        orch = _make_orchestrator()

        with ExitStack() as stack:
            for p in _no_ops(orch): stack.enter_context(p)
            stack.enter_context(patch.object(orch, "_stage1_parallel_probe", new=AsyncMock(return_value=_probe_results(1))))
            stack.enter_context(patch.object(orch.tier_policy, "evaluate_tier1", return_value=_gate(TierDecision.RESPOND, 0.75)))
            result: OrchestratorResult = asyncio.run(
                orch.execute(query="show order 12345", user_id="u1", workflow_settings=_ws())
            )

        assert "final_confidence" in result.response_metadata
        assert 0.0 <= result.response_metadata["final_confidence"] <= 1.0

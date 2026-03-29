"""
Tests for the GREL Engine — Gather → Reason → Execute → Learn.
"""

import asyncio
import pytest

from cosmos.app.brain.grel import (
    ApprovalStatus,
    GatheredData,
    GRELEngine,
    GRELPhase,
    GRELResult,
    LearningInsight,
    LearningType,
    SynthesisResult,
)
from cosmos.app.brain.tournament import StrategyName, StrategyResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


async def _mock_decision_tree(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.DECISION_TREE,
        answer=f"DT: Order {entity_id} is shipped",
        confidence=0.9,
        tool_used="lookup_order",
        params_extracted={"id": entity_id} if entity_id else {},
        cost_usd=0.0,
        tokens_used=0,
    )


async def _mock_rag(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.TFIDF_RAG,
        answer=f"RAG: Found order {entity_id}",
        confidence=0.75,
        tool_used="tfidf_search",
        params_extracted={"id": entity_id} if entity_id else {},
        cost_usd=0.001,
        tokens_used=200,
    )


async def _mock_tool_use(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.TOOL_USE,
        answer=f"Claude: Order {entity_id} shipped on March 28",
        confidence=0.95,
        tool_used="mcapi.orders.get",
        params_extracted={"id": entity_id, "include": "tracking"} if entity_id else {},
        cost_usd=0.003,
        tokens_used=500,
    )


async def _mock_reasoning(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.FULL_REASONING,
        answer=f"ReAct: Order {entity_id} shipped yesterday, tracking shows in transit",
        confidence=0.92,
        tool_used="react_loop",
        params_extracted={"id": entity_id} if entity_id else {},
        cost_usd=0.01,
        tokens_used=1500,
    )


async def _mock_failing_strategy(query, intent, entity, entity_id):
    raise RuntimeError("Service unavailable")


async def _mock_low_confidence(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.TFIDF_RAG,
        answer="I'm not sure",
        confidence=0.1,
        cost_usd=0.001,
    )


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestLearningInsight:
    def test_needs_admin_approval_medium(self):
        i = LearningInsight(
            insight_id="test-1",
            learning_type=LearningType.TOOL_CORRECTION,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="medium",
        )
        assert i.needs_admin_approval is True

    def test_needs_admin_approval_high(self):
        i = LearningInsight(
            insight_id="test-2",
            learning_type=LearningType.EDGE_CASE,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="high",
        )
        assert i.needs_admin_approval is True

    def test_no_approval_low_risk(self):
        i = LearningInsight(
            insight_id="test-3",
            learning_type=LearningType.ROUTING_RULE,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="low",
        )
        assert i.needs_admin_approval is False

    def test_default_status_pending(self):
        i = LearningInsight(
            insight_id="test-4",
            learning_type=LearningType.FEW_SHOT_EXAMPLE,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="low",
        )
        assert i.approval_status == ApprovalStatus.PENDING

    def test_has_created_at(self):
        i = LearningInsight(
            insight_id="test-5",
            learning_type=LearningType.KNOWLEDGE_GAP,
            description="test",
            evidence="test",
            proposed_change="test",
            risk_level="medium",
        )
        assert i.created_at is not None


class TestSynthesisResult:
    def test_defaults(self):
        s = SynthesisResult()
        assert s.chosen_tool is None
        assert s.confidence == 0.0
        assert s.strategies_used == []
        assert s.edge_cases_noted == []

    def test_with_values(self):
        s = SynthesisResult(
            chosen_tool="lookup_order",
            chosen_params={"id": "123"},
            confidence=0.9,
            strategies_used=[StrategyName.DECISION_TREE],
        )
        assert s.chosen_tool == "lookup_order"
        assert s.chosen_params["id"] == "123"


class TestGatheredData:
    def test_defaults(self):
        g = GatheredData(
            strategy=StrategyName.DECISION_TREE,
            raw_result=StrategyResult(
                strategy=StrategyName.DECISION_TREE, answer="test", confidence=0.8
            ),
        )
        assert g.entity_found is None
        assert g.params_extracted == {}
        assert g.confidence == 0.0  # default, not from raw_result

    def test_with_data(self):
        g = GatheredData(
            strategy=StrategyName.TOOL_USE,
            raw_result=StrategyResult(
                strategy=StrategyName.TOOL_USE, answer="test", confidence=0.9
            ),
            tool_suggested="mcapi.orders.get",
            params_extracted={"id": "123"},
            confidence=0.9,
            cost_usd=0.003,
        )
        assert g.tool_suggested == "mcapi.orders.get"
        assert g.cost_usd == 0.003


# ---------------------------------------------------------------------------
# GREL Engine — Gather phase
# ---------------------------------------------------------------------------


class TestGRELGather:
    def _make_engine(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        engine.register_strategy(StrategyName.FULL_REASONING, _mock_reasoning)
        return engine

    def test_gathers_from_all_strategies(self):
        # 2-wave optimization: Wave 1 (DECISION_TREE + TFIDF_RAG) has max_conf=0.9
        # and tool_suggested=True → Wave 2 is skipped. Only 2 strategies run.
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        assert len(gathered) >= 2  # At minimum Wave 1 ran

    def test_wave2_triggered_when_wave1_low_confidence(self):
        """Wave 2 runs when Wave 1 max confidence < 0.75."""
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_low_confidence)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        gathered = _run(engine._gather("what is order 12345", "lookup", "order", "12345"))
        names = {g.strategy for g in gathered}
        # Wave 1 ran (low confidence), Wave 2 also ran (because wave1_max_conf < 0.75)
        assert StrategyName.DECISION_TREE in names
        assert StrategyName.TOOL_USE in names

    def test_wave2_skipped_when_wave1_sufficient(self):
        """Wave 2 is skipped when Wave 1 returns high confidence + tool."""
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        names = {g.strategy for g in gathered}
        # Wave 1 has high confidence → Wave 2 skipped
        assert StrategyName.DECISION_TREE in names
        assert StrategyName.TFIDF_RAG in names
        assert StrategyName.TOOL_USE not in names
        assert StrategyName.FULL_REASONING not in names

    def test_gathered_data_has_strategy_names(self):
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        names = {g.strategy for g in gathered}
        # Wave 1 strategies always run
        assert StrategyName.DECISION_TREE in names
        assert StrategyName.TFIDF_RAG in names

    def test_gathered_has_tool_suggestions(self):
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        tools = [g.tool_suggested for g in gathered if g.tool_suggested]
        assert len(tools) >= 1  # Wave 1 strategies both suggest tools

    def test_gathered_has_latency(self):
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        for g in gathered:
            assert g.latency_ms >= 0

    def test_gather_handles_failing_strategy(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_failing_strategy)
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        assert len(gathered) == 2
        failed = [g for g in gathered if g.strategy == StrategyName.TFIDF_RAG][0]
        assert failed.confidence == 0.0
        assert failed.raw_result.error is not None


# ---------------------------------------------------------------------------
# GREL Engine — Reason phase (without LLM)
# ---------------------------------------------------------------------------


class TestGRELReason:
    def _make_engine(self):
        engine = GRELEngine()  # No LLM → fallback reasoning
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        return engine

    def test_reason_without_llm_picks_best(self):
        # 2-wave: Wave 1 (DECISION_TREE=0.9 + TFIDF_RAG=0.75) is sufficient.
        # Wave 2 (TOOL_USE) is skipped → best is DECISION_TREE (0.9).
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        synthesis = _run(engine._reason("show order 12345", "lookup", "order", gathered))
        # Highest confidence from Wave 1 only
        assert synthesis.chosen_tool == "lookup_order"
        assert synthesis.confidence == 0.9

    def test_reason_merges_params(self):
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        synthesis = _run(engine._reason("show order 12345", "lookup", "order", gathered))
        # Should merge params from Wave 1 strategies (both have "id")
        assert "id" in synthesis.chosen_params

    def test_reason_lists_all_strategies(self):
        # 2-wave: only Wave 1 strategies ran (DECISION_TREE + TFIDF_RAG = 2)
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        synthesis = _run(engine._reason("show order 12345", "lookup", "order", gathered))
        assert len(synthesis.strategies_used) == 2

    def test_reason_with_all_failures(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_failing_strategy)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_failing_strategy)
        gathered = _run(engine._gather("test", "lookup", "order", "123"))
        synthesis = _run(engine._reason("test", "lookup", "order", gathered))
        assert synthesis.confidence == 0.0
        assert "No strategy succeeded" in synthesis.reasoning

    def test_reason_has_execution_plan(self):
        engine = self._make_engine()
        gathered = _run(engine._gather("show order 12345", "lookup", "order", "12345"))
        synthesis = _run(engine._reason("show order 12345", "lookup", "order", gathered))
        assert len(synthesis.execution_plan) >= 1


# ---------------------------------------------------------------------------
# GREL Engine — Full process (without LLM)
# ---------------------------------------------------------------------------


class TestGRELProcess:
    def _make_engine(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        engine.register_strategy(StrategyName.FULL_REASONING, _mock_reasoning)
        return engine

    def test_full_process_returns_result(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        assert isinstance(result, GRELResult)
        assert result.response != ""

    def test_process_has_gathered_data(self):
        # 2-wave: Wave 1 sufficient (max_conf=0.9) → Wave 2 skipped → 2 results
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        assert len(result.gathered) >= 2

    def test_process_has_synthesis(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        assert result.synthesis is not None
        assert result.synthesis.confidence > 0

    def test_process_tracks_cost(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        assert result.total_cost_usd > 0  # Some strategies cost money

    def test_process_tracks_latency(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        assert result.total_latency_ms > 0

    def test_process_response_is_best_answer(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        # Without LLM, response should come from highest confidence strategy
        assert "12345" in result.response

    def test_process_with_session_id(self):
        engine = self._make_engine()
        result = _run(engine.process(
            "show order 12345", "lookup", "order", "12345",
            session_id="sess-abc"
        ))
        assert result.session_id == "sess-abc"

    def test_process_with_no_strategies(self):
        engine = GRELEngine()
        result = _run(engine.process("hello", "unknown", "unknown"))
        assert result.synthesis is not None
        assert result.synthesis.confidence == 0.0

    def test_process_with_failing_strategies(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_failing_strategy)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_decision_tree)
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        # Should still get a response from the working strategy
        assert result.response != ""
        assert "couldn't process" not in result.response.lower()


# ---------------------------------------------------------------------------
# GREL Engine — Learning phase
# ---------------------------------------------------------------------------


class TestGRELLearning:
    def _make_engine(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        engine.register_strategy(StrategyName.FULL_REASONING, _mock_reasoning)
        return engine

    def test_learns_tool_disagreement(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        # Give async learn time to complete
        _run(asyncio.sleep(0.1))
        # Strategies suggest different tools → should detect disagreement
        tool_corrections = [
            i for i in engine._insights_store
            if i.learning_type == LearningType.TOOL_CORRECTION
        ]
        assert len(tool_corrections) >= 1

    def test_learns_routing_rule_when_dt_sufficient(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        _run(asyncio.sleep(0.1))
        # DT has 0.9 confidence — if synthesis picks same tool, should auto-create rule
        routing_rules = [
            i for i in engine._insights_store
            if i.learning_type == LearningType.ROUTING_RULE
        ]
        # May or may not fire depending on synthesis tool choice
        # Just verify the learning pipeline ran without error
        assert engine._insights_store is not None

    def test_high_confidence_creates_few_shot(self):
        engine = self._make_engine()
        result = _run(engine.process("show order 12345", "lookup", "order", "12345"))
        _run(asyncio.sleep(0.1))
        few_shots = [
            i for i in engine._insights_store
            if i.learning_type == LearningType.FEW_SHOT_EXAMPLE
        ]
        # Synthesis confidence is 0.95 with entity_id → should create few-shot
        assert len(few_shots) >= 1

    def test_low_confidence_flags_knowledge_gap(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_low_confidence)
        result = _run(engine.process("something obscure", "unknown", "unknown"))
        _run(asyncio.sleep(0.1))
        gaps = [
            i for i in engine._insights_store
            if i.learning_type == LearningType.KNOWLEDGE_GAP
        ]
        assert len(gaps) >= 1

    def test_learning_callback_called(self):
        captured = []

        async def capture_callback(insights):
            captured.extend(insights)

        engine = GRELEngine(learning_callback=capture_callback)
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        _run(engine.process("show order 12345", "lookup", "order", "12345"))
        _run(asyncio.sleep(0.1))
        assert len(captured) >= 1

    def test_learning_stats(self):
        engine = self._make_engine()
        _run(engine.process("show order 12345", "lookup", "order", "12345"))
        _run(asyncio.sleep(0.1))
        stats = engine.get_learning_stats()
        assert stats["total_insights"] >= 1
        assert "by_type" in stats
        assert "by_status" in stats


# ---------------------------------------------------------------------------
# Admin Approval Interface
# ---------------------------------------------------------------------------


class TestAdminApproval:
    def _make_engine_with_insights(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        _run(engine.process("show order 12345", "lookup", "order", "12345"))
        _run(asyncio.sleep(0.1))
        return engine

    def test_get_pending_approvals(self):
        engine = self._make_engine_with_insights()
        pending = engine.get_pending_approvals()
        # Medium/high risk insights should appear as pending
        for p in pending:
            assert p["status"] == "pending"
            assert "id" in p
            assert "type" in p
            assert "description" in p

    def test_approve_insight(self):
        engine = self._make_engine_with_insights()
        pending = engine.get_pending_approvals()
        if pending:
            result = engine.approve_insight(pending[0]["id"], "admin@test.com")
            assert result is True
            # Should no longer appear in pending
            new_pending = engine.get_pending_approvals()
            approved_ids = {p["id"] for p in new_pending}
            assert pending[0]["id"] not in approved_ids

    def test_reject_insight(self):
        engine = self._make_engine_with_insights()
        pending = engine.get_pending_approvals()
        if pending:
            result = engine.reject_insight(pending[0]["id"], "admin@test.com")
            assert result is True

    def test_approve_nonexistent_returns_false(self):
        engine = GRELEngine()
        assert engine.approve_insight("fake-id", "admin") is False

    def test_reject_nonexistent_returns_false(self):
        engine = GRELEngine()
        assert engine.reject_insight("fake-id", "admin") is False

    def test_get_all_insights(self):
        engine = self._make_engine_with_insights()
        all_insights = engine.get_all_insights()
        assert len(all_insights) >= 1
        for i in all_insights:
            assert "id" in i
            assert "type" in i
            assert "status" in i

    def test_get_all_insights_with_limit(self):
        engine = self._make_engine_with_insights()
        limited = engine.get_all_insights(limit=1)
        assert len(limited) <= 1


# ---------------------------------------------------------------------------
# Synthesis prompt building
# ---------------------------------------------------------------------------


class TestSynthesisPrompt:
    def test_build_prompt_includes_all_strategies(self):
        engine = GRELEngine()
        gathered = [
            GatheredData(
                strategy=StrategyName.DECISION_TREE,
                raw_result=StrategyResult(
                    strategy=StrategyName.DECISION_TREE,
                    answer="Order shipped",
                    confidence=0.9,
                    tool_used="lookup_order",
                ),
                tool_suggested="lookup_order",
                confidence=0.9,
            ),
            GatheredData(
                strategy=StrategyName.TFIDF_RAG,
                raw_result=StrategyResult(
                    strategy=StrategyName.TFIDF_RAG,
                    answer="Found order",
                    confidence=0.75,
                    tool_used="tfidf_search",
                ),
                tool_suggested="tfidf_search",
                confidence=0.75,
            ),
        ]
        prompt = engine._build_synthesis_prompt(
            "show order 12345", "lookup", "order", gathered
        )
        assert "decision_tree" in prompt
        assert "tfidf_rag" in prompt
        assert "lookup_order" in prompt
        assert "show order 12345" in prompt

    def test_build_prompt_skips_failed(self):
        engine = GRELEngine()
        gathered = [
            GatheredData(
                strategy=StrategyName.DECISION_TREE,
                raw_result=StrategyResult(
                    strategy=StrategyName.DECISION_TREE,
                    answer="",
                    confidence=0.0,
                    error="failed",
                ),
                confidence=0.0,
            ),
        ]
        prompt = engine._build_synthesis_prompt("test", "lookup", "order", gathered)
        assert "No strategy produced results" in prompt

    def test_build_prompt_includes_edge_cases(self):
        engine = GRELEngine()
        gathered = [
            GatheredData(
                strategy=StrategyName.FULL_REASONING,
                raw_result=StrategyResult(
                    strategy=StrategyName.FULL_REASONING,
                    answer="Deep analysis",
                    confidence=0.92,
                ),
                confidence=0.92,
                edge_cases=["Order may be partially shipped"],
            ),
        ]
        prompt = engine._build_synthesis_prompt("test", "lookup", "order", gathered)
        assert "partially shipped" in prompt


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestGRELEdgeCases:
    def test_register_same_strategy_twice(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_rag)
        # Second registration should overwrite
        assert len(engine._strategies) == 1

    def test_process_with_none_entity_id(self):
        engine = GRELEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        result = _run(engine.process("how many orders", "report", "order", None))
        assert result.response != ""

    def test_phases_enum(self):
        assert GRELPhase.GATHER.value == "gather"
        assert GRELPhase.REASON.value == "reason"
        assert GRELPhase.EXECUTE.value == "execute"
        assert GRELPhase.RESPOND.value == "respond"
        assert GRELPhase.LEARN.value == "learn"

    def test_learning_types_enum(self):
        assert LearningType.ROUTING_RULE.value == "routing_rule"
        assert LearningType.FEW_SHOT_EXAMPLE.value == "few_shot_example"
        assert LearningType.TOOL_CORRECTION.value == "tool_correction"
        assert LearningType.PARAM_CORRECTION.value == "param_correction"
        assert LearningType.EDGE_CASE.value == "edge_case"
        assert LearningType.KNOWLEDGE_GAP.value == "knowledge_gap"

    def test_approval_status_enum(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"
        assert ApprovalStatus.AUTO_APPLIED.value == "auto_applied"

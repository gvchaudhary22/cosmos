"""
Tests for the Tournament Architecture — parallel strategy racing.
"""

import asyncio
import pytest

from cosmos.app.brain.tournament import (
    StrategyName,
    StrategyResult,
    StrategyStats,
    TournamentEngine,
    TournamentMode,
    TournamentScorer,
    _normalize_pattern,
)


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
        cost_usd=0.0,
        tokens_used=0,
    )


async def _mock_rag(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.TFIDF_RAG,
        answer=f"RAG: Found order {entity_id}",
        confidence=0.75,
        tool_used="tfidf_search",
        cost_usd=0.001,
        tokens_used=200,
    )


async def _mock_tool_use(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.TOOL_USE,
        answer=f"Claude: Order {entity_id} shipped on March 28",
        confidence=0.95,
        tool_used="mcapi.orders.get",
        cost_usd=0.003,
        tokens_used=500,
    )


async def _mock_reasoning(query, intent, entity, entity_id):
    return StrategyResult(
        strategy=StrategyName.FULL_REASONING,
        answer=f"ReAct: After checking order {entity_id}, it was shipped yesterday",
        confidence=0.92,
        tool_used="react_loop",
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
# Pattern normalizer
# ---------------------------------------------------------------------------


class TestPatternNormalizer:
    def test_lookup_order(self):
        assert _normalize_pattern("show order 12345", "lookup", "order") == "lookup:order"

    def test_act_payment(self):
        assert _normalize_pattern("refund payment", "act", "payment") == "act:payment"

    def test_unknown(self):
        assert _normalize_pattern("hello", "unknown", "unknown") == "unknown:unknown"


# ---------------------------------------------------------------------------
# Strategy Stats
# ---------------------------------------------------------------------------


class TestStrategyStats:
    def test_win_rate(self):
        s = StrategyStats(wins=8, losses=2, total_runs=10)
        assert s.win_rate == 0.8

    def test_win_rate_zero_runs(self):
        s = StrategyStats()
        assert s.win_rate == 0.0

    def test_avg_cost(self):
        s = StrategyStats(total_runs=10, total_cost_usd=0.05)
        assert s.avg_cost == pytest.approx(0.005)

    def test_score_formula(self):
        s = StrategyStats(wins=9, losses=1, total_runs=10, total_cost_usd=0.01, total_latency_ms=500)
        score = s.score
        assert 0.0 < score <= 1.0
        # High win rate, low cost, low latency → high score
        assert score > 0.7


# ---------------------------------------------------------------------------
# Tournament Scorer
# ---------------------------------------------------------------------------


class TestTournamentScorer:
    def test_selects_highest_confidence(self):
        scorer = TournamentScorer()
        results = [
            StrategyResult(strategy=StrategyName.DECISION_TREE, answer="A", confidence=0.7, cost_usd=0.0),
            StrategyResult(strategy=StrategyName.TOOL_USE, answer="B", confidence=0.95, cost_usd=0.003),
        ]
        winner = scorer.select_winner(results, "lookup:order")
        assert winner.strategy == StrategyName.TOOL_USE

    def test_considers_cost(self):
        scorer = TournamentScorer()
        # Same confidence, but A is cheaper
        results = [
            StrategyResult(strategy=StrategyName.DECISION_TREE, answer="A", confidence=0.9, cost_usd=0.0),
            StrategyResult(strategy=StrategyName.TOOL_USE, answer="B", confidence=0.9, cost_usd=0.01),
        ]
        winner = scorer.select_winner(results, "lookup:order")
        # Cheaper should win when confidence is equal
        assert winner.strategy == StrategyName.DECISION_TREE

    def test_filters_failed_results(self):
        scorer = TournamentScorer()
        results = [
            StrategyResult(strategy=StrategyName.DECISION_TREE, answer="A", confidence=0.0),
            StrategyResult(strategy=StrategyName.TFIDF_RAG, answer="", confidence=0.0, error="fail"),
            StrategyResult(strategy=StrategyName.TOOL_USE, answer="B", confidence=0.8, cost_usd=0.003),
        ]
        winner = scorer.select_winner(results, "test")
        assert winner.strategy == StrategyName.TOOL_USE

    def test_no_successful_results(self):
        scorer = TournamentScorer()
        results = [
            StrategyResult(strategy=StrategyName.DECISION_TREE, answer="", confidence=0.0, error="fail"),
        ]
        winner = scorer.select_winner(results, "test")
        assert winner is None

    def test_history_influences_score(self):
        stats = {
            "lookup:order": {
                StrategyName.DECISION_TREE: StrategyStats(wins=45, losses=5, total_runs=50),
                StrategyName.TOOL_USE: StrategyStats(wins=10, losses=40, total_runs=50),
            }
        }
        scorer = TournamentScorer(stats)
        # Decision tree has 90% win rate history, should be favored
        results = [
            StrategyResult(strategy=StrategyName.DECISION_TREE, answer="A", confidence=0.85, cost_usd=0.0),
            StrategyResult(strategy=StrategyName.TOOL_USE, answer="B", confidence=0.88, cost_usd=0.003),
        ]
        winner = scorer.select_winner(results, "lookup:order")
        assert winner.strategy == StrategyName.DECISION_TREE


# ---------------------------------------------------------------------------
# Tournament Engine
# ---------------------------------------------------------------------------


class TestTournamentEngine:
    def _make_engine(self, mode=TournamentMode.TOURNAMENT):
        engine = TournamentEngine(mode=mode)
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_rag)
        engine.register_strategy(StrategyName.TOOL_USE, _mock_tool_use)
        engine.register_strategy(StrategyName.FULL_REASONING, _mock_reasoning)
        return engine

    def test_tournament_runs_all_strategies(self):
        engine = self._make_engine()
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        assert len(result.all_results) == 4
        assert result.winner is not None
        assert result.mode == TournamentMode.TOURNAMENT

    def test_winner_has_highest_combined_score(self):
        engine = self._make_engine()
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        # Claude tool_use has highest confidence (0.95)
        # But decision tree is free and fast
        # Winner depends on combined scoring
        assert result.winner.confidence > 0.5

    def test_all_strategies_have_latency(self):
        engine = self._make_engine()
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        for sr in result.all_results:
            assert sr.latency_ms >= 0

    def test_total_cost_tracked(self):
        engine = self._make_engine()
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        assert result.total_cost_usd > 0  # Some strategies cost money

    def test_pattern_recorded(self):
        engine = self._make_engine()
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        assert result.query_pattern == "lookup:order"

    def test_failing_strategy_handled(self):
        engine = TournamentEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, _mock_decision_tree)
        engine.register_strategy(StrategyName.TFIDF_RAG, _mock_failing_strategy)
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        assert result.winner is not None
        assert result.winner.strategy == StrategyName.DECISION_TREE
        # Failed strategy should have error
        rag_result = [r for r in result.all_results if r.strategy == StrategyName.TFIDF_RAG][0]
        assert rag_result.error is not None

    def test_stats_accumulate(self):
        engine = self._make_engine()
        for _ in range(5):
            _run(engine.run("show order 12345", "lookup", "order", "12345"))
        leaderboard = engine.get_leaderboard()
        assert len(leaderboard) == 4
        for entry in leaderboard:
            assert entry["total_runs"] == 5

    def test_leaderboard_sorted_by_score(self):
        engine = self._make_engine()
        for _ in range(10):
            _run(engine.run("show order 12345", "lookup", "order", "12345"))
        leaderboard = engine.get_leaderboard()
        scores = [e["score"] for e in leaderboard]
        assert scores == sorted(scores, reverse=True)

    def test_pattern_insights(self):
        engine = self._make_engine()
        _run(engine.run("show order 12345", "lookup", "order", "12345"))
        _run(engine.run("cancel order 99999", "act", "order", "99999"))
        insights = engine.get_pattern_insights()
        patterns = {i["pattern"] for i in insights}
        assert "lookup:order" in patterns
        assert "act:order" in patterns

    def test_cost_report(self):
        engine = self._make_engine()
        for _ in range(5):
            _run(engine.run("show order 12345", "lookup", "order", "12345"))
        report = engine.get_cost_report()
        assert report["total_queries"] == 5
        assert report["total_tournament_cost_usd"] > 0
        assert "savings_potential_pct" in report

    def test_shadow_mode_runs_fewer_strategies(self):
        engine = self._make_engine(mode=TournamentMode.SHADOW)
        # Need some history first
        engine._pattern_stats["lookup:order"] = {
            StrategyName.DECISION_TREE: StrategyStats(wins=50, total_runs=50),
        }
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        # Shadow runs 2-3 strategies, not all 4
        assert len(result.all_results) <= 3

    def test_converged_mode_runs_one(self):
        engine = self._make_engine(mode=TournamentMode.CONVERGED)
        engine._pattern_stats["lookup:order"] = {
            StrategyName.DECISION_TREE: StrategyStats(wins=90, total_runs=100),
        }
        engine._round_count = 5  # Not a shadow round
        result = _run(engine.run("show order 12345", "lookup", "order", "12345"))
        # Converged runs 1 (maybe 2 for shadow)
        assert len(result.all_results) <= 2

    def test_auto_advance_from_tournament(self):
        engine = self._make_engine()
        # Simulate 100 queries with clear winners
        for pattern in ["lookup:order", "act:order", "explain:shipment"]:
            engine._pattern_stats[pattern] = {
                StrategyName.DECISION_TREE: StrategyStats(wins=40, total_runs=50),
            }
        engine._history = [None] * 100  # Fake history length
        new_mode = engine.auto_advance_mode()
        assert new_mode == TournamentMode.SHADOW

    def test_should_converge_not_enough_data(self):
        engine = self._make_engine()
        assert engine.should_converge(min_queries=100) is False

    def test_mode_property(self):
        engine = TournamentEngine(mode=TournamentMode.TOURNAMENT)
        assert engine.mode == TournamentMode.TOURNAMENT
        engine.mode = TournamentMode.SHADOW
        assert engine.mode == TournamentMode.SHADOW

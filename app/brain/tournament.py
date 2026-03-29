"""
Tournament Architecture — Parallel Strategy Racing.

Runs multiple routing/resolution strategies in parallel on each query,
scores the results, and learns which strategy works best for each
query pattern. Over time, converges to optimal routing.

Strategies:
  A: Decision Tree (free, instant)
  B: TF-IDF RAG retrieval (cheap, fast)
  C: Tool-Use with domain scoping (medium cost)
  D: Full ReAct reasoning (expensive, slow)

Modes:
  TOURNAMENT: Run all strategies, return best, log everything (discovery phase)
  SHADOW: Run winning strategy + shadow 1-2 others for monitoring
  CONVERGED: Route to predicted best strategy, shadow 10% for drift detection

Typical timeline:
  Week 1-2: TOURNAMENT (all strategies, high cost, maximum learning)
  Week 3-4: SHADOW (route to winner, shadow losers for validation)
  Month 2+: CONVERGED (optimal routing, minimal shadow traffic)
"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple


class TournamentMode(str, Enum):
    TOURNAMENT = "tournament"  # Run all strategies, return best
    SHADOW = "shadow"          # Run winner + shadow 1-2 others
    CONVERGED = "converged"    # Route to predicted best, shadow 10%


class StrategyName(str, Enum):
    DECISION_TREE = "decision_tree"
    TFIDF_RAG = "tfidf_rag"
    TOOL_USE = "tool_use"
    FULL_REASONING = "full_reasoning"
    HYBRID_RETRIEVAL = "hybrid_retrieval"


@dataclass
class StrategyResult:
    """Result from one strategy execution."""

    strategy: StrategyName
    answer: str
    confidence: float  # 0.0 - 1.0
    tool_used: Optional[str] = None
    params_extracted: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    tokens_used: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None and self.confidence > 0.0


@dataclass
class TournamentResult:
    """Result of a tournament round."""

    query: str
    intent: str
    entity: str
    winner: Optional[StrategyResult] = None
    all_results: List[StrategyResult] = field(default_factory=list)
    mode: TournamentMode = TournamentMode.TOURNAMENT
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    query_pattern: str = ""  # Normalized pattern for learning

    @property
    def winner_strategy(self) -> Optional[StrategyName]:
        return self.winner.strategy if self.winner else None


@dataclass
class StrategyStats:
    """Accumulated stats for a strategy across a query pattern."""

    wins: int = 0
    losses: int = 0
    total_runs: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    avg_confidence: float = 0.0
    error_count: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.total_runs, 1)

    @property
    def avg_cost(self) -> float:
        return self.total_cost_usd / max(self.total_runs, 1)

    @property
    def avg_latency(self) -> float:
        return self.total_latency_ms / max(self.total_runs, 1)

    @property
    def score(self) -> float:
        """Combined score: high win rate, low cost, low latency.

        Formula: win_rate * 0.5 + cost_score * 0.3 + latency_score * 0.2
        """
        cost_score = max(0, 1.0 - (self.avg_cost / 0.01))  # $0.01 = worst
        latency_score = max(0, 1.0 - (self.avg_latency / 2000))  # 2s = worst
        return (
            self.win_rate * 0.5
            + cost_score * 0.3
            + latency_score * 0.2
        )


# -----------------------------------------------------------------------
# Query Pattern Normalizer
# -----------------------------------------------------------------------

def _normalize_pattern(query: str, intent: str, entity: str) -> str:
    """Normalize a query into a pattern for learning.

    "show order 12345" → "lookup:order"
    "why was order 99999 delayed" → "explain:order"
    "cancel order 12345" → "act:order"
    "how many orders today" → "report:order"
    """
    return f"{intent}:{entity}"


# -----------------------------------------------------------------------
# Tournament Scorer
# -----------------------------------------------------------------------

class TournamentScorer:
    """Scores and selects the winning strategy from parallel results.

    Scoring considers:
    1. Confidence (highest = better)
    2. Cost (lowest = better, weighted)
    3. Latency (lowest = better, weighted)
    4. Historical win rate for this pattern (if available)
    """

    def __init__(self, pattern_stats: Dict[str, Dict[StrategyName, StrategyStats]] = None):
        self._pattern_stats = pattern_stats or {}

    def select_winner(
        self,
        results: List[StrategyResult],
        pattern: str,
    ) -> Optional[StrategyResult]:
        """Select the winning strategy from a list of results."""
        successful = [r for r in results if r.success]
        if not successful:
            return None

        scored = []
        for r in successful:
            score = self._score_result(r, pattern)
            scored.append((r, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def _score_result(self, result: StrategyResult, pattern: str) -> float:
        """Score a single result.

        Base: confidence * 0.6 + cost_score * 0.2 + latency_score * 0.1 + history * 0.1
        """
        # Confidence component (0-1)
        conf_score = result.confidence

        # Cost component (0-1, lower cost = higher score)
        max_cost = 0.01  # $0.01 as normalization ceiling
        cost_score = max(0, 1.0 - (result.cost_usd / max_cost))

        # Latency component (0-1, lower latency = higher score)
        max_latency = 2000  # 2 seconds as normalization ceiling
        latency_score = max(0, 1.0 - (result.latency_ms / max_latency))

        # History component
        history_score = 0.5  # neutral default
        if pattern in self._pattern_stats:
            stats = self._pattern_stats[pattern].get(result.strategy)
            if stats and stats.total_runs >= 5:
                history_score = stats.win_rate

        return (
            conf_score * 0.6
            + cost_score * 0.2
            + latency_score * 0.1
            + history_score * 0.1
        )


# -----------------------------------------------------------------------
# Tournament Engine
# -----------------------------------------------------------------------

class TournamentEngine:
    """Orchestrates parallel strategy execution and learning.

    Usage:
        engine = TournamentEngine()
        engine.register_strategy(StrategyName.DECISION_TREE, decision_tree_fn)
        engine.register_strategy(StrategyName.TFIDF_RAG, rag_fn)
        engine.register_strategy(StrategyName.CLAUDE_TOOL_USE, tool_use_fn)
        engine.register_strategy(StrategyName.FULL_REASONING, reasoning_fn)

        result = await engine.run(query, intent, entity)
        # result.winner has the best answer
        # result.all_results has all strategy outputs for comparison
    """

    def __init__(
        self,
        mode: TournamentMode = TournamentMode.TOURNAMENT,
        shadow_percentage: float = 0.1,
    ):
        self._mode = mode
        self._shadow_pct = shadow_percentage
        self._strategies: Dict[StrategyName, Callable] = {}
        # Pattern → Strategy → Stats
        self._pattern_stats: Dict[str, Dict[StrategyName, StrategyStats]] = {}
        self._scorer = TournamentScorer(self._pattern_stats)
        self._history: List[TournamentResult] = []
        self._round_count = 0

    @property
    def mode(self) -> TournamentMode:
        return self._mode

    @mode.setter
    def mode(self, value: TournamentMode):
        self._mode = value

    def register_strategy(
        self,
        name: StrategyName,
        fn: Callable[..., Coroutine[Any, Any, StrategyResult]],
    ):
        """Register an async strategy function.

        The function signature should be:
            async def strategy(query: str, intent: str, entity: str,
                               entity_id: str | None) -> StrategyResult
        """
        self._strategies[name] = fn

    async def run(
        self,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str] = None,
    ) -> TournamentResult:
        """Run the tournament for a query.

        In TOURNAMENT mode: runs all strategies in parallel.
        In SHADOW mode: runs winner + shadows in parallel.
        In CONVERGED mode: runs predicted winner, shadows 10%.
        """
        self._round_count += 1
        pattern = _normalize_pattern(query, intent, entity)
        start = time.monotonic()

        if self._mode == TournamentMode.TOURNAMENT:
            results = await self._run_all(query, intent, entity, entity_id)
        elif self._mode == TournamentMode.SHADOW:
            results = await self._run_shadow(query, intent, entity, entity_id, pattern)
        else:
            results = await self._run_converged(query, intent, entity, entity_id, pattern)

        total_latency = (time.monotonic() - start) * 1000
        total_cost = sum(r.cost_usd for r in results)

        # Select winner
        winner = self._scorer.select_winner(results, pattern)

        result = TournamentResult(
            query=query,
            intent=intent,
            entity=entity,
            winner=winner,
            all_results=results,
            mode=self._mode,
            total_latency_ms=total_latency,
            total_cost_usd=total_cost,
            query_pattern=pattern,
        )

        # Record stats
        self._record_stats(result)
        self._history.append(result)

        # Keep history bounded
        if len(self._history) > 10000:
            self._history = self._history[-5000:]

        return result

    async def _run_all(
        self, query: str, intent: str, entity: str, entity_id: Optional[str]
    ) -> List[StrategyResult]:
        """Run ALL strategies in parallel."""
        tasks = []
        for name, fn in self._strategies.items():
            tasks.append(self._run_strategy(name, fn, query, intent, entity, entity_id))

        return await asyncio.gather(*tasks)

    async def _run_shadow(
        self, query: str, intent: str, entity: str,
        entity_id: Optional[str], pattern: str,
    ) -> List[StrategyResult]:
        """Run predicted winner + 1-2 shadow strategies."""
        predicted = self._predict_winner(pattern)
        strategies_to_run = [predicted]

        # Add 1-2 shadows (second and third best)
        all_ranked = self._rank_strategies(pattern)
        for s in all_ranked:
            if s != predicted and len(strategies_to_run) < 3:
                strategies_to_run.append(s)

        tasks = []
        for name in strategies_to_run:
            if name in self._strategies:
                tasks.append(
                    self._run_strategy(
                        name, self._strategies[name],
                        query, intent, entity, entity_id,
                    )
                )

        return await asyncio.gather(*tasks)

    async def _run_converged(
        self, query: str, intent: str, entity: str,
        entity_id: Optional[str], pattern: str,
    ) -> List[StrategyResult]:
        """Run predicted winner only, shadow 10% of traffic."""
        predicted = self._predict_winner(pattern)
        strategies_to_run = [predicted]

        # Shadow 10% of traffic with a random other strategy
        if self._round_count % int(1 / max(self._shadow_pct, 0.01)) == 0:
            all_ranked = self._rank_strategies(pattern)
            for s in all_ranked:
                if s != predicted:
                    strategies_to_run.append(s)
                    break

        tasks = []
        for name in strategies_to_run:
            if name in self._strategies:
                tasks.append(
                    self._run_strategy(
                        name, self._strategies[name],
                        query, intent, entity, entity_id,
                    )
                )

        return await asyncio.gather(*tasks)

    async def _run_strategy(
        self,
        name: StrategyName,
        fn: Callable,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str],
    ) -> StrategyResult:
        """Run a single strategy with timing and error handling."""
        start = time.monotonic()
        try:
            result = await fn(query, intent, entity, entity_id)
            result.latency_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return StrategyResult(
                strategy=name,
                answer="",
                confidence=0.0,
                latency_ms=latency,
                error=str(e),
            )

    def _predict_winner(self, pattern: str) -> StrategyName:
        """Predict which strategy will win for this pattern."""
        if pattern not in self._pattern_stats:
            # No data → default to cheapest with reasonable accuracy
            return StrategyName.DECISION_TREE

        stats = self._pattern_stats[pattern]
        best_name = StrategyName.DECISION_TREE
        best_score = -1.0

        for name, s in stats.items():
            if s.total_runs >= 3 and s.score > best_score:
                best_score = s.score
                best_name = name

        return best_name

    def _rank_strategies(self, pattern: str) -> List[StrategyName]:
        """Rank strategies by score for a pattern."""
        if pattern not in self._pattern_stats:
            return list(self._strategies.keys())

        stats = self._pattern_stats[pattern]
        scored = [(name, s.score) for name, s in stats.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        ranked = [name for name, _ in scored]

        # Add any unranked strategies at the end
        for name in self._strategies:
            if name not in ranked:
                ranked.append(name)

        return ranked

    def _record_stats(self, result: TournamentResult):
        """Record tournament results into pattern stats."""
        pattern = result.query_pattern
        if pattern not in self._pattern_stats:
            self._pattern_stats[pattern] = {}

        for sr in result.all_results:
            if sr.strategy not in self._pattern_stats[pattern]:
                self._pattern_stats[pattern][sr.strategy] = StrategyStats()

            stats = self._pattern_stats[pattern][sr.strategy]
            stats.total_runs += 1
            stats.total_cost_usd += sr.cost_usd
            stats.total_latency_ms += sr.latency_ms

            if sr.error:
                stats.error_count += 1

            if result.winner and sr.strategy == result.winner.strategy:
                stats.wins += 1
            elif sr.success:
                stats.losses += 1

            # Running average confidence
            if sr.success:
                stats.avg_confidence = (
                    (stats.avg_confidence * (stats.total_runs - 1) + sr.confidence)
                    / stats.total_runs
                )

    # -------------------------------------------------------------------
    # Analytics & Reporting
    # -------------------------------------------------------------------

    def get_leaderboard(self) -> List[dict]:
        """Get global strategy leaderboard across all patterns."""
        global_stats: Dict[StrategyName, StrategyStats] = {}

        for pattern_stats in self._pattern_stats.values():
            for name, stats in pattern_stats.items():
                if name not in global_stats:
                    global_stats[name] = StrategyStats()
                g = global_stats[name]
                g.wins += stats.wins
                g.losses += stats.losses
                g.total_runs += stats.total_runs
                g.total_cost_usd += stats.total_cost_usd
                g.total_latency_ms += stats.total_latency_ms
                g.error_count += stats.error_count

        leaderboard = []
        for name, stats in global_stats.items():
            leaderboard.append({
                "strategy": name.value,
                "wins": stats.wins,
                "total_runs": stats.total_runs,
                "win_rate": round(stats.win_rate * 100, 1),
                "avg_cost_usd": round(stats.avg_cost, 6),
                "avg_latency_ms": round(stats.avg_latency, 1),
                "score": round(stats.score, 3),
                "errors": stats.error_count,
            })

        leaderboard.sort(key=lambda x: x["score"], reverse=True)
        return leaderboard

    def get_pattern_insights(self) -> List[dict]:
        """Get per-pattern winner analysis."""
        insights = []
        for pattern, stats in self._pattern_stats.items():
            best_name = None
            best_score = -1.0
            for name, s in stats.items():
                if s.score > best_score:
                    best_score = s.score
                    best_name = name

            total_queries = sum(s.total_runs for s in stats.values()) // max(len(stats), 1)
            insights.append({
                "pattern": pattern,
                "best_strategy": best_name.value if best_name else "unknown",
                "best_score": round(best_score, 3),
                "total_queries": total_queries,
                "strategies_tested": len(stats),
            })

        insights.sort(key=lambda x: x["total_queries"], reverse=True)
        return insights

    def get_cost_report(self) -> dict:
        """Get cost analysis: current vs optimal routing."""
        total_cost = sum(r.total_cost_usd for r in self._history)
        winner_cost = sum(
            r.winner.cost_usd for r in self._history if r.winner
        )

        # Estimate optimal cost: cheapest winning strategy per pattern
        optimal_cost = 0.0
        for pattern, stats in self._pattern_stats.items():
            cheapest_winner = None
            cheapest_cost = float("inf")
            for name, s in stats.items():
                if s.win_rate > 0.6 and s.avg_cost < cheapest_cost:
                    cheapest_cost = s.avg_cost
                    cheapest_winner = name
            if cheapest_winner:
                pattern_queries = sum(
                    s.total_runs for s in stats.values()
                ) // max(len(stats), 1)
                optimal_cost += cheapest_cost * pattern_queries

        return {
            "total_tournament_cost_usd": round(total_cost, 4),
            "winner_only_cost_usd": round(winner_cost, 4),
            "estimated_optimal_cost_usd": round(optimal_cost, 4),
            "savings_potential_pct": round(
                (1 - optimal_cost / max(total_cost, 0.001)) * 100, 1
            ),
            "total_queries": len(self._history),
            "mode": self._mode.value,
        }

    def should_converge(self, min_queries: int = 100) -> bool:
        """Check if enough data has been collected to converge.

        Returns True if:
        - At least min_queries queries processed
        - At least 80% of patterns have a clear winner (win_rate > 0.7)
        """
        if len(self._history) < min_queries:
            return False

        clear_winners = 0
        total_patterns = len(self._pattern_stats)
        if total_patterns == 0:
            return False

        for stats in self._pattern_stats.values():
            for s in stats.values():
                if s.total_runs >= 10 and s.win_rate > 0.7:
                    clear_winners += 1
                    break

        return (clear_winners / total_patterns) >= 0.8

    def auto_advance_mode(self) -> TournamentMode:
        """Auto-advance tournament mode based on data.

        TOURNAMENT → SHADOW after 100 queries with 80% clear winners
        SHADOW → CONVERGED after 500 queries with 90% clear winners
        """
        if self._mode == TournamentMode.TOURNAMENT:
            if self.should_converge(min_queries=100):
                self._mode = TournamentMode.SHADOW
        elif self._mode == TournamentMode.SHADOW:
            if self.should_converge(min_queries=500):
                self._mode = TournamentMode.CONVERGED
        return self._mode

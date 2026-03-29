"""
Tier Cost Tracker — Per-query, per-tier, per-user cost accounting.

Tracks:
  - LLM token cost (existing, from cost_tracker.py)
  - API call cost (MCAPI calls have rate limits, each counts)
  - DB query cost (each Tier 3 template execution)
  - Total per-query cost across all tiers
  - Per-user daily budget enforcement
  - Per-pattern cost analysis (which query types are expensive?)
"""

import time
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class TierCostEntry:
    """Cost breakdown for a single tier execution."""
    tier: int
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_usd: float = 0.0
    api_calls: int = 0
    api_cost_usd: float = 0.0  # Estimated per-call cost
    db_queries: int = 0
    db_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class QueryCostRecord:
    """Full cost record for one query across all tiers."""
    query_hash: str
    user_id: str
    company_id: str
    query_preview: str  # First 100 chars
    tiers_visited: List[int] = field(default_factory=list)
    tier_costs: List[TierCostEntry] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    resolution_tier: int = 0
    timestamp: float = field(default_factory=time.time)

    def add_tier_cost(self, entry: TierCostEntry):
        self.tier_costs.append(entry)
        self.tiers_visited.append(entry.tier)
        self.total_cost_usd += entry.total_cost_usd
        self.total_latency_ms += entry.latency_ms


# Cost constants
LLM_COST_PER_1K_INPUT = 0.003    # Sonnet input
LLM_COST_PER_1K_OUTPUT = 0.015   # Sonnet output
API_COST_PER_CALL = 0.0001       # Estimated MCAPI cost
DB_COST_PER_QUERY = 0.00005      # Estimated DB query cost

# Default daily budget per user (USD)
DEFAULT_DAILY_BUDGET_USD = 5.0


class TierCostTracker:
    """
    Tracks and enforces cost budgets across tiers.

    Usage:
        tracker = TierCostTracker()

        # Before query: check budget
        if tracker.check_budget(user_id):
            # After each tier: record cost
            tracker.record_tier_cost(record, tier=1, tokens_in=500, tokens_out=200, api_calls=2)
            tracker.record_tier_cost(record, tier=2, tokens_in=800, tokens_out=300)

            # After query: finalize
            tracker.finalize(record)
    """

    def __init__(self, daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD):
        self.daily_budget = daily_budget_usd
        # user_id → list of QueryCostRecord for today
        self._daily_costs: Dict[str, List[QueryCostRecord]] = defaultdict(list)
        self._daily_reset_time: float = 0.0
        # Pattern cost tracking: query_hash → cumulative cost
        self._pattern_costs: Dict[str, float] = defaultdict(float)
        self._pattern_counts: Dict[str, int] = defaultdict(int)

    def check_budget(self, user_id: str) -> bool:
        """Check if user has remaining daily budget."""
        self._maybe_reset_daily()
        spent = sum(r.total_cost_usd for r in self._daily_costs.get(user_id, []))
        if spent >= self.daily_budget:
            logger.warning("cost_tracker.budget_exceeded", user_id=user_id, spent=spent)
            return False
        return True

    def get_remaining_budget(self, user_id: str) -> float:
        """Get remaining daily budget for a user."""
        self._maybe_reset_daily()
        spent = sum(r.total_cost_usd for r in self._daily_costs.get(user_id, []))
        return max(0.0, self.daily_budget - spent)

    def start_record(self, query: str, user_id: str, company_id: str) -> QueryCostRecord:
        """Start tracking cost for a new query."""
        query_hash = hashlib.md5(query.strip().lower().encode()).hexdigest()[:12]
        return QueryCostRecord(
            query_hash=query_hash,
            user_id=user_id,
            company_id=company_id,
            query_preview=query[:100],
        )

    def record_tier_cost(
        self,
        record: QueryCostRecord,
        tier: int,
        tokens_in: int = 0,
        tokens_out: int = 0,
        api_calls: int = 0,
        db_queries: int = 0,
        latency_ms: float = 0.0,
    ):
        """Record cost for a single tier execution."""
        llm_cost = (tokens_in / 1000 * LLM_COST_PER_1K_INPUT) + (tokens_out / 1000 * LLM_COST_PER_1K_OUTPUT)
        api_cost = api_calls * API_COST_PER_CALL
        db_cost = db_queries * DB_COST_PER_QUERY
        total = llm_cost + api_cost + db_cost

        entry = TierCostEntry(
            tier=tier,
            llm_tokens_in=tokens_in,
            llm_tokens_out=tokens_out,
            llm_cost_usd=round(llm_cost, 6),
            api_calls=api_calls,
            api_cost_usd=round(api_cost, 6),
            db_queries=db_queries,
            db_cost_usd=round(db_cost, 6),
            total_cost_usd=round(total, 6),
            latency_ms=latency_ms,
        )
        record.add_tier_cost(entry)

    def finalize(self, record: QueryCostRecord):
        """Finalize and store the cost record."""
        self._maybe_reset_daily()
        self._daily_costs[record.user_id].append(record)
        self._pattern_costs[record.query_hash] += record.total_cost_usd
        self._pattern_counts[record.query_hash] += 1

        logger.info(
            "cost_tracker.query_complete",
            user_id=record.user_id,
            tiers=record.tiers_visited,
            total_cost=round(record.total_cost_usd, 5),
            total_ms=round(record.total_latency_ms, 1),
        )

    def get_expensive_patterns(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """Get the most expensive query patterns for optimization."""
        sorted_patterns = sorted(
            self._pattern_costs.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return [
            {
                "query_hash": h,
                "total_cost_usd": round(c, 4),
                "query_count": self._pattern_counts[h],
                "avg_cost_usd": round(c / self._pattern_counts[h], 5),
            }
            for h, c in sorted_patterns[:top_n]
        ]

    def get_user_daily_summary(self, user_id: str) -> Dict[str, Any]:
        """Get daily cost summary for a user."""
        self._maybe_reset_daily()
        records = self._daily_costs.get(user_id, [])
        total = sum(r.total_cost_usd for r in records)
        tier_breakdown = defaultdict(float)
        for r in records:
            for tc in r.tier_costs:
                tier_breakdown[f"tier_{tc.tier}"] += tc.total_cost_usd

        return {
            "user_id": user_id,
            "query_count": len(records),
            "total_cost_usd": round(total, 4),
            "remaining_budget_usd": round(max(0, self.daily_budget - total), 4),
            "tier_breakdown": {k: round(v, 4) for k, v in tier_breakdown.items()},
        }

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight."""
        now = time.time()
        today_start = now - (now % 86400)
        if today_start > self._daily_reset_time:
            self._daily_costs.clear()
            self._daily_reset_time = today_start

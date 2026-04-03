"""
Cost Tracker for COSMOS Phase 4.

Tracks real-time token usage and costs with daily and per-session budgets.
"""

import structlog
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

from app.engine.model_router import PROFILES, ModelTier

logger = structlog.get_logger()


@dataclass
class CostEntry:
    timestamp: datetime
    session_id: str
    model_tier: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    intent: str
    cached: bool


class CostTracker:
    """Tracks token usage and costs in real-time."""

    def __init__(
        self,
        daily_budget_usd: float = 50.0,
        per_session_budget_usd: float = 1.0,
    ) -> None:
        self._entries: List[CostEntry] = []
        self._daily_budget = daily_budget_usd
        self._session_budget = per_session_budget_usd

    def record(
        self,
        session_id: str,
        tier: str,
        input_tokens: int,
        output_tokens: int,
        intent: str,
        cached: bool = False,
    ) -> CostEntry:
        """Record a cost entry and return it."""
        # Calculate cost
        try:
            model_tier = ModelTier(tier)
            profile = PROFILES[model_tier]
        except (ValueError, KeyError):
            # Fallback to Sonnet pricing if tier is unknown
            profile = PROFILES[ModelTier.SONNET]

        input_cost = (input_tokens / 1000) * profile.cost_per_1k_input
        output_cost = (output_tokens / 1000) * profile.cost_per_1k_output
        total_cost = input_cost + output_cost

        # Apply cache discount (90% reduction on cached input)
        if cached:
            total_cost = total_cost * 0.1

        entry = CostEntry(
            timestamp=datetime.now(tz=None),
            session_id=session_id,
            model_tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(total_cost, 6),
            intent=intent,
            cached=cached,
        )

        self._entries.append(entry)

        logger.info(
            "cost_tracker.record",
            session_id=session_id,
            tier=tier,
            cost_usd=entry.cost_usd,
            cached=cached,
        )

        return entry

    def check_budget(self, session_id: str) -> Dict:
        """
        Check if session/daily budget is exceeded.
        Returns {"allowed": bool, "daily_remaining": float,
                 "session_remaining": float, "warning": str|None}
        """
        today = date.today()

        daily_total = sum(
            e.cost_usd
            for e in self._entries
            if e.timestamp.date() == today
        )
        session_total = sum(
            e.cost_usd
            for e in self._entries
            if e.session_id == session_id
        )

        daily_remaining = self._daily_budget - daily_total
        session_remaining = self._session_budget - session_total

        warning = None
        allowed = True

        if daily_remaining <= 0:
            allowed = False
            warning = "Daily budget exceeded"
        elif session_remaining <= 0:
            allowed = False
            warning = "Session budget exceeded"
        elif daily_remaining < self._daily_budget * 0.1:
            warning = "Daily budget below 10%"
        elif session_remaining < self._session_budget * 0.1:
            warning = "Session budget below 10%"

        return {
            "allowed": allowed,
            "daily_remaining": round(daily_remaining, 6),
            "session_remaining": round(session_remaining, 6),
            "warning": warning,
        }

    def get_daily_summary(self) -> Dict:
        """Today's costs: total, by model, by intent, query count."""
        today = date.today()
        today_entries = [e for e in self._entries if e.timestamp.date() == today]

        total_cost = sum(e.cost_usd for e in today_entries)

        by_model: Dict[str, float] = {}
        by_intent: Dict[str, float] = {}
        for e in today_entries:
            by_model[e.model_tier] = by_model.get(e.model_tier, 0) + e.cost_usd
            by_intent[e.intent] = by_intent.get(e.intent, 0) + e.cost_usd

        return {
            "date": today.isoformat(),
            "total_cost_usd": round(total_cost, 6),
            "query_count": len(today_entries),
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "by_intent": {k: round(v, 6) for k, v in by_intent.items()},
            "budget_usd": self._daily_budget,
            "remaining_usd": round(self._daily_budget - total_cost, 6),
        }

    def get_session_summary(self, session_id: str) -> Dict:
        """Per-session cost breakdown."""
        session_entries = [e for e in self._entries if e.session_id == session_id]

        total_cost = sum(e.cost_usd for e in session_entries)
        total_input = sum(e.input_tokens for e in session_entries)
        total_output = sum(e.output_tokens for e in session_entries)

        by_model: Dict[str, int] = {}
        for e in session_entries:
            by_model[e.model_tier] = by_model.get(e.model_tier, 0) + 1

        return {
            "session_id": session_id,
            "total_cost_usd": round(total_cost, 6),
            "query_count": len(session_entries),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "by_model": by_model,
            "budget_usd": self._session_budget,
            "remaining_usd": round(self._session_budget - total_cost, 6),
        }

    def get_cost_trend(self, days: int = 7) -> List[Dict]:
        """Daily cost trend for dashboarding."""
        from datetime import timedelta

        today = date.today()
        trend = []

        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            day_entries = [e for e in self._entries if e.timestamp.date() == d]
            day_cost = sum(e.cost_usd for e in day_entries)
            trend.append({
                "date": d.isoformat(),
                "cost_usd": round(day_cost, 6),
                "query_count": len(day_entries),
            })

        return trend

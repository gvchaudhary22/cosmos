"""
Cost API endpoints for COSMOS Phase 4.

Provides real-time cost monitoring, budget status, and usage analytics.

Routes:
  GET /cosmos/api/v1/costs/current    — Current budget status
  GET /cosmos/api/v1/costs/daily      — Daily cost summary
  GET /cosmos/api/v1/costs/session/{session_id} — Per-session costs
  GET /cosmos/api/v1/costs/trend      — Cost trend over days
  GET /cosmos/api/v1/costs/models     — Model usage breakdown
"""

from fastapi import APIRouter, Query
from typing import Optional

from app.engine.cost_tracker import CostTracker
from app.engine.model_router import ModelRouter

router = APIRouter()

# Module-level singletons; replaced at app startup via configure()
_cost_tracker: Optional[CostTracker] = None
_model_router: Optional[ModelRouter] = None


def configure(cost_tracker: CostTracker, model_router: ModelRouter) -> None:
    """Wire up the cost tracker and model router (called at app startup)."""
    global _cost_tracker, _model_router
    _cost_tracker = cost_tracker
    _model_router = model_router


def _get_tracker() -> CostTracker:
    if _cost_tracker is None:
        return CostTracker()  # fallback with defaults
    return _cost_tracker


def _get_router() -> ModelRouter:
    if _model_router is None:
        return ModelRouter()
    return _model_router


@router.get("/current")
async def get_current_budget(session_id: str = Query(default="default")):
    """Current budget status for a session."""
    tracker = _get_tracker()
    return tracker.check_budget(session_id)


@router.get("/daily")
async def get_daily_summary():
    """Today's cost summary."""
    tracker = _get_tracker()
    return tracker.get_daily_summary()


@router.get("/session/{session_id}")
async def get_session_costs(session_id: str):
    """Per-session cost breakdown."""
    tracker = _get_tracker()
    return tracker.get_session_summary(session_id)


@router.get("/trend")
async def get_cost_trend(days: int = Query(default=7, ge=1, le=90)):
    """Cost trend over the specified number of days."""
    tracker = _get_tracker()
    return tracker.get_cost_trend(days)


@router.get("/models")
async def get_model_usage():
    """Model usage breakdown from the router."""
    mr = _get_router()
    return mr.get_usage_stats()

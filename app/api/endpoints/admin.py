"""
Admin API endpoints for COSMOS — analytics, audit log, distillation.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.db.session import get_db
from app.learning.analytics import AnalyticsEngine
from app.learning.collector import DistillationCollector

router = APIRouter()


# --- Pydantic models ---

class AnalyticsEntry(BaseModel):
    event_type: str
    count: int
    avg_duration_ms: Optional[float] = None
    total_tokens: Optional[int] = None
    total_cost_usd: Optional[float] = None


class AnalyticsResponse(BaseModel):
    period: str = "last_24h"
    entries: list[AnalyticsEntry] = Field(default_factory=list)


class AuditLogEntry(BaseModel):
    id: str
    action: str
    user_id: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: dict = Field(default_factory=dict)
    created_at: datetime


class AuditLogResponse(BaseModel):
    entries: list[AuditLogEntry] = Field(default_factory=list)
    total: int = 0


class DashboardResponse(BaseModel):
    period_days: int
    total_queries: int
    queries_per_day: list = Field(default_factory=list)
    avg_confidence: float = 0.0
    confidence_distribution: dict = Field(default_factory=dict)
    intent_breakdown: dict = Field(default_factory=dict)
    entity_breakdown: dict = Field(default_factory=dict)
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    escalation_rate: float = 0.0
    total_cost_usd: float = 0.0
    avg_cost_per_query: float = 0.0
    model_usage_breakdown: dict = Field(default_factory=dict)


class IntentAnalyticsResponse(BaseModel):
    intent: str
    period_days: int
    total_queries: int = 0
    avg_confidence: float = 0.0
    avg_latency_ms: float = 0.0
    escalation_count: int = 0
    escalation_rate: float = 0.0
    total_cost_usd: float = 0.0


class CostReportResponse(BaseModel):
    period_days: int
    total_cost_usd: float = 0.0
    total_queries: int = 0
    avg_cost_per_query: float = 0.0
    by_model: dict = Field(default_factory=dict)
    by_intent: dict = Field(default_factory=dict)
    by_day: list = Field(default_factory=list)


class HourlyTrafficEntry(BaseModel):
    hour: int
    count: int


class DistillationStatsResponse(BaseModel):
    total_records: int = 0
    avg_confidence: float = 0.0
    feedback_distribution: dict = Field(default_factory=dict)
    total_cost_usd: float = 0.0
    exportable_records: int = 0


class ExportRequest(BaseModel):
    min_confidence: float = 0.7
    min_feedback: int = 4
    format: str = "jsonl"


class ExportResponse(BaseModel):
    data: str
    record_count: int


# --- Analytics endpoints ---

@router.get("/analytics", response_model=DashboardResponse)
async def get_analytics(days: int = 7, db=Depends(get_db)):
    """Get analytics dashboard data."""
    engine = AnalyticsEngine(db)
    result = await engine.get_dashboard(days=days)
    return DashboardResponse(**result)


@router.get("/analytics/intents/{intent}", response_model=IntentAnalyticsResponse)
async def get_intent_analytics(intent: str, days: int = 7, db=Depends(get_db)):
    """Get per-intent analytics deep dive."""
    engine = AnalyticsEngine(db)
    result = await engine.get_intent_analytics(intent=intent, days=days)
    return IntentAnalyticsResponse(**result)


@router.get("/analytics/costs", response_model=CostReportResponse)
async def get_cost_report(days: int = 30, db=Depends(get_db)):
    """Get cost breakdown by model, intent, and day."""
    engine = AnalyticsEngine(db)
    result = await engine.get_cost_report(days=days)
    return CostReportResponse(**result)


@router.get("/analytics/traffic", response_model=List[HourlyTrafficEntry])
async def get_hourly_traffic(days: int = 1, db=Depends(get_db)):
    """Get queries per hour for traffic pattern analysis."""
    engine = AnalyticsEngine(db)
    return await engine.get_hourly_traffic(days=days)


# --- Audit log ---

@router.get("/audit-log", response_model=AuditLogResponse)
async def get_audit_log(
    action: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
):
    """Get audit log entries with optional filters."""
    from sqlalchemy import select
    from app.db.models import AuditLog

    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(stmt)
    records = result.scalars().all()

    # Count total
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(AuditLog)
    if action:
        count_stmt = count_stmt.where(AuditLog.action == action)
    if user_id:
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)
    total = (await db.execute(count_stmt)).scalar() or 0

    entries = [
        AuditLogEntry(
            id=str(r.id),
            action=r.action,
            user_id=r.user_id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            details=r.details or {},
            created_at=r.created_at,
        )
        for r in records
    ]
    return AuditLogResponse(entries=entries, total=total)


# --- Distillation endpoints ---

@router.get("/distillation/stats", response_model=DistillationStatsResponse)
async def get_distillation_stats(db=Depends(get_db)):
    """Get distillation collection stats."""
    collector = DistillationCollector(db)
    result = await collector.get_stats()
    return DistillationStatsResponse(**result)


@router.post("/distillation/export", response_model=ExportResponse)
async def export_training_data(request: ExportRequest, db=Depends(get_db)):
    """Export high-quality records as training data."""
    collector = DistillationCollector(db)
    data = await collector.export_training_data(
        min_confidence=request.min_confidence,
        min_feedback=request.min_feedback,
        format=request.format,
    )
    record_count = len(data.strip().split("\n")) if data.strip() else 0
    return ExportResponse(data=data, record_count=record_count)

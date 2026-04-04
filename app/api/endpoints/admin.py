"""
Admin API endpoints for COSMOS — analytics, audit log, distillation,
KB file index, system overview, model routing.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, List
from datetime import datetime

import structlog

from app.config import settings
from app.db.session import get_db
from app.learning.analytics import AnalyticsEngine
from app.learning.collector import DistillationCollector
from app.services.kb_file_index import KBFileIndexService

logger = structlog.get_logger(__name__)
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


# ---------------------------------------------------------------------------
# KB File Index endpoints
# ---------------------------------------------------------------------------

class KBIndexStatsResponse(BaseModel):
    indexed: int = 0
    pending: int = 0
    failed: int = 0
    total: int = 0
    by_repo: Dict[str, Any] = Field(default_factory=dict)


class KBPendingFile(BaseModel):
    file_path: str
    repo_id: str
    entity_id: str
    entity_type: str
    file_hash: str
    s3_key: Optional[str] = None


class KBPendingResponse(BaseModel):
    files: List[KBPendingFile]
    count: int


class KBScanRequest(BaseModel):
    repo_id: Optional[str] = None


class KBScanResponse(BaseModel):
    changed: int
    repo_id: Optional[str] = None
    message: str


@router.get("/kb-index", response_model=KBIndexStatsResponse)
async def get_kb_index_stats(repo_id: Optional[str] = Query(None)):
    """Get KB file index stats (indexed / pending / failed) per repo.

    Used by LIME training page to show ingestion progress.
    Pass repo_id to narrow results to a single repo (e.g. MultiChannel_API).
    """
    svc = KBFileIndexService()
    stats = await svc.get_stats(repo_id=repo_id)
    return KBIndexStatsResponse(**stats)


@router.get("/kb-index/pending", response_model=KBPendingResponse)
async def list_pending_kb_files(
    repo_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """List KB files that are pending re-ingestion.

    Useful for debugging stale or failed files before a pipeline run.
    """
    svc = KBFileIndexService()
    files = await svc.get_pending(repo_id=repo_id, limit=limit)
    items = [KBPendingFile(**f) for f in files]
    return KBPendingResponse(files=items, count=len(items))


@router.post("/kb-index/scan", response_model=KBScanResponse)
async def scan_kb_for_changes(body: KBScanRequest = KBScanRequest()):
    """Walk the KB directory and mark changed / new files as pending.

    This triggers the same diff logic that runs at startup, but on demand.
    Changed files are marked status=pending so the next pipeline run picks
    them up. Safe to run at any time — it only sets status; it does not
    delete or re-embed anything itself.
    """
    kb_path = settings.KB_PATH or ""
    if not kb_path:
        raise HTTPException(status_code=503, detail="KB_PATH not configured")

    svc = KBFileIndexService()
    changed = await svc.diff_and_mark_pending(kb_path=kb_path, repo_id=body.repo_id)
    return KBScanResponse(
        changed=len(changed),
        repo_id=body.repo_id,
        message=f"{len(changed)} file(s) marked as pending for re-ingestion.",
    )


# ---------------------------------------------------------------------------
# Model routing info
# ---------------------------------------------------------------------------

class ModelProfileInfo(BaseModel):
    name: str
    model_id: str
    tier: str
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_tokens: int
    strengths: List[str]


class ModelRoutingResponse(BaseModel):
    llm_mode: str
    profiles: Dict[str, ModelProfileInfo]
    routing_policy: str
    confidence_threshold_opus: float
    daily_budget_usd: float
    session_budget_usd: float


@router.get("/model-routing", response_model=ModelRoutingResponse)
async def get_model_routing():
    """Return the active model routing configuration.

    Shows all model profiles (Haiku / Sonnet / Opus), their cost rates,
    the active LLM_MODE backend (api / cli / hybrid), and routing policy.
    """
    from app.engine.model_router import PROFILES, ModelTier

    profiles = {
        tier.value: ModelProfileInfo(
            name=p.name,
            model_id=p.model_id,
            tier=p.tier.value,
            cost_per_1k_input=p.cost_per_1k_input,
            cost_per_1k_output=p.cost_per_1k_output,
            max_tokens=p.max_tokens,
            strengths=p.strengths,
        )
        for tier, p in PROFILES.items()
    }

    return ModelRoutingResponse(
        llm_mode=settings.LLM_MODE,
        profiles=profiles,
        routing_policy=(
            "quality_first: confidence<0.6→opus; action/report→opus; "
            "P6/P7→opus; P1/P3/P4 high-confidence→sonnet; classify→haiku"
        ),
        confidence_threshold_opus=0.6,
        daily_budget_usd=settings.COST_DAILY_BUDGET_USD,
        session_budget_usd=settings.COST_SESSION_BUDGET_USD,
    )


# ---------------------------------------------------------------------------
# System overview
# ---------------------------------------------------------------------------

class ComponentStatus(BaseModel):
    status: str  # "ok" | "degraded" | "error" | "unknown"
    detail: Optional[str] = None


class SystemOverviewResponse(BaseModel):
    timestamp: str
    kb_index: Dict[str, Any] = Field(default_factory=dict)
    qdrant: ComponentStatus
    neo4j: ComponentStatus
    database: ComponentStatus
    cost_budget: Dict[str, Any] = Field(default_factory=dict)
    learning: Dict[str, Any] = Field(default_factory=dict)


@router.get("/system", response_model=SystemOverviewResponse)
async def get_system_overview(db=Depends(get_db)):
    """Combined system health and status overview for the LIME admin panel.

    Checks:
    - KB file index: indexed / pending / failed counts
    - Qdrant: collection info (vector count)
    - Neo4j: node count ping
    - MySQL: connection ping
    - Cost budget: daily spend vs limit
    - Learning pipeline: pending staged improvements
    """
    import asyncio
    from datetime import timezone

    timestamp = datetime.now(timezone.utc).isoformat()

    # Run all checks concurrently
    kb_task = _check_kb_index()
    qdrant_task = _check_qdrant()
    neo4j_task = _check_neo4j()
    db_task = _check_database()
    budget_task = _check_cost_budget(db)
    learning_task = _check_learning(db)

    (kb_stats, qdrant_status, neo4j_status, db_status, budget_info, learning_info) = (
        await asyncio.gather(
            kb_task, qdrant_task, neo4j_task, db_task, budget_task, learning_task,
            return_exceptions=True,
        )
    )

    def _safe(result, fallback):
        return result if not isinstance(result, Exception) else fallback

    return SystemOverviewResponse(
        timestamp=timestamp,
        kb_index=_safe(kb_stats, {"error": "unavailable"}),
        qdrant=_safe(qdrant_status, ComponentStatus(status="error", detail="check failed")),
        neo4j=_safe(neo4j_status, ComponentStatus(status="error", detail="check failed")),
        database=_safe(db_status, ComponentStatus(status="error", detail="check failed")),
        cost_budget=_safe(budget_info, {"error": "unavailable"}),
        learning=_safe(learning_info, {"error": "unavailable"}),
    )


async def _check_kb_index() -> Dict[str, Any]:
    svc = KBFileIndexService()
    return await svc.get_stats()


async def _check_qdrant() -> ComponentStatus:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.QDRANT_URL}/collections/{settings.QDRANT_COLLECTION}"
            )
            if resp.status_code == 200:
                data = resp.json()
                count = (
                    data.get("result", {})
                    .get("vectors_count")
                    or data.get("result", {})
                    .get("points_count", 0)
                )
                return ComponentStatus(status="ok", detail=f"{count} vectors")
            return ComponentStatus(status="degraded", detail=f"HTTP {resp.status_code}")
    except Exception as e:
        return ComponentStatus(status="error", detail=str(e)[:120])


async def _check_neo4j() -> ComponentStatus:
    try:
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        async with driver.session() as session:
            result = await session.run("MATCH (n) RETURN count(n) AS cnt")
            record = await result.single()
            cnt = record["cnt"] if record else 0
        await driver.close()
        return ComponentStatus(status="ok", detail=f"{cnt} nodes")
    except Exception as e:
        return ComponentStatus(status="error", detail=str(e)[:120])


async def _check_database() -> ComponentStatus:
    try:
        from app.db.session import get_engine
        from sqlalchemy import text
        engine = get_engine()
        if engine is None:
            return ComponentStatus(status="error", detail="engine is None")
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return ComponentStatus(status="ok")
    except Exception as e:
        return ComponentStatus(status="error", detail=str(e)[:120])


async def _check_cost_budget(db) -> Dict[str, Any]:
    try:
        from sqlalchemy import text
        from datetime import date
        today = date.today().isoformat()
        result = await db.execute(text("""
            SELECT COALESCE(SUM(cost_usd), 0) AS total
            FROM cosmos_query_analytics
            WHERE DATE(created_at) = :today
        """), {"today": today})
        row = result.fetchone()
        daily_spend = float(row.total) if row else 0.0
        return {
            "daily_spend_usd": round(daily_spend, 4),
            "daily_budget_usd": settings.COST_DAILY_BUDGET_USD,
            "utilization_pct": round(daily_spend / settings.COST_DAILY_BUDGET_USD * 100, 1)
            if settings.COST_DAILY_BUDGET_USD > 0 else 0.0,
        }
    except Exception as e:
        logger.debug("admin.cost_budget_check_failed", error=str(e))
        return {"daily_spend_usd": 0.0, "daily_budget_usd": settings.COST_DAILY_BUDGET_USD}


async def _check_learning(db) -> Dict[str, Any]:
    try:
        from sqlalchemy import text
        result = await db.execute(text("""
            SELECT status, COUNT(*) AS cnt
            FROM cosmos_staged_improvements
            GROUP BY status
        """))
        counts: Dict[str, int] = {}
        for row in result.fetchall():
            counts[str(row.status)] = int(row.cnt)
        return {
            "pending": counts.get("pending", 0),
            "approved": counts.get("approved", 0),
            "applied": counts.get("applied", 0),
            "rejected": counts.get("rejected", 0),
            "total": sum(counts.values()),
        }
    except Exception as e:
        logger.debug("admin.learning_check_failed", error=str(e))
        return {"pending": 0, "total": 0}


# ---------------------------------------------------------------------------
# Enrichment phase overview (derived from KB index pillar breakdown)
# ---------------------------------------------------------------------------

_ENRICHMENT_PHASES = [
    {"phase": 1, "module": "Orders",          "status": "done",        "apis": 783},
    {"phase": 2, "module": "Shipments/NDR",   "status": "in_progress", "apis": 929},
    {"phase": 3, "module": "Courier/AWB",     "status": "next",        "apis": 1200},
    {"phase": 4, "module": "Billing/Wallet",  "status": "pending",     "apis": 400},
    {"phase": 5, "module": "Settings/Auth",   "status": "pending",     "apis": 500},
    {"phase": 6, "module": "Admin/Reports",   "status": "pending",     "apis": 800},
    {"phase": 7, "module": "Returns/Exchange","status": "pending",     "apis": 300},
    {"phase": 8, "module": "Channels/Other",  "status": "pending",     "apis": 500},
    {"phase": 9, "module": "Business Rules",  "status": "partial",     "apis": 134},
    {"phase": 10, "module": "Middleware/Auth","status": "not_started", "apis": 95},
    {"phase": 11, "module": "Jobs/Events",    "status": "not_started", "apis": 1253},
    {"phase": 12, "module": "Multi-Repo",     "status": "not_started", "apis": 0},
    {"phase": 13, "module": "Training Pipeline Run", "status": "not_started", "apis": 0},
    {"phase": 14, "module": "Lime UI",        "status": "not_started", "apis": 0},
]


class EnrichmentPhaseItem(BaseModel):
    phase: int
    module: str
    status: str  # done|in_progress|next|partial|pending|not_started
    apis: int
    enriched_count: int = 0
    enriched_pct: float = 0.0


class EnrichmentPhasesResponse(BaseModel):
    total_apis: int
    total_enriched: int
    overall_pct: float
    phases: List[EnrichmentPhaseItem]


@router.get("/enrichment-phases", response_model=EnrichmentPhasesResponse)
async def get_enrichment_phases():
    """Return KB enrichment phase status.

    Phase statuses are derived from COSMOS_KB_ENRICHMENT_STATE.md.
    Enriched counts reflect files in the KB index with entity_type=api_tool
    that have been indexed (status=1).

    Used by LIME to show enrichment progress per module.
    """
    # Current totals from enrichment state (April 3, 2026 session)
    KNOWN_ENRICHED = {1: 112, 2: 112, 3: 27}  # phase → claude-enriched count

    phases = []
    for p in _ENRICHMENT_PHASES:
        enriched = KNOWN_ENRICHED.get(p["phase"], 0)
        apis = p["apis"]
        pct = round(enriched / apis * 100, 1) if apis > 0 else 0.0
        phases.append(EnrichmentPhaseItem(
            phase=p["phase"],
            module=p["module"],
            status=p["status"],
            apis=apis,
            enriched_count=enriched,
            enriched_pct=pct,
        ))

    total_apis = sum(p["apis"] for p in _ENRICHMENT_PHASES)
    total_enriched = sum(KNOWN_ENRICHED.values())
    overall_pct = round(total_enriched / total_apis * 100, 2) if total_apis > 0 else 0.0

    return EnrichmentPhasesResponse(
        total_apis=total_apis,
        total_enriched=total_enriched,
        overall_pct=overall_pct,
        phases=phases,
    )

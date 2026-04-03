"""
Learning API — Goal 5: Continuous Learning review endpoints.

Used by LIME to:
  - Display staged improvements for human review
  - Approve / reject proposals
  - Trigger manual continuous learning run
  - View latest eval report
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import StagedImprovement, StagedImprovementStatus, StagedImprovementType
from app.db.session import AsyncSessionLocal, get_db
from app.services.kb_eval import KBEvaluator

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class StagedImprovementItem(BaseModel):
    id: str
    type: str
    status: str
    source_query: Optional[str] = None
    source_domain: Optional[str] = None
    source_confidence: Optional[float] = None
    proposed_entity_id: Optional[str] = None
    proposed_pillar: Optional[str] = None
    proposed_content: Optional[Dict[str, Any]] = None
    proposed_rationale: Optional[str] = None
    eval_recall_before: Optional[float] = None
    eval_domain: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_note: Optional[str] = None
    created_at: str


class StagedImprovementListResponse(BaseModel):
    items: List[StagedImprovementItem]
    total: int
    pending: int
    approved: int
    rejected: int


class ReviewRequest(BaseModel):
    reviewer: str
    note: Optional[str] = None


class ContinuousRunResponse(BaseModel):
    status: str
    message: str


class EvalRecallResponse(BaseModel):
    recall_at_5: float
    recall_at_1: float
    recall_at_3: float
    tool_accuracy: float
    domain_accuracy: float
    seeds_run: int
    weak_domains: List[str]
    by_domain: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _improvement_to_item(imp: StagedImprovement) -> StagedImprovementItem:
    """Convert a StagedImprovement ORM object to response schema."""
    content = None
    if imp.proposed_content:
        try:
            content = json.loads(imp.proposed_content)
        except (json.JSONDecodeError, TypeError):
            content = {"raw": imp.proposed_content}

    return StagedImprovementItem(
        id=str(imp.id),
        type=imp.improvement_type.value if hasattr(imp.improvement_type, "value") else str(imp.improvement_type),
        status=imp.status.value if hasattr(imp.status, "value") else str(imp.status),
        source_query=imp.source_query,
        source_domain=imp.source_domain,
        source_confidence=imp.source_confidence,
        proposed_entity_id=imp.proposed_entity_id,
        proposed_pillar=imp.proposed_pillar,
        proposed_content=content,
        proposed_rationale=imp.proposed_rationale,
        eval_recall_before=imp.eval_recall_before,
        eval_domain=imp.eval_domain,
        reviewed_by=imp.reviewed_by,
        reviewed_at=imp.reviewed_at.isoformat() if imp.reviewed_at else None,
        review_note=imp.review_note,
        created_at=imp.created_at.isoformat() if imp.created_at else "",
    )


async def _run_continuous_learning_background(
    anthropic_api_key: str,
    kb_path: str,
    vectorstore,
) -> None:
    """Background task: run continuous learning cycle with its own DB session."""
    from app.learning.continuous import ContinuousLearningOrchestrator

    try:
        async with AsyncSessionLocal() as session:
            orchestrator = ContinuousLearningOrchestrator(
                db_session=session,
                vectorstore=vectorstore,
                anthropic_api_key=anthropic_api_key,
                kb_path=kb_path,
            )
            result = await orchestrator.run(
                eval_sample_size=100,
                max_proposals=10,
                triggered_by="api",
            )
            logger.info(
                "learning.background_run_complete",
                run_id=result.run_id,
                proposals_saved=result.proposals_saved,
                improvements_applied=result.improvements_applied,
            )
    except Exception as e:
        logger.error("learning.background_run_failed", error=str(e))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/staged", response_model=StagedImprovementListResponse)
async def list_staged_improvements(
    status: Optional[str] = Query(None, description="Filter by status: pending|approved|rejected|applied"),
    improvement_type: Optional[str] = Query(None, description="Filter by type"),
    domain: Optional[str] = Query(None, description="Filter by source domain"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> StagedImprovementListResponse:
    """List staged improvements for human review. Used by LIME feedback panel."""
    try:
        # Build filter conditions
        filters = []
        if status:
            try:
                filters.append(StagedImprovement.status == StagedImprovementStatus(status))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        if improvement_type:
            try:
                filters.append(StagedImprovement.improvement_type == StagedImprovementType(improvement_type))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid improvement_type: {improvement_type}")
        if domain:
            filters.append(StagedImprovement.source_domain == domain)

        # Fetch items
        stmt = (
            select(StagedImprovement)
            .order_by(StagedImprovement.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if filters:
            stmt = stmt.where(*filters)

        result = await db.execute(stmt)
        items = result.scalars().all()

        # Count by status
        count_stmt = select(
            StagedImprovement.status,
            func.count().label("cnt"),
        ).group_by(StagedImprovement.status)
        count_result = await db.execute(count_stmt)
        counts = {row[0]: row[1] for row in count_result.all()}

        def _count(s: StagedImprovementStatus) -> int:
            return counts.get(s, counts.get(s.value, 0))

        total = sum(counts.values())

        return StagedImprovementListResponse(
            items=[_improvement_to_item(i) for i in items],
            total=total,
            pending=_count(StagedImprovementStatus.pending),
            approved=_count(StagedImprovementStatus.approved),
            rejected=_count(StagedImprovementStatus.rejected),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("learning.list_staged_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/staged/{improvement_id}/approve")
async def approve_improvement(
    improvement_id: str,
    request: ReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Approve a staged improvement. Status transitions: pending → approved."""
    try:
        result = await db.execute(
            select(StagedImprovement).where(StagedImprovement.id == improvement_id)
        )
        improvement = result.scalar_one_or_none()

        if not improvement:
            raise HTTPException(status_code=404, detail=f"Staged improvement {improvement_id} not found")

        if improvement.status not in (StagedImprovementStatus.pending, "pending"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve improvement with status: {improvement.status}",
            )

        improvement.status = StagedImprovementStatus.approved
        improvement.reviewed_by = request.reviewer
        improvement.reviewed_at = datetime.now(timezone.utc)
        improvement.review_note = request.note
        improvement.updated_at = datetime.now(timezone.utc)

        await db.commit()

        logger.info(
            "learning.approved",
            improvement_id=improvement_id,
            reviewer=request.reviewer,
        )
        return {
            "id": improvement_id,
            "status": "approved",
            "reviewed_by": request.reviewer,
            "message": "Improvement approved. Will be applied on next continuous learning run.",
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("learning.approve_failed", improvement_id=improvement_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/staged/{improvement_id}/reject")
async def reject_improvement(
    improvement_id: str,
    request: ReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Reject a staged improvement. Status transitions: pending → rejected."""
    try:
        result = await db.execute(
            select(StagedImprovement).where(StagedImprovement.id == improvement_id)
        )
        improvement = result.scalar_one_or_none()

        if not improvement:
            raise HTTPException(status_code=404, detail=f"Staged improvement {improvement_id} not found")

        if improvement.status not in (StagedImprovementStatus.pending, "pending"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reject improvement with status: {improvement.status}",
            )

        improvement.status = StagedImprovementStatus.rejected
        improvement.reviewed_by = request.reviewer
        improvement.reviewed_at = datetime.now(timezone.utc)
        improvement.review_note = request.note
        improvement.updated_at = datetime.now(timezone.utc)

        await db.commit()

        logger.info(
            "learning.rejected",
            improvement_id=improvement_id,
            reviewer=request.reviewer,
        )
        return {
            "id": improvement_id,
            "status": "rejected",
            "reviewed_by": request.reviewer,
            "message": "Improvement rejected.",
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("learning.reject_failed", improvement_id=improvement_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/continuous/run", response_model=ContinuousRunResponse)
async def trigger_continuous_run(
    background_tasks: BackgroundTasks,
    request: Request,
) -> ContinuousRunResponse:
    """Trigger a manual continuous learning run in the background.

    The run will:
      1. Evaluate recall@5 on 100 random seeds
      2. Collect low-confidence traces from last 24h
      3. Generate KB improvement proposals via Opus 4.6
      4. Persist staged improvements (pending review)
      5. Apply any previously approved improvements
    """
    api_key = settings.ANTHROPIC_API_KEY or ""
    kb_path = settings.KB_PATH or ""
    vectorstore = getattr(request.app.state, "vectorstore", None)

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not configured — cannot run continuous learning",
        )

    background_tasks.add_task(
        _run_continuous_learning_background,
        anthropic_api_key=api_key,
        kb_path=kb_path,
        vectorstore=vectorstore,
    )

    logger.info("learning.continuous_run_triggered")
    return ContinuousRunResponse(
        status="started",
        message="Continuous learning run started in background",
    )


@router.get("/continuous/last-run")
async def get_last_run(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Return a summary of the last continuous learning run.

    Derives the summary from the most recently created StagedImprovement records,
    grouped by their creation timestamp (proxy for run batches).
    """
    try:
        # Get the most recent staged improvement as a proxy for the last run
        stmt = (
            select(StagedImprovement)
            .order_by(StagedImprovement.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        latest = result.scalar_one_or_none()

        if not latest:
            return {
                "last_run": None,
                "message": "No continuous learning runs have been executed yet.",
            }

        # Count total staged improvements created in the same minute as the latest
        from sqlalchemy import func
        count_stmt = select(func.count()).select_from(StagedImprovement)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Count by status
        pending_stmt = select(func.count()).select_from(StagedImprovement).where(
            StagedImprovement.status == StagedImprovementStatus.pending
        )
        pending_result = await db.execute(pending_stmt)
        pending = pending_result.scalar() or 0

        approved_stmt = select(func.count()).select_from(StagedImprovement).where(
            StagedImprovement.status == StagedImprovementStatus.approved
        )
        approved_result = await db.execute(approved_stmt)
        approved = approved_result.scalar() or 0

        applied_stmt = select(func.count()).select_from(StagedImprovement).where(
            StagedImprovement.status == StagedImprovementStatus.applied
        )
        applied_result = await db.execute(applied_stmt)
        applied = applied_result.scalar() or 0

        return {
            "last_run": {
                "last_proposal_created_at": latest.created_at.isoformat() if latest.created_at else None,
                "total_staged": total,
                "pending": pending,
                "approved": approved,
                "applied": applied,
                "rejected": total - pending - approved - applied,
            },
        }

    except Exception as e:
        logger.error("learning.last_run_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/eval/recall", response_model=EvalRecallResponse)
async def get_eval_recall(
    request: Request,
    sample_size: int = Query(50, ge=10, le=500, description="Number of eval seeds to sample"),
) -> EvalRecallResponse:
    """Run a quick eval and return recall@5 per domain.

    Uses the vectorstore from app state and the configured KB path.
    Samples `sample_size` seeds from the global eval set.
    """
    vectorstore = getattr(request.app.state, "vectorstore", None)
    kb_path = settings.KB_PATH or ""

    evaluator = KBEvaluator(vectorstore=vectorstore, kb_path=kb_path)

    try:
        report = await evaluator.run_eval(sample_size=sample_size)
    except Exception as e:
        logger.error("learning.eval_recall_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Eval failed: {str(e)}")

    by_domain = {}
    for domain, ds in report.by_domain.items():
        total = max(ds.total, 1)
        by_domain[domain] = {
            "total": ds.total,
            "recall_at_1": round(ds.recall_at_1 / total, 4),
            "recall_at_3": round(ds.recall_at_3 / total, 4),
            "recall_at_5": round(ds.recall_at_5 / total, 4),
            "tool_match": round(ds.tool_match / total, 4),
            "domain_match": round(ds.domain_match / total, 4),
        }

    return EvalRecallResponse(
        recall_at_5=round(report.recall_at_5, 4),
        recall_at_1=round(report.recall_at_1, 4),
        recall_at_3=round(report.recall_at_3, 4),
        tool_accuracy=round(report.tool_accuracy, 4),
        domain_accuracy=round(report.domain_accuracy, 4),
        seeds_run=report.evaluated,
        weak_domains=report.weak_domains,
        by_domain=by_domain,
    )

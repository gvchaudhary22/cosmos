"""
Report agent API endpoints for COSMOS.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List

from app.services.reportagent import ReportAgentService

router = APIRouter(tags=["reports"])

_service = ReportAgentService()


# --- Request / Response Models ---


class GenerateReportRequest(BaseModel):
    repo_id: Optional[str] = Field(None, description="Repository / tenant identifier")
    end_date: Optional[str] = Field(
        None, description="End date in ISO format (defaults to now)"
    )


class ReportSummary(BaseModel):
    id: str
    repo_id: Optional[str] = None
    report_type: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None


class ReportFull(BaseModel):
    id: str
    repo_id: Optional[str] = None
    report_type: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    sections: Dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None
    created_at: Optional[str] = None


# --- Helpers ---


def _parse_end_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO date string to a timezone-aware datetime, or return None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {raw}")


# --- Endpoints ---


@router.post("/weekly", response_model=ReportFull)
async def generate_weekly_report(request: GenerateReportRequest):
    """Generate a weekly analytics report."""
    try:
        end_date = _parse_end_date(request.end_date)
        result = await _service.generate_weekly_report(
            repo_id=request.repo_id,
            end_date=end_date,
        )
        return ReportFull(**result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")


@router.post("/monthly", response_model=ReportFull)
async def generate_monthly_report(request: GenerateReportRequest):
    """Generate a monthly analytics report."""
    try:
        end_date = _parse_end_date(request.end_date)
        result = await _service.generate_monthly_report(
            repo_id=request.repo_id,
            end_date=end_date,
        )
        return ReportFull(**result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")


@router.get("/", response_model=List[ReportSummary])
async def list_reports(
    type: Optional[str] = Query(None, description="Filter by report type (weekly, monthly)"),
    repo_id: Optional[str] = Query(None, description="Filter by repo id"),
    limit: int = Query(20, ge=1, le=100, description="Max reports to return"),
):
    """List stored reports."""
    try:
        reports = await _service.list_reports(
            report_type=type,
            repo_id=repo_id,
            limit=limit,
        )
        return [ReportSummary(**r) for r in reports]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list reports: {exc}")


@router.get("/{report_id}", response_model=ReportFull)
async def get_report(report_id: str):
    """Get a specific report by id."""
    try:
        report = await _service.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        return ReportFull(**report)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to get report: {exc}")


@router.get("/{report_id}/markdown")
async def get_report_markdown(report_id: str):
    """Get a report formatted as Markdown."""
    try:
        report = await _service.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        markdown = _service.format_as_markdown(report)
        return PlainTextResponse(content=markdown, media_type="text/markdown")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to format report: {exc}")

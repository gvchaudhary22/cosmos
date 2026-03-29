"""API endpoints for Phase 2 action approval workflow."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / Response models
# --------------------------------------------------------------------------- #

class ActionRequestBody(BaseModel):
    session_id: str
    tool_name: str
    params: dict = Field(default_factory=dict)
    risk_level: str = "low"
    user_id: str
    user_role: str = "agent"


class ApproveBody(BaseModel):
    approver_id: str
    approver_role: str


class RejectBody(BaseModel):
    rejector_id: str
    reason: str


class ActionResponse(BaseModel):
    id: str
    session_id: str
    tool_name: str
    params: dict
    risk_level: str
    requested_by: str
    status: str
    approved_by: Optional[str] = None
    execution_result: Optional[dict] = None
    rejection_reason: Optional[str] = None


class PendingListResponse(BaseModel):
    pending: List[ActionResponse]
    total: int


class AuditEntry(BaseModel):
    event: str
    request_id: str
    timestamp: str
    # Additional fields vary by event type
    details: dict = Field(default_factory=dict)


class AuditTrailResponse(BaseModel):
    entries: List[dict]
    total: int


# --------------------------------------------------------------------------- #
# Module-level engine references (set via configure())
# --------------------------------------------------------------------------- #

_approval_engine = None
_auditor = None


def configure(approval_engine, auditor):
    """Inject the approval engine and auditor dependencies."""
    global _approval_engine, _auditor
    _approval_engine = approval_engine
    _auditor = auditor


def _get_engine():
    if _approval_engine is None:
        raise HTTPException(status_code=503, detail="Approval engine not configured")
    return _approval_engine


def _get_auditor():
    if _auditor is None:
        raise HTTPException(status_code=503, detail="Auditor not configured")
    return _auditor


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.post("/request", response_model=ActionResponse)
async def request_action(body: ActionRequestBody):
    """Request an action (from chat flow). Creates an approval request."""
    engine = _get_engine()
    auditor = _get_auditor()

    req = await engine.request_action(
        session_id=body.session_id,
        tool_name=body.tool_name,
        params=body.params,
        risk_level=body.risk_level,
        user_id=body.user_id,
        user_role=body.user_role,
    )
    await auditor.log_request(req)

    # If auto-approved, execute and log
    if req.status == "approved":
        result = await engine.execute_action(req)
        await auditor.log_approval(req.id, req.approved_by, "approved")
        await auditor.log_execution(req.id, result, result.get("success", False))

    return _to_response(req)


@router.get("/pending", response_model=PendingListResponse)
async def list_pending(approver_role: str = "admin"):
    """List pending approvals visible to the given role."""
    engine = _get_engine()
    pending = await engine.list_pending(approver_role)
    return PendingListResponse(
        pending=[_to_response(r) for r in pending],
        total=len(pending),
    )


@router.post("/{request_id}/approve", response_model=ActionResponse)
async def approve_action(request_id: str, body: ApproveBody):
    """Approve a pending action request."""
    engine = _get_engine()
    auditor = _get_auditor()

    try:
        req = await engine.approve(
            request_id=request_id,
            approver_id=body.approver_id,
            approver_role=body.approver_role,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    await auditor.log_approval(request_id, body.approver_id, "approved")
    if req.execution_result:
        await auditor.log_execution(
            request_id, req.execution_result, req.execution_result.get("success", False)
        )

    return _to_response(req)


@router.post("/{request_id}/reject", response_model=ActionResponse)
async def reject_action(request_id: str, body: RejectBody):
    """Reject a pending action request."""
    engine = _get_engine()
    auditor = _get_auditor()

    try:
        req = await engine.reject(
            request_id=request_id,
            rejector_id=body.rejector_id,
            reason=body.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await auditor.log_approval(request_id, body.rejector_id, "rejected", reason=body.reason)
    return _to_response(req)


@router.get("/{request_id}", response_model=ActionResponse)
async def get_action(request_id: str):
    """Get action details by ID."""
    engine = _get_engine()
    req = await engine.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Action request '{request_id}' not found")
    return _to_response(req)


@router.get("/audit/trail", response_model=AuditTrailResponse)
async def get_audit_trail(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 100,
):
    """Get audit trail with optional filters."""
    auditor = _get_auditor()
    entries = await auditor.get_audit_trail(
        session_id=session_id,
        user_id=user_id,
        limit=limit,
    )
    return AuditTrailResponse(entries=entries, total=len(entries))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _to_response(req) -> ActionResponse:
    return ActionResponse(
        id=req.id,
        session_id=req.session_id,
        tool_name=req.tool_name,
        params=req.params,
        risk_level=req.risk_level,
        requested_by=req.requested_by,
        status=req.status,
        approved_by=req.approved_by,
        execution_result=req.execution_result,
        rejection_reason=req.rejection_reason,
    )

"""
Approval engine for COSMOS Phase 2 write actions.

Manages the lifecycle of action requests:
  request -> (auto-approve | pending) -> approve/reject -> execute -> audit
"""

import uuid
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Role hierarchy for approval
# --------------------------------------------------------------------------- #

ROLE_HIERARCHY: Dict[str, int] = {
    "agent": 0,
    "support_agent": 0,
    "supervisor": 1,
    "support_admin": 2,
    "manager": 2,
    "admin": 3,
}

# Minimum role level required per risk level
RISK_APPROVAL_LEVEL: Dict[str, int] = {
    "low": 1,       # supervisor+
    "medium": 1,    # supervisor+
    "high": 2,      # manager+
    "critical": 3,  # admin only
}


def _role_level(role: str) -> int:
    return ROLE_HIERARCHY.get(role, 0)


# --------------------------------------------------------------------------- #
# ActionRequest dataclass
# --------------------------------------------------------------------------- #

@dataclass
class ActionRequest:
    id: str
    session_id: str
    tool_name: str
    params: dict
    risk_level: str        # low / medium / high / critical
    requested_by: str      # user_id of the requester
    status: str = "pending"  # pending / approved / rejected / executed / failed / expired
    created_at: datetime = field(default_factory=datetime.utcnow)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    execution_result: Optional[dict] = None
    rejection_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# ApprovalEngine
# --------------------------------------------------------------------------- #

class ApprovalEngine:
    """Manages action approval lifecycle."""

    def __init__(self, tool_registry=None, mcapi_client=None):
        self.tool_registry = tool_registry
        self.mcapi = mcapi_client
        # In-memory store (swap for DB adapter in production)
        self._requests: Dict[str, ActionRequest] = {}

    # ------------------------------------------------------------------ #
    # Request
    # ------------------------------------------------------------------ #

    async def request_action(
        self,
        session_id: str,
        tool_name: str,
        params: dict,
        risk_level: str,
        user_id: str,
        user_role: str = "agent",
        dry_run: bool = False,
    ) -> ActionRequest:
        """
        Create an approval request.

        Auto-approve low-risk actions for supervisor+ roles.
        Agents can NEVER self-approve.
        """
        request_id = str(uuid.uuid4())
        req = ActionRequest(
            id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            params=params,
            risk_level=risk_level,
            requested_by=user_id,
        )

        # Check if the requester's role can auto-approve
        requester_level = _role_level(user_role)
        required_level = RISK_APPROVAL_LEVEL.get(risk_level, 3)

        # Agents can NEVER self-approve any write action
        if user_role in ("agent", "support_agent"):
            req.status = "pending"
        elif requester_level >= required_level:
            # Auto-approve: role is high enough
            req.status = "approved"
            req.approved_by = user_id
            req.approved_at = datetime.utcnow()
            logger.info(
                "approval.auto_approved",
                request_id=request_id,
                tool=tool_name,
                risk=risk_level,
                role=user_role,
            )
        else:
            req.status = "pending"

        # Dry-run: mark request as dry_run, don't store for real approval
        if dry_run:
            req.status = f"dry_run:{req.status}"  # e.g., "dry_run:approved" or "dry_run:pending"
            logger.info("approval.dry_run", request_id=request_id, tool=tool_name,
                         risk=risk_level, would_be=req.status.replace("dry_run:", ""))

        self._requests[request_id] = req

        logger.info(
            "approval.request_created",
            request_id=request_id,
            tool=tool_name,
            risk=risk_level,
            status=req.status,
        )
        return req

    # ------------------------------------------------------------------ #
    # Approve
    # ------------------------------------------------------------------ #

    async def approve(
        self, request_id: str, approver_id: str, approver_role: str
    ) -> ActionRequest:
        """Approve a pending action request and execute it."""
        req = self._requests.get(request_id)
        if req is None:
            raise ValueError(f"Action request '{request_id}' not found")
        if req.status != "pending":
            raise ValueError(
                f"Cannot approve request in status '{req.status}'"
            )

        # Agents cannot approve anything
        if approver_role in ("agent", "support_agent"):
            raise PermissionError("Agent role cannot approve actions")

        # Validate approver has sufficient role level
        required_level = RISK_APPROVAL_LEVEL.get(req.risk_level, 3)
        approver_level = _role_level(approver_role)

        if approver_level < required_level:
            raise PermissionError(
                f"Role '{approver_role}' (level {approver_level}) cannot approve "
                f"'{req.risk_level}' risk actions (requires level {required_level})"
            )

        req.status = "approved"
        req.approved_by = approver_id
        req.approved_at = datetime.utcnow()

        logger.info(
            "approval.approved",
            request_id=request_id,
            approver=approver_id,
            role=approver_role,
        )

        # Execute immediately after approval
        result = await self.execute_action(req)
        return req

    # ------------------------------------------------------------------ #
    # Reject
    # ------------------------------------------------------------------ #

    async def reject(
        self, request_id: str, rejector_id: str, reason: str
    ) -> ActionRequest:
        """Reject a pending action request."""
        req = self._requests.get(request_id)
        if req is None:
            raise ValueError(f"Action request '{request_id}' not found")
        if req.status != "pending":
            raise ValueError(
                f"Cannot reject request in status '{req.status}'"
            )

        req.status = "rejected"
        req.approved_by = rejector_id  # stores who acted on it
        req.approved_at = datetime.utcnow()
        req.rejection_reason = reason

        logger.info(
            "approval.rejected",
            request_id=request_id,
            rejector=rejector_id,
            reason=reason,
        )
        return req

    # ------------------------------------------------------------------ #
    # Execute
    # ------------------------------------------------------------------ #

    async def execute_action(self, request: ActionRequest, dry_run: bool = False) -> dict:
        """Execute the approved action via the tool registry.

        When dry_run=True: validates the full approval chain and tool lookup
        but returns a mock success response WITHOUT calling the external API.
        """
        # Dry-run accepts dry_run: prefixed statuses
        valid_statuses = ("approved",)
        if dry_run:
            valid_statuses = ("approved", "dry_run:approved")

        if request.status not in valid_statuses and not request.status.startswith("dry_run:"):
            raise ValueError(
                f"Cannot execute request in status '{request.status}'"
            )

        try:
            tool = self.tool_registry.get(request.tool_name) if self.tool_registry else None
            if tool is None:
                raise ValueError(f"Tool '{request.tool_name}' not found in registry")

            if dry_run:
                # Mock execution: validate everything but don't call external API
                request.status = "dry_run:executed"
                request.execution_result = {
                    "success": True,
                    "dry_run": True,
                    "data": {"mock": True, "tool": request.tool_name, "params": request.params},
                    "message": f"Dry-run: {request.tool_name} would execute with params {list(request.params.keys())}",
                }
                logger.info("approval.dry_run_executed", request_id=request.id,
                             tool=request.tool_name)
                return request.execution_result

            context = {"approved": True}
            result = await tool.execute(request.params, context=context)

            if result.success:
                request.status = "executed"
                request.execution_result = {"success": True, "data": result.data}
            else:
                request.status = "failed"
                request.execution_result = {"success": False, "error": result.error}

            logger.info(
                "approval.executed",
                request_id=request.id,
                tool=request.tool_name,
                success=result.success,
            )
            return request.execution_result

        except Exception as exc:
            request.status = "failed"
            request.execution_result = {"success": False, "error": str(exc)}
            logger.error(
                "approval.execution_error",
                request_id=request.id,
                error=str(exc),
            )
            return request.execution_result

    # ------------------------------------------------------------------ #
    # List pending
    # ------------------------------------------------------------------ #

    async def list_pending(self, approver_role: str) -> List[ActionRequest]:
        """List pending approvals visible to this role."""
        approver_level = _role_level(approver_role)
        result = []
        for req in self._requests.values():
            if req.status != "pending":
                continue
            required_level = RISK_APPROVAL_LEVEL.get(req.risk_level, 3)
            if approver_level >= required_level:
                result.append(req)
        return sorted(result, key=lambda r: r.created_at)

    # ------------------------------------------------------------------ #
    # Get single request
    # ------------------------------------------------------------------ #

    async def get_request(self, request_id: str) -> Optional[ActionRequest]:
        """Get a single action request by ID."""
        return self._requests.get(request_id)

    # ------------------------------------------------------------------ #
    # Expire stale
    # ------------------------------------------------------------------ #

    async def expire_stale(self, max_age_minutes: int = 30) -> int:
        """Expire pending requests older than max_age_minutes."""
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        expired_count = 0
        for req in self._requests.values():
            if req.status == "pending" and req.created_at < cutoff:
                req.status = "expired"
                expired_count += 1
                logger.info("approval.expired", request_id=req.id)
        return expired_count

"""
Action audit trail for COSMOS Phase 2.

Logs every action attempt, approval decision, execution result, and provides
query access to the audit history.
"""

import structlog
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.engine.approval import ActionRequest

logger = structlog.get_logger()


class ActionAuditor:
    """Logs every action attempt, approval, execution, and result."""

    def __init__(self):
        # In-memory store (swap for DB adapter in production)
        self._entries: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Log request creation
    # ------------------------------------------------------------------ #

    async def log_request(self, action_request: ActionRequest) -> None:
        """Log that an action was requested."""
        entry = {
            "event": "action_requested",
            "request_id": action_request.id,
            "session_id": action_request.session_id,
            "tool_name": action_request.tool_name,
            "params": action_request.params,
            "risk_level": action_request.risk_level,
            "requested_by": action_request.requested_by,
            "status": action_request.status,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._entries.append(entry)
        logger.info(
            "audit.action_requested",
            request_id=action_request.id,
            tool_name=action_request.tool_name,
            risk_level=action_request.risk_level,
        )

    # ------------------------------------------------------------------ #
    # Log approval / rejection
    # ------------------------------------------------------------------ #

    async def log_approval(
        self, request_id: str, approver: str, decision: str, reason: str = None
    ) -> None:
        """Log an approval or rejection decision."""
        entry = {
            "event": "action_decision",
            "request_id": request_id,
            "approver": approver,
            "decision": decision,  # "approved" or "rejected"
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._entries.append(entry)
        logger.info(
            "audit.action_decision",
            request_id=request_id,
            approver=approver,
            decision=decision,
        )

    # ------------------------------------------------------------------ #
    # Log execution result
    # ------------------------------------------------------------------ #

    async def log_execution(
        self, request_id: str, result: dict, success: bool
    ) -> None:
        """Log the execution outcome of an approved action."""
        entry = {
            "event": "action_executed",
            "request_id": request_id,
            "success": success,
            "result": result,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._entries.append(entry)
        logger.info(
            "audit.action_executed",
            request_id=request_id,
            success=success,
        )

    # ------------------------------------------------------------------ #
    # Query audit trail
    # ------------------------------------------------------------------ #

    async def get_audit_trail(
        self,
        session_id: str = None,
        user_id: str = None,
        request_id: str = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query the audit trail with optional filters.

        At least one filter is recommended to avoid returning everything.
        """
        results = []
        for entry in reversed(self._entries):  # newest first
            if session_id and entry.get("session_id") != session_id:
                continue
            if user_id:
                if (
                    entry.get("requested_by") != user_id
                    and entry.get("approver") != user_id
                ):
                    continue
            if request_id and entry.get("request_id") != request_id:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

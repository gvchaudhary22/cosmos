import structlog
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import AuditLog

logger = structlog.get_logger()


class AuditLogger:
    """Log every action to icrm_audit_log for compliance."""

    async def log(
        self,
        session: AsyncSession,
        user_id: int,
        action: str,
        tool_name: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        cedar_decision: Optional[Dict[str, Any]] = None,
    ):
        entry = AuditLog(
            user_id=str(user_id),
            action=action,
            resource_type=tool_name,
            details={
                "params": params or {},
                "result": result or {},
                "cedar_decision": cedar_decision or {},
            },
        )
        session.add(entry)
        await session.commit()
        logger.info("audit_logged", action=action, tool=tool_name, user=user_id)

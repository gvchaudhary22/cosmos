"""
Bridge API endpoints -- Primary interface for MARS<->COSMOS communication.

POST /cosmos/api/v1/bridge/process  -- Full MARS->COSMOS flow
GET  /cosmos/api/v1/bridge/stats    -- Bridge statistics
GET  /cosmos/api/v1/bridge/health   -- MARS connectivity status
"""

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID, uuid4

from app.config import settings

logger = structlog.get_logger()

router = APIRouter()


class BridgeProcessRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=10000)
    company_id: str
    user_id: str
    channel: str = "mars"
    intent_hint: Optional[str] = None
    entity_hint: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class BridgeProcessResponse(BaseModel):
    session_id: str
    message_id: str
    content: str
    confidence: float
    tools_used: list[str] = Field(default_factory=list)
    escalated: bool = False
    escalation_reason: Optional[str] = None
    total_latency_ms: float = 0.0


@router.post("/process", response_model=BridgeProcessResponse)
async def bridge_process(request_body: BridgeProcessRequest, request: Request):
    """Full MARS->COSMOS flow.

    MARS has already done rule check and determined this query needs AI.
    COSMOS runs its ReAct engine and returns the result.
    """
    import time

    start = time.monotonic()
    session_id = request_body.session_id or str(uuid4())
    message_id = str(uuid4())

    logger.info(
        "bridge.process.start",
        session_id=session_id,
        company_id=request_body.company_id,
        channel=request_body.channel,
    )

    engine = getattr(request.app.state, "react_engine", None)

    if engine is None:
        return BridgeProcessResponse(
            session_id=session_id,
            message_id=message_id,
            content="[COSMOS] ReAct engine not initialized.",
            confidence=0.0,
            escalated=True,
            escalation_reason="Engine not initialized",
        )

    try:
        session_context = {
            "user_id": request_body.user_id,
            "company_id": request_body.company_id,
            "channel": request_body.channel,
        }
        if request_body.intent_hint:
            session_context["intent_hint"] = request_body.intent_hint
        if request_body.entity_hint:
            session_context["entity_hint"] = request_body.entity_hint
        session_context.update(request_body.metadata)

        result = await engine.process(request_body.message, session_context)

        latency_ms = (time.monotonic() - start) * 1000

        return BridgeProcessResponse(
            session_id=session_id,
            message_id=message_id,
            content=result.response,
            confidence=result.confidence,
            tools_used=result.tools_used,
            escalated=result.escalated,
            escalation_reason="Low confidence" if result.escalated else None,
            total_latency_ms=round(latency_ms, 1),
        )
    except Exception as exc:
        logger.error("bridge.process.error", error=str(exc))
        return BridgeProcessResponse(
            session_id=session_id,
            message_id=message_id,
            content="An error occurred during processing.",
            confidence=0.0,
            escalated=True,
            escalation_reason=str(exc),
        )


@router.get("/stats")
async def bridge_stats():
    """Bridge statistics -- MARS vs COSMOS handling ratios."""
    try:
        from app.clients.mars import MarsClient
        from app.middleware.mars_bridge import MarsBridge

        client = MarsClient(
            base_url=settings.MARS_BASE_URL,
            timeout=settings.MARS_TIMEOUT,
        )
        bridge = MarsBridge(client, enabled=settings.MARS_BRIDGE_ENABLED)
        stats = bridge.get_stats()
        await client.close()
        return {"status": "ok", "stats": stats}
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "stats": {"mars_handled": 0, "cosmos_handled": 0, "mars_unavailable": 0},
        }


@router.get("/health")
async def bridge_health():
    """MARS connectivity status."""
    try:
        from app.clients.mars import MarsClient

        client = MarsClient(
            base_url=settings.MARS_BASE_URL,
            timeout=settings.MARS_TIMEOUT,
        )
        healthy = await client.health_check()
        await client.close()
        return {
            "status": "connected" if healthy else "disconnected",
            "mars_url": settings.MARS_BASE_URL,
            "bridge_enabled": settings.MARS_BRIDGE_ENABLED,
        }
    except Exception as exc:
        return {
            "status": "error",
            "mars_url": settings.MARS_BASE_URL,
            "bridge_enabled": settings.MARS_BRIDGE_ENABLED,
            "error": str(exc),
        }

"""
Chat API endpoints for COSMOS.

Provides non-streaming and SSE streaming chat via the ReAct engine.
"""

import json
import time
from uuid import UUID, uuid4
from typing import Optional

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.events.kafka_bus import QueryCompletedEvent

logger = structlog.get_logger()
router = APIRouter()


class ChatRequest(BaseModel):
    session_id: Optional[UUID] = None
    message: str = Field(..., min_length=1, max_length=10000)
    user_id: str
    company_id: Optional[str] = None
    channel: str = "web"
    tournament_mode: bool = False
    metadata: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    session_id: UUID
    message_id: UUID
    role: str = "assistant"
    content: str
    model: Optional[str] = None
    token_count: Optional[int] = None
    tools_used: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    escalated: bool = False
    total_loops: Optional[int] = None


def _get_engine(request: Request):
    """Retrieve the ReActEngine from app.state, or None if not initialized."""
    return getattr(request.app.state, "react_engine", None)


def _get_tournament_engine(request: Request):
    """Retrieve the TournamentEngine from app.state, or None if not initialized."""
    return getattr(request.app.state, "tournament_engine", None)


def _get_event_bus(request: Request):
    """Retrieve the EventBus from app.state, or None."""
    return getattr(request.app.state, "event_bus", None)


async def _emit_query_event(request: Request, chat_req, session_id, result, latency_ms: float):
    """Fire-and-forget: produce a QueryCompletedEvent to Kafka."""
    bus = _get_event_bus(request)
    if bus is None:
        return
    try:
        event = QueryCompletedEvent(
            session_id=str(session_id),
            user_id=chat_req.user_id,
            company_id=chat_req.company_id,
            query=chat_req.message,
            intent=getattr(result, "intent", "unknown"),
            entity=getattr(result, "entity", "unknown"),
            confidence=result.confidence,
            tools_used=result.tools_used,
            response=result.response,
            escalated=result.escalated,
            latency_ms=latency_ms,
            model=getattr(result, "model", "unknown"),
            tokens_in=getattr(result, "tokens_in", 0),
            tokens_out=getattr(result, "tokens_out", 0),
            cost_usd=getattr(result, "cost_usd", 0.0),
        )
        await bus.produce_query_completed(event)
    except Exception as e:
        logger.warning("chat.kafka_emit_failed", error=str(e))


@router.post("", response_model=ChatResponse)
async def chat(request: Request, chat_req: ChatRequest):
    """Send a message and get an AI response via the ReAct engine."""
    engine = _get_engine(request)
    session_id = chat_req.session_id or uuid4()

    if engine is None:
        return ChatResponse(
            session_id=session_id,
            message_id=uuid4(),
            content="[COSMOS] ReAct engine not initialized.",
            model=None,
            token_count=0,
            tools_used=[],
            confidence=0.0,
            escalated=False,
            total_loops=0,
        )

    # Check if tournament mode is requested and available
    if chat_req.tournament_mode:
        tournament = _get_tournament_engine(request)
        if tournament is not None:
            return await _handle_tournament(chat_req, session_id, engine, tournament)

    # Build session context from request fields
    session_context = {
        "user_id": chat_req.user_id,
        "company_id": chat_req.company_id,
        "channel": chat_req.channel,
    }
    session_context.update(chat_req.metadata)

    t0 = time.monotonic()
    result = await engine.process(chat_req.message, session_context)
    latency_ms = (time.monotonic() - t0) * 1000

    # Fire-and-forget Kafka event (distillation + analytics)
    await _emit_query_event(request, chat_req, session_id, result, latency_ms)

    return ChatResponse(
        session_id=session_id,
        message_id=uuid4(),
        content=result.response,
        model=None,
        token_count=None,
        tools_used=result.tools_used,
        confidence=result.confidence,
        escalated=result.escalated,
        total_loops=result.total_loops,
    )


async def _handle_tournament(chat_req, session_id, react_engine, tournament_engine):
    """Run query through the TournamentEngine and return the winning result."""
    # Classify intent/entity via the react engine's classifier for tournament input
    classification = react_engine.classifier.classify(chat_req.message)

    tournament_result = await tournament_engine.run(
        query=chat_req.message,
        intent=classification.intent.value,
        entity=classification.entity.value,
        entity_id=classification.entity_id,
    )

    if tournament_result.winner:
        content = tournament_result.winner.answer
        confidence = tournament_result.winner.confidence
        tools_used = [tournament_result.winner.strategy.value]
    else:
        content = "[COSMOS] Tournament produced no winning strategy."
        confidence = 0.0
        tools_used = []

    return ChatResponse(
        session_id=session_id,
        message_id=uuid4(),
        content=content,
        model=None,
        token_count=None,
        tools_used=tools_used,
        confidence=confidence,
        escalated=confidence < 0.3,
        total_loops=1,
    )


@router.post("/stream")
async def chat_stream(request: Request, chat_req: ChatRequest):
    """
    SSE streaming chat endpoint.

    Streams ReAct engine progress as Server-Sent Events:

    - event: phase     -- processing phase changes (classifying, reasoning, acting...)
    - event: tool      -- tool execution status
    - event: result    -- tool result data
    - event: thinking  -- intermediate reasoning content
    - event: chunk     -- LLM response text chunks
    - event: done      -- final metadata (confidence, tools_used, escalated)
    - event: error     -- error information
    """
    session_id = str(chat_req.session_id or uuid4())
    engine = _get_engine(request)

    async def generate():
        try:
            # --- Phase: classifying ---
            yield _sse("phase", {"phase": "classifying", "session_id": session_id})

            if engine is not None:
                # Build minimal session context from request
                session_context = {
                    "user_id": chat_req.user_id,
                    "company_id": chat_req.company_id,
                    "channel": chat_req.channel,
                }
                session_context.update(chat_req.metadata)

                yield _sse("phase", {"phase": "reasoning"})

                # Process through ReAct engine
                t0 = time.monotonic()
                result = await engine.process(chat_req.message, session_context)
                latency_ms = (time.monotonic() - t0) * 1000

                # Emit tool events
                for step in result.steps:
                    for tr in step.tool_results:
                        yield _sse("tool", {
                            "tool": tr.tool_name,
                            "status": "success" if tr.success else "error",
                            "latency_ms": round(tr.latency_ms, 1),
                        })
                        if tr.success and tr.data is not None:
                            # Serialize tool data safely
                            try:
                                data_payload = tr.data if isinstance(tr.data, (dict, list)) else str(tr.data)
                            except Exception:
                                data_payload = str(tr.data)
                            yield _sse("result", {"tool": tr.tool_name, "data": data_payload})

                yield _sse("thinking", {"content": f"Evaluating confidence: {result.confidence:.2f}"})

                # Stream the final response in chunks
                response_text = result.response
                chunk_size = 50
                for i in range(0, len(response_text), chunk_size):
                    chunk = response_text[i : i + chunk_size]
                    yield _sse("chunk", {"text": chunk})

                # Fire-and-forget Kafka event (distillation + analytics)
                await _emit_query_event(request, chat_req, session_id, result, latency_ms)

                # Done event
                yield _sse("done", {
                    "session_id": session_id,
                    "confidence": result.confidence,
                    "tools_used": result.tools_used,
                    "escalated": result.escalated,
                    "total_loops": result.total_loops,
                    "latency_ms": round(result.total_latency_ms, 1),
                })
            else:
                # No engine available -- stub response
                stub_text = "[COSMOS] ReAct engine not initialized. Returning stub."
                yield _sse("chunk", {"text": stub_text})
                yield _sse("done", {
                    "session_id": session_id,
                    "confidence": 0.0,
                    "tools_used": [],
                    "escalated": False,
                    "total_loops": 0,
                    "latency_ms": 0.0,
                })

        except Exception as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _sse(event: str, data: dict) -> str:
    """Format a single Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

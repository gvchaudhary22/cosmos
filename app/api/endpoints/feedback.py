"""
Feedback API endpoints for COSMOS.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import List, Optional

from app.db.session import get_db
from app.learning.feedback import FeedbackEngine
from app.events.kafka_bus import FeedbackEvent as KafkaFeedbackEvent

logger = structlog.get_logger()

router = APIRouter()


class FeedbackRequest(BaseModel):
    message_id: str
    session_id: str
    score: int = Field(..., ge=1, le=5)
    text: Optional[str] = None
    categories: Optional[List[str]] = None


class FeedbackResponse(BaseModel):
    id: str
    session_id: str
    message_id: str
    score: int
    text: Optional[str] = None
    categories: List[str] = Field(default_factory=list)


class FeedbackSummaryResponse(BaseModel):
    period_days: int
    total_feedback: int
    avg_score: float
    score_distribution: dict = Field(default_factory=dict)
    daily_trend: list = Field(default_factory=list)


class FeedbackEntry(BaseModel):
    id: str
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    score: Optional[int] = None
    text: Optional[str] = None
    categories: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None


@router.post("", response_model=FeedbackResponse)
async def submit_feedback(http_request: Request, request: FeedbackRequest, db=Depends(get_db)):
    """Submit feedback on an AI response. Score 1-5."""
    engine = FeedbackEngine(db)
    try:
        result = await engine.submit_feedback(
            message_id=request.message_id,
            session_id=request.session_id,
            score=request.score,
            text=request.text,
            categories=request.categories,
        )

        # Fire-and-forget Kafka event for DPO training pipeline
        bus = getattr(http_request.app.state, "event_bus", None)
        if bus:
            try:
                kafka_event = KafkaFeedbackEvent(
                    session_id=request.session_id,
                    message_id=request.message_id,
                    rating=request.score,
                    comment=request.text,
                    tags=request.categories or [],
                )
                await bus.produce_feedback(kafka_event)
            except Exception as e:
                logger.warning("feedback.kafka_emit_failed", error=str(e))

        return FeedbackResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/session/{session_id}", response_model=List[FeedbackEntry])
async def get_session_feedback(session_id: str, db=Depends(get_db)):
    """Get all feedback for a session."""
    engine = FeedbackEngine(db)
    return await engine.get_session_feedback(session_id)


@router.get("/summary", response_model=FeedbackSummaryResponse)
async def get_feedback_summary(days: int = 7, db=Depends(get_db)):
    """Get aggregated feedback summary."""
    engine = FeedbackEngine(db)
    result = await engine.get_feedback_summary(days=days)
    return FeedbackSummaryResponse(**result)


@router.get("/low-scoring", response_model=List[FeedbackEntry])
async def get_low_scoring(max_score: int = 2, limit: int = 50, db=Depends(get_db)):
    """Get queries that scored poorly for review."""
    engine = FeedbackEngine(db)
    return await engine.get_low_scoring_queries(max_score=max_score, limit=limit)

"""
Kafka consumer handlers — process events and write to DB.

Each handler receives a deserialized dict from Kafka and persists it
using the appropriate service (DistillationCollector, AnalyticsEngine, etc.)
"""

import uuid
import structlog
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.db.models import (
    DistillationRecord,
    QueryAnalytics,
)

logger = structlog.get_logger()


async def handle_query_completed(event: dict) -> None:
    """Handle cosmos.query.completed events.

    Writes to both:
    1. icrm_distillation_records (training data)
    2. icrm_query_analytics (performance metrics)
    """
    async with AsyncSessionLocal() as db:
        try:
            # 1. Distillation record
            session_id = _safe_uuid(event.get("session_id"))
            distillation = DistillationRecord(
                id=uuid.uuid4(),
                session_id=session_id or uuid.uuid4(),
                user_query=event.get("query", "")[:10000],
                intent=event.get("intent", "unknown"),
                entity=event.get("entity", "unknown"),
                tools_used=event.get("tools_used", []),
                tool_results=[],
                llm_prompt=event.get("query", ""),
                llm_response=event.get("response", ""),
                final_response=event.get("response", ""),
                confidence=event.get("confidence", 0.0),
                model_used=event.get("model", "unknown"),
                token_count_input=event.get("tokens_in", 0),
                token_count_output=event.get("tokens_out", 0),
                cost_usd=event.get("cost_usd", 0.0),
            )
            db.add(distillation)

            # 2. Query analytics
            analytics = QueryAnalytics(
                id=uuid.uuid4(),
                session_id=session_id,
                intent=event.get("intent", "unknown"),
                entity=event.get("entity", "unknown"),
                confidence=event.get("confidence", 0.0),
                latency_ms=event.get("latency_ms", 0.0),
                tools_used=event.get("tools_used", []),
                escalated=event.get("escalated", False),
                model=event.get("model", "unknown"),
                cost_usd=event.get("cost_usd", 0.0),
            )
            db.add(analytics)

            await db.commit()
            logger.debug(
                "event.query_completed.persisted",
                session_id=event.get("session_id"),
                intent=event.get("intent"),
            )
        except Exception as e:
            await db.rollback()
            logger.error("event.query_completed.failed", error=str(e))


async def handle_learning_insight(event: dict) -> None:
    """Handle cosmos.learning.insight events.

    Forwards to KB pipeline for knowledge base updates.
    """
    logger.info(
        "event.learning_insight.received",
        insight_id=event.get("insight_id"),
        learning_type=event.get("learning_type"),
    )
    # The actual KB pipeline integration is handled by the wiring module.
    # This handler ensures the event is logged and could trigger
    # additional processing (e.g., admin notification, Slack alert).


async def handle_feedback(event: dict) -> None:
    """Handle cosmos.feedback.submitted events.

    Links feedback to the distillation record for DPO training data.
    """
    async with AsyncSessionLocal() as db:
        try:
            from sqlalchemy import select, update
            session_id = _safe_uuid(event.get("session_id"))
            if session_id is None:
                return

            # Find the most recent distillation record for this session
            stmt = (
                select(DistillationRecord)
                .where(DistillationRecord.session_id == session_id)
                .order_by(DistillationRecord.created_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            record = result.scalar_one_or_none()

            if record:
                record.feedback_score = event.get("rating")
                record.feedback_text = event.get("comment")
                await db.commit()
                logger.info(
                    "event.feedback.linked_to_distillation",
                    session_id=str(session_id),
                    rating=event.get("rating"),
                )
        except Exception as e:
            await db.rollback()
            logger.error("event.feedback.failed", error=str(e))


async def handle_kb_updated(event: dict) -> None:
    """Handle cosmos.kb.updated events.

    Logs KB update events for audit trail. Could trigger
    downstream consumers (analytics, MARS notification).
    """
    logger.info(
        "event.kb_updated.received",
        update_count=event.get("update_count"),
        source=event.get("source"),
    )


def _safe_uuid(val) -> uuid.UUID:
    """Convert string to UUID, return None if invalid."""
    if val is None:
        return None
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return None

"""
Feedback engine for COSMOS.

Handles agent feedback on AI responses — scoring, categorization, and trend analysis.
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from cosmos.app.db.models import Feedback as FeedbackModel

logger = structlog.get_logger()

VALID_CATEGORIES = {"accurate", "helpful", "fast", "wrong", "irrelevant", "slow"}


class FeedbackEngine:
    """Handles agent feedback on AI responses."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def submit_feedback(
        self,
        message_id: str,
        session_id: str,
        score: int,
        text: str = None,
        categories: List[str] = None,
    ) -> dict:
        """Submit feedback. Score 1-5. Categories: accurate/helpful/fast/wrong/irrelevant/slow."""
        if score < 1 or score > 5:
            raise ValueError("Feedback score must be between 1 and 5")

        if categories:
            invalid = set(categories) - VALID_CATEGORIES
            if invalid:
                raise ValueError(f"Invalid categories: {invalid}. Valid: {VALID_CATEGORIES}")

        feedback_id = str(uuid.uuid4())
        record = FeedbackModel(
            id=uuid.UUID(feedback_id),
            session_id=session_id,
            message_id=message_id,
            rating=score,
            comment=text,
            tags=categories or [],
        )

        try:
            self.db.add(record)
            await self.db.commit()
            logger.info("feedback.submitted", feedback_id=feedback_id, score=score)
        except Exception as exc:
            await self.db.rollback()
            logger.error("feedback.submit_failed", error=str(exc))
            raise

        return {
            "id": feedback_id,
            "session_id": session_id,
            "message_id": message_id,
            "score": score,
            "text": text,
            "categories": categories or [],
        }

    async def get_session_feedback(self, session_id: str) -> List[dict]:
        """Get all feedback for a session."""
        stmt = (
            select(FeedbackModel)
            .where(FeedbackModel.session_id == session_id)
            .order_by(FeedbackModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        records = result.scalars().all()

        return [
            {
                "id": str(r.id),
                "session_id": str(r.session_id),
                "message_id": str(r.message_id) if r.message_id else None,
                "score": r.rating,
                "text": r.comment,
                "categories": r.tags or [],
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]

    async def get_feedback_summary(self, days: int = 7) -> dict:
        """Aggregated feedback: avg score, category breakdown, trend."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Average score
        avg_stmt = select(func.avg(FeedbackModel.rating)).where(
            FeedbackModel.created_at >= cutoff
        )
        avg_result = await self.db.execute(avg_stmt)
        avg_score = avg_result.scalar() or 0.0

        # Total count
        count_stmt = select(func.count()).select_from(FeedbackModel).where(
            FeedbackModel.created_at >= cutoff
        )
        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Score distribution
        dist_stmt = (
            select(FeedbackModel.rating, func.count())
            .where(FeedbackModel.created_at >= cutoff)
            .group_by(FeedbackModel.rating)
        )
        dist_result = await self.db.execute(dist_stmt)
        score_distribution = {str(row[0]): row[1] for row in dist_result.all()}

        # Daily trend
        trend_stmt = (
            select(
                func.date(FeedbackModel.created_at).label("day"),
                func.avg(FeedbackModel.rating).label("avg_score"),
                func.count().label("count"),
            )
            .where(FeedbackModel.created_at >= cutoff)
            .group_by(func.date(FeedbackModel.created_at))
            .order_by(func.date(FeedbackModel.created_at))
        )
        trend_result = await self.db.execute(trend_stmt)
        trend = [
            {
                "date": str(row.day),
                "avg_score": round(float(row.avg_score), 2),
                "count": row.count,
            }
            for row in trend_result.all()
        ]

        return {
            "period_days": days,
            "total_feedback": total,
            "avg_score": round(float(avg_score), 2),
            "score_distribution": score_distribution,
            "daily_trend": trend,
        }

    async def get_low_scoring_queries(self, max_score: int = 2, limit: int = 50) -> List[dict]:
        """Get queries that scored poorly for review."""
        stmt = (
            select(FeedbackModel)
            .where(FeedbackModel.rating <= max_score)
            .order_by(FeedbackModel.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        records = result.scalars().all()

        return [
            {
                "id": str(r.id),
                "session_id": str(r.session_id) if r.session_id else None,
                "message_id": str(r.message_id) if r.message_id else None,
                "score": r.rating,
                "text": r.comment,
                "categories": r.tags or [],
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]

"""
Analytics engine for COSMOS.

Tracks performance metrics: query volume, confidence, latency, costs, and tool usage.
Uses in-memory buffer with periodic flush to DB.
"""

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QueryAnalytics as QueryAnalyticsModel

logger = structlog.get_logger()


class AnalyticsEngine:
    """Tracks COSMOS performance metrics with in-memory buffer."""

    FLUSH_THRESHOLD = 50  # Flush to DB after this many buffered records

    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        self._buffer: List[dict] = []

    async def record_query(
        self,
        session_id: str,
        intent: str,
        entity: str,
        confidence: float,
        latency_ms: float,
        tools_used: List[str],
        escalated: bool,
        model: str,
        cost_usd: float,
    ) -> None:
        """Record a query for analytics."""
        record = QueryAnalyticsModel(
            id=uuid.uuid4(),
            session_id=uuid.UUID(session_id) if _is_uuid(session_id) else None,
            intent=intent,
            entity=entity,
            confidence=confidence,
            latency_ms=latency_ms,
            tools_used=tools_used or [],
            escalated=escalated,
            model=model,
            cost_usd=cost_usd,
        )

        self._buffer.append(record)

        if len(self._buffer) >= self.FLUSH_THRESHOLD:
            await self._flush()
        else:
            # Single record — write immediately for simplicity
            try:
                self.db.add(record)
                await self.db.commit()
            except Exception as exc:
                await self.db.rollback()
                logger.error("analytics.record_failed", error=str(exc))
                raise

    async def _flush(self) -> None:
        """Flush buffered records to DB."""
        if not self._buffer:
            return
        try:
            self.db.add_all(self._buffer)
            await self.db.commit()
            logger.info("analytics.flushed", count=len(self._buffer))
            self._buffer = []
        except Exception as exc:
            await self.db.rollback()
            logger.error("analytics.flush_failed", error=str(exc))
            self._buffer = []

    async def get_dashboard(self, days: int = 7) -> dict:
        """Dashboard data with query volume, confidence, latency, cost, and breakdown."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Total queries
        total_stmt = select(func.count()).select_from(QueryAnalyticsModel).where(
            QueryAnalyticsModel.created_at >= cutoff
        )
        total = (await self.db.execute(total_stmt)).scalar() or 0

        # Queries per day
        trend_stmt = (
            select(
                func.date(QueryAnalyticsModel.created_at).label("day"),
                func.count().label("count"),
            )
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(func.date(QueryAnalyticsModel.created_at))
            .order_by(func.date(QueryAnalyticsModel.created_at))
        )
        trend = [
            {"date": str(row.day), "count": row.count}
            for row in (await self.db.execute(trend_stmt)).all()
        ]

        # Average confidence
        avg_conf = (
            await self.db.execute(
                select(func.avg(QueryAnalyticsModel.confidence)).where(
                    QueryAnalyticsModel.created_at >= cutoff
                )
            )
        ).scalar() or 0.0

        # Confidence distribution (buckets)
        conf_dist = await self._confidence_distribution(cutoff)

        # Intent breakdown
        intent_stmt = (
            select(QueryAnalyticsModel.intent, func.count())
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(QueryAnalyticsModel.intent)
        )
        intent_breakdown = {
            row[0]: row[1] for row in (await self.db.execute(intent_stmt)).all()
        }

        # Entity breakdown
        entity_stmt = (
            select(QueryAnalyticsModel.entity, func.count())
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(QueryAnalyticsModel.entity)
        )
        entity_breakdown = {
            row[0]: row[1] for row in (await self.db.execute(entity_stmt)).all()
        }

        # Latency stats
        latency_stmt = select(
            func.avg(QueryAnalyticsModel.latency_ms),
        ).where(QueryAnalyticsModel.created_at >= cutoff)
        avg_latency = (await self.db.execute(latency_stmt)).scalar() or 0.0

        # p95 latency — approximate via sorted query
        p95_stmt = (
            select(QueryAnalyticsModel.latency_ms)
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .order_by(QueryAnalyticsModel.latency_ms.asc())
        )
        all_latencies = [
            row[0] for row in (await self.db.execute(p95_stmt)).all() if row[0] is not None
        ]
        p95_latency = all_latencies[int(len(all_latencies) * 0.95)] if all_latencies else 0.0

        # Escalation rate
        escalated_stmt = select(func.count()).select_from(QueryAnalyticsModel).where(
            QueryAnalyticsModel.created_at >= cutoff,
            QueryAnalyticsModel.escalated.is_(True),
        )
        escalated_count = (await self.db.execute(escalated_stmt)).scalar() or 0
        escalation_rate = (escalated_count / total) if total > 0 else 0.0

        # Cost
        cost_stmt = select(func.sum(QueryAnalyticsModel.cost_usd)).where(
            QueryAnalyticsModel.created_at >= cutoff
        )
        total_cost = (await self.db.execute(cost_stmt)).scalar() or 0.0
        avg_cost = (total_cost / total) if total > 0 else 0.0

        # Model usage breakdown
        model_stmt = (
            select(QueryAnalyticsModel.model, func.count())
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(QueryAnalyticsModel.model)
        )
        model_breakdown = {
            row[0]: row[1] for row in (await self.db.execute(model_stmt)).all()
        }

        return {
            "period_days": days,
            "total_queries": total,
            "queries_per_day": trend,
            "avg_confidence": round(float(avg_conf), 4),
            "confidence_distribution": conf_dist,
            "intent_breakdown": intent_breakdown,
            "entity_breakdown": entity_breakdown,
            "avg_latency_ms": round(float(avg_latency), 2),
            "p95_latency_ms": round(float(p95_latency), 2),
            "escalation_rate": round(float(escalation_rate), 4),
            "total_cost_usd": round(float(total_cost), 4),
            "avg_cost_per_query": round(float(avg_cost), 6),
            "model_usage_breakdown": model_breakdown,
        }

    async def _confidence_distribution(self, cutoff: datetime) -> dict:
        """Bucket confidence into ranges."""
        buckets = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.8": 0, "0.8-1.0": 0}
        stmt = select(QueryAnalyticsModel.confidence).where(
            QueryAnalyticsModel.created_at >= cutoff
        )
        rows = (await self.db.execute(stmt)).all()
        for (c,) in rows:
            if c is None:
                continue
            if c < 0.3:
                buckets["0.0-0.3"] += 1
            elif c < 0.5:
                buckets["0.3-0.5"] += 1
            elif c < 0.8:
                buckets["0.5-0.8"] += 1
            else:
                buckets["0.8-1.0"] += 1
        return buckets

    async def get_intent_analytics(self, intent: str, days: int = 7) -> dict:
        """Per-intent deep dive."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        base_filter = and_(
            QueryAnalyticsModel.created_at >= cutoff,
            QueryAnalyticsModel.intent == intent,
        )

        total = (
            await self.db.execute(
                select(func.count()).select_from(QueryAnalyticsModel).where(base_filter)
            )
        ).scalar() or 0

        avg_conf = (
            await self.db.execute(
                select(func.avg(QueryAnalyticsModel.confidence)).where(base_filter)
            )
        ).scalar() or 0.0

        avg_latency = (
            await self.db.execute(
                select(func.avg(QueryAnalyticsModel.latency_ms)).where(base_filter)
            )
        ).scalar() or 0.0

        escalated = (
            await self.db.execute(
                select(func.count()).select_from(QueryAnalyticsModel).where(
                    base_filter, QueryAnalyticsModel.escalated.is_(True)
                )
            )
        ).scalar() or 0

        total_cost = (
            await self.db.execute(
                select(func.sum(QueryAnalyticsModel.cost_usd)).where(base_filter)
            )
        ).scalar() or 0.0

        return {
            "intent": intent,
            "period_days": days,
            "total_queries": total,
            "avg_confidence": round(float(avg_conf), 4),
            "avg_latency_ms": round(float(avg_latency), 2),
            "escalation_count": escalated,
            "escalation_rate": round(escalated / total, 4) if total > 0 else 0.0,
            "total_cost_usd": round(float(total_cost), 4),
        }

    async def get_cost_report(self, days: int = 30) -> dict:
        """Cost breakdown by model, intent, day."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # By model
        model_stmt = (
            select(QueryAnalyticsModel.model, func.sum(QueryAnalyticsModel.cost_usd), func.count())
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(QueryAnalyticsModel.model)
        )
        by_model = {
            row[0]: {"cost_usd": round(float(row[1] or 0), 4), "queries": row[2]}
            for row in (await self.db.execute(model_stmt)).all()
        }

        # By intent
        intent_stmt = (
            select(QueryAnalyticsModel.intent, func.sum(QueryAnalyticsModel.cost_usd), func.count())
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(QueryAnalyticsModel.intent)
        )
        by_intent = {
            row[0]: {"cost_usd": round(float(row[1] or 0), 4), "queries": row[2]}
            for row in (await self.db.execute(intent_stmt)).all()
        }

        # By day
        day_stmt = (
            select(
                func.date(QueryAnalyticsModel.created_at).label("day"),
                func.sum(QueryAnalyticsModel.cost_usd),
                func.count(),
            )
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(func.date(QueryAnalyticsModel.created_at))
            .order_by(func.date(QueryAnalyticsModel.created_at))
        )
        by_day = [
            {"date": str(row.day), "cost_usd": round(float(row[1] or 0), 4), "queries": row[2]}
            for row in (await self.db.execute(day_stmt)).all()
        ]

        # Total
        total_cost = sum(v["cost_usd"] for v in by_model.values())
        total_queries = sum(v["queries"] for v in by_model.values())

        return {
            "period_days": days,
            "total_cost_usd": round(total_cost, 4),
            "total_queries": total_queries,
            "avg_cost_per_query": round(total_cost / total_queries, 6) if total_queries > 0 else 0.0,
            "by_model": by_model,
            "by_intent": by_intent,
            "by_day": by_day,
        }

    async def get_hourly_traffic(self, days: int = 1) -> List[dict]:
        """Queries per hour for traffic pattern analysis."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stmt = (
            select(
                func.strftime("%H", QueryAnalyticsModel.created_at).label("hour"),
                func.count().label("count"),
            )
            .where(QueryAnalyticsModel.created_at >= cutoff)
            .group_by(func.strftime("%H", QueryAnalyticsModel.created_at))
            .order_by(func.strftime("%H", QueryAnalyticsModel.created_at))
        )
        rows = (await self.db.execute(stmt)).all()

        return [{"hour": int(row.hour), "count": row.count} for row in rows]


def _is_uuid(val: str) -> bool:
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False

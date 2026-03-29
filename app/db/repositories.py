"""Database repositories for COSMOS engines.

Each repository class wraps SQLAlchemy async operations for a specific domain.
The engine classes continue to work with in-memory storage by default,
but can be wired to these repositories for persistence.
"""

import uuid
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update, delete, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from cosmos.app.db.models import (
    ActionApproval,
    AuditLog,
    Analytics,
    QueryAnalytics,
    Feedback,
    KnowledgeEntry,
    KnowledgeCategory,
    DistillationRecord,
    ICRMSession,
    ConversationContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_id(val: str = None) -> str:
    """Return a string UUID. Validates if val is given, generates if not."""
    if val:
        return str(uuid.UUID(val))  # validates and normalizes
    return str(uuid.uuid4())


def _safe_id(val: str) -> Optional[str]:
    """Validate and return a string UUID, or None if invalid."""
    try:
        return str(uuid.UUID(val))
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# ApprovalRepository
# ---------------------------------------------------------------------------

class ApprovalRepository:
    """Persists action approval requests to icrm_action_approvals table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def create(self, request_data: dict) -> dict:
        """Insert new approval request.

        Expected keys: session_id, action_type, risk_level, approval_mode,
                       reason, metadata (optional).
        Returns the created record as a dict.
        """
        async with self._session_factory() as session:
            record = ActionApproval(
                id=_make_id(request_data.get("id")),
                session_id=_safe_id(request_data.get("session_id")),
                tool_execution_id=_safe_id(request_data.get("tool_execution_id")),
                action_type=request_data.get("action_type", "unknown"),
                risk_level=request_data.get("risk_level", "low"),
                approval_mode=request_data.get("approval_mode", "manual"),
                approved=request_data.get("approved"),
                approved_by=request_data.get("approved_by"),
                reason=request_data.get("reason"),
                metadata_=request_data.get("metadata", {}),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return _approval_to_dict(record)

    async def get_by_id(self, request_id: str) -> Optional[dict]:
        """Get request by ID."""
        async with self._session_factory() as session:
            stmt = select(ActionApproval).where(ActionApproval.id == _make_id(request_id))
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            return _approval_to_dict(record) if record else None

    async def update_status(self, request_id: str, status: str, **kwargs) -> Optional[dict]:
        """Update request status (approve/reject/execute/expire).

        Supported kwargs: approved_by, reason, approved (bool).
        """
        async with self._session_factory() as session:
            stmt = select(ActionApproval).where(ActionApproval.id == _make_id(request_id))
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record is None:
                return None

            # Map status to model fields
            if status == "approved":
                record.approved = True
                record.approved_by = kwargs.get("approved_by")
                record.resolved_at = _now()
                record.approval_mode = "manual"
            elif status == "rejected":
                record.approved = False
                record.approved_by = kwargs.get("approved_by")
                record.reason = kwargs.get("reason")
                record.resolved_at = _now()
            elif status == "expired":
                record.approved = False
                record.reason = "expired"
                record.resolved_at = _now()

            await session.commit()
            await session.refresh(record)
            return _approval_to_dict(record)

    async def list_pending(self, role_level: int = 0) -> List[dict]:
        """List pending requests (approved IS NULL means pending)."""
        async with self._session_factory() as session:
            stmt = (
                select(ActionApproval)
                .where(ActionApproval.approved.is_(None))
                .order_by(ActionApproval.created_at.asc())
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            return [_approval_to_dict(r) for r in records]

    async def expire_stale(self, max_age_minutes: int = 30) -> int:
        """Expire old unapproved requests."""
        cutoff = _now() - timedelta(minutes=max_age_minutes)
        async with self._session_factory() as session:
            stmt = (
                select(ActionApproval)
                .where(
                    ActionApproval.approved.is_(None),
                    ActionApproval.created_at < cutoff,
                )
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            count = 0
            for record in records:
                record.approved = False
                record.reason = "expired"
                record.resolved_at = _now()
                count += 1
            await session.commit()
            return count


def _approval_to_dict(record: ActionApproval) -> dict:
    return {
        "id": str(record.id),
        "session_id": str(record.session_id) if record.session_id else None,
        "tool_execution_id": str(record.tool_execution_id) if record.tool_execution_id else None,
        "action_type": record.action_type,
        "risk_level": record.risk_level.value if hasattr(record.risk_level, "value") else str(record.risk_level),
        "approval_mode": record.approval_mode.value if hasattr(record.approval_mode, "value") else str(record.approval_mode),
        "approved": record.approved,
        "approved_by": record.approved_by,
        "reason": record.reason,
        "metadata": record.metadata_ or {},
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
    }


# ---------------------------------------------------------------------------
# AuditRepository
# ---------------------------------------------------------------------------

class AuditRepository:
    """Persists audit log entries to icrm_audit_log table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def log_entry(self, entry: dict) -> None:
        """Insert an audit log entry.

        Expected keys: action, session_id, user_id, resource_type,
                       resource_id, details, ip_address.
        """
        async with self._session_factory() as session:
            record = AuditLog(
                id=str(uuid.uuid4()),
                session_id=_safe_id(entry.get("session_id")),
                user_id=entry.get("user_id"),
                action=entry.get("action", "unknown"),
                resource_type=entry.get("resource_type"),
                resource_id=entry.get("resource_id"),
                details=entry.get("details", {}),
                ip_address=entry.get("ip_address"),
            )
            session.add(record)
            await session.commit()

    async def get_trail(
        self,
        session_id: str = None,
        user_id: str = None,
        limit: int = 100,
    ) -> List[dict]:
        """Query audit trail with optional filters."""
        async with self._session_factory() as session:
            filters = []
            if session_id:
                filters.append(AuditLog.session_id == _safe_id(session_id))
            if user_id:
                filters.append(AuditLog.user_id == user_id)

            stmt = (
                select(AuditLog)
                .where(and_(*filters) if filters else True)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()

            return [
                {
                    "id": str(r.id),
                    "session_id": str(r.session_id) if r.session_id else None,
                    "user_id": r.user_id,
                    "action": r.action,
                    "resource_type": r.resource_type,
                    "resource_id": r.resource_id,
                    "details": r.details or {},
                    "ip_address": r.ip_address,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]


# ---------------------------------------------------------------------------
# AnalyticsRepository
# ---------------------------------------------------------------------------

class AnalyticsRepository:
    """Persists query analytics to icrm_query_analytics and icrm_analytics tables."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def record_query(self, data: dict) -> None:
        """Record a query analytics entry."""
        async with self._session_factory() as session:
            record = QueryAnalytics(
                id=str(uuid.uuid4()),
                session_id=_safe_id(data.get("session_id")),
                intent=data.get("intent"),
                entity=data.get("entity"),
                confidence=data.get("confidence"),
                latency_ms=data.get("latency_ms"),
                tools_used=data.get("tools_used", []),
                escalated=data.get("escalated", False),
                model=data.get("model"),
                cost_usd=data.get("cost_usd", 0.0),
            )
            session.add(record)
            await session.commit()

    async def get_dashboard(self, days: int = 7) -> dict:
        """Dashboard data: query count, avg confidence, avg latency, cost, intent breakdown."""
        cutoff = _now() - timedelta(days=days)
        async with self._session_factory() as session:
            # Total queries
            total = (
                await session.execute(
                    select(func.count()).select_from(QueryAnalytics).where(
                        QueryAnalytics.created_at >= cutoff
                    )
                )
            ).scalar() or 0

            # Average confidence
            avg_conf = (
                await session.execute(
                    select(func.avg(QueryAnalytics.confidence)).where(
                        QueryAnalytics.created_at >= cutoff
                    )
                )
            ).scalar() or 0.0

            # Average latency
            avg_latency = (
                await session.execute(
                    select(func.avg(QueryAnalytics.latency_ms)).where(
                        QueryAnalytics.created_at >= cutoff
                    )
                )
            ).scalar() or 0.0

            # Total cost
            total_cost = (
                await session.execute(
                    select(func.sum(QueryAnalytics.cost_usd)).where(
                        QueryAnalytics.created_at >= cutoff
                    )
                )
            ).scalar() or 0.0

            # Escalation count
            escalated = (
                await session.execute(
                    select(func.count()).select_from(QueryAnalytics).where(
                        QueryAnalytics.created_at >= cutoff,
                        QueryAnalytics.escalated.is_(True),
                    )
                )
            ).scalar() or 0

            return {
                "period_days": days,
                "total_queries": total,
                "avg_confidence": round(float(avg_conf), 4),
                "avg_latency_ms": round(float(avg_latency), 2),
                "total_cost_usd": round(float(total_cost), 4),
                "escalation_count": escalated,
                "escalation_rate": round(escalated / total, 4) if total > 0 else 0.0,
            }

    async def get_intent_breakdown(self, days: int = 7) -> dict:
        """Intent breakdown: count per intent."""
        cutoff = _now() - timedelta(days=days)
        async with self._session_factory() as session:
            stmt = (
                select(QueryAnalytics.intent, func.count())
                .where(QueryAnalytics.created_at >= cutoff)
                .group_by(QueryAnalytics.intent)
            )
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

    async def get_cost_report(self, days: int = 30) -> dict:
        """Cost breakdown by model and intent."""
        cutoff = _now() - timedelta(days=days)
        async with self._session_factory() as session:
            # By model
            model_stmt = (
                select(
                    QueryAnalytics.model,
                    func.sum(QueryAnalytics.cost_usd),
                    func.count(),
                )
                .where(QueryAnalytics.created_at >= cutoff)
                .group_by(QueryAnalytics.model)
            )
            model_result = await session.execute(model_stmt)
            by_model = {
                row[0]: {"cost_usd": round(float(row[1] or 0), 4), "queries": row[2]}
                for row in model_result.all()
            }

            # By intent
            intent_stmt = (
                select(
                    QueryAnalytics.intent,
                    func.sum(QueryAnalytics.cost_usd),
                    func.count(),
                )
                .where(QueryAnalytics.created_at >= cutoff)
                .group_by(QueryAnalytics.intent)
            )
            intent_result = await session.execute(intent_stmt)
            by_intent = {
                row[0]: {"cost_usd": round(float(row[1] or 0), 4), "queries": row[2]}
                for row in intent_result.all()
            }

            total_cost = sum(v["cost_usd"] for v in by_model.values())
            total_queries = sum(v["queries"] for v in by_model.values())

            return {
                "period_days": days,
                "total_cost_usd": round(total_cost, 4),
                "total_queries": total_queries,
                "by_model": by_model,
                "by_intent": by_intent,
            }


# ---------------------------------------------------------------------------
# FeedbackRepository
# ---------------------------------------------------------------------------

class FeedbackRepository:
    """Persists feedback to icrm_feedback table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def submit(self, data: dict) -> dict:
        """Submit feedback. Expected keys: session_id, message_id, user_id, rating, comment, tags."""
        async with self._session_factory() as session:
            feedback_id = str(uuid.uuid4())
            record = Feedback(
                id=feedback_id,
                session_id=_safe_id(data.get("session_id")),
                message_id=_safe_id(data.get("message_id")),
                user_id=data.get("user_id"),
                rating=data.get("rating"),
                comment=data.get("comment"),
                tags=data.get("tags", []),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return _feedback_to_dict(record)

    async def get_by_session(self, session_id: str) -> List[dict]:
        """Get all feedback for a session."""
        async with self._session_factory() as session:
            stmt = (
                select(Feedback)
                .where(Feedback.session_id == _safe_id(session_id))
                .order_by(Feedback.created_at.desc())
            )
            result = await session.execute(stmt)
            return [_feedback_to_dict(r) for r in result.scalars().all()]

    async def get_summary(self, days: int = 7) -> dict:
        """Aggregated feedback summary."""
        cutoff = _now() - timedelta(days=days)
        async with self._session_factory() as session:
            avg_score = (
                await session.execute(
                    select(func.avg(Feedback.rating)).where(Feedback.created_at >= cutoff)
                )
            ).scalar() or 0.0

            total = (
                await session.execute(
                    select(func.count()).select_from(Feedback).where(
                        Feedback.created_at >= cutoff
                    )
                )
            ).scalar() or 0

            # Score distribution
            dist_stmt = (
                select(Feedback.rating, func.count())
                .where(Feedback.created_at >= cutoff)
                .group_by(Feedback.rating)
            )
            dist_result = await session.execute(dist_stmt)
            score_distribution = {str(row[0]): row[1] for row in dist_result.all()}

            return {
                "period_days": days,
                "total_feedback": total,
                "avg_score": round(float(avg_score), 2),
                "score_distribution": score_distribution,
            }

    async def get_low_scoring(self, max_score: int = 2, limit: int = 50) -> List[dict]:
        """Get poorly scoring feedback entries."""
        async with self._session_factory() as session:
            stmt = (
                select(Feedback)
                .where(Feedback.rating <= max_score)
                .order_by(Feedback.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [_feedback_to_dict(r) for r in result.scalars().all()]


def _feedback_to_dict(record: Feedback) -> dict:
    return {
        "id": str(record.id),
        "session_id": str(record.session_id) if record.session_id else None,
        "message_id": str(record.message_id) if record.message_id else None,
        "user_id": record.user_id,
        "rating": record.rating,
        "comment": record.comment,
        "tags": record.tags or [],
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


# ---------------------------------------------------------------------------
# KnowledgeRepository
# ---------------------------------------------------------------------------

class KnowledgeRepository:
    """Persists knowledge entries to icrm_knowledge_entries table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def add(self, data: dict) -> dict:
        """Add a knowledge entry.

        Expected keys: category, question, answer, source, confidence.
        """
        async with self._session_factory() as session:
            entry_id = str(uuid.uuid4())
            try:
                cat_enum = KnowledgeCategory(data.get("category", "faq"))
            except ValueError:
                cat_enum = KnowledgeCategory.faq

            record = KnowledgeEntry(
                id=entry_id,
                category=cat_enum,
                question=data.get("question", ""),
                answer=data.get("answer", ""),
                source=data.get("source"),
                confidence=data.get("confidence", 1.0),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return _knowledge_to_dict(record)

    async def search(self, query: str, category: str = None, limit: int = 5) -> List[dict]:
        """Search knowledge entries. Basic ILIKE search on question and answer."""
        async with self._session_factory() as session:
            filters = [KnowledgeEntry.enabled.is_(True)]
            if category:
                try:
                    cat_enum = KnowledgeCategory(category)
                    filters.append(KnowledgeEntry.category == cat_enum)
                except ValueError:
                    pass

            # Simple text search using ILIKE
            search_term = f"%{query}%"
            filters.append(
                or_(
                    KnowledgeEntry.question.ilike(search_term),
                    KnowledgeEntry.answer.ilike(search_term),
                )
            )

            stmt = (
                select(KnowledgeEntry)
                .where(and_(*filters))
                .order_by(KnowledgeEntry.usage_count.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            entries = result.scalars().all()

            # Increment usage counts
            for entry in entries:
                entry.usage_count = (entry.usage_count or 0) + 1
            await session.commit()

            return [_knowledge_to_dict(e) for e in entries]

    async def get_stats(self) -> dict:
        """Knowledge base statistics."""
        async with self._session_factory() as session:
            # Per-category count
            cat_stmt = (
                select(KnowledgeEntry.category, func.count())
                .where(KnowledgeEntry.enabled.is_(True))
                .group_by(KnowledgeEntry.category)
            )
            cat_result = await session.execute(cat_stmt)
            by_category = {
                row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
                for row in cat_result.all()
            }

            total = sum(by_category.values())

            # Coverage gaps
            all_categories = {c.value for c in KnowledgeCategory}
            covered = set(by_category.keys())
            gaps = list(all_categories - covered)

            return {
                "total_entries": total,
                "by_category": by_category,
                "coverage_gaps": gaps,
            }


def _knowledge_to_dict(record: KnowledgeEntry) -> dict:
    return {
        "id": str(record.id),
        "category": record.category.value if hasattr(record.category, "value") else str(record.category),
        "question": record.question,
        "answer": record.answer,
        "source": record.source,
        "confidence": record.confidence,
        "usage_count": record.usage_count or 0,
        "enabled": record.enabled,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


# ---------------------------------------------------------------------------
# DistillationRepository
# ---------------------------------------------------------------------------

class DistillationRepository:
    """Persists distillation records to icrm_distillation_records table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def log(self, data: dict) -> str:
        """Log a distillation record. Returns the record ID."""
        record_id = str(uuid.uuid4())
        async with self._session_factory() as session:
            record = DistillationRecord(
                id=record_id,
                session_id=_safe_id(data.get("session_id")) or str(uuid.uuid4()),
                user_query=data.get("user_query", ""),
                intent=data.get("intent"),
                entity=data.get("entity"),
                tools_used=data.get("tools_used", []),
                tool_results=data.get("tool_results", []),
                llm_prompt=data.get("llm_prompt"),
                llm_response=data.get("llm_response"),
                final_response=data.get("final_response"),
                confidence=data.get("confidence"),
                model_used=data.get("model_used"),
                token_count_input=data.get("token_count_input", 0),
                token_count_output=data.get("token_count_output", 0),
                cost_usd=data.get("cost_usd", 0.0),
            )
            session.add(record)
            await session.commit()
        return str(record_id)

    async def add_feedback(self, record_id: str, score: int, text: str = None) -> None:
        """Add feedback to an existing distillation record."""
        if score < 1 or score > 5:
            raise ValueError("Feedback score must be between 1 and 5")

        async with self._session_factory() as session:
            stmt = select(DistillationRecord).where(
                DistillationRecord.id == _make_id(record_id)
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record is None:
                raise ValueError(f"Distillation record {record_id} not found")

            record.feedback_score = score
            record.feedback_text = text
            await session.commit()

    async def export(self, min_confidence: float = 0.7, min_feedback: int = 4) -> List[dict]:
        """Export high-quality records for training data."""
        async with self._session_factory() as session:
            stmt = select(DistillationRecord).where(
                DistillationRecord.confidence >= min_confidence,
                DistillationRecord.feedback_score >= min_feedback,
            )
            result = await session.execute(stmt)
            records = result.scalars().all()

            return [
                {
                    "id": str(r.id),
                    "user_query": r.user_query,
                    "final_response": r.final_response,
                    "intent": r.intent,
                    "entity": r.entity,
                    "tools_used": r.tools_used or [],
                    "confidence": r.confidence,
                    "feedback_score": r.feedback_score,
                    "model_used": r.model_used,
                }
                for r in records
            ]

    async def get_stats(self) -> dict:
        """Get distillation collection stats."""
        async with self._session_factory() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(DistillationRecord)
                )
            ).scalar() or 0

            avg_confidence = (
                await session.execute(
                    select(func.avg(DistillationRecord.confidence))
                )
            ).scalar() or 0.0

            total_cost = (
                await session.execute(
                    select(func.sum(DistillationRecord.cost_usd))
                )
            ).scalar() or 0.0

            exportable = (
                await session.execute(
                    select(func.count()).select_from(DistillationRecord).where(
                        DistillationRecord.confidence >= 0.7,
                        DistillationRecord.feedback_score >= 4,
                    )
                )
            ).scalar() or 0

            return {
                "total_records": total,
                "avg_confidence": round(float(avg_confidence), 4),
                "total_cost_usd": round(float(total_cost), 4),
                "exportable_records": exportable,
            }


# ---------------------------------------------------------------------------
# SessionStateRepository
# ---------------------------------------------------------------------------

class SessionStateRepository:
    """Persists session state to icrm_sessions + icrm_conversation_context tables."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def create_session(self, data: dict) -> dict:
        """Create a new session.

        Expected keys: user_id, company_id, channel, metadata.
        """
        async with self._session_factory() as session:
            session_id = str(uuid.uuid4())
            record = ICRMSession(
                id=session_id,
                user_id=data.get("user_id", "unknown"),
                company_id=data.get("company_id"),
                channel=data.get("channel", "web"),
                status="active",
                metadata_=data.get("metadata", {}),
            )
            session.add(record)

            # Create associated context
            context = ConversationContext(
                id=str(uuid.uuid4()),
                session_id=session_id,
                entities={},
                tool_state={},
                user_profile={},
            )
            session.add(context)
            await session.commit()
            await session.refresh(record)

            return {
                "id": str(record.id),
                "user_id": record.user_id,
                "company_id": record.company_id,
                "channel": record.channel,
                "status": record.status,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            }

    async def get_state(self, session_id: str) -> Optional[dict]:
        """Get session + context state."""
        async with self._session_factory() as session:
            stmt = select(ICRMSession).where(ICRMSession.id == _make_id(session_id))
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record is None:
                return None

            # Load context
            ctx_stmt = select(ConversationContext).where(
                ConversationContext.session_id == _make_id(session_id)
            )
            ctx_result = await session.execute(ctx_stmt)
            ctx = ctx_result.scalar_one_or_none()

            return {
                "id": str(record.id),
                "user_id": record.user_id,
                "company_id": record.company_id,
                "channel": record.channel,
                "status": record.status,
                "metadata": record.metadata_ or {},
                "context": {
                    "intent": ctx.intent if ctx else None,
                    "entities": ctx.entities if ctx else {},
                    "tool_state": ctx.tool_state if ctx else {},
                    "user_profile": ctx.user_profile if ctx else {},
                    "conversation_summary": ctx.conversation_summary if ctx else None,
                } if ctx else None,
                "created_at": record.created_at.isoformat() if record.created_at else None,
                "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            }

    async def update_state(self, session_id: str, updates: dict) -> dict:
        """Update session context state.

        Supported update keys: intent, entities, tool_state, user_profile,
                               conversation_summary, status, metadata.
        """
        async with self._session_factory() as session:
            # Update session-level fields
            sess_stmt = select(ICRMSession).where(ICRMSession.id == _make_id(session_id))
            sess_result = await session.execute(sess_stmt)
            record = sess_result.scalar_one_or_none()
            if record is None:
                raise ValueError(f"Session {session_id} not found")

            if "status" in updates:
                record.status = updates["status"]
            if "metadata" in updates:
                record.metadata_ = updates["metadata"]

            # Update context
            ctx_stmt = select(ConversationContext).where(
                ConversationContext.session_id == _make_id(session_id)
            )
            ctx_result = await session.execute(ctx_stmt)
            ctx = ctx_result.scalar_one_or_none()

            if ctx:
                if "intent" in updates:
                    ctx.intent = updates["intent"]
                if "entities" in updates:
                    ctx.entities = updates["entities"]
                if "tool_state" in updates:
                    ctx.tool_state = updates["tool_state"]
                if "user_profile" in updates:
                    ctx.user_profile = updates["user_profile"]
                if "conversation_summary" in updates:
                    ctx.conversation_summary = updates["conversation_summary"]

            await session.commit()
            return await self.get_state(session_id)


# ---------------------------------------------------------------------------
# CostRepository
# ---------------------------------------------------------------------------

class CostRepository:
    """Persists cost entries to icrm_analytics table for long-term analytics."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def record(self, entry: dict) -> None:
        """Record a cost entry as an analytics event."""
        async with self._session_factory() as session:
            record = Analytics(
                id=str(uuid.uuid4()),
                session_id=_safe_id(entry.get("session_id")),
                event_type="cost_record",
                event_data={
                    "model_tier": entry.get("model_tier"),
                    "input_tokens": entry.get("input_tokens", 0),
                    "output_tokens": entry.get("output_tokens", 0),
                    "intent": entry.get("intent"),
                    "cached": entry.get("cached", False),
                },
                user_id=entry.get("user_id"),
                company_id=entry.get("company_id"),
                duration_ms=entry.get("duration_ms"),
                token_count=(entry.get("input_tokens", 0) + entry.get("output_tokens", 0)),
                model=entry.get("model_tier"),
                cost_usd=entry.get("cost_usd", 0.0),
            )
            session.add(record)
            await session.commit()

    async def get_daily_summary(self, date: str = None) -> dict:
        """Get cost summary for a specific date (defaults to today)."""
        target_date = date or _now().strftime("%Y-%m-%d")
        async with self._session_factory() as session:
            stmt = (
                select(
                    func.sum(Analytics.cost_usd),
                    func.count(),
                    func.sum(Analytics.token_count),
                )
                .where(
                    Analytics.event_type == "cost_record",
                    func.date(Analytics.created_at) == target_date,
                )
            )
            result = await session.execute(stmt)
            row = result.one()

            return {
                "date": target_date,
                "total_cost_usd": round(float(row[0] or 0), 4),
                "query_count": row[1] or 0,
                "total_tokens": row[2] or 0,
            }

    async def get_session_summary(self, session_id: str) -> dict:
        """Cost summary for a specific session."""
        async with self._session_factory() as session:
            stmt = (
                select(
                    func.sum(Analytics.cost_usd),
                    func.count(),
                    func.sum(Analytics.token_count),
                )
                .where(
                    Analytics.event_type == "cost_record",
                    Analytics.session_id == _safe_id(session_id),
                )
            )
            result = await session.execute(stmt)
            row = result.one()

            return {
                "session_id": session_id,
                "total_cost_usd": round(float(row[0] or 0), 4),
                "query_count": row[1] or 0,
                "total_tokens": row[2] or 0,
            }

    async def get_trend(self, days: int = 7) -> List[dict]:
        """Daily cost trend."""
        cutoff = _now() - timedelta(days=days)
        async with self._session_factory() as session:
            stmt = (
                select(
                    func.date(Analytics.created_at).label("day"),
                    func.sum(Analytics.cost_usd),
                    func.count(),
                )
                .where(
                    Analytics.event_type == "cost_record",
                    Analytics.created_at >= cutoff,
                )
                .group_by(func.date(Analytics.created_at))
                .order_by(func.date(Analytics.created_at))
            )
            result = await session.execute(stmt)

            return [
                {
                    "date": str(row[0]),
                    "cost_usd": round(float(row[1] or 0), 4),
                    "query_count": row[2],
                }
                for row in result.all()
            ]

"""
Distillation data collector for COSMOS.

Collects query-response pairs for future model fine-tuning.
Non-blocking — does not slow down the ReAct response pipeline.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cosmos.app.db.models import DistillationRecord as DistillationRecordModel

logger = structlog.get_logger()


@dataclass
class DistillationRecord:
    id: str
    session_id: str
    user_query: str
    intent: str
    entity: str
    tools_used: List[str]
    tool_results: List[dict]
    llm_prompt: str
    llm_response: str
    final_response: str
    confidence: float
    feedback_score: Optional[int]
    feedback_text: Optional[str]
    created_at: datetime
    model_used: str
    token_count_input: int
    token_count_output: int
    cost_usd: float


class DistillationCollector:
    """Collects and stores training data for future model distillation."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def log_interaction(
        self,
        session_id: str,
        react_result: Any,
        llm_prompt: str,
        llm_response: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
    ) -> str:
        """Log a complete interaction for distillation. Returns the record ID."""
        record_id = str(uuid.uuid4())

        # Extract fields from react_result
        tools_used = getattr(react_result, "tools_used", []) or []
        confidence = getattr(react_result, "confidence", 0.0)
        final_response = getattr(react_result, "response", "")

        # Extract intent/entity from first REASON step if available
        intent = "unknown"
        entity = "unknown"
        steps = getattr(react_result, "steps", [])
        if steps:
            content = steps[0].content
            if "Intent=" in content:
                intent = content.split("Intent=")[1].split(",")[0]
            if "Entity=" in content:
                entity = content.split("Entity=")[1].split(",")[0]

        # Extract tool results from steps
        tool_results = []
        for step in steps:
            for tr in getattr(step, "tool_results", []):
                tool_results.append({
                    "tool_name": tr.tool_name,
                    "success": tr.success,
                    "data": str(tr.data)[:500] if tr.data else None,
                    "error": tr.error,
                })

        # Estimate cost if not provided
        cost = _estimate_cost(model, tokens_in, tokens_out)

        record = DistillationRecordModel(
            id=uuid.UUID(record_id),
            session_id=uuid.UUID(session_id) if _is_uuid(session_id) else uuid.uuid4(),
            user_query=llm_prompt[:10000] if llm_prompt else "",
            intent=intent,
            entity=entity,
            tools_used=tools_used,
            tool_results=tool_results,
            llm_prompt=llm_prompt,
            llm_response=llm_response,
            final_response=final_response,
            confidence=confidence,
            model_used=model,
            token_count_input=tokens_in,
            token_count_output=tokens_out,
            cost_usd=cost,
        )

        try:
            self.db.add(record)
            await self.db.commit()
            logger.info("distillation.logged", record_id=record_id, model=model)
        except Exception as exc:
            await self.db.rollback()
            logger.error("distillation.log_failed", error=str(exc))
            raise

        return record_id

    async def add_feedback(self, record_id: str, score: int, text: str = None) -> None:
        """Add agent feedback to an existing record."""
        if score < 1 or score > 5:
            raise ValueError("Feedback score must be between 1 and 5")

        stmt = select(DistillationRecordModel).where(
            DistillationRecordModel.id == record_id
        )
        result = await self.db.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise ValueError(f"Distillation record {record_id} not found")

        record.feedback_score = score
        record.feedback_text = text
        await self.db.commit()
        logger.info("distillation.feedback_added", record_id=record_id, score=score)

    async def export_training_data(
        self,
        min_confidence: float = 0.7,
        min_feedback: int = 4,
        format: str = "jsonl",
    ) -> str:
        """Export high-quality records as training data in JSONL format."""
        stmt = select(DistillationRecordModel).where(
            DistillationRecordModel.confidence >= min_confidence,
            DistillationRecordModel.feedback_score >= min_feedback,
        )
        result = await self.db.execute(stmt)
        records = result.scalars().all()

        lines = []
        for r in records:
            entry = {
                "messages": [
                    {"role": "user", "content": r.user_query},
                    {"role": "assistant", "content": r.final_response},
                ],
                "intent": r.intent,
                "entity": r.entity,
                "tools_used": r.tools_used or [],
                "confidence": r.confidence,
                "feedback_score": r.feedback_score,
                "model": r.model_used,
            }
            lines.append(json.dumps(entry))

        return "\n".join(lines)

    async def get_stats(self) -> dict:
        """Get collection stats: total records, avg confidence, feedback distribution."""
        total_stmt = select(func.count()).select_from(DistillationRecordModel)
        total_result = await self.db.execute(total_stmt)
        total = total_result.scalar() or 0

        avg_conf_stmt = select(func.avg(DistillationRecordModel.confidence)).select_from(
            DistillationRecordModel
        )
        avg_conf_result = await self.db.execute(avg_conf_stmt)
        avg_confidence = avg_conf_result.scalar() or 0.0

        # Feedback distribution
        fb_stmt = select(
            DistillationRecordModel.feedback_score,
            func.count(),
        ).where(
            DistillationRecordModel.feedback_score.isnot(None)
        ).group_by(DistillationRecordModel.feedback_score)
        fb_result = await self.db.execute(fb_stmt)
        feedback_dist = {str(row[0]): row[1] for row in fb_result.all()}

        # Total cost
        cost_stmt = select(func.sum(DistillationRecordModel.cost_usd)).select_from(
            DistillationRecordModel
        )
        cost_result = await self.db.execute(cost_stmt)
        total_cost = cost_result.scalar() or 0.0

        # Exportable (high quality) count
        export_stmt = select(func.count()).select_from(DistillationRecordModel).where(
            DistillationRecordModel.confidence >= 0.7,
            DistillationRecordModel.feedback_score >= 4,
        )
        export_result = await self.db.execute(export_stmt)
        exportable = export_result.scalar() or 0

        return {
            "total_records": total,
            "avg_confidence": round(float(avg_confidence), 4),
            "feedback_distribution": feedback_dist,
            "total_cost_usd": round(float(total_cost), 4),
            "exportable_records": exportable,
        }


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate cost in USD based on model and token counts."""
    # Approximate pricing per 1M tokens
    pricing = {
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-sonnet-4-6": (3.0, 15.0),
    }
    input_rate, output_rate = pricing.get(model, (1.0, 5.0))
    return (tokens_in * input_rate + tokens_out * output_rate) / 1_000_000


def _is_uuid(val: str) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False

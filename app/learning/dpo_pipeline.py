"""
DPO (Direct Preference Optimization) Training Pipeline for COSMOS.

Converts feedback-linked distillation records into DPO training pairs:
  - Positive: High-confidence responses with good feedback (score >= 4)
  - Negative: Low-confidence responses OR bad feedback (score <= 2)

Output format: JSONL with chosen/rejected pairs for DPO fine-tuning.

Usage:
    pipeline = DPOPipeline(db_session)
    pairs = await pipeline.generate_pairs()
    jsonl = await pipeline.export_jsonl()
    stats = await pipeline.get_stats()
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import structlog
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


@dataclass
class DPOPair:
    """A single DPO training pair: same prompt, chosen vs rejected response."""
    prompt: str
    chosen: str
    rejected: str
    intent: str
    entity: str
    chosen_confidence: float
    rejected_confidence: float
    chosen_feedback: Optional[int]
    rejected_feedback: Optional[int]
    chosen_model: str
    rejected_model: str


class DPOPipeline:
    """Generates DPO training pairs from distillation + feedback data.

    Strategy:
    1. Same-session pairs: If a session has multiple attempts (retry/escalation),
       the higher-scoring response is "chosen", lower is "rejected"
    2. Cross-session pairs: Group by intent+entity, pair high-feedback with low-feedback
    3. Confidence-based: Pair high-confidence with low-confidence on similar queries
    """

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def generate_pairs(
        self,
        min_chosen_feedback: int = 4,
        max_rejected_feedback: int = 2,
        min_chosen_confidence: float = 0.7,
        max_rejected_confidence: float = 0.4,
        limit: int = 5000,
    ) -> List[DPOPair]:
        """Generate DPO pairs from distillation records.

        Pairs high-quality responses (chosen) with low-quality (rejected)
        for the same intent+entity combination.
        """
        from app.db.models import DistillationRecord

        # Fetch chosen candidates (high feedback + high confidence)
        chosen_stmt = (
            select(DistillationRecord)
            .where(
                and_(
                    DistillationRecord.feedback_score >= min_chosen_feedback,
                    DistillationRecord.confidence >= min_chosen_confidence,
                )
            )
            .order_by(DistillationRecord.feedback_score.desc())
            .limit(limit)
        )
        chosen_result = await self.db.execute(chosen_stmt)
        chosen_records = chosen_result.scalars().all()

        if not chosen_records:
            logger.info("dpo.no_chosen_records")
            return []

        # Fetch rejected candidates (low feedback OR low confidence)
        rejected_stmt = (
            select(DistillationRecord)
            .where(
                or_(
                    DistillationRecord.feedback_score <= max_rejected_feedback,
                    and_(
                        DistillationRecord.confidence <= max_rejected_confidence,
                        DistillationRecord.feedback_score.is_(None),
                    ),
                )
            )
            .order_by(DistillationRecord.confidence.asc())
            .limit(limit)
        )
        rejected_result = await self.db.execute(rejected_stmt)
        rejected_records = rejected_result.scalars().all()

        if not rejected_records:
            logger.info("dpo.no_rejected_records")
            return []

        # Build intent+entity index for matching
        rejected_by_key = {}
        for r in rejected_records:
            key = f"{r.intent}:{r.entity}"
            if key not in rejected_by_key:
                rejected_by_key[key] = []
            rejected_by_key[key].append(r)

        # Generate pairs: match chosen with rejected by intent+entity
        pairs = []
        for chosen in chosen_records:
            key = f"{chosen.intent}:{chosen.entity}"
            candidates = rejected_by_key.get(key, [])
            if not candidates:
                # Fallback: try intent-only match
                for rkey, rlist in rejected_by_key.items():
                    if rkey.startswith(f"{chosen.intent}:"):
                        candidates = rlist
                        break

            if not candidates:
                continue

            # Pick the best rejected candidate (lowest confidence/feedback)
            rejected = candidates[0]
            candidates.pop(0)  # Don't reuse

            pair = DPOPair(
                prompt=chosen.user_query,
                chosen=chosen.final_response or chosen.llm_response or "",
                rejected=rejected.final_response or rejected.llm_response or "",
                intent=chosen.intent,
                entity=chosen.entity,
                chosen_confidence=chosen.confidence,
                rejected_confidence=rejected.confidence,
                chosen_feedback=chosen.feedback_score,
                rejected_feedback=rejected.feedback_score,
                chosen_model=chosen.model_used or "unknown",
                rejected_model=rejected.model_used or "unknown",
            )

            # Skip if chosen == rejected (dedup)
            if pair.chosen.strip() == pair.rejected.strip():
                continue

            pairs.append(pair)

        logger.info(
            "dpo.pairs_generated",
            total_pairs=len(pairs),
            chosen_pool=len(chosen_records),
            rejected_pool=len(rejected_records),
        )
        return pairs

    async def export_jsonl(
        self,
        min_chosen_feedback: int = 4,
        max_rejected_feedback: int = 2,
        limit: int = 5000,
    ) -> str:
        """Export DPO training data as JSONL.

        Format compatible with TRL DPOTrainer:
        {"prompt": "...", "chosen": "...", "rejected": "...", "metadata": {...}}
        """
        pairs = await self.generate_pairs(
            min_chosen_feedback=min_chosen_feedback,
            max_rejected_feedback=max_rejected_feedback,
            limit=limit,
        )

        lines = []
        for pair in pairs:
            entry = {
                "prompt": pair.prompt,
                "chosen": pair.chosen,
                "rejected": pair.rejected,
                "metadata": {
                    "intent": pair.intent,
                    "entity": pair.entity,
                    "chosen_confidence": pair.chosen_confidence,
                    "rejected_confidence": pair.rejected_confidence,
                    "chosen_feedback": pair.chosen_feedback,
                    "rejected_feedback": pair.rejected_feedback,
                    "chosen_model": pair.chosen_model,
                    "rejected_model": pair.rejected_model,
                },
            }
            lines.append(json.dumps(entry))

        return "\n".join(lines)

    async def export_sft_jsonl(
        self,
        min_confidence: float = 0.7,
        min_feedback: int = 4,
        limit: int = 10000,
    ) -> str:
        """Export SFT (Supervised Fine-Tuning) data as JSONL.

        Simpler than DPO — just high-quality query-response pairs.
        Format: {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}
        """
        from app.db.models import DistillationRecord

        stmt = (
            select(DistillationRecord)
            .where(
                and_(
                    DistillationRecord.confidence >= min_confidence,
                    DistillationRecord.feedback_score >= min_feedback,
                )
            )
            .order_by(DistillationRecord.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        records = result.scalars().all()

        lines = []
        for r in records:
            response = r.final_response or r.llm_response or ""
            if not response.strip():
                continue

            entry = {
                "messages": [
                    {"role": "user", "content": r.user_query},
                    {"role": "assistant", "content": response},
                ],
                "metadata": {
                    "intent": r.intent,
                    "entity": r.entity,
                    "tools_used": r.tools_used or [],
                    "confidence": r.confidence,
                    "feedback_score": r.feedback_score,
                    "model": r.model_used,
                },
            }
            lines.append(json.dumps(entry))

        logger.info("sft.exported", total_records=len(lines))
        return "\n".join(lines)

    async def get_stats(self) -> dict:
        """Get DPO pipeline statistics."""
        from app.db.models import DistillationRecord

        # Total records
        total_stmt = select(func.count()).select_from(DistillationRecord)
        total = (await self.db.execute(total_stmt)).scalar() or 0

        # With feedback
        with_fb_stmt = select(func.count()).select_from(DistillationRecord).where(
            DistillationRecord.feedback_score.isnot(None)
        )
        with_feedback = (await self.db.execute(with_fb_stmt)).scalar() or 0

        # DPO-ready chosen (high quality)
        chosen_stmt = select(func.count()).select_from(DistillationRecord).where(
            and_(
                DistillationRecord.feedback_score >= 4,
                DistillationRecord.confidence >= 0.7,
            )
        )
        chosen_ready = (await self.db.execute(chosen_stmt)).scalar() or 0

        # DPO-ready rejected (low quality)
        rejected_stmt = select(func.count()).select_from(DistillationRecord).where(
            or_(
                DistillationRecord.feedback_score <= 2,
                and_(
                    DistillationRecord.confidence <= 0.4,
                    DistillationRecord.feedback_score.is_(None),
                ),
            )
        )
        rejected_ready = (await self.db.execute(rejected_stmt)).scalar() or 0

        # SFT-ready
        sft_stmt = select(func.count()).select_from(DistillationRecord).where(
            and_(
                DistillationRecord.confidence >= 0.7,
                DistillationRecord.feedback_score >= 4,
            )
        )
        sft_ready = (await self.db.execute(sft_stmt)).scalar() or 0

        # Intent distribution in training data
        intent_stmt = (
            select(DistillationRecord.intent, func.count())
            .where(DistillationRecord.feedback_score.isnot(None))
            .group_by(DistillationRecord.intent)
            .order_by(func.count().desc())
            .limit(10)
        )
        intent_result = await self.db.execute(intent_stmt)
        intent_dist = {row[0]: row[1] for row in intent_result.all()}

        return {
            "total_records": total,
            "with_feedback": with_feedback,
            "dpo_chosen_ready": chosen_ready,
            "dpo_rejected_ready": rejected_ready,
            "estimated_dpo_pairs": min(chosen_ready, rejected_ready),
            "sft_ready": sft_ready,
            "top_intents": intent_dist,
        }

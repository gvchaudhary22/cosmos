"""
Continuous Learning Orchestrator — Goal 5.

Runs the full loop:
  1. Collect low-confidence traces from DistillationRecord (confidence < 0.4)
  2. Run incremental eval (sample 100 seeds) to measure current recall@5
  3. Generate auto-actions from weak domains + low-conf traces
  4. Persist proposals as StagedImprovement records (status=pending)
  5. Optionally apply approved improvements to KB

Triggered by:
  - POST /cosmos/api/v1/learning/continuous/run  (manual)
  - After every KB pipeline run (kb_watcher callback)
  - Scheduled: daily at 2am UTC (via scheduler)

Human review loop:
  - LIME polls GET /cosmos/api/v1/learning/staged for pending items
  - Reviewer approves/rejects via POST /cosmos/api/v1/learning/staged/{id}/approve|reject
  - Approved items are applied to KB on next run
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DistillationRecord as DistillationRecordModel,
    StagedImprovement,
    StagedImprovementStatus,
    StagedImprovementType,
)
from app.learning.auto_actions import AutoActionGenerator
from app.learning.collector import DistillationCollector
from app.services.kb_eval import KBEvaluator

logger = structlog.get_logger(__name__)


@dataclass
class ContinuousLearningRun:
    run_id: str
    triggered_by: str
    eval_recall_at_5: float
    eval_seeds_run: int
    weak_domains: list
    low_conf_traces_analyzed: int
    proposals_generated: int
    proposals_saved: int
    improvements_applied: int
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "triggered_by": self.triggered_by,
            "eval": {
                "recall_at_5": round(self.eval_recall_at_5, 4),
                "seeds_run": self.eval_seeds_run,
                "weak_domains": self.weak_domains,
            },
            "proposals": {
                "traces_analyzed": self.low_conf_traces_analyzed,
                "generated": self.proposals_generated,
                "saved": self.proposals_saved,
            },
            "improvements_applied": self.improvements_applied,
            "duration_ms": round(self.duration_ms, 1),
        }


class ContinuousLearningOrchestrator:
    """Orchestrates the full continuous learning cycle for Goal 5."""

    def __init__(
        self,
        db_session: AsyncSession,
        vectorstore,           # for KBEvaluator
        anthropic_api_key: str,
        kb_path: str = None,
    ):
        self.db = db_session
        self.evaluator = KBEvaluator(vectorstore, kb_path)
        self.generator = AutoActionGenerator(anthropic_api_key)
        self.collector = DistillationCollector(db_session)

    async def run(
        self,
        eval_sample_size: int = 100,  # seeds to eval
        max_proposals: int = 10,
        triggered_by: str = "manual",
    ) -> ContinuousLearningRun:
        """Execute one full continuous learning cycle. Returns a run summary."""

        run_start = time.monotonic()
        run_id = str(uuid.uuid4())

        logger.info("continuous_learning.start", run_id=run_id, trigger=triggered_by)

        # Step 1: Run eval
        eval_report = await self.evaluator.run_eval(sample_size=eval_sample_size)

        # Step 2: Collect low-confidence traces (last 24h, confidence < 0.4)
        low_conf_traces = await self._get_low_confidence_traces(
            confidence_threshold=0.4,
            limit=50,
        )

        # Step 3: Generate proposals
        proposals_from_eval = await self.generator.generate_from_eval(
            eval_report, max_proposals=max_proposals // 2
        )
        proposals_from_traces = await self.generator.generate_from_traces(
            low_conf_traces, max_proposals=max_proposals // 2
        )
        all_proposals = proposals_from_eval + proposals_from_traces

        # Step 4: Persist as StagedImprovement records (dedup by entity_id)
        saved = await self._save_proposals(all_proposals)

        # Step 5: Apply any already-approved improvements (from previous runs)
        applied = await self._apply_approved()

        duration = (time.monotonic() - run_start) * 1000

        result = ContinuousLearningRun(
            run_id=run_id,
            triggered_by=triggered_by,
            eval_recall_at_5=eval_report.recall_at_5,
            eval_seeds_run=eval_report.evaluated,
            weak_domains=eval_report.weak_domains,
            low_conf_traces_analyzed=len(low_conf_traces),
            proposals_generated=len(all_proposals),
            proposals_saved=saved,
            improvements_applied=applied,
            duration_ms=duration,
        )

        logger.info(
            "continuous_learning.complete",
            run_id=run_id,
            recall_at_5=f"{eval_report.recall_at_5:.1%}",
            proposals=len(all_proposals),
            applied=applied,
        )

        return result

    async def _get_low_confidence_traces(
        self, confidence_threshold: float = 0.4, limit: int = 50
    ) -> list:
        """Fetch recent low-confidence distillation records."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        stmt = (
            select(DistillationRecordModel)
            .where(
                DistillationRecordModel.confidence < confidence_threshold,
                DistillationRecordModel.created_at >= cutoff,
            )
            .order_by(DistillationRecordModel.confidence.asc())
            .limit(limit)
        )
        try:
            result = await self.db.execute(stmt)
            records = result.scalars().all()
        except Exception as e:
            logger.warning("continuous_learning.fetch_traces_failed", error=str(e))
            return []

        return [
            {
                "query": r.user_query[:500] if r.user_query else "",
                "confidence": r.confidence,
                "intent": r.intent,
                "entity": r.entity,
                "tools_used": r.tools_used or [],
                "record_id": str(r.id),
            }
            for r in records
        ]

    async def _save_proposals(self, proposals: list) -> int:
        """Persist proposals as StagedImprovement records. Skip duplicates."""
        saved = 0
        for p in proposals:
            try:
                # Dedup: skip if same entity_id already pending
                existing = await self.db.execute(
                    select(StagedImprovement).where(
                        StagedImprovement.proposed_entity_id == p.proposed_entity_id,
                        StagedImprovement.status == StagedImprovementStatus.pending,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                improvement = StagedImprovement(
                    improvement_type=StagedImprovementType(p.action_type),
                    status=StagedImprovementStatus.pending,
                    source_query=p.source_query[:500] if p.source_query else None,
                    source_confidence=p.source_confidence,
                    source_domain=p.source_domain,
                    proposed_entity_id=p.proposed_entity_id,
                    proposed_pillar=p.proposed_pillar,
                    proposed_content=json.dumps(p.proposed_content),
                    proposed_rationale=p.rationale,
                    eval_recall_before=p.eval_recall_before,
                    eval_domain=p.eval_domain,
                )
                self.db.add(improvement)
                saved += 1
            except Exception as e:
                logger.warning("continuous_learning.save_proposal_failed", error=str(e))
                await self.db.rollback()
                continue

        if saved > 0:
            try:
                await self.db.commit()
            except Exception as e:
                logger.warning("continuous_learning.commit_proposals_failed", error=str(e))
                await self.db.rollback()
                saved = 0

        return saved

    async def _apply_approved(self) -> int:
        """Apply approved improvements to KB. Returns count applied."""
        try:
            stmt = select(StagedImprovement).where(
                StagedImprovement.status == StagedImprovementStatus.approved
            )
            result = await self.db.execute(stmt)
            approved = result.scalars().all()
        except Exception as e:
            logger.warning("continuous_learning.fetch_approved_failed", error=str(e))
            return 0

        applied = 0
        for improvement in approved:
            try:
                # For now: mark as applied and log (actual KB write depends on kb_ingestor integration)
                improvement.status = StagedImprovementStatus.applied
                improvement.updated_at = datetime.now(timezone.utc)
                applied += 1
                logger.info(
                    "continuous_learning.applied",
                    entity_id=improvement.proposed_entity_id,
                    type=improvement.improvement_type,
                )
            except Exception as e:
                logger.warning("continuous_learning.apply_failed", error=str(e))

        if applied > 0:
            try:
                await self.db.commit()
            except Exception as e:
                logger.warning("continuous_learning.commit_applied_failed", error=str(e))
                await self.db.rollback()
                applied = 0

        return applied

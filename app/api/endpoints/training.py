"""
Training Pipeline API endpoints — trigger and monitor training jobs.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from app.db.session import get_db
from app.services.training import TrainingService

router = APIRouter()
_svc = TrainingService()


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class TriggerRequest(BaseModel):
    repo_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/embeddings", tags=["training"])
async def trigger_embedding_training(req: TriggerRequest) -> Dict[str, Any]:
    """Trigger embedding generation pipeline.

    Pulls text from knowledge entries and distillation records, generates
    TF-IDF embeddings, and writes them to pgvector. The work runs in the
    background; poll GET /jobs/{id} for status.
    """
    try:
        return await _svc.trigger_embedding_training(repo_id=req.repo_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/intent", tags=["training"])
async def trigger_intent_training(req: TriggerRequest) -> Dict[str, Any]:
    """Trigger intent classifier training.

    Pulls labeled intents from distillation records, trains a centroid-based
    TF-IDF classifier with leave-one-out cross-validation, and persists the
    model artefact. Runs in background.
    """
    try:
        return await _svc.trigger_intent_training(repo_id=req.repo_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/graph-weights", tags=["training"])
async def trigger_graph_weight_optimization(req: TriggerRequest) -> Dict[str, Any]:
    """Trigger graph weight optimization.

    Pulls tool execution records and outcome data, computes composite weights
    factoring success rate, feedback, confidence, and latency, then persists
    optimised weights. Runs in background.
    """
    try:
        return await _svc.trigger_graph_weight_optimization(repo_id=req.repo_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/page-role", tags=["training"])
async def trigger_page_role_training(req: TriggerRequest) -> Dict[str, Any]:
    """Trigger page-role embedding training from Pillar 4 data.

    Loads PageDocuments from the knowledge base, builds training documents
    from page summaries, fields, actions, and role info, generates TF-IDF
    embeddings, and stores them in pgvector. Also generates page-specific
    intent data from eval_cases. Runs in background.
    """
    try:
        return await _svc.trigger_page_role_training(repo_id=req.repo_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/cross-repo", tags=["training"])
async def trigger_cross_repo_training(req: TriggerRequest) -> Dict[str, Any]:
    """Trigger cross-repo navigation training.

    Loads cross_repo_mapping.yaml from each repo's pillar_4 directory,
    creates training pairs linking seller and admin page views, generates
    embeddings, and stores them in pgvector. Runs in background.
    """
    try:
        return await _svc.trigger_cross_repo_training(repo_id=req.repo_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/jobs", tags=["training"])
async def list_training_jobs(
    job_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List recent training jobs, optionally filtered by type."""
    return await _svc.list_training_jobs(
        job_type=job_type, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}", tags=["training"])
async def get_training_status(job_id: str) -> Dict[str, Any]:
    """Get the current status and metrics of a training job."""
    try:
        return await _svc.get_training_status(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# DPO / SFT Training Data Export
# ---------------------------------------------------------------------------

@router.get("/export/stats", tags=["training"])
async def get_training_data_stats(db=Depends(get_db)) -> Dict[str, Any]:
    """Get statistics about available DPO/SFT training data.

    Shows counts of chosen/rejected records, estimated DPO pairs,
    SFT-ready records, and top intents.
    """
    from app.learning.dpo_pipeline import DPOPipeline
    pipeline = DPOPipeline(db)
    return await pipeline.get_stats()


@router.get("/export/dpo", tags=["training"], response_class=PlainTextResponse)
async def export_dpo_training_data(
    min_chosen_feedback: int = 4,
    max_rejected_feedback: int = 2,
    limit: int = 5000,
    upload_s3: bool = True,
    db=Depends(get_db),
):
    """Export DPO training data as JSONL.

    Format: {"prompt": "...", "chosen": "...", "rejected": "...", "metadata": {...}}
    Compatible with HuggingFace TRL DPOTrainer.

    If upload_s3=true (default) and S3 is configured, auto-uploads to S3 and
    records it in cosmos_s3_exports.
    """
    from app.learning.dpo_pipeline import DPOPipeline
    pipeline = DPOPipeline(db)
    jsonl = await pipeline.export_jsonl(
        min_chosen_feedback=min_chosen_feedback,
        max_rejected_feedback=max_rejected_feedback,
        limit=limit,
    )

    if jsonl and upload_s3:
        await _push_export_to_s3(jsonl, "dpo", db)

    return PlainTextResponse(
        content=jsonl or "# No DPO pairs available yet. Collect more feedback.",
        media_type="application/jsonl",
        headers={"Content-Disposition": "attachment; filename=cosmos_dpo_training.jsonl"},
    )


@router.get("/export/sft", tags=["training"], response_class=PlainTextResponse)
async def export_sft_training_data(
    min_confidence: float = 0.7,
    min_feedback: int = 4,
    limit: int = 10000,
    upload_s3: bool = True,
    db=Depends(get_db),
):
    """Export SFT (Supervised Fine-Tuning) data as JSONL.

    Format: {"messages": [{"role": "user", ...}, {"role": "assistant", ...}], "metadata": {...}}
    Compatible with OpenAI fine-tuning format.

    If upload_s3=true (default) and S3 is configured, auto-uploads to S3.
    """
    from app.learning.dpo_pipeline import DPOPipeline
    pipeline = DPOPipeline(db)
    jsonl = await pipeline.export_sft_jsonl(
        min_confidence=min_confidence,
        min_feedback=min_feedback,
        limit=limit,
    )

    if jsonl and upload_s3:
        await _push_export_to_s3(jsonl, "sft", db)

    return PlainTextResponse(
        content=jsonl or "# No SFT records available yet. Collect more high-quality interactions.",
        media_type="application/jsonl",
        headers={"Content-Disposition": "attachment; filename=cosmos_sft_training.jsonl"},
    )


async def _push_export_to_s3(jsonl: str, export_type: str, db) -> None:
    """Fire-and-forget S3 upload for training exports. Records result in cosmos_s3_exports."""
    import asyncio
    import time

    async def _upload():
        try:
            from app.services.s3_client import S3Client
            from app.config import settings

            s3 = S3Client.from_settings()
            if not s3.enabled:
                return

            record_count = jsonl.count("\n") + 1
            s3_key = await s3.upload_training_export(
                jsonl, export_type, record_count, prefix=settings.S3_TRAINING_PREFIX
            )
            if not s3_key:
                return

            # Record in DB
            from app.db.session import AsyncSessionLocal
            from sqlalchemy import text
            from datetime import datetime
            async with AsyncSessionLocal() as session:
                await session.execute(text("""
                    INSERT INTO cosmos_s3_exports
                        (export_type, s3_key, s3_bucket, record_count, size_bytes, status)
                    VALUES
                        (:etype, :key, :bucket, :count, :size, 'uploaded')
                """), {
                    "etype": export_type,
                    "key": s3_key,
                    "bucket": settings.S3_BUCKET or "",
                    "count": record_count,
                    "size": len(jsonl.encode()),
                })
                await session.commit()
        except Exception as e:
            import structlog
            structlog.get_logger().warning("training.s3_upload_failed", export_type=export_type, error=str(e))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_upload())
    except RuntimeError:
        pass

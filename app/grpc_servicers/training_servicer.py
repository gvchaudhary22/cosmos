"""
gRPC servicer implementation for Training service.

Bridges gRPC requests to the underlying TrainingService,
converting between protobuf messages and domain objects.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict

import grpc
import structlog
from google.protobuf import timestamp_pb2

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.services.training import TrainingService

logger = structlog.get_logger(__name__)


def _job_dict_to_proto(job: Dict[str, Any]) -> cosmos_pb2.TrainingJobResponse:
    """Convert a training job dict to protobuf TrainingJobResponse.

    The underlying service returns a row-mapping with keys like ``id``,
    ``job_type``, ``status``, ``metrics``, ``started_at``, etc.
    """
    metrics_raw = job.get("metrics") or {}
    if isinstance(metrics_raw, str):
        import json
        try:
            metrics_raw = json.loads(metrics_raw)
        except (json.JSONDecodeError, TypeError):
            metrics_raw = {}

    metrics_map: Dict[str, str] = {}
    if isinstance(metrics_raw, dict):
        metrics_map = {k: str(v) for k, v in metrics_raw.items()}

    resp = cosmos_pb2.TrainingJobResponse(
        job_id=str(job.get("id", job.get("job_id", ""))),
        job_type=job.get("job_type", "") or "",
        repo_id=str(job.get("repo_id", "") or ""),
        status=job.get("status", "") or "",
        metrics=metrics_map,
        error=job.get("error", "") or "",
    )

    for field_name in ("started_at", "completed_at"):
        val = job.get(field_name)
        if val is not None:
            ts = timestamp_pb2.Timestamp()
            try:
                ts.FromDatetime(val)
                getattr(resp, field_name).CopyFrom(ts)
            except (TypeError, AttributeError, ValueError):
                pass

    return resp


class TrainingServicer(cosmos_pb2_grpc.TrainingServiceServicer):
    """gRPC servicer for the training pipeline service."""

    def __init__(self) -> None:
        self._svc = TrainingService()

    async def TriggerEmbeddingTraining(
        self, request: cosmos_pb2.TrainingRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TrainingJobResponse:
        """Trigger an embedding generation pipeline.

        Creates the job immediately and returns its record; the actual
        training runs asynchronously in the background.
        """
        logger.info("grpc.training.TriggerEmbeddingTraining", repo_id=request.repo_id)
        try:
            repo_id = request.repo_id or None
            job = await self._svc.trigger_embedding_training(repo_id=repo_id)
            return _job_dict_to_proto(job)
        except Exception as exc:
            logger.error("grpc.training.TriggerEmbeddingTraining.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TrainingJobResponse()

    async def TriggerIntentTraining(
        self, request: cosmos_pb2.TrainingRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TrainingJobResponse:
        """Trigger intent classifier training pipeline."""
        logger.info("grpc.training.TriggerIntentTraining", repo_id=request.repo_id)
        try:
            repo_id = request.repo_id or None
            job = await self._svc.trigger_intent_training(repo_id=repo_id)
            return _job_dict_to_proto(job)
        except Exception as exc:
            logger.error("grpc.training.TriggerIntentTraining.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TrainingJobResponse()

    async def TriggerGraphWeightOptimization(
        self, request: cosmos_pb2.TrainingRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TrainingJobResponse:
        """Trigger graph weight optimization pipeline."""
        logger.info("grpc.training.TriggerGraphWeightOptimization", repo_id=request.repo_id)
        try:
            repo_id = request.repo_id or None
            job = await self._svc.trigger_graph_weight_optimization(repo_id=repo_id)
            return _job_dict_to_proto(job)
        except Exception as exc:
            logger.error("grpc.training.TriggerGraphWeightOptimization.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TrainingJobResponse()

    async def GetTrainingStatus(
        self, request: cosmos_pb2.GetJobRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TrainingJobResponse:
        """Get the current status of a training job."""
        logger.info("grpc.training.GetTrainingStatus", job_id=request.job_id)
        try:
            job = await self._svc.get_training_status(request.job_id)
            return _job_dict_to_proto(job)
        except ValueError as exc:
            logger.error("grpc.training.GetTrainingStatus.not_found", error=str(exc))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return cosmos_pb2.TrainingJobResponse()
        except Exception as exc:
            logger.error("grpc.training.GetTrainingStatus.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TrainingJobResponse()

    async def ListTrainingJobs(
        self, request: cosmos_pb2.ListJobsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ListJobsResponse:
        """List recent training jobs with optional type filter."""
        logger.info("grpc.training.ListTrainingJobs", job_type=request.job_type)
        try:
            limit = request.limit if request.limit > 0 else 50
            job_type = request.job_type or None
            jobs = await self._svc.list_training_jobs(job_type=job_type, limit=limit)
            return cosmos_pb2.ListJobsResponse(
                jobs=[_job_dict_to_proto(j) for j in jobs],
            )
        except Exception as exc:
            logger.error("grpc.training.ListTrainingJobs.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ListJobsResponse()

    async def WatchTrainingJob(
        self, request: cosmos_pb2.GetJobRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[cosmos_pb2.TrainingProgress]:
        """Stream live training job progress until the job completes or fails.

        Polls the job status every 2 seconds and yields a
        ``TrainingProgress`` message. The stream ends when the job reaches
        ``completed`` or ``failed`` status, or after 200 seconds (100 polls).
        """
        logger.info("grpc.training.WatchTrainingJob", job_id=request.job_id)
        try:
            for _ in range(100):
                try:
                    job = await self._svc.get_training_status(request.job_id)
                except ValueError:
                    context.set_code(grpc.StatusCode.NOT_FOUND)
                    context.set_details(f"Training job {request.job_id} not found")
                    return

                status = job.get("status", "unknown") or "unknown"
                metrics_raw = job.get("metrics") or {}
                if isinstance(metrics_raw, str):
                    import json
                    try:
                        metrics_raw = json.loads(metrics_raw)
                    except (json.JSONDecodeError, TypeError):
                        metrics_raw = {}

                metrics_map: Dict[str, str] = {}
                if isinstance(metrics_raw, dict):
                    metrics_map = {k: str(v) for k, v in metrics_raw.items()}

                # Estimate progress from status
                progress = 0.0
                if status == "queued":
                    progress = 0.0
                elif status == "running":
                    progress = float(metrics_raw.get("progress", 0.5))
                elif status == "completed":
                    progress = 1.0
                elif status == "failed":
                    progress = 0.0

                yield cosmos_pb2.TrainingProgress(
                    job_id=request.job_id,
                    progress=progress,
                    stage=status,
                    message=f"Job is {status}",
                    metrics=metrics_map,
                )

                if status in ("completed", "failed"):
                    return

                await asyncio.sleep(2)

        except Exception as exc:
            logger.error("grpc.training.WatchTrainingJob.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))

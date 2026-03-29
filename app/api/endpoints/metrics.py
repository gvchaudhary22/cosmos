"""Prometheus-compatible metrics endpoint for COSMOS."""

from fastapi import APIRouter, Response

from app.monitoring.metrics import collect_all

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    return Response(content=collect_all(), media_type="text/plain; charset=utf-8")

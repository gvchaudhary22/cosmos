"""Metrics middleware for recording request-level Prometheus metrics."""

import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.monitoring.metrics import METRICS


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request metrics for every API call."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        # Skip recording for the metrics endpoint itself to avoid feedback loops
        if request.url.path == "/cosmos/metrics":
            return response

        # Record request metrics
        if METRICS:
            METRICS["cosmos_requests_total"].inc(
                method=request.method,
                endpoint=request.url.path,
                status=str(response.status_code),
            )
            METRICS["cosmos_request_duration_seconds"].observe(
                duration,
                method=request.method,
                endpoint=request.url.path,
            )

        return response

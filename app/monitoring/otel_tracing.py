"""
OpenTelemetry tracing — Phase 6d: Wave-level spans for COSMOS.

Provides:
  - configure_tracer()   : set up OTLP exporter once at startup
  - wave_span()          : async context manager that wraps a wave or stage in a span
  - record_wave_event()  : record a wave progress dict as span attributes + event

Architecture
------------
All tracing is best-effort — every public function in this module catches
exceptions and logs a debug warning rather than propagating errors.  OTEL is
an observability tool and must never break the production query path.

Span hierarchy (per request):
  cosmos.request
    ├── cosmos.wave.1  (wave1_scope_detect)
    ├── cosmos.wave.2  (wave2_deep_retrieval)
    │     ├── cosmos.retrieval.hybrid
    │     └── cosmos.retrieval.vector
    ├── cosmos.wave.3  (wave3_langgraph)  — only when enabled
    ├── cosmos.wave.4  (wave4_neo4j)      — only when enabled
    └── cosmos.wave.5  (wave5_page_intel) — only when page_signal=True

Configuration (via environment variables):
  OTEL_EXPORTER_OTLP_ENDPOINT  — default: http://localhost:4317
  OTEL_SERVICE_NAME            — default: cosmos
  OTEL_TRACES_SAMPLER          — default: parentbased_always_on
  COSMOS_OTEL_ENABLED          — set to "false" to disable (default: true)
"""

import contextlib
import logging
import os
from typing import Any, Dict, Generator, Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_tracer = None
_otel_enabled: bool = os.environ.get("COSMOS_OTEL_ENABLED", "true").lower() != "false"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def configure_tracer(service_name: str = "") -> None:
    """
    Initialise the OTLP tracer.  Safe to call multiple times — idempotent.

    Parameters
    ----------
    service_name:
        Override for OTEL_SERVICE_NAME env var (default: "cosmos").
    """
    global _tracer, _otel_enabled

    if not _otel_enabled:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        svc = service_name or os.environ.get("OTEL_SERVICE_NAME", "cosmos")
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

        resource = Resource.create({"service.name": svc})
        provider = TracerProvider(resource=resource)

        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(svc)
        logger.info("otel_tracing.configured", service=svc, endpoint=endpoint)

    except ImportError:
        # opentelemetry-sdk or exporter not installed — disable silently
        _otel_enabled = False
        logger.info("otel_tracing.disabled",
                    reason="opentelemetry packages not installed")
    except Exception as exc:
        _otel_enabled = False
        logger.warning("otel_tracing.setup_failed", error=str(exc))


def _get_tracer():
    """Return the configured tracer, or None if OTEL is unavailable."""
    global _tracer, _otel_enabled
    if not _otel_enabled:
        return None
    if _tracer is None:
        configure_tracer()
    return _tracer


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def wave_span(
    wave_id: int,
    task_id: str,
    attributes: Optional[Dict[str, Any]] = None,
):
    """
    Async context manager that wraps a wave execution in an OTEL span.

    Usage::

        async with wave_span(1, "wave1_scope_detect", {"query_len": 42}):
            ...wave logic...

    Always yields — even when OTEL is unavailable (no-op span).
    """
    tracer = _get_tracer()
    span_name = f"cosmos.wave.{wave_id}"

    if tracer is None:
        # OTEL unavailable — no-op
        yield None
        return

    try:
        from opentelemetry import trace
        with tracer.start_as_current_span(span_name) as span:
            try:
                span.set_attribute("wave.id", wave_id)
                span.set_attribute("wave.task_id", task_id)
                if attributes:
                    for k, v in attributes.items():
                        _safe_set_attribute(span, f"wave.{k}", v)
                yield span
            except Exception as inner_exc:
                if span.is_recording():
                    span.record_exception(inner_exc)
                    span.set_status(
                        trace.Status(trace.StatusCode.ERROR, str(inner_exc))
                    )
                raise
    except Exception as exc:
        # Span setup failed — do not break the query path
        logger.debug("otel_tracing.span_error", wave=wave_id, error=str(exc))
        yield None


@contextlib.asynccontextmanager
async def retrieval_span(
    leg_name: str,
    attributes: Optional[Dict[str, Any]] = None,
):
    """
    Async context manager for a single HybridRetriever leg span.

    Usage::

        async with retrieval_span("vector_search", {"top_k": 10}):
            ...

    Span name: cosmos.retrieval.<leg_name>
    """
    tracer = _get_tracer()
    span_name = f"cosmos.retrieval.{leg_name}"

    if tracer is None:
        yield None
        return

    try:
        from opentelemetry import trace
        with tracer.start_as_current_span(span_name) as span:
            try:
                span.set_attribute("retrieval.leg", leg_name)
                if attributes:
                    for k, v in attributes.items():
                        _safe_set_attribute(span, f"retrieval.{k}", v)
                yield span
            except Exception as inner_exc:
                if span.is_recording():
                    span.record_exception(inner_exc)
                    span.set_status(
                        trace.Status(trace.StatusCode.ERROR, str(inner_exc))
                    )
                raise
    except Exception as exc:
        logger.debug("otel_tracing.retrieval_span_error", leg=leg_name, error=str(exc))
        yield None


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

def record_wave_event(
    span,
    wave_id: int,
    task_id: str,
    status: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Record a wave progress event as a span event + attributes.

    Parameters
    ----------
    span:
        Active OTEL span (from wave_span context manager), or None.
    wave_id, task_id, status:
        From _emit_wave_progress() in QueryOrchestrator.
    data:
        Optional dict of extra key/value attributes.
    """
    if span is None:
        return
    try:
        if not span.is_recording():
            return
        event_attrs = {
            "wave.id": wave_id,
            "wave.task_id": task_id,
            "wave.status": status,
        }
        if data:
            for k, v in data.items():
                event_attrs[f"wave.data.{k}"] = str(v)[:256]
        span.add_event(f"wave:{wave_id}:{status}", attributes=event_attrs)
    except Exception as exc:
        logger.debug("otel_tracing.record_event_error", error=str(exc))


def record_span_attribute(span, key: str, value: Any) -> None:
    """Safely set a single attribute on an active span."""
    if span is None:
        return
    try:
        if span.is_recording():
            _safe_set_attribute(span, key, value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request-level span
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def request_span(
    query: str,
    user_id: str = "",
    repo_id: str = "",
    session_id: str = "",
):
    """
    Top-level span for a complete COSMOS request.

    Usage (in QueryOrchestrator.execute or hybrid_chat endpoint)::

        async with request_span(query, user_id=uid, repo_id=repo_id):
            result = await orchestrator.execute(...)
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    try:
        from opentelemetry import trace
        with tracer.start_as_current_span("cosmos.request") as span:
            try:
                _safe_set_attribute(span, "request.query_len", len(query))
                _safe_set_attribute(span, "request.user_id", user_id or "anon")
                _safe_set_attribute(span, "request.repo_id", repo_id or "")
                _safe_set_attribute(span, "request.session_id", session_id or "")
                yield span
            except Exception as inner_exc:
                if span.is_recording():
                    span.record_exception(inner_exc)
                    span.set_status(
                        trace.Status(trace.StatusCode.ERROR, str(inner_exc))
                    )
                raise
    except Exception as exc:
        logger.debug("otel_tracing.request_span_error", error=str(exc))
        yield None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_set_attribute(span, key: str, value: Any) -> None:
    """Convert value to OTEL-compatible type before setting."""
    if value is None:
        return
    if isinstance(value, (bool, int, float, str)):
        span.set_attribute(key, value)
    elif isinstance(value, (list, tuple)):
        # OTEL supports homogeneous lists of primitives
        span.set_attribute(key, str(value)[:512])
    else:
        span.set_attribute(key, str(value)[:512])

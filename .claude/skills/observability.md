# SKILL: Observability (COSMOS)
> If you can't measure COSMOS's retrieval quality, you don't own it. Every query leaves a trace.

## ACTIVATION
Auto-loaded for monitoring, alerting, logging, health endpoints, or production readiness tasks.

## CORE PRINCIPLES
1. **Measured Quality**: Track recall@5, confidence distribution, and latency — not just uptime.
2. **Structured Logs**: All logs are JSON with `correlation_id`, `query_id`, `pillar`, `wave`.
3. **Symptom-Based Alerts**: Alert on user-facing degradation (low confidence, high latency, BLOCKED responses), not causes (CPU, memory).
4. **Design-In**: Observability is part of every retrieval feature, not post-ship.
5. **Trace Every Wave**: Each retrieval wave must emit timing + result count + confidence.

## COSMOS-SPECIFIC METRICS (SLIs)

| Metric | Type | Target |
|--------|------|--------|
| `cosmos_query_latency_p95_seconds` | Histogram | < 2.0s |
| `cosmos_confidence_score` | Gauge | > 0.6 avg |
| `cosmos_recall_at_5` | Gauge | > 0.75 |
| `cosmos_hallucination_blocks_total` | Counter | < 1% of queries |
| `cosmos_retrieval_leg_hits` | Counter per leg | Monitor distribution |
| `cosmos_wave_duration_seconds` | Histogram per wave | < 0.5s per wave |
| `cosmos_kb_chunks_indexed` | Gauge per pillar | Monitor freshness |

## PATTERNS

### Structured Logging (structlog)
```python
import structlog

log = structlog.get_logger()

# Query lifecycle
log.info("query.received",
    query_id=query_id,
    correlation_id=correlation_id,
    query_length=len(query),
    source="mars_api"
)

log.info("wave.completed",
    query_id=query_id,
    wave=1,
    legs_activated=["exact", "ppr", "vector"],
    results_count=15,
    duration_ms=142
)

log.info("response.generated",
    query_id=query_id,
    confidence=0.82,
    citations=3,
    model="claude-opus-4-6",
    total_duration_ms=890
)

# Blocks and errors
log.warning("hallucination.blocked",
    query_id=query_id,
    fabricated_ids=["ORD-FAKE-123"],
    blocked=True
)

log.error("retrieval.failed",
    query_id=query_id,
    error_code="ERR-COSMOS-003",
    leg="qdrant_vector",
    error=str(e)
)
```

### Health Endpoints
```python
# /health — shallow (is the process alive?)
@app.get("/health")
async def health():
    return {"status": "ok", "service": "cosmos", "version": VERSION}

# /ready — deep (are all dependencies connected?)
@app.get("/ready")
async def ready():
    checks = {
        "qdrant": await check_qdrant(),      # port 6333
        "neo4j": await check_neo4j(),         # port 7687
        "mysql": await check_mysql(),          # port 3309
        "redis": await check_redis(),          # port 6380
    }
    all_healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={"status": "ready" if all_healthy else "degraded", "checks": checks}
    )

# /metrics — Prometheus format
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

### Correlation IDs
```python
# Middleware injects correlation_id from MARS request headers
class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
```

### Wave Tracing
```python
# Every wave emits start/end with timing
async def execute_wave(wave_num: int, legs: list, query_id: str):
    start = time.monotonic()
    log.info("wave.start", query_id=query_id, wave=wave_num, legs=legs)
    try:
        results = await asyncio.gather(*[leg() for leg in legs])
        duration_ms = (time.monotonic() - start) * 1000
        log.info("wave.end", query_id=query_id, wave=wave_num,
                 results=len(results), duration_ms=duration_ms)
        WAVE_DURATION.labels(wave=wave_num).observe(duration_ms / 1000)
        return results
    except Exception as e:
        log.error("wave.failed", query_id=query_id, wave=wave_num, error=str(e))
        raise
```

## CHECKLISTS

### Production Readiness
- [ ] All logs use `structlog` JSON format — no plain `print()` or `logging.info("string")`
- [ ] Every log line has `correlation_id` and `query_id`
- [ ] `/health` and `/ready` endpoints configured and tested
- [ ] Prometheus metrics exposed at `/metrics`
- [ ] Alerts defined for: confidence < 0.4 avg, P95 latency > 3s, error rate > 2%
- [ ] Runbook exists in `docs/operations/playbooks.md` for every alert
- [ ] `tests/test_health.py` covers both `/health` and `/ready`

## ANTI-PATTERNS
- **String Logging**: `log.info(f"Query {query} processed")` — use structured fields.
- **Missing query_id**: Logs without `query_id` are untraceable across wave execution.
- **CPU-Based Alerts**: Paging on CPU > 80% with no user impact.
- **Silent Confidence**: Not logging when confidence gate refuses a query.
- **Untraced External Calls**: Calling Anthropic API without timing and logging the call.

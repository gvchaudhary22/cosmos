# Cosmos Error Code Registry

Stable, searchable error identifiers for logs, alerts, and runbooks.
Format: `ERR-COSMOS-NNN` — logged at structured key `error_code`.

---

## ERR-COSMOS-001 — Data Validation Failed

**Trigger:** Input data failed validation (feature out of range, >5% missing values, schema mismatch).
**Logged as:** `error_code=ERR-COSMOS-001`
**Runbook:**
1. Check validation report in response body for specific failing features
2. Verify input schema matches `app/guardrails/` validators
3. For missing values: check upstream data pipeline for collection gaps
4. For out-of-range: verify feature normalization/scaling applied correctly
5. Temporary bypass (admin only): set `COSMOS_STRICT_VALIDATION=false` (logs warning)

---

## ERR-COSMOS-002 — Inference Timeout

**Trigger:** AI model (Anthropic API) did not respond within `COSMOS_INFERENCE_TIMEOUT_SECONDS`.
**Logged as:** `error_code=ERR-COSMOS-002`
**Runbook:**
1. Check Anthropic API status
2. Review prompt size — large prompts have higher latency; consider chunking
3. Switch to faster model: `classify` profile (Haiku) for the specific task
4. Increase `COSMOS_INFERENCE_TIMEOUT_SECONDS` if task requires long reasoning
5. Check network latency to Anthropic endpoints from deployment environment

---

## ERR-COSMOS-003 — Knowledge Graph Query Failed

**Trigger:** Knowledge graph query exceeded timeout or returned corrupt/incomplete data.
**Logged as:** `error_code=ERR-COSMOS-003`
**Runbook:**
1. Check graph DB connectivity (Neo4j/PostgreSQL status)
2. Verify graph schema consistency: `python -m cosmos.tools.validate_graph`
3. For timeout: optimize query with index hints, limit traversal depth
4. Check for cycles in graph that cause infinite traversal
5. Restart graph query cache if stale: `POST /admin/graph/reset-cache`

---

## ERR-COSMOS-004 — Model Confidence Below Threshold

**Trigger:** Model output confidence score below `COSMOS_MIN_CONFIDENCE` (default: 0.5).
**Logged as:** `error_code=ERR-COSMOS-004`
**Runbook:**
1. Review confidence threshold in `app/config.py` — may be too high for this task
2. Check if input was outside training distribution (novelty score > 0.8)
3. Route to human review queue if confidence < 0.3
4. Check if model needs retraining on recent data (learning module)
5. Log the low-confidence case for future training data collection

---

## ERR-COSMOS-005 — Learning Pipeline Failure

**Trigger:** Continuous learning pipeline failed to process feedback or update model state.
**Logged as:** `error_code=ERR-COSMOS-005`
**Runbook:**
1. Check `app/learning/` logs for specific failure point
2. Verify feedback data schema matches expected format
3. Check disk/memory for training data storage limits
4. Inspect `learning_sessions` DB table for stuck session records
5. Reset learning pipeline: `POST /admin/learning/reset` (drops in-progress batch)

---

## ERR-COSMOS-006 — gRPC Service Unavailable

**Trigger:** gRPC server is not responding or returned a non-OK status.
**Logged as:** `error_code=ERR-COSMOS-006`
**Runbook:**
1. Check gRPC server health: `grpc_health_probe -addr=:50051`
2. Verify `grpc_server.py` process is running
3. Check gRPC servicer logs in `grpc_servicers/` for panic/exception
4. Restart gRPC server if crashed: it should auto-restart via process manager
5. If client-side: verify proto version matches server (regenerate via `protoc`)

---

## ERR-COSMOS-007 — Guardrail Policy Violation

**Trigger:** Request was blocked by guardrails policy (prompt injection, toxic content, scope violation).
**Logged as:** `error_code=ERR-COSMOS-007`
**Runbook:**
1. Review the violated policy in `app/guardrails/`
2. Check if policy is overly strict for this use case — adjust threshold
3. If prompt injection detected, investigate the upstream source of the request
4. Log violation for security audit trail
5. Never auto-bypass guardrails — escalate policy changes through review

---

## Usage in Code

```python
import structlog

log = structlog.get_logger()

# Log with structured error code
log.error(
    "data validation failed",
    error_code="ERR-COSMOS-001",
    feature="order_value",
    value=input_val,
    expected_range="[0, 1000000]"
)

# Raise with code
raise ValueError("[ERR-COSMOS-001] Data validation failed: order_value out of range")
```

## Alerting

Configure alerts on error code patterns:
```
# Datadog monitor
count(logs("error_code:ERR-COSMOS-*" service:cosmos)) > 5 in 5m → page on-call

# Individual codes for routing:
ERR-COSMOS-001 → data-engineering on-call
ERR-COSMOS-002, ERR-COSMOS-004 → ai-platform on-call
ERR-COSMOS-003 → platform/db on-call
ERR-COSMOS-007 → security on-call (immediate)
```

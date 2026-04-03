# Skill: review

## Purpose
Structured code and architecture review for COSMOS. Produces severity-ranked findings with actionable fix recommendations.

## Loaded By
`reviewer` · `security-engineer`

---

## Review Checklist

### P0 — Block (must fix before merge)
- [ ] SQL injection vectors (raw string interpolation in queries)
- [ ] Cross-tenant data leakage (missing `company_id` filter)
- [ ] Secrets in code or logs
- [ ] Sync I/O in async context (blocking the event loop)
- [ ] Uncaught exceptions that crash the FastAPI app
- [ ] HallucinationGuard or ConfidenceGate bypassed
- [ ] Tests deleted or commented out

### P1 — Fix before merge (correctness)
- [ ] Missing `await` on coroutine (silently produces wrong result)
- [ ] DB session not closed on error path (`async with` pattern missing)
- [ ] Embedding dimension mismatch (must be 1536d for text-embedding-3-small)
- [ ] Wrong confidence threshold (< 0.3 must refuse, not return empty)
- [ ] `CREATE INDEX IF NOT EXISTS` in MySQL (not supported, causes startup error)
- [ ] JSON column with `DEFAULT '{}'` in MySQL (not supported)

### P2 — Should fix (quality)
- [ ] Missing structured log with `error_code=ERR-COSMOS-NNN`
- [ ] Magic numbers not in config (thresholds, timeouts, pool sizes)
- [ ] No `try/except` around external calls (Qdrant, Neo4j, MCAPI)
- [ ] `os.environ.get()` in service code instead of `settings.*`
- [ ] Missing type annotations on public functions

### P3 — Nice to have (polish)
- [ ] Docstring missing on public class
- [ ] Test coverage < 80% on new code
- [ ] TODO comment without issue number

---

## Review Report Format

```markdown
## Review: [PR / Change Description]
**Date:** YYYY-MM-DD
**Reviewer:** reviewer agent
**Verdict:** ✓ approve | ✗ block | ⚠ approve-with-comments

### P0 Blockers
[none] or:
- **[file:line]** [description] — [fix recommendation]

### P1 Fixes
- **[file:line]** [description] — [fix recommendation]

### P2 Suggestions
- **[file:line]** [description]

### P3 Polish
- **[file:line]** [description]

### What's Good
- [positive observation 1]
- [positive observation 2]
```

---

## COSMOS-Specific Review Patterns

### Retrieval pipeline changes
When reviewing changes to `app/graph/retrieval.py` or `app/engine/wave_executor.py`:
- Verify RRF weights sum to a consistent total
- Verify all 5 legs are still called in parallel (not sequential)
- Check that MMR diversity filter is applied after reranking
- Verify `lost-in-middle` prevention still places best docs at positions 0 and -1

### Guardrail changes
When reviewing changes to `app/guardrails/`:
- Verify `HallucinationGuard` threshold is not relaxed without ADR
- Verify `ConfidenceGate` lower bound is not below 0.3 without ADR
- Verify tenant isolation filter (`company_id`) is preserved

### KB ingestor changes
When reviewing changes to `app/services/kb_ingestor.py` or `chunker.py`:
- Verify quality gate still rejects < 50 char content
- Verify chunk size stays in 200-500 token range
- Verify `content_hash` skip logic is preserved (never re-embed unchanged)
- Check `trust_score` assignment is correct (0.9 human / 0.7 auto)

### New endpoint changes
When reviewing a new `app/api/endpoints/` file:
- Verify it's registered in `app/api/routes.py`
- Verify auth middleware is applied (MARS JWT forwarded)
- Verify rate limiter is active (60 req/min default)
- Verify Prometheus metrics middleware is active

---

## Ship Gate Checklist
Before recommending ship:
- [ ] All P0 and P1 findings resolved
- [ ] `pytest tests/ -x -q` passes
- [ ] `ruff check app/` passes
- [ ] `recall@5 > 0.75` (if retrieval changes)
- [ ] No new `[ERROR]` level startup messages
- [ ] CHANGELOG.md updated

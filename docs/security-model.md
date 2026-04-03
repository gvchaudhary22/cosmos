# Security Model

## Threat Surface

COSMOS sits at the intersection of user input, LLM inference, and Shiprocket production data. The primary threats:

1. **Prompt injection** — malicious KB content or user input hijacking the LLM
2. **Tenant isolation breach** — ICRM operator A seeing company B's data
3. **Hallucination as attack surface** — fabricated entity IDs or API calls executed as real
4. **Secret exfiltration** — API keys or credentials leaking via LLM response
5. **Privilege escalation** — operator requesting destructive actions without approval
6. **Supply chain** — malicious packages in `requirements.txt`

---

## Defense Layers

### Layer 1: Prompt Injection Defense

Every user message and KB chunk is sanitized before injection into the LLM prompt:

```python
# app/guardrails/rules.py
INJECTION_PATTERNS = [
    r"ignore previous instructions",
    r"you are now",
    r"system:\s*you",
    r"\\n\\nHuman:",
    r"<\|im_start\|>",
]
```

KB content with injection patterns is flagged with `trust_score = 0.0` and excluded from context.

Factuality prompt (10 rules injected into every call) prevents instruction hijacking by keeping the LLM focused on the retrieved context.

### Layer 2: Tenant Isolation

Every DB query includes `company_id` filter. No cross-tenant data ever enters the same context window.

```python
# Enforced in every service
async def search_orders(query: str, company_id: str) -> list:
    sql = "SELECT * FROM orders WHERE company_id = :company_id AND ..."
    result = await session.execute(text(sql), {"company_id": company_id})
```

`mars_safety.py` guardrail blocks any response that contains `company_id` values not matching the current session.

### Layer 3: Anti-Hallucination (8 layers)

See `docs/architecture.md` — Anti-Hallucination System. Key controls:

- **HallucinationGuard** (`app/guardrails/advanced_guards.py`): BLOCK if 3+ entity IDs in response not in retrieved context
- **GroundingChecker** (`app/engine/grounding.py`): ≥ 30% of response terms must appear in context
- **ConfidenceGate** (`app/engine/confidence.py`): < 0.3 → refuse entirely

### Layer 4: Approval Gate for Destructive Actions

Write tools (cancel order, reattempt pickup, etc.) require explicit approval:

```python
# app/engine/approval.py
if tool.blast_radius >= BlastRadius.IRREVERSIBLE:
    require_approval(tool, operator_id, session_id)
```

Blast radius levels: `LOW` (read-only) · `MEDIUM` (reversible write) · `HIGH` (irreversible) · `IRREVERSIBLE` (permanent).

`IRREVERSIBLE` actions always require human approval. `HIGH` actions require approval in production, auto-approved in dry-run mode.

### Layer 5: Secret Protection

Pre-commit hook scans for secrets before every commit:
```bash
# .claude/hooks/pre-commit.sh
SECRET_PATTERNS=(
    'sk-[a-zA-Z0-9]{40,}'          # Anthropic API keys
    'AKIA[0-9A-Z]{16}'             # AWS access keys
    'password\s*=\s*["\x27][^"]+' # Hardcoded passwords
)
```

`pre-tool-use.sh` blocks bash commands that write to `.env` files or emit base64-encoded strings.

### Layer 6: Rate Limiting

HTTP rate limiter (`app/middleware/rate_limiter.py`): 60 req/min per session by default.

MCAPI rate limit: 100 req/10s (configured in `settings.MCAPI_RATE_LIMIT`).

Circuit breaker (`app/engine/circuit_breaker.py`): opens after 5 consecutive upstream failures, closes after 30s.

---

## Hook Safety

Hooks are the enforcement layer for invariants. They must not be tampered with.

`pre-tool-use.sh` blocks:
- `git commit --no-verify` (bypass pre-commit hook)
- `git push --force origin main` (force-push to protected branch)
- `rm -rf` on non-temp directories
- `curl | bash` (arbitrary remote code execution)
- Deletion or modification of `.claude/` files
- Writing to `.env` via bash echo/redirect

If a hook is blocking a legitimate operation, investigate the root cause. Never disable hooks to "fix" a problem — fix the problem instead.

---

## Dependency Security (SCA)

`requirements.txt` is scanned by `safety check` in CI.

Known-safe package versions are pinned. Unpinned dependencies are a P0 security finding.

```bash
# Run locally
.venv/bin/safety check -r requirements.txt
```

Critical CVEs block deployment. High CVEs require ADR before waiving.

---

## OWASP Top 10 Mapping

| OWASP | COSMOS Control |
|-------|---------------|
| A01: Broken Access Control | Tenant isolation via `company_id` in all queries |
| A02: Cryptographic Failures | No secrets in code (pre-commit scan), HTTPS only |
| A03: Injection | SQL params always bound (`text(sql), {"param": value}`), no string interpolation |
| A04: Insecure Design | Approval gate for write tools, blast radius model |
| A05: Security Misconfiguration | `.env.example` documents all required settings |
| A06: Vulnerable Components | `safety check` in CI |
| A07: Auth Failures | Auth delegated to MARS (JWT forwarded, never re-implemented) |
| A08: Software Integrity | Pinned deps, pre-commit hook |
| A09: Logging Failures | Structured logging with `error_code=ERR-COSMOS-NNN` on every error |
| A10: SSRF | All outbound HTTP via `app/clients/` with allowlisted domains |

---

## Incident Response

For security incidents, file a private report via `SECURITY.md` process.

Escalation path:
1. Identify the affected guardrail layer
2. Check `app/monitoring/` for recent anomaly signals
3. Review `cosmos_feedback_traces` table for abnormal confidence patterns
4. Check wave trace logs for injection attempts
5. Rotate affected credentials immediately

For hallucination incidents:
1. Check `HallucinationGuard` logs (`cosmos.hallucination_blocked`)
2. Identify which KB chunk provided the grounding failure
3. Flag chunk with `trust_score = 0.0`
4. Trigger re-ingest of affected pillar
5. Run eval benchmark to verify recovery

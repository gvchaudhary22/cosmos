# Security Policy

## Reporting a Vulnerability

**Do not report security vulnerabilities in GitHub issues.**

Report security vulnerabilities privately to the AI Platform team:
- **Email:** ai-platform-security@shiprocket.com
- **Slack:** #ai-platform-security (private channel)
- Include: description, reproduction steps, impact assessment, affected versions

We will acknowledge receipt within 24 hours and provide a remediation timeline within 72 hours.

---

## Threat Model

See `docs/security-model.md` for the full threat model. Summary:

| Threat | Primary Defense |
|--------|----------------|
| Prompt injection | Sanitization + factuality prompt + trust scores |
| Tenant isolation breach | `company_id` filters on all DB queries + `mars_safety.py` guardrail |
| Hallucination (fabricated data) | 8-layer anti-hallucination pipeline |
| Secret exfiltration | Pre-commit scan + pre-tool-use hook |
| Privilege escalation | Approval gate + blast radius model |
| Supply chain | Pinned deps + `safety check` in CI |
| Unauthorized API access | MARS JWT validation on every request |

---

## Supported Versions

| Version | Security support |
|---------|-----------------|
| `main` | ✓ Active |
| `develop` | ✓ Active |
| Any tagged release | ✓ For 90 days after release |
| Older branches | ✗ No support |

---

## Security-Relevant Configuration

The following settings are security-sensitive. Do not expose in logs, metrics, or API responses:

```bash
ANTHROPIC_API_KEY       # LLM API key
AIGATEWAY_API_KEY       # AI Gateway key
AWS_ACCESS_KEY_ID       # S3 credentials
AWS_SECRET_ACCESS_KEY   # S3 credentials
MARS_DB_PASSWORD        # Database password
NEO4J_PASSWORD          # Graph DB password
KAFKA_CLUSTER_PASSWORD  # Kafka SASL credential
```

All of the above must be in `.env` only. Never commit to git (enforced by pre-commit scan).

---

## Security Gates in CI

Every PR must pass:
1. **Secret scan** — zero secrets in changed files
2. **OWASP dependency check** — `safety check -r requirements.txt` (zero critical CVEs)
3. **Pre-tool-use hook** — blocks injection, destructive commands, env writes

---

## Known Limitations

1. **Local mutex only** — `.cosmos/state/STATE.md` locking is filesystem-local. In distributed runner setups, there is no cross-runner mutual exclusion.
2. **MCP auth delegation** — MCP tool calls rely on MARS JWT validation. Compromised MARS credentials grant COSMOS access.
3. **KB trust scores** — Auto-generated KB content has `trust_score: 0.7`. This reduces but does not eliminate the risk of low-quality content reaching the LLM context.

---

## Dependency Baseline

The following packages have elevated privilege or surface area. Pin these to exact versions:

| Package | Risk | Reason |
|---------|------|--------|
| `anthropic` | High | LLM API access |
| `qdrant-client` | Medium | Vector DB writes |
| `neo4j` | Medium | Graph DB writes |
| `aiomysql` | Medium | Relational DB access |
| `aiokafka` | Medium | Event stream access |
| `fastapi` | Medium | HTTP surface |

Run `safety check` after any dependency update:
```bash
.venv/bin/safety check -r requirements.txt
```

---

## Incident Severity Levels

| Level | Description | Response time |
|-------|-------------|---------------|
| P0: Critical | Tenant isolation breach, active data exfiltration | < 1 hour |
| P1: High | Guardrail bypass, secret exposure | < 4 hours |
| P2: Medium | Hallucination spike, auth degradation | < 24 hours |
| P3: Low | Config exposure, low-impact misconfig | < 72 hours |

For P0/P1: immediately rotate affected credentials and notify affected tenants.

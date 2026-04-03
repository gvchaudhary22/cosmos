# SKILL: Security & Identity (COSMOS)
> COSMOS receives queries from sellers, operators, and support agents. Every input is untrusted until validated.

## ACTIVATION
Auto-loaded for security audits, guardrail design, prompt injection defense, KB safety, input validation, auth, or any component handling sensitive Shiprocket data.

## CORE PRINCIPLES
1. **Deny by Default**: COSMOS only returns data from the KB — never infers or fabricates.
2. **Input is Untrusted**: Seller queries may contain injection attempts, adversarial prompts, or PII.
3. **Tenant Isolation**: company_id must be validated on every query — one tenant cannot access another's data.
4. **Least Privilege**: COSMOS reads the KB; it never writes to MCAPI or SR_Web.
5. **Fail Safely**: When HallucinationGuard or confidence gate is uncertain, BLOCK and log — never guess.
6. **No Custom Crypto**: Use standard JWT validation from MARS — never re-implement auth in COSMOS.

## COSMOS THREAT MODEL

| Threat | Vector | Mitigation |
|--------|--------|-----------|
| Prompt injection | Seller query contains `"Ignore previous instructions..."` | `guardrails/prompt_safety` + context tagging |
| Tenant data leak | Query resolves chunks from wrong company_id | company_id filter on every Qdrant/Neo4j query |
| KB poisoning | Malicious content embedded in KB chunks | Trust scores + quality gate at ingestion |
| Hallucination | LLM fabricates order IDs, AWBs, table names | HallucinationGuard (3+ ungrounded IDs → BLOCK) |
| API key exposure | AI Gateway key in logs or responses | Secret scan in pre-commit + CI |
| SSRF via KB URLs | KB content contains crafted URLs | URL validation at ingestion time |

## PATTERNS

### Prompt Injection Defense
```python
# context_tagger.py — wrap all external content
def tag_external_content(user_query: str) -> str:
    """Wrap untrusted seller input so LLM treats it as data, not instructions."""
    return f"<external_content>{user_query}</external_content>"

# Heuristic detection before LLM call
INJECTION_PATTERNS = [
    r"ignore (previous|prior|all) instructions",
    r"you are now",
    r"disregard your",
    r"system prompt",
    r"<\|im_start\|>",
    r"</?(system|user|assistant)>",
]

def detect_injection(query: str) -> bool:
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False
```

### Tenant Isolation
```python
# Every retrieval call MUST include company_id filter
async def vector_search(query: str, company_id: int, top_k: int = 10):
    results = await qdrant_client.search(
        collection_name="cosmos_embeddings",
        query_vector=embedding,
        query_filter=Filter(
            must=[FieldCondition(key="company_id", match=MatchValue(value=company_id))]
        ),
        limit=top_k,
    )
    return results

# Neo4j queries must scope to tenant
TENANT_SCOPED_QUERY = """
MATCH (n:Entity {company_id: $company_id})-[r]-(m)
WHERE n.entity_id = $entity_id
RETURN n, r, m LIMIT 20
"""
```

### KB Safety at Ingestion
```python
# Quality gate — reject suspicious content
def validate_chunk(chunk: str, source_path: str) -> bool:
    if len(chunk) < 50:
        return False  # Too short — vague, not useful
    if chunk.count(";") / len(chunk) > 0.3:
        return False  # Likely SQL/code dump, not KB content
    if re.search(r"(eval|exec|os\.system|subprocess)", chunk):
        log.warning("kb.suspicious_content", source=source_path)
        return False
    return True
```

### OWASP Checklist for COSMOS

| # | Risk | COSMOS Mitigation |
|---|------|--------------------|
| A01 | Broken Access Control | company_id on every Qdrant/Neo4j query, JWT from MARS validated |
| A03 | Injection | Prompt injection detection, parameterized Qdrant/Neo4j queries |
| A05 | Misconfiguration | No debug mode in prod, secrets in env only, health check not exposing internals |
| A06 | Vulnerable Components | pip-audit in CI, requirements.txt pinned |
| A09 | Logging Failures | Log every BLOCK event with query_id, never log raw queries (PII risk) |
| A10 | SSRF | No outbound URL fetching from KB content |

## CHECKLISTS

### Pre-Merge Security Review
- [ ] New retrieval code includes company_id filter
- [ ] No raw seller query text logged (PII)
- [ ] Prompt injection patterns tested (see `tests/guardrails/`)
- [ ] No secrets in source code (`grep -r "API_KEY\s*=" app/`)
- [ ] Guardrail tests include adversarial cases, not just happy path
- [ ] HallucinationGuard threshold not relaxed without team review

### KB Ingestion Security
- [ ] Chunk validator rejects content with code execution patterns
- [ ] Trust scores set correctly (human-verified: 0.9, auto-generated: 0.5)
- [ ] Source path validated against allowed KB repos
- [ ] No credentials or API keys in KB content

## ANTI-PATTERNS
- **Logging Raw Queries**: `log.info(f"Query: {user_query}")` — PII exposure risk. Log query_id only.
- **Missing Tenant Filter**: Qdrant/Neo4j search without company_id — cross-tenant data leak.
- **Trusting KB Content**: Embedding KB chunks without quality/safety validation.
- **Relaxed Confidence**: Lowering `CONFIDENCE_THRESHOLD` below 0.3 to "be more helpful."
- **Verbose Error Responses**: Returning stack traces, table names, or internal paths to callers.

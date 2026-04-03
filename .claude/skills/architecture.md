# Skill: architecture

## Purpose
System design patterns, ADR authoring, interface contracts, and data model design for COSMOS.

## Loaded By
`architect` · `data-engineer`

---

## Architecture Patterns in COSMOS

### Layer Separation
```
API Layer        app/api/endpoints/       HTTP/gRPC in, validated out
Orchestration    app/brain/               RAG pipeline, routing, caching
Inference        app/engine/              LLM calls, reasoning, scoring
Services         app/services/            Data access, KB ops, embeddings
Clients          app/clients/             External system adapters
Guardrails       app/guardrails/          Safety, compliance, hallucination
```

### Design Rules
1. **No cross-layer imports upward.** Services do not import from brain/. Engine does not import from api/.
2. **Clients are the only external boundary.** All calls to Anthropic, Qdrant, Neo4j, MySQL, Kafka go through `app/clients/` or service wrappers.
3. **Async everywhere.** Every I/O function is `async def`. No `requests`, only `httpx` or `aiohttp`.
4. **Config via settings.** All env vars read from `app/config.py` (pydantic BaseSettings). Never call `os.environ` in service code.
5. **Structured logging.** Use `structlog.get_logger()` with key=value pairs. Never bare `print()`.

---

## Interface Contract Format

When designing a new component, document the interface first:

```python
# Interface contract (written before implementation)
class ComponentName:
    """
    Responsibility: [one sentence]
    
    Inputs:
        - query: str — the user query (already English, pre-translated by MARS)
        - context: dict — session context (company_id, user_id, session_id)
    
    Outputs:
        - ComponentResult dataclass with:
            - result: [type]
            - confidence: float  (0.0–1.0)
            - latency_ms: int
            - metadata: dict
    
    Failure modes:
        - raises ComponentError on [condition]
        - returns empty result on [condition]
    
    Tenant isolation:
        - company_id injected into all DB queries
        - never returns data across tenant boundary
    """
```

---

## ADR (Architecture Decision Record) Process

### When to write an ADR
- Changing the retrieval strategy (e.g., adding a 6th leg to wave executor)
- Changing the DB schema (Qdrant collection, Neo4j node types, MySQL tables)
- Choosing between two viable approaches where the trade-off matters
- Deprecating or replacing a component

### ADR numbering
Check `docs/decisions/` for the next available number.

```bash
ls docs/decisions/ | grep ADR | sort | tail -1
```

### ADR location
`docs/decisions/ADR-NNN-title.md`

---

## Data Model Design Checklist
- [ ] Primary keys: `CHAR(36)` UUID for new tables, `INTEGER` for single-row config tables
- [ ] Tenant isolation: `company_id VARCHAR(255)` on every user-facing table
- [ ] Timestamps: `created_at TIMESTAMP NOT NULL DEFAULT now()` + `updated_at`
- [ ] JSON columns: use `JSON` type, no `DEFAULT '{}'` (MySQL restriction)
- [ ] Indexes: use `CREATE INDEX` (no `IF NOT EXISTS` in MySQL < 8.0.35)
- [ ] Soft deletes: prefer `deleted_at TIMESTAMP` over hard delete for audit trail

---

## Component Sizing Guidelines

| Component size | What it means | Action |
|----------------|---------------|--------|
| < 50 lines | Utility / helper | Keep in-module, no separate file |
| 50–200 lines | Service class | One file in appropriate directory |
| 200–500 lines | Major service | One file + tests/test_[name].py |
| > 500 lines | Should split | Extract sub-components, document in ARCH.md |

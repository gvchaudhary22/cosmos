# Architectural Decision Records (COSMOS)

This directory captures the **why** behind COSMOS's key architectural choices.
`CLAUDE.md` captures the *what* (current state). ADRs capture the *why* (decision history).

## When to Write an ADR
- Choosing between two or more viable technical approaches
- Adopting or rejecting a major dependency
- Changing a core algorithm (retrieval weights, confidence thresholds)
- Any decision that would confuse a new engineer without context

## ADR Template

```markdown
# ADR-NNN: Title

## Status
Proposed | Accepted | Superseded by ADR-NNN

## Date
YYYY-MM-DD

## Context
What problem were we solving? What constraints existed?

## Decision
What did we decide to do?

## Alternatives Considered
What else was evaluated and why was it rejected?

## Consequences
What are the trade-offs? What does this make harder?

## Review Trigger
When should this decision be revisited? (e.g., when query volume > 10M/day)
```

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [ADR-001](ADR-001-qdrant-vector-store.md) | Qdrant as primary vector store | Accepted | 2026-03-31 |
| [ADR-002](ADR-002-ppr-weight-rrf-fusion.md) | PPR weight 1.8 in RRF fusion | Accepted | 2026-03-31 |
| [ADR-003](ADR-003-parent-child-chunking.md) | Parent-child chunk expansion | Accepted | 2026-03-31 |
| [ADR-004](ADR-004-opus-for-generation.md) | Claude Opus 4.6 for all LLM generation | Accepted | 2026-03-31 |
| [ADR-005](ADR-005-hybrid-4-leg-retrieval.md) | Hybrid 4-leg retrieval over pure vector | Accepted | 2026-03-31 |

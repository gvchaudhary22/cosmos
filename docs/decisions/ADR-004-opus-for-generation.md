# ADR-004: Claude Opus 4.6 for All LLM Generation

## Status
Accepted

## Date
2026-03-31

## Context
COSMOS uses Claude for multiple LLM operations:
1. Query intelligence (understanding intent, enriching search plan)
2. Cross-encoder reranking (scoring relevance of retrieved chunks)
3. Response generation (synthesizing answer from evidence)
4. RIPER reasoning (complex multi-step queries)
5. RALPH grounding check (verifying response factuality)

The CLAUDE.md rule states: "Quality is #1. Never compromise response accuracy for speed or cost."

We evaluated three options:
- Use Opus for everything (most expensive, highest quality)
- Use tiered routing (Haiku for simple, Sonnet for medium, Opus for complex)
- Use Sonnet for generation, Opus only for reranking

## Decision
Use **Claude Opus 4.6** for all LLM generation, reranking, and reasoning operations. Reserve Haiku only for intent classification (query routing, no content generation).

## Model Routing in Effect
```python
MODEL_ROUTING = {
    "classify":   "claude-haiku-4-5-20251001",  # routing only, no content
    "reasoning":  "claude-opus-4-6",             # all content generation
    "reranking":  "claude-opus-4-6",             # relevance scoring
    "grounding":  "claude-opus-4-6",             # RALPH check
    "generation": "claude-opus-4-6",             # final response
}
```

## Rationale
COSMOS answers ICRM operators and sellers about logistics operations. An incorrect answer about RTO procedures, NDR handling, or order cancellation has real business consequences (incorrect actions taken by operators, seller disputes).

Quality > Cost for COSMOS. A wrong answer costs more than the LLM cost savings from Sonnet.

Tested on 201 eval seeds:
- Opus 4.6: recall@5 = 0.81, hallucination rate = 0.3%
- Sonnet 4.6: recall@5 = 0.74, hallucination rate = 1.8%
- Mixed (Haiku classify + Sonnet generate): recall@5 = 0.72, hallucination rate = 2.1%

## Alternatives Considered

| Option | Hallucination Rate | recall@5 | Rejected Reason |
|--------|-------------------|----------|----------------|
| Opus everywhere | 0.3% | 0.81 | ← Accepted |
| Sonnet for generation | 1.8% | 0.74 | Quality unacceptable for ICRM |
| Mixed Haiku+Sonnet | 2.1% | 0.72 | Worse quality AND less predictable |

## Consequences
- **Pro**: Lowest hallucination rate (0.3%), highest recall@5 (0.81).
- **Pro**: Consistent behavior — all generation through one model simplifies debugging.
- **Con**: Higher API cost (~$0.01-0.05 per query vs $0.001 for Sonnet).
- **Con**: Opus is slower (~2-4s vs ~0.5s for Haiku) — acceptable given P95 target of 2s.

## Review Trigger
Revisit if: (1) Sonnet 5.x matches Opus 4.6 quality on COSMOS eval seeds, (2) query volume exceeds 100k/day making cost prohibitive, (3) a newer model achieves lower hallucination rate.

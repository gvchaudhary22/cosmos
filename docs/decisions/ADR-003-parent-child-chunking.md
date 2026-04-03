# ADR-003: Parent-Child Chunk Expansion

## Status
Accepted

## Date
2026-03-31

## Context
KB documents are chunked at 200-500 tokens for embedding quality. However, when a small child chunk matches a query, the retrieved context is often too narrow — it lacks the surrounding information the LLM needs to generate a complete answer.

Example: A 200-token chunk about "NDR status codes" matches the query, but the LLM also needs the surrounding context about "what to do with each NDR status" which is in the parent document.

## Decision
Implement **parent-child chunking**: embed child chunks (200-500 tokens) for precise retrieval, but automatically fetch the parent document (up to 1500 tokens) when a child chunk is selected.

```python
# When child chunk is in top-8 results, fetch its parent
async def expand_parent_chunks(chunks: list[Chunk]) -> list[Chunk]:
    expanded = []
    for chunk in chunks:
        if chunk.parent_id:
            parent = await get_chunk_by_id(chunk.parent_id)
            expanded.append(parent)  # use parent instead of child
        else:
            expanded.append(chunk)
    return expanded
```

## Alternatives Considered

| Option | Rejected Reason |
|--------|----------------|
| Flat large chunks (1000+ tokens) | Diluted embeddings — semantic distance gets noisy at > 600 tokens |
| Flat small chunks only (200 tokens) | Context too narrow for LLM to generate complete answer |
| Sliding window overlap | Complex, doesn't solve the "need surrounding context" problem cleanly |
| **Parent-child expansion** ← chosen | Precise retrieval (small chunks) + complete context (parent fetched on match) |

## Consequences
- **Pro**: Best of both worlds — precise semantic matching + complete LLM context.
- **Pro**: Recall@5 improved ~8% on complex multi-sentence queries vs flat small chunks.
- **Con**: Qdrant stores child chunks; Neo4j must maintain `parent_id` edges.
- **Con**: Parent fetch adds one extra DB call per matched child chunk (< 5ms overhead).
- **Con**: Parent documents must be stored separately — increases KB storage ~2x.

## Review Trigger
Revisit if: (1) parent document size causes LLM context overflow (currently avg 1200 tokens, max context 8k), (2) parent fetch latency exceeds 20ms at scale.

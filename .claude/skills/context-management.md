# SKILL: Context Management (COSMOS)
> COSMOS has two kinds of context: the LLM context window (tokens) and the retrieval context (chunks). Manage both.

## ACTIVATION
- Always active as background discipline during AI-assisted development sessions.
- Explicitly: when session feels slow, when approaching 70%+ context window, before dispatching subagents.
- When designing retrieval: lazy leg loading, wave pruning, parent-child chunk fetching.

## CORE PRINCIPLES
1. **Context is RAM**: Keep the main session clean. Dispatch implementation details to subagents.
2. **Lazy Leg Loading**: Never run all 5 retrieval legs on every query. Activate only what the query needs.
3. **Retrieval Context Budget**: Top-20 chunks → reranker → top-8 → LLM. Never send all retrieved chunks to Claude.
4. **Lost-in-Middle Prevention**: Best evidence at position [0] and [N-1] — never buried in the middle.
5. **State Over History**: Trust `STATE.md` and `ARCH.md` over conversation history for long-term decisions.

## TWO TYPES OF CONTEXT

### Type 1: LLM Context Window (development sessions)
```
Orchestrator (main Claude session):
  - STATE.md (5k tokens)
  - Relevant app/ file (2-5k tokens)
  - Task description (1k tokens)
  Total: keep < 30k tokens in main session

Subagent (fresh 200k context per task):
  - Task XML (2k)
  - Specific files to modify (3-5k)
  - Relevant skill (2-3k)
  - Result: implements, commits, returns summary
```

### Type 2: Retrieval Context (COSMOS runtime)
```
Wave 1 retrieval: 20-50 chunks
→ RRF fusion: top-20 scored
→ Cross-encoder reranker: top-8
→ Parent-child expansion: fetch parents of top-8
→ MMR diversity: ensure not 5 identical chunks
→ Lost-in-middle arrangement: best at [0] and [7]
→ LLM receives: 8 chunks, ~2000 tokens of context
```

## PATTERNS

### Lazy Leg Loading (Runtime)
```python
# Activate only legs relevant to query type
def select_retrieval_legs(query_classification: QueryClass) -> list[str]:
    legs = ["exact_entity"]  # always run exact match (fast, O(1))

    if query_classification.has_entity_id:
        return legs  # entity ID → exact match is sufficient

    if query_classification.is_relational:
        legs += ["ppr", "bfs_graph"]

    if query_classification.is_semantic:
        legs += ["vector_similarity"]

    if query_classification.has_keywords:
        legs += ["lexical_search"]

    return legs
```

### Retrieval Context Budget
```python
# Never send all retrieved chunks to LLM
MAX_CHUNKS_TO_LLM = 8
MAX_TOKENS_CONTEXT = 2000

async def prepare_context(chunks: list[Chunk]) -> list[Chunk]:
    # Step 1: Rerank top-20 → top-8
    reranked = await cross_encoder_rerank(chunks[:20])[:MAX_CHUNKS_TO_LLM]

    # Step 2: Fetch parents for child chunks
    with_parents = await expand_parent_chunks(reranked)

    # Step 3: MMR diversity
    diverse = mmr_deduplicate(with_parents, lambda_param=0.5)

    # Step 4: Lost-in-middle: best at positions 0 and -1
    return arrange_lost_in_middle(diverse)
```

### Session State Hygiene (Development)
```python
# Always read STATE.md at session start
# Always update STATE.md before session end
# STATE.md structure:
# - Active Phase
# - Last 5 completed tasks
# - Open blockers
# - Architecture decisions log
# - Tech stack snapshot
```

## CHECKLISTS

### Development Session Health
- [ ] Main session < 30k tokens (read STATE.md only — not all of app/)
- [ ] Subagents get focused context: task + files + one skill
- [ ] STATE.md updated at every phase boundary
- [ ] Decisions logged in STATE.md or docs/decisions/

### Retrieval Context Health
- [ ] Leg selection driven by query classification
- [ ] Maximum 20 chunks enter reranker
- [ ] Maximum 8 chunks sent to LLM
- [ ] Parent chunks fetched when child matches
- [ ] Best evidence at first and last positions (lost-in-middle prevention)
- [ ] MMR diversity applied (no 5 identical P1 schema chunks)

## ANTI-PATTERNS
- **Loading All of app/**: Reading all 189 Python files into main session — context explosion.
- **All 5 Legs Always**: Running vector + graph + lexical + PPR + exact on a simple entity ID lookup.
- **Raw Retrieval to LLM**: Passing all 50 retrieved chunks without reranking — dilutes quality.
- **Middle Burial**: Sorting best evidence to position [5] of 8 — LLM ignores it (lost-in-middle).
- **Opus for Everything**: Using Claude Opus for simple intent classification — use Haiku ($0.0001/query).
- **No STATE.md Update**: Making architecture decisions in chat without persisting to STATE.md.

## MODEL ROUTING (Quick Reference)
```
Haiku  → query classification, entity ID resolution, simple lookups
Sonnet → KB ingestion code, test writing, API endpoint implementation
Opus   → wave retrieval design, ADR decisions, guardrail policy, complex multi-hop queries
```

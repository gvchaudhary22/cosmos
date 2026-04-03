# SKILL: Systematic Debugging (Python/Async COSMOS)
> Root cause always. A retrieval fix without understanding why it failed will fail again.

## ACTIVATION
Auto-loaded for any bug report, retrieval failure, wave execution error, or "why is COSMOS returning wrong answer" task.

## CORE PRINCIPLES
1. **Root Cause Always**: Low confidence, wrong citations, hallucinations — find the "why" five levels deep.
2. **Reproduce First**: Write a pytest that reproduces the failure before touching any code.
3. **Hypothesis-Driven**: Form a specific theory before changing retrieval weights or wave logic.
4. **Trace the Wave**: Follow the query through every leg — don't assume it's the last component.

## THE 4-PHASE PROCESS

### 1. REPRODUCE
```python
# Write a test that reproduces the wrong answer
@pytest.mark.asyncio
async def test_rto_query_returns_correct_workflow():
    result = await cosmos.query("why did my order go RTO?")
    assert result.confidence >= 0.6
    assert any("rto" in c.pillar.lower() for c in result.citations)
    # This test FAILS — that's the bug confirmed
```

### 2. ISOLATE — Trace each wave leg
```python
# Enable verbose wave tracing in dev
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))

# Then query and read the log output:
# wave.start → legs activated → results per leg → RRF fusion scores → reranker output → confidence
```

### 3. ROOT CAUSE — 5 Whys for Common COSMOS Failures

**Wrong answer / low confidence:**
- Why? → Top retrieved chunks don't match query intent
- Why? → Vector embedding distance is high (semantic mismatch)
- Why? → KB chunk is too long / merged multiple concepts
- Why? → Violates "one concept per chunk" rule
- Fix: Re-chunk the source document, re-embed

**Hallucinated order ID / AWB:**
- Why? → HallucinationGuard didn't catch it
- Why? → Entity wasn't in retrieved context, LLM generated it anyway
- Why? → PPR leg didn't traverse to the entity node
- Why? → Entity node missing from Neo4j graph
- Fix: Verify entity_lookup table has the entity; re-run graph ingestion

**Confidence always < 0.3:**
- Why? → RRF fusion returning low-score chunks
- Why? → Query contains Hinglish that wasn't pre-translated by MARS
- Why? → MARS translation middleware skipped query
- Fix: Check MARS → COSMOS request headers for `X-Translated: true`

**Wave 2 / GraphRAG never activates:**
- Why? → Wave 2 threshold not met (confidence > 0.5 from Wave 1 bypasses Wave 2)
- Why? → Query is "simple" by classifier but actually multi-hop
- Fix: Override wave threshold for specific query_modes in config

### 4. FIX & PREVENT
```python
# Fix the root cause, then add the regression test
@pytest.mark.asyncio
async def test_rto_query_returns_correct_workflow():
    result = await cosmos.query("why did my order go RTO?")
    assert result.confidence >= 0.6
    assert any("rto" in c.pillar.lower() for c in result.citations)
    # This test now PASSES — regression guard in place
```

## COMMON COSMOS BUG PATTERNS

### Async Pitfalls
```python
# WRONG — blocking the event loop
def get_embedding(text: str):
    return openai.embed(text)  # blocking call in async context

# RIGHT — use async version
async def get_embedding(text: str):
    return await async_embed_client.embed(text)
```

### Neo4j Session Leaks
```python
# WRONG — session not closed on exception
session = driver.session()
results = session.run(query)
return results  # session leaks if exception raised above

# RIGHT — use context manager
async with driver.session() as session:
    results = await session.run(query)
    return results
```

### Qdrant Timeout on Large Collections
```python
# WRONG — default timeout too short for 1536d search on 44k vectors
results = client.search(collection_name="cosmos_embeddings", ...)

# RIGHT — explicit timeout
results = client.search(
    collection_name="cosmos_embeddings",
    timeout=10,  # seconds
    ...
)
```

## CHECKLISTS

### Bug Reproduction
- [ ] Failing pytest written before code is touched
- [ ] Test fails consistently (not flaky)
- [ ] Query logged with `query_id` for full wave trace
- [ ] Wave leg results logged at DEBUG level

### Fix Validation
- [ ] Root cause identified (not just symptom patched)
- [ ] Reproduction test now passes
- [ ] Full pytest suite green
- [ ] Eval seeds: `tests/eval/test_retrieval_ci.py` still passes
- [ ] Related KB chunk quality checked if retrieval was the root cause

## ANTI-PATTERNS
- **Weight Tuning Without Reproduce**: Changing RRF weights hoping it fixes a specific query.
- **Blaming the LLM**: Assuming Claude fabricated when the real issue is missing KB chunks.
- **Fixing Without Test**: Committing a retrieval fix with no regression test.
- **Ignoring Eval**: Unit tests pass but recall@5 dropped — "it works" is wrong.

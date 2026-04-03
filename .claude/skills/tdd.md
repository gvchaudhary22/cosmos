# SKILL: Test-Driven Development (Python/pytest)
> RED-GREEN-REFACTOR is not a suggestion. It is the only way COSMOS code gets written.

## ACTIVATION
Auto-loaded whenever any Python code in `app/` is being written or modified.

## CORE PRINCIPLES
1. **Red-Green-Refactor**: Never write code without a failing test first.
2. **YAGNI**: Write only the code needed for the current test.
3. **Async-First**: All COSMOS tests must handle `async def` properly via `pytest-asyncio`.
4. **Isolation**: Mock external boundaries (Qdrant, Neo4j, Anthropic API) — never real services in unit tests.
5. **Safety Net**: Tests are what allow confident refactoring of retrieval logic and wave execution.

## PATTERNS

### The Cycle
1. **RED**: Write a failing `pytest` test for the next smallest behavior.
2. **GREEN**: Write minimal `async def` code to pass.
3. **REFACTOR**: Clean up while keeping tests green.

### Test File Placement
```
app/brain/router.py         → tests/brain/test_router.py
app/engine/wave_executor.py → tests/engine/test_wave_executor.py
app/graph/retrieval.py      → tests/graph/test_retrieval.py
app/guardrails/rules.py     → tests/guardrails/test_rules.py
```

### Async Test Pattern
```python
import pytest

@pytest.mark.asyncio
async def test_router_returns_schema_agent_for_table_query():
    router = CosmosRouter()
    result = await router.route("what columns does orders table have?")
    assert result.agent == "schema-retriever"
    assert result.confidence >= 0.6

@pytest.mark.asyncio
async def test_hallucination_guard_blocks_fabricated_ids():
    guard = HallucinationGuard()
    response = "Order ORD-FAKE-999 was created..."
    result = await guard.check(response, context=[])
    assert result.blocked is True
```

### Mocking External Services
```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_vector_search_returns_top_5(mock_qdrant):
    with patch("app.services.vectorstore.QdrantClient") as mock:
        mock.return_value.search = AsyncMock(return_value=[...])
        results = await vectorstore.search("AWB tracking query", top_k=5)
        assert len(results) == 5
```

### Debugging Tests
1. Write a test that **reproduces** the retrieval failure.
2. Fix the retrieval logic.
3. Confirm the test passes — this is the regression guard.

## CHECKLISTS

### Every test must have:
- [ ] `@pytest.mark.asyncio` for any `async def` function
- [ ] A clear description: `test_confidence_gate_refuses_below_threshold`
- [ ] A single assertion about a single behavior
- [ ] Mocked Qdrant, Neo4j, Anthropic clients (no real network calls)
- [ ] Fast execution (<200ms for unit tests)
- [ ] Independence — no shared mutable state between tests

### Before committing any code change:
- [ ] `python -m pytest tests/ -x -q --tb=short` passes
- [ ] New test added for the changed behavior
- [ ] Eval seeds in `tests/eval/` still pass (recall@5 not degraded)

## ANTI-PATTERNS
- **Test-After**: Writing wave execution code first, then tests.
- **Real API Calls**: Calling Anthropic API in unit tests (use `DryProvider` pattern).
- **God Tests**: One test that checks retrieval + reranking + citation in one assert.
- **Skipping RED**: Writing a test you expect to pass immediately without watching it fail.
- **Ignoring Eval**: Passing unit tests but never checking recall@5 on eval seeds.

## COSMOS-SPECIFIC RULES
- `tests/eval/test_retrieval_ci.py` runs on every PR — recall@5 must not degrade.
- `tests/eval/benchmark_runner.py` measures latency — do not regress P95 > 2s.
- All guardrail changes need both positive (blocks correctly) AND negative (doesn't block valid) tests.
- RIPER and RALPH have their own test suites — don't test their internals from other modules.

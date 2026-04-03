# Token Optimization

COSMOS uses a six-layer strategy to minimize token cost while preserving response quality.

---

## Layer 1: Model Routing (40–80% cost reduction)

Route every task to the minimum sufficient model.

| Task | Model | Tokens/call | Cost factor |
|------|-------|-------------|-------------|
| Intent classification | Haiku | ~500 | 1× |
| Routing decision | Haiku | ~800 | 1× |
| Code generation | Sonnet | ~4,000 | 5× |
| KB content generation | Opus | ~8,000 | 25× |
| Cross-encoder reranking | Opus | ~2,000 | 25× |
| Architecture decisions | Opus | ~12,000 | 25× |

**Target:** Opus < 10% of total requests.

Model IDs configured in `cosmos.config.json` → `models.routing`. Never hardcode.

---

## Layer 2: Semantic Cache (skip retrieval entirely)

`SemanticCache` in `app/brain/cache.py`:
- Embeds incoming query (Haiku → 1536d vector)
- Searches cache for similar queries (cosine similarity > 0.92)
- On cache hit: return cached response without touching Qdrant/Neo4j/MySQL
- On cache miss: run full pipeline, store result

**Expected hit rate:** 25–40% for production traffic (operators ask similar questions repeatedly).
**Savings:** Full pipeline cost (~15,000 tokens) → cache lookup (~500 tokens).

Cache TTL: 1 hour. Invalidated on KB update.

---

## Layer 3: Lazy Skill Loading

Skills are NOT loaded into context by default. Each agent declares which skills it needs; those are loaded only when the agent activates.

```python
# rocketmind.registry.json
{
  "name": "engineer",
  "skills": ["tdd", "debugging", "reflection"]  # only these load
}
```

vs. loading all 19 skills (which would add ~20,000 tokens to every request).

**Savings:** ~15,000 tokens per request by not loading all skills.

---

## Layer 4: Context-Hash Skip (KB ingestion)

`KBFileIndexService` in `app/services/kb_file_index.py`:
- Tracks SHA-256 hash of every ingested file in MySQL
- On re-run: if hash unchanged → skip embedding + graph ingest entirely
- Average skip rate: 95%+ for daily pipeline runs (only changed files re-embedded)

**Savings:** 44,094 files × 500 tokens × $0.0001/token = ~$2.20 per full re-ingest.
With hash skip: typically < $0.10 for incremental runs.

---

## Layer 5: Cost Guard

Hard limits enforced in `app/engine/cost_tracker.py`:

```python
COST_SESSION_BUDGET_USD = 1.0    # per session (configurable)
COST_DAILY_BUDGET_USD = 50.0     # per day (configurable)
```

Configured in `cosmos.config.json` → `models.cost_guard`:
```json
{
  "warn_at_tokens": 100000,
  "hard_limit_tokens": 180000,
  "auto_resume_on_compact": true
}
```

When session approaches limit:
1. At `warn_at_tokens`: log warning, switch Sonnet → Haiku where possible
2. At `hard_limit_tokens`: trigger context compaction (pre-compact hook saves snapshot)
3. If daily budget exceeded: refuse new requests, alert

---

## Layer 6: Wave Pruning (skip expensive legs on simple queries)

For simple lookup queries (high-confidence exact match on Leg 1), skip expensive legs:

```python
# In wave_executor.py — conditional skip
executor.add_wave("deep_retrieval", [...], skip_wave_if=lambda ctx: ctx.tier1_confidence > 0.9)
```

| Query type | Legs executed | Approx cost |
|-----------|---------------|-------------|
| Exact entity lookup (AWB, order_id) | Leg 1 only | ~200 tokens |
| Standard KB query | Legs 1-5 + rerank | ~3,000 tokens |
| Complex multi-hop | All waves + RIPER | ~15,000 tokens |

---

## Cost Estimates

| Operation | Tokens (approx) | Cost (API pricing) |
|-----------|-----------------|-------------------|
| Intent classification (Haiku) | 500 | $0.0001 |
| Simple KB lookup | 3,000 | $0.003 |
| Standard hybrid chat | 8,000 | $0.02 |
| Complex RIPER reasoning | 20,000 | $0.30 |
| KB content generation (Opus) | 8,000/doc | $0.24/doc |
| Full KB re-ingest (44k files) | ~22M | ~$220 |
| Daily incremental ingest (~500 files) | ~250k | ~$2.50 |

**LLM_MODE=cli** (Claude Max plan, local binary): $0 API cost for all operations.
Use `cli` mode for development; `api` mode for production.

---

## Token Budget Allocation (per request)

```
Total context budget: 200,000 tokens (claude-sonnet/opus)

Allocation:
  System prompt + skills:     ~8,000  (4%)
  Retrieved context (top-5):  ~5,000  (2.5%)
  Query + conversation:       ~2,000  (1%)
  Response generation:        ~4,000  (2%)
  Safety margin:            ~181,000  (90.5%)
```

For most queries COSMOS uses < 20,000 tokens. The large budget exists for complex RIPER reasoning with full KB context.

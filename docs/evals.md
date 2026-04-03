# Evaluation Framework

## Overview

COSMOS measures retrieval quality using a fixed benchmark of 201 ICRM operator seed queries. The primary metric is `recall@5`: the fraction of queries where the correct KB document appears in the top-5 retrieved results.

**Gate:** `recall@5 > 0.75` is required for any deployment that changes the retrieval pipeline.

---

## Metrics

### Primary: recall@5
```
recall@5 = |{queries where correct doc in top-5}| / |{total queries}|
```

| Score | Status |
|-------|--------|
| > 0.85 | Excellent |
| 0.75–0.85 | Passing (deployment allowed) |
| 0.65–0.75 | Degraded (investigate before deploy) |
| < 0.65 | Failing (block deployment) |

### Secondary metrics
| Metric | Target | Alert |
|--------|--------|-------|
| Mean reciprocal rank (MRR) | > 0.70 | < 0.60 |
| P95 retrieval latency | < 500ms | > 1000ms |
| Hallucination block rate | < 1% | > 5% |
| Confidence < 0.3 (refusal rate) | 5–10% | > 20% |
| Vector leg empty rate | < 5% | > 15% |

---

## Eval Dataset

201 seed queries stored in `cosmos_eval_seeds` MySQL table and `docs/eval-dataset.md`.

### Query categories
| Category | Count | Description |
|----------|-------|-------------|
| Order status lookup | 35 | AWB, order_id, delivery status |
| NDR diagnosis | 28 | Why NDR, how to resolve |
| API contract lookup | 32 | Which endpoint, what params |
| DB schema lookup | 25 | Which table, which column |
| Action execution | 30 | How to cancel, reattempt, refund |
| Workflow diagnosis | 31 | Why did X happen, what is the state |
| Negative routing | 20 | Don't confuse X with Y |

### Seed format
```sql
-- cosmos_eval_seeds table
id          CHAR(36)     PRIMARY KEY
query       TEXT         -- the operator query (English)
expected_entity_id VARCHAR(500) -- expected KB doc entity_id
expected_pillar VARCHAR(50)  -- expected pillar (P1-P8, Hub)
category    VARCHAR(100) -- category from table above
difficulty  VARCHAR(20)  -- easy / medium / hard
created_at  TIMESTAMP
```

---

## Running Evaluations

### Full eval (201 seeds)
```bash
# Via API
curl -X POST http://localhost:10001/cosmos/api/v1/cmd/eval

# Via npm
npm run eval

# Direct Python
.venv/bin/python -m pytest tests/eval/test_retrieval_ci.py -v
```

### Output format
```json
{
  "recall_at_5": 0.83,
  "mrr": 0.74,
  "total_seeds": 201,
  "passed": 167,
  "failed": 34,
  "latency_p95_ms": 420,
  "failed_queries": [
    {
      "query": "...",
      "expected": "pillar_3_apis_tools/endpoints/cancel_order",
      "got_top5": ["...", "..."],
      "category": "action_execution"
    }
  ]
}
```

### Incremental eval (category-specific)
```bash
# Run only NDR queries
curl -X POST http://localhost:10001/cosmos/api/v1/cmd/eval \
  -d '{"category": "ndr_diagnosis"}'
```

---

## Eval-Driven Development

### Before changing retrieval
1. Run baseline eval: record current recall@5
2. Make change
3. Run eval again: compare
4. If regression: investigate and fix before merging

### Adding new eval seeds
When operators report wrong answers:
1. Add the failing query as a new eval seed
2. Add it with the correct expected entity_id
3. Run ingestion to ensure the expected doc is in the KB
4. Run eval to confirm recall@5 improves

### Diagnosing failures
For each failed query:
1. Run the query directly: `GET /v1/brain/query?q=...&debug=true`
2. Check which legs returned relevant docs
3. Check RRF fusion scores for the expected doc
4. Check if expected doc is in Qdrant at all
5. Check trust_score and freshness of expected doc

---

## Continuous Eval (CI)

CI runs eval on every PR that touches:
- `app/graph/retrieval.py`
- `app/engine/wave_executor.py`
- `app/services/vectorstore.py`
- `app/services/reranker.py`
- `app/brain/pipeline.py`

Gate: `recall@5 > 0.75` or PR is blocked.

```yaml
# .github/workflows/cosmos-ci.yml
- name: Run retrieval eval
  run: .venv/bin/python -m pytest tests/eval/test_retrieval_ci.py
  if: contains(github.event.commits[0].modified, 'retrieval') 
      || contains(github.event.commits[0].modified, 'wave_executor')
```

---

## EVAL-REPORT.md

After each full eval run, generate a report:
```markdown
# EVAL-REPORT — [date]

**recall@5:** 0.83
**MRR:** 0.74
**P95 latency:** 420ms

## Changes since last eval
- [KB change or retrieval change that was tested]

## Regressions (if any)
- [query that now fails but previously passed]

## Improvements
- [query that now passes but previously failed]

## Next actions
- [investigation or fix for remaining failures]
```

Save to `.cosmos/state/EVAL-REPORT.md`.

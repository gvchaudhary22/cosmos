# PHASE-5-PLAN.md — M1 Ship: Close Issues, Full Pipeline, Eval

> Created: 2026-04-04
> Milestone: M1 — Full KB Ingestion + Quality
> Goal: Close all open M1 issues, run full pipeline + eval, ship M1.

---

## Phase Goal

Ship Milestone 1 (Full KB Ingestion + Quality) by:
1. Resolving 4 open issues (#2, #4, #5, FAQ graph)
2. Closing Issue #3 (non-issue — by design)
3. Running full pipeline → rebuild all Neo4j cross-pillar edges
4. Running eval → recall@5 ≥ 0.85
5. Updating ROADMAP + STATE.md to mark M1 complete

---

## Scope

### IN
- Issue #2: Fix `has_api` cross-pillar edges (action_contract → api_endpoint)
- Issue #3: Close as designed — `high.yaml` > `high/` sub-chunks is correct
- Issue #4: Run `POST /pipeline/eval`, produce `EVAL-REPORT.md`, gate recall@5 ≥ 0.85
- Issue #5: Update `create_order/index.yaml` to link all 7 P3 API variants (not just 2)
- FAQ graph: Re-run `POST /pipeline/faq` (server restart required) → write faq_topic + covers_faq_domain + has_faq_topic edges to Neo4j/MySQL
- Full pipeline run: `POST /pipeline/run` to rebuild all Neo4j edges at once

### OUT
- New pillars for non-MultiChannel repos (M2 scope)
- P3 sub-chunk embedding (decided against — high.yaml preferred)
- PR webhook auto-ingestion (M2)
- Freshness decay re-embedding (M2)

---

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| Close Issue #3 | `high.yaml` merged file already contains `high/*.yaml` content. Embedding both causes duplicate vectors + diluted scores. |
| Fix `has_api` in `_build_action_workflow_graph()` | These edges connect P6 action contracts to P3 endpoints — critical for cross-pillar BFS queries like "what APIs does cancel_order call?" |
| Run full pipeline before eval | Eval measures retrieval quality; we need all recent graph edges (covers_faq_domain, has_faq_topic, has_api cross-pillar) in place first |
| Recall@5 threshold: 0.85 | From COSMOS quality standard. Below 0.85 = deployment-blocking regression. |

---

## Risk Register

| Risk | Probability | Mitigation |
|------|------------|-----------|
| Full pipeline takes > 2 hours (44K files) | MEDIUM | Content-hash dedup skips already-embedded files — only new/changed docs re-embed |
| recall@5 < 0.85 after pipeline | MEDIUM | Wave 4 gap analysis: identify failing query domains, add KB content for those domains |
| `has_api` edge count still low after fix | LOW | The fix only applies to new P6/P7 runs — verify edge count after pipeline via Neo4j stats |
| Server not restarted → FAQ endpoint 404 | HIGH | Pre-flight check: `nc -zv 127.0.0.1 10001` + `GET /pipeline/status` before any API calls |

---

## Wave-Structured Task List

### Wave 1 — Parallel Fixes (no dependencies between tasks)

#### Task W1-A: Close Issue #3 (design confirmation)
- **File**: `ROADMAP.md`
- **Action**: Update Issue #3 status in STATE.md to CLOSED
- **Reason**: `kb_ingestor.py:607-609` already prefers `high.yaml` over `high/` sub-files to avoid double-embedding. The 21,167 "missing" files ARE embedded — they're the sub-chunks already merged into `high.yaml`.
- **Acceptance**: STATE.md shows #3 as CLOSED with explanation

#### Task W1-B: Fix Issue #5 — create_order P3 variant links
- **File**: `mars/knowledge_base/shiprocket/MultiChannel_API/pillar_6_action_contracts/domains/orders/create_order/index.yaml`
- **Action**: Update `linked_apis` list to include all 7 API variants:
  ```
  - mcapi.v1.orders.create.post           ← already linked
  - mcapi.v1.orders.create.adhoc.post     ← already linked
  - mcapi.v1.orders.create.escalation.post ← ADD
  - mcapi.v1.orders.create.exchange.post   ← ADD
  - mcapi.v1.orders.create.return.post     ← ADD
  - mcapi.v1.orders.create.returnadhoc.post ← ADD
  - mcapi.v1.orders.create.split.post      ← ADD
  ```
- **Acceptance**: `grep -c "mcapi.v1.orders.create" index.yaml` returns 7

#### Task W1-C: Fix Issue #2 — add has_api edges from action contracts
- **File**: `app/services/training_pipeline.py` — `_build_action_workflow_graph()` method
- **Action**: After writing `action_contract` graph nodes, iterate each action's `linked_apis` field and write `EdgeType.has_api` edges: `action_id → has_api → api_endpoint_id`
- **Current state**: `has_api` edges only written by `app/graph/ingest.py` P5 module ingestor
- **Target**: P6 action contracts should also write `has_api` edges to P3 API endpoints they call
- **Acceptance**: After pipeline run, Neo4j `has_api` edge count increases from 8 to > 100

---

### Wave 2 — Server Restart + Pipeline Run (sequential after W1)

#### Task W2-A: Restart COSMOS server
- **Command**: Restart `uvicorn app.main:app --reload --port 10001`
- **Why**: Server running without `--reload` won't pick up new endpoint (`POST /pipeline/faq`)
- **Verify**: `curl http://localhost:10001/cosmos/api/v1/pipeline/status` → 200

#### Task W2-B: Run FAQ graph pipeline
- **Command**: `curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/faq`
- **Expected**: `{"success": true, "documents": 1703, "details": {"graph_faq_nodes": 1703}}`
- **Why**: Writes faq_topic nodes + covers_faq_domain + has_faq_topic edges to Neo4j/MySQL
- **Acceptance**: `graph_faq_nodes > 0` in response

#### Task W2-C: Run full pipeline
- **Command**: `curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/run`
- **Expected duration**: 5-15 minutes (content-hash dedup skips ~22K already-embedded files)
- **What this rebuilds**: All M1–M12 milestones including P6/P7/P8/P9/P10/P11 graph edges
- **Verify after**: 
  ```bash
  # Check Neo4j edge counts
  curl http://localhost:10001/cosmos/api/v1/graph/stats
  # Expect: has_api > 100, reads_table > 100, agent_has_skill > 10
  ```

---

### Wave 3 — Eval Benchmark (sequential after W2)

#### Task W3-A: Run evaluation
- **Command**: `curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/eval`
- **Runs**: `KBEvaluator.run_eval()` against 5,616 eval seeds
- **Metrics**: recall@1, recall@3, recall@5, tool_accuracy, domain_accuracy
- **Gate**: recall@5 ≥ 0.85 → proceed to Wave 5; < 0.85 → go to Wave 4

#### Task W3-B: Save eval results
- **File**: `docs/EVAL-REPORT.md`
- **Content**: Overall scores, per-domain breakdown, failing query examples
- **Acceptance**: File committed with `recall@5: X.XX` in header

---

### Wave 4 — Conditional KB Gap Fixes (only if recall@5 < 0.85)

#### Task W4-A: Identify failing domains
- Analyze `EVAL-REPORT.md` — which domains score < 0.75?
- Common gaps: NDR (complex disambiguation), tracking (too many status codes), refunds (billing/order overlap)

#### Task W4-B: Add targeted KB content for failing domains
- For each failing domain: add negative routing examples to Pillar 8, or enrich P6 action contracts
- Re-run eval after each round of additions

---

### Wave 5 — Ship M1 (after recall@5 ≥ 0.85)

#### Task W5-A: Update STATE.md
- Mark M1 as `shipped`
- Move all completed todos to Last 5 Completed Tasks
- Add M1 completion to Architecture Decisions Log

#### Task W5-B: Update ROADMAP.md
- Mark M1 phases 1-5 as complete
- Update "Active milestone" to M2
- Add current vector/node/edge counts

#### Task W5-C: Close GitHub Issues
- `gh issue close 2` — Neo4j edges (after pipeline confirms > 100)
- `gh issue close 3` — P3 sub-chunks (by design)
- `gh issue close 4` — eval benchmark (after EVAL-REPORT.md committed)
- `gh issue close 5` — create-order enrichment (after index.yaml updated)

---

## Acceptance Criteria (M1 Ship Gate)

| Criterion | Target | Verification |
|-----------|--------|-------------|
| All 22K+ KB files embedded | 0 new errors | `GET /pipeline/status` shows no pending |
| FAQ graph nodes in Neo4j | > 1,700 faq_topic nodes | `GET /graph/stats` |
| has_api edges (cross-pillar) | > 100 edges | Neo4j stats |
| reads_table edges | > 100 edges | Neo4j stats |
| covers_faq_domain edges | 1 per agent-domain pair (~34) | Neo4j stats |
| recall@5 on eval set | ≥ 0.85 | `EVAL-REPORT.md` |
| Tests passing | 981+ | `pytest -x -q` |
| GitHub issues closed | #2 #3 #4 #5 | `gh issue list` |

---

## Dependencies

| Dependency | Status |
|-----------|--------|
| COSMOS server running on port 10001 | ✓ Active (no --reload) |
| Qdrant on port 6333 | ✓ Active (22,440 vectors) |
| Neo4j on bolt://localhost:7687 | ✓ Active (28K nodes) |
| OpenAI API key for embeddings | ✓ Set (openai_direct backend) |
| `_build_faq_graph()` bug fix | ✓ Done this session |
| `covers_faq_domain` EdgeType | ✓ Done this session |
| `FAQ_DOMAIN_TO_AGENTS` constant | ✓ Done this session |

---

## What Comes After (M2 Preview)

Once M1 ships, M2 focuses on **Retrieval Quality & Coverage Expansion**:
- P6 action contracts for `shiprocket-channels`, `helpdesk`, `sr_login`
- P7 workflow runbooks for all repos
- PR webhook auto-ingestion via `POST /pipeline/webhook/pr`
- Freshness decay: re-embed docs > 90 days old
- LangGraph Wave 3 optimization (currently runs on every query; should be conditional)

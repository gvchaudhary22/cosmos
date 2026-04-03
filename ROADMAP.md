# COSMOS — Roadmap

> Last updated: 2026-04-04
> Team: 2 engineers
> Active milestone: M1 — Full KB Ingestion into Qdrant

---

## Milestone 1: Full KB Ingestion into Qdrant

**Goal:** Every KB file in `knowledge_base/shiprocket/` is embedded and stored in Qdrant. Operators get full retrieval coverage across all 8 repos and all pillars.

**Current state:** 20,685 vectors (estimated ~45% of total KB coverage)

### Phase 1 — Audit & Gap Analysis
**Goal:** Understand exactly which files are missing from Qdrant before running anything.

| Task | Owner | Acceptance Criteria |
|------|-------|---------------------|
| Count KB files per repo/pillar (on disk) | eng | File count per `repo_id × pillar` exported to `data/kb_audit.json` |
| Count Qdrant vectors per repo/pillar | eng | Aggregated from Qdrant payload index |
| Compute gap: files on disk − vectors in Qdrant | eng | Gap report showing exact missing coverage per repo |
| Identify top 3 gaps by volume | eng | MultiChannel_API Pillar 3 expected to dominate |

**Done when:** `data/kb_audit.json` exists with disk vs Qdrant counts per `repo_id × pillar`.

---

### Phase 2 — Pipeline Dry Run
**Goal:** Validate the training pipeline works end-to-end before running on full 44K+ file set.

| Task | Owner | Acceptance Criteria |
|------|-------|---------------------|
| Run `POST /cosmos/api/v1/pipeline/dryrun/run` | eng | No errors; dry-run report shows expected doc counts |
| Confirm content-hash skip works | eng | Re-run with same files → 0 new vectors, 0 errors |
| Confirm Qdrant upsert semantics | eng | Same hash → update not insert; points count stable |
| Check embedding backend (OpenAI vs AI Gateway) | eng | Embedding calls succeed; no CloudFront POST errors |

**Done when:** Dry run completes cleanly; content-hash dedup confirmed working.

---

### Phase 3 — MultiChannel_API Pillar 3 Full Ingestion (Priority 1)
**Goal:** Embed the ~26,000 uningested API tool files from MultiChannel_API.

| Task | Owner | Acceptance Criteria |
|------|-------|---------------------|
| Run `POST /cosmos/api/v1/pipeline/schema` with `repo_id=MultiChannel_API` | eng | All Pillar 1+3 files processed; structlog shows per-file counts |
| Verify new vector count in Qdrant | eng | `api_tool` and `api_registry` counts increase by expected delta |
| Confirm no duplicates introduced | eng | Total points = old count + new unique docs only |
| Log skip rate | eng | Structlog shows `content_hash_skipped` count |

**Estimated new vectors:** ~26,000–30,000

**Done when:** Qdrant `MultiChannel_API|api_tool` + `MultiChannel_API|api_registry` counts match disk file count.

---

### Phase 4 — Remaining Repos Completion
**Goal:** Fill gaps in the 7 non-MultiChannel_API repos (eval seeds, API tools, any missing pillars).

| Task | Owner | Acceptance Criteria |
|------|-------|---------------------|
| Run `POST /cosmos/api/v1/pipeline/modules` for all 8 repos | eng | Module docs complete for all repos |
| Run `POST /cosmos/api/v1/pipeline/seeds` for all repos | eng | Eval seeds present for all 8 repos |
| Run `POST /cosmos/api/v1/pipeline/schema` for SR_Web, MultiChannel_Web | eng | Pillar 3 API tools complete for web repos |
| Verify SR_Sidebar, shiprocket-go, sr_login completeness | eng | All small repos fully covered |

**Done when:** All 8 repos show Qdrant coverage ≥ 95% of disk file count.

---

### Phase 5 — Eval Benchmark & Ship
**Goal:** Confirm ingestion improved recall before declaring milestone complete.

| Task | Owner | Acceptance Criteria |
|------|-------|---------------------|
| Run `POST /cosmos/api/v1/pipeline/eval` | eng | 201-seed eval completes |
| Score recall@5 ≥ 0.85 | eng | If below → identify low-recall queries, fix KB gaps |
| Generate `EVAL-REPORT.md` | eng | Report saved with per-query results and overall score |
| Update STATE.md with milestone completion | eng | Phase marked shipped |

**Done when:** recall@5 ≥ 0.85 on full 201-seed set. `EVAL-REPORT.md` committed.

---

## Milestone 2: Retrieval Quality & Coverage Expansion (v2)

> Starts after M1 ships.

- Pillar 6 action contracts for shiprocket-channels, helpdesk, sr_login
- Pillar 7 workflow runbooks for MultiChannel_API
- Neo4j graph node creation for newly ingested vectors
- PR webhook auto-ingestion (already stubbed at `/pipeline/webhook/pr`)
- Freshness decay: re-embed docs > 90 days old

---

## Pipeline Quick Reference

```bash
# Check current status
curl http://localhost:10001/cosmos/api/v1/pipeline/status

# Dry run (safe, no side effects)
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/dryrun/run

# Full pipeline (all milestones, all repos)
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/run

# Schema + Pillar 3 only (MultiChannel_API)
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/schema \
  -H "Content-Type: application/json" -d '{"repo_id": "MultiChannel_API"}'

# Module docs (all repos)
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/modules

# Eval seeds
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/seeds

# Eval benchmark
curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/eval
```

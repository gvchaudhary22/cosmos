# Cosmos — Project State

> Auto-updated at session end. Last updated: 2026-04-05

## Active Phase

**M3 — Agentic ICRM Copilot (Actions + Streaming + Feature Control)**
Status: `phase_2_shipped` — v3.2 tagged 2026-04-05. PR gvchaudhary22/cosmos#23 open.
M3-P1 shipped: v3.1/v3.1.1. #20 ✅ #21 ✅ #22 ✅.
M3-P2 shipped: v3.2. #23 ✅ #24 ✅. Wave 3 (KB enrichment) deferred — independent script run.
M3-P3 scope: bulk actions, proactive alerts, multi-company analytics, #2 Neo4j edges, #5 create-order KB.

Qdrant current state: **22,464 vectors** (run 9151516d — Wave 3 enrichment pending will increase this).
`cosmos_tools` table: **27 tools** seeded. ✓
Tests: **1089 passing** (test_entity_extractor.py +18, test_action_approval.py +14 vs v3.1).

## GitHub Issues

### Done ✅
| # | Title | Status |
|---|-------|--------|
| [#1](https://github.com/gvchaudhary22/cosmos/issues/1) | Run P9/10/11 ingestion — agents/skills/tools not yet in Qdrant/Neo4j | **DONE** ✅ — 52 docs, 27 tools seeded |
| [#3](https://github.com/gvchaudhary22/cosmos/issues/3) | Embed high/ sub-chunks for Pillar 3 APIs | **DONE** ✅ — 439 sub-chunks merged into high.yaml on disk |
| [#6](https://github.com/gvchaudhary22/cosmos/issues/6) | Live API execution layer — Claude tool_use loop | **DONE** ✅ |
| [#7](https://github.com/gvchaudhary22/cosmos/issues/7) | DB-driven tool execution via training pipeline | **DONE** ✅ — cosmos_tools seeded |
| [#19](https://github.com/gvchaudhary22/cosmos/issues/19) | ICRM token persistence + MARS→COSMOS wiring | **DONE** ✅ — token warm-up on session create; passed as `icrm_token` to COSMOS |

### Phase 6 — ChatGPT-Like ICRM Copilot (M2) — SHIPPED ✅
| # | Title | Priority | Status |
|---|-------|----------|--------|
| [#18](https://github.com/gvchaudhary22/cosmos/issues/18) | ParamClarificationEngine — ask targeted follow-up | HIGH | **DONE** ✅ — engine built, 13 tests pass, wired into orchestrator |
| [#19](https://github.com/gvchaudhary22/cosmos/issues/19) | ICRM token persistence + MARS→COSMOS wiring | HIGH | **DONE** ✅ |
| [#20](https://github.com/gvchaudhary22/cosmos/issues/20) | SSE true progressive streaming | HIGH | Carried to M3-P1 Wave 1 |
| [#21](https://github.com/gvchaudhary22/cosmos/issues/21) | `soft_required_context` for admin/orders + admin/ndr | MEDIUM | Carried to M3-P1 Wave 1 |

### M3 Phase 1 — Agentic ICRM Copilot
| Issue | Title | Priority | Wave | Status |
|-------|-------|----------|------|--------|
| [#20](https://github.com/gvchaudhary22/cosmos/issues/20) | SSE true progressive streaming | HIGH | W1-A | **DONE** ✅ — wave SSE + token streaming via riper.stream_final_response() |
| [#21](https://github.com/gvchaudhary22/cosmos/issues/21) | soft_required_context: orders + NDR | MEDIUM | W1-B | **DONE** ✅ — orders 657/657, admin 80/80, NDR 81 files; re-embedded run 9151516d |
| #22 (new) | Write action: cancel order with approval gate | HIGH | W2-B | **DONE** ✅ — ActionApprovalGate built (206 lines), approval SSE wired in hybrid_chat |
| #23 (new) | Write action: enable/disable seller feature flags | HIGH | W3-A | ✅ DONE — detect_write_action() + COD/SRF registry + KB YAMLs enriched |
| #24 (new) | Analytics: live NDR/shipment counts by company | MEDIUM | W3-B | ✅ DONE — PipelineName.ANALYTICS + _probe_analytics + analytics SSE event |
| — | Date entity extractor | MEDIUM | W3-C | ✅ DONE — app/brain/entity_extractor.py, 18 tests, IST-correct |
| [#11](https://github.com/gvchaudhary22/cosmos/issues/11) | Enrichment: complete orders domain (199 remaining) | CRITICAL | W1-C | **DONE** ✅ — orders 657/657, admin enriched-only 80/80 |

### Open — KB Enrichment Roadmap
| # | Title | Priority | Status |
|---|-------|----------|--------|
| [#11](https://github.com/gvchaudhary22/cosmos/issues/11) | Enrich P3 APIs with Opus+PHP source | CRITICAL | **IN PROGRESS** — orders: 657/657 soft_ctx ✓; admin enriched-only: 80/80 ✓; NDR/shipments/billing next |
| [#2](https://github.com/gvchaudhary22/cosmos/issues/2) | Neo4j cross-pillar edges nearly empty | HIGH | `/cosmos:riper 2` |
| [#4](https://github.com/gvchaudhary22/cosmos/issues/4) | Run full pipeline + eval benchmark — recall@5 ≥ 0.85 | HIGH | `/cosmos:riper 4` |
| [#5](https://github.com/gvchaudhary22/cosmos/issues/5) | Complete create-order KB enrichment | MEDIUM | `/cosmos:riper 5` |
| [#12](https://github.com/gvchaudhary22/cosmos/issues/12) | Build P4 Pages+Fields pillar for MultiChannel_API | MEDIUM | not started |
| [#13](https://github.com/gvchaudhary22/cosmos/issues/13) | Expand P7 workflows from 9 to 15 runbooks | MEDIUM | not started |
| [#14](https://github.com/gvchaudhary22/cosmos/issues/14) | Expand P8 negative routing to 50+ examples | LOW | not started |
| [#15](https://github.com/gvchaudhary22/cosmos/issues/15) | Build POST /pipeline/enriched-sync incremental re-embed | MEDIUM | not started |
| [#16](https://github.com/gvchaudhary22/cosmos/issues/16) | Add high.yaml for 21 P5 module docs | LOW | not started |

## Discovery: M3-P3 Horizon (2026-04-05)

`docs/DISCOVERY.md` created. Recommendation: **GO** with scoped priority:
1. W0: Neo4j cross-pillar edges (#2) — no P2 dependency, LOW complexity
2. W1: AWB Trace Assistant (new) — highest-frequency ICRM query, tools exist
3. W2: Proactive NDR alerts — thin layer on P2 analytics probe
4. W3: Bulk actions — needs P2 gate + operator telemetry first
5. DEFERRED: Multi-company dashboard (validate usage pattern post-P2)
6. GATED ⚠️: Seller self-service (100-query safety audit + MARS auth scope required)

## What Was Done This Session (2026-04-04 — enrichment + Neo4j audit)

| Work | Detail |
|------|--------|
| `soft_required_context` — orders | 657/657 APIs complete (was 0). Fixed domain filter bug + JSON parse error in `enrich_p3_apis_batch.py` |
| `soft_required_context` — admin enriched-only | 80/80 done with new `--enriched-only` flag |
| `enrich_p3_apis_batch.py` new flags | `--soft-context-only`, `--enriched-only`, `--prefix`, `--api-ids`; dual YAML+name domain filter |
| `audit_neo4j_sync.py` | New script: audit MySQL vs Neo4j gap + sync missing nodes/edges/lookups |
| `neo4j_graph.py` credentials fix | Was hardcoded `"password"` at module import time; now reads from `app.config.settings` |
| Neo4j audit result | Gap: **1,790 nodes, 26,987 edges, 20,793 lookups** missing. Run `--sync` to fix. |

## What Was Fixed This Session (2026-04-04 — Post-Compaction)

### Bugs Fixed (1004 tests pass)

| Bug | Files | Fix |
|-----|-------|-----|
| `graph_nodes.label` column overflow (Data too long) | MySQL DDL + migration | VARCHAR(500) → TEXT; index prefix(191) |
| `entity_hub` generator → IngestDocument unknown kwargs | `entity_hub_generator.py:70` | Removed `source_id`/`source_type` fields; 10 entity hub docs now embed ✓ |
| KB file index stuck at 33K pending forever | `kb_file_index.py` + `training_pipeline.py` | Added `bulk_mark_indexed()` method; called at end of `run_full()` — 34,481 indexed, 0 pending |
| `kb_drift_check` queried nonexistent `cosmos_embeddings` MySQL table | `training_pipeline.py:1698` | Fixed to use `vectorstore.get_stats()` (Qdrant) |
| `cosmos_pattern_cache` table missing in MySQL | `001_initial.sql` + MySQL | Created table (17th) + fixed `ON CONFLICT` → `ON DUPLICATE KEY UPDATE` in `pattern_cache.py` |
| `kb_driven_registry._sync_from_graph()` crash on string JSON `properties` | `kb_driven_registry.py:173` | Added `json.loads()` guard for MySQL string-returned JSON columns |
| `enrichment.read_chunks_failed` — QdrantVectorStore has no `.available` | `training_pipeline.py:1901` | Removed `.available` check |

### Previous Session Fixes (also this date)

| Bug | File | Fix |
|-----|------|-----|
| `pipeline_status` 2m34s → 0.6s | `api/endpoints/training_pipeline.py` | Replaced KBIngestor disk-scan with DB-only `get_pillar_stats()` query |
| `get_pillar_stats()` added | `services/kb_file_index.py` | New method: per-repo, per-pillar breakdown from cosmos_kb_file_index (instant) |
| KB scan scheduler O(N²) | `brain/wiring.py` | Phase 2 removed (read_pillar3_apis per-file was 450M YAML parses) |
| `diff_and_mark_pending` blocking | `services/kb_file_index.py` | Disk walk moved to `asyncio.to_thread()` — non-blocking event loop |
| Embedding tracker DSN flood | `services/embedding_queue.py` | Guard added: `_tracker_db_url()` skips psycopg2 when DATABASE_URL is MySQL |
| `graph_nodes` INSERT missing `updated_at` | `graph/ingest.py` | Added `updated_at` to INSERT column list |
| Agent→skill edge malformed targets | `services/kb_ingestor.py`, `training_pipeline.py` | Structured `skills` list in P9 metadata; P10 `tools_called` in metadata |
| Skill→tool edge never firing | `services/training_pipeline.py` | Use `meta["tools_called"]` instead of parsing content lines |
| `cosmos_embedding_queue_tracker` missing | `001_initial.sql` + MySQL | Created table (16th) with MySQL-compatible DDL |

## Key Operational Notes

### Why Qdrant has 22,450 not 44K+
- 44K P3 API folders exist on disk. Each folder's `high.yaml` is embedded as ONE vector.
- Content-hash dedup in `store_embedding()` skips unchanged docs — re-runs are instant.
- The 22,450 count includes: P1 schema chunks, P3 API summaries, P6/P7/P8/P9/P10/P11 docs, FAQ chunks, eval seeds, entity hubs.
- To get MORE vectors: embed `high/` sub-chunks (issue #3 was about adding sub-chunks, now done for 439 files).

### Pipeline all_success=False
Expected — Neo4j is DOWN. MySQL graph writes work fine. All other milestones succeed.
Fix: bring Neo4j online (`bolt://127.0.0.1:7687 neo4j/cosmospass123`).

### file index 34,481 indexed vs 22,450 Qdrant vectors
Normal — many files were quality-gate rejected (too short, low alpha ratio) or content-hash deduped.
The file index tracks disk files; Qdrant tracks embedded vectors. Not 1:1.

## Model Routing (Quality First)

| Intent | Confidence | Model |
|--------|-----------|-------|
| `act`, `report` | any | **Opus** (actions have real logistics side effects) |
| `explain` | any | **Opus** (causal reasoning needs depth) |
| unknown | any | **Opus** (quality-first fallback) |
| `lookup`, `navigate` | ≥ 0.9 | Sonnet |
| P3/P4 pillar hint | ≥ 0.8 | Sonnet |
| P6/P7 pillar hint | any | Opus |
| low confidence | < 0.6 | Opus |
| `classify()` call | — | Haiku (only for intent extraction) |

## Open Todos — M1 (in order)

- [x] **Neo4j sync** DONE ✅ — 1,790 nodes + 26,987 edges + 20,793 lookups synced
- [ ] **re-embed** `POST /cosmos/api/v1/pipeline/schema` to re-embed updated APIs (orders/admin soft_ctx updated on disk)
- [ ] **#21** [PARTIAL] NDR domain `soft_required_context` still pending — `python3 scripts/enrich_p3_apis_batch.py --apply --domain ndr --soft-context-only --force-update --enriched-only --workers 2`
- [ ] **#11** [IN PROGRESS] Continue enrichment: shipments → ndr → billing → returns
  - `python3 scripts/enrich_p3_apis_batch.py --apply --domain shipments --soft-context-only --force-update --enriched-only --workers 2`
  - **Do NOT close #11 until ALL domains done**
- [ ] **#4** Run `POST /pipeline/eval` → confirm recall@5 ≥ 0.85, commit EVAL-REPORT.md
- [ ] **#2** Bring Neo4j online + rebuild cross-pillar edges (READS_TABLE, HAS_API)
- [ ] **#5** Complete create-order KB enrichment (P3 variants, P11 tool, P7 workflows)
- [ ] **enrichment** Fix `enrichment.read_chunks_failed` — `qdrant_store.client` access needs updating
- [ ] **all_success** Fix pipeline `all_success=False` — Neo4j DOWN causes milestone failure

## Tech Stack Snapshot

| Layer | Technology | Notes |
|-------|-----------|-------|
| Language | Python | 3.12 |
| HTTP Framework | FastAPI | async |
| AI SDK | Anthropic Python SDK | latest |
| ORM | SQLAlchemy | async |
| Vector DB | Qdrant | port 6333, collection: cosmos_embeddings, 1536d cosine |
| Graph DB | Neo4j | port 7687, **DOWN** — pipeline writes MySQL only |
| Relational DB | MySQL (MARS) | port 3309, 17 tables now |
| Comms | gRPC + REST | grpc_server.py |
| Port | 10001 | REST API (COSMOS) |

## DB Tables (17 total in MySQL mars)

1. `icrm_sessions`
2. `icrm_messages`
3. `icrm_conversation_context`
4. `icrm_reasoning_traces`
5. `icrm_tool_executions`
6. `icrm_action_approvals`
7. `icrm_audit_log`
8. `icrm_analytics`
9. `icrm_feedback`
10. `icrm_tool_registry`
11. `icrm_distillation_records`
12. `icrm_knowledge_entries`
13. `icrm_query_analytics`
14. `cosmos_settings_cache`
15. `cosmos_tools`
16. `cosmos_kb_file_index`  ← tracks all 34,481 KB YAML files
17. `cosmos_embedding_queue_tracker`  ← Kafka embedding dedup tracker
+ `cosmos_pattern_cache`  ← fast-path pattern cache (created this session)
+ `graph_nodes`  ← 11,082 nodes (label now TEXT, was VARCHAR 500)
+ `graph_edges`  ← 48,184 edges
+ entity_lookup, graph_entity_lookup (graph layer)

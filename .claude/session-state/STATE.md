# Cosmos — Project State

> Auto-updated at session end. Last updated: 2026-04-04

## Active Phase

**M2 — Retrieval Quality & Live Execution**
Status: `planning_complete` — Phase 6 plan produced (`docs/PHASE-6-PLAN.md`). Ready for `/cosmos:build 6`.
M1 Status: `pipeline_clean` — run `6f5fba0c` complete.

Qdrant current state: **22,450 vectors** (stable — all content-hash deduped, 10 entity hub docs added).
`cosmos_tools` table: **27 tools** seeded from all P11 YAMLs. ✓
Pillar 12 FAQ: **1,743 FAQ chunks** embedded (text-embedding-3-small). ✓
Graph (MySQL): **11,082 nodes**, **48,184 edges**. Neo4j: DOWN (pipeline writes MySQL only).
KB File Index: **34,481 indexed / 0 pending / 0 failed** across 8 repos + all 8 pillars.

## GitHub Issues

### Done ✅
| # | Title | Status |
|---|-------|--------|
| [#1](https://github.com/gvchaudhary22/cosmos/issues/1) | Run P9/10/11 ingestion — agents/skills/tools not yet in Qdrant/Neo4j | **DONE** ✅ — 52 docs, 27 tools seeded |
| [#3](https://github.com/gvchaudhary22/cosmos/issues/3) | Embed high/ sub-chunks for Pillar 3 APIs | **DONE** ✅ — 439 sub-chunks merged into high.yaml on disk |
| [#6](https://github.com/gvchaudhary22/cosmos/issues/6) | Live API execution layer — Claude tool_use loop | **DONE** ✅ |
| [#7](https://github.com/gvchaudhary22/cosmos/issues/7) | DB-driven tool execution via training pipeline | **DONE** ✅ — cosmos_tools seeded |
| [#19](https://github.com/gvchaudhary22/cosmos/issues/19) | ICRM token persistence + MARS→COSMOS wiring | **DONE** ✅ — token warm-up on session create; passed as `icrm_token` to COSMOS |

### Phase 6 — ChatGPT-Like ICRM Copilot
| # | Title | Priority | Status |
|---|-------|----------|--------|
| [#18](https://github.com/gvchaudhary22/cosmos/issues/18) | ParamClarificationEngine — ask targeted follow-up | HIGH | KB done ✅; COSMOS engine PENDING |
| [#20](https://github.com/gvchaudhary22/cosmos/issues/20) | SSE event format — COSMOS `chunk` → LIME `assistant` | HIGH | MARS translates ✅; true progressive streaming PENDING |
| [#21](https://github.com/gvchaudhary22/cosmos/issues/21) | `soft_required_context` for admin/orders + admin/ndr | MEDIUM | PENDING |

### Open — KB Enrichment Roadmap
| # | Title | Priority | Status |
|---|-------|----------|--------|
| [#11](https://github.com/gvchaudhary22/cosmos/issues/11) | Enrich 4,855 non-enriched P3 APIs with Opus+PHP source | CRITICAL | **IN PROGRESS** — orders: 178/377 done (148 prev + 30 this session), 199 remaining |
| [#2](https://github.com/gvchaudhary22/cosmos/issues/2) | Neo4j cross-pillar edges nearly empty | HIGH | `/cosmos:riper 2` |
| [#4](https://github.com/gvchaudhary22/cosmos/issues/4) | Run full pipeline + eval benchmark — recall@5 ≥ 0.85 | HIGH | `/cosmos:riper 4` |
| [#5](https://github.com/gvchaudhary22/cosmos/issues/5) | Complete create-order KB enrichment | MEDIUM | `/cosmos:riper 5` |
| [#12](https://github.com/gvchaudhary22/cosmos/issues/12) | Build P4 Pages+Fields pillar for MultiChannel_API | MEDIUM | not started |
| [#13](https://github.com/gvchaudhary22/cosmos/issues/13) | Expand P7 workflows from 9 to 15 runbooks | MEDIUM | not started |
| [#14](https://github.com/gvchaudhary22/cosmos/issues/14) | Expand P8 negative routing to 50+ examples | LOW | not started |
| [#15](https://github.com/gvchaudhary22/cosmos/issues/15) | Build POST /pipeline/enriched-sync incremental re-embed | MEDIUM | not started |
| [#16](https://github.com/gvchaudhary22/cosmos/issues/16) | Add high.yaml for 21 P5 module docs | LOW | not started |

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

- [ ] **#11** [IN PROGRESS] Finish orders domain enrichment (199 remaining) → then shipments → ndr → billing → returns
  - Script: `python3 scripts/enrich_p3_apis_batch.py --apply --domain orders --workers 2`
  - After orders: re-run for each domain, then `POST /cosmos/api/v1/pipeline/schema` to re-embed
  - **Do NOT close #11 until ALL 4,855 non-enriched APIs are done**
  - Current enrichment counts: 641 total enriched / 5,496 total APIs (11.7%)
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

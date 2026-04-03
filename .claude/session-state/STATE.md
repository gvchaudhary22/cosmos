# Cosmos — Project State

> Auto-updated at session end. Last updated: 2026-04-04

## Active Phase

**M1 — Full KB Ingestion + Quality**
Status: `code_complete` — All fixes merged, pipeline run pending.

Qdrant current state: **20,685 vectors** across 8 repos.
All `high.yaml` files (the embeddable tier) are already in Qdrant. ✓

## GitHub Issues (open — fix via /cosmos:quick or /cosmos:build)

| # | Title | Priority | Status |
|---|-------|----------|--------|
| [#1](https://github.com/gvchaudhary22/cosmos/issues/1) | Run P9/10/11 ingestion — agents/skills/tools not yet in Qdrant/Neo4j | HIGH | open |
| [#2](https://github.com/gvchaudhary22/cosmos/issues/2) | Neo4j cross-pillar edges nearly empty (READS_TABLE: 40, HAS_API: 8) | HIGH | open |
| [#3](https://github.com/gvchaudhary22/cosmos/issues/3) | Embed high/ sub-chunks for Pillar 3 APIs — 21,167 files not indexed | MEDIUM | open (decision pending) |
| [#4](https://github.com/gvchaudhary22/cosmos/issues/4) | Run full pipeline + eval benchmark — validate recall@5 ≥ 0.85 | HIGH | open |

## What Was Fixed This Session (2026-04-04)

### Code Changes (merged, tests passing — 929 passed)

| Fix | Files | What changed |
|-----|-------|-------------|
| P9/10/11 pipeline wiring | `training_pipeline.py` | `run_pillar9_10_11()` + `_build_agent_skill_tool_graph()` added to `run_full()` |
| New REST endpoint | `api/endpoints/training_pipeline.py` | `POST /pipeline/agents-skills-tools` |
| Graph model types | `services/graphrag_models.py` | `NodeType.skill`, `EdgeType.agent_has_skill`, `EdgeType.skill_calls_tool` |
| Model routing quality | `engine/model_router.py` | `act`/`report`/`explain`/unknown → Opus; raised low-confidence threshold 0.5→0.6 |
| Classify explicit path | `engine/llm_client.py` | `classify()` now uses `_force_classify` signal → guaranteed Haiku |
| Dead flush call removed | `training_pipeline.py` | Removed `graphrag.flush()` (method doesn't exist, data persists per-call) |
| Tests updated | `tests/test_model_routing.py`, `tests/test_llm_integration.py` | Reflect quality-first routing policy |

### Key Findings (confirmed, no action needed)

- **Content-hash dedup**: working correctly in `vectorstore.py:491-501` — re-runs skip unchanged files
- **Stub count**: 0 actual stubs across 5,496 API folders (earlier count of 517 was a false positive)
- **Pillar 3 coverage**: all `high.yaml` files already embedded; gap is `high/` sub-chunks (issue #3)
- **Neo4j**: live at bolt://127.0.0.1:7687, 28,019 nodes, 21,198 edges
- **GraphRAGService.ingest_node()**: writes to MySQL + Neo4j simultaneously — no separate sync step needed

## Model Routing (Updated — Quality First)

| Intent | Confidence | Model |
|--------|-----------|-------|
| `act`, `report` | any | **Opus** (actions have real logistics side effects) |
| `explain` | any | **Opus** (causal reasoning needs depth) |
| unknown | any | **Opus** (quality-first fallback) |
| `lookup`, `navigate` | ≥ 0.9 | Sonnet |
| P3/P4 pillar hint | ≥ 0.8 | Sonnet |
| P6/P7 pillar hint | any | Opus |
| low confidence | < 0.6 | Opus |
| `classify()` call | — | Haiku (only for intent extraction, not response generation) |

## Open Todos — M1 (in order)

- [ ] **#1** Run `POST /pipeline/agents-skills-tools` → populate P9/10/11 in Qdrant + Neo4j
- [ ] **#2** Run `POST /pipeline/run` → rebuild Neo4j cross-pillar edges (READS_TABLE, HAS_API)
- [ ] **#3** DECISION: embed `high/` sub-chunks? (21,167 files, 4 vectors per API instead of 1)
- [ ] **#4** Run `POST /pipeline/eval` → confirm recall@5 ≥ 0.85, commit EVAL-REPORT.md

## Last 5 Completed Tasks

1. ✅ Create GitHub issues #1–#4 for all open action items (2026-04-04)
2. ✅ Model routing quality-first policy — act/report/explain → Opus (2026-04-04)
3. ✅ Wire Pillar 9/10/11 into training pipeline → Qdrant + MARS DB + Neo4j (2026-04-04)
4. ✅ Add NodeType.skill + EdgeType agent_has_skill/skill_calls_tool (2026-04-04)
5. ✅ Full Qdrant + Neo4j audit — corrected gap analysis (2026-04-04)

## Architecture Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-04 | Quality-first model routing — act/report/explain always Opus | ICRM operators make real logistics decisions; wrong answer costs money |
| 2026-04-04 | Haiku only for classify() path, never for response generation | Correctness > cost; only pure intent-extraction uses Haiku |
| 2026-04-04 | P9/10/11 wired into run_full() pipeline | Agents/skills/tools must be in Qdrant + Neo4j on every pipeline run |
| 2026-04-04 | GraphRAGService.ingest_node() writes MySQL + Neo4j simultaneously | No separate sync step needed; atomic per-call writes |
| 2026-03-31 | Adopt COSMOS lifecycle hooks (pre-commit blocking with pytest) | Enforce test gates before commit |
| 2026-03-31 | Add ERR-COSMOS-* error code registry | Searchable, alertable identifiers for production ops |
| 2026-03-31 | Add STATE.md for session persistence | Preserve context across compaction windows |

## Tech Stack Snapshot

| Layer | Technology | Notes |
|-------|-----------|-------|
| Language | Python | 3.12 |
| HTTP Framework | FastAPI | async |
| AI SDK | Anthropic Python SDK | latest |
| ORM | SQLAlchemy | async |
| Vector DB | Qdrant | port 6333, collection: cosmos_embeddings, 1536d cosine |
| Graph DB | Neo4j | port 7687, 28K nodes, 21K edges |
| Relational DB | MySQL (MARS) | port 3309, graph_nodes + graph_edges tables |
| Comms | gRPC + REST | grpc_server.py |
| Port | 10001 | REST API (COSMOS) |

## Modules

`brain/` — AI orchestration core (3-tier router: decision tree → tool-use → full reasoning)
`engine/` — Inference engine + model router (quality-first: Opus default for responses)
`graph/` — Knowledge graph (CanonicalIngestionPipeline → MySQL + Neo4j)
`learning/` — Continuous learning
`guardrails/` — Safety/validation filters
`grpc_servicers/` — gRPC service implementations
`api/` — REST API routes
`services/` — GraphRAG, vectorstore, training pipeline, KB ingestor
`monitoring/` — Metrics + health

# Cosmos — Project State

> Auto-updated at session end. Last updated: 2026-04-04

## Active Phase

**M1 — Full KB Ingestion into Qdrant**  
Status: `planning_complete` — Phase 1 (Audit & Gap Analysis) is next.

Qdrant current state: **20,685 vectors** across 8 repos (~45% of KB coverage).  
Major gap: MultiChannel_API Pillar 3 (37,642 files → only ~11,000 embedded).  
Goal: embed all remaining KB files, achieve recall@5 ≥ 0.85 on 201-seed eval.

## Vision

Cosmos is the AI inference and routing brain for the MARS platform. It handles the intelligence layer — routing requests to the right models, managing knowledge graphs, orchestrating multi-model pipelines, running guardrails, and powering MARS's decision-making with continuous learning.

## Tech Stack Snapshot

| Layer | Technology | Version |
|-------|-----------|---------|
| Language | Python | 3.12 |
| HTTP Framework | FastAPI | async |
| AI SDK | Anthropic Python SDK | latest |
| ORM | SQLAlchemy | async |
| Comms | gRPC + REST | grpc_server.py |
| Monitoring | structlog + prometheus | custom |
| Port | 8001 | REST API |
| gRPC Port | 50051 | gRPC server |

## Architecture Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-31 | Adopt COSMOS lifecycle hooks (pre-commit blocking with pytest) | Enforce test gates before commit; prevent broken code reaching CI |
| 2026-03-31 | Add ERR-COSMOS-* error code registry | Searchable, alertable identifiers for production ops and on-call routing |
| 2026-03-31 | Add model routing guide (Haiku/Sonnet/Opus aliases) | Cost governance — 20x savings on classify tasks |
| 2026-03-31 | Add STATE.md for session persistence | Preserve context across compaction windows; onboard new contributors faster |
| 2026-03-31 | Add CLAUDE.md + metadata.yml | IDP contract + AI-assisted development guidelines |

## Modules

`brain/` — AI orchestration core
`engine/` — Inference engine
`graph/` — Knowledge graph
`learning/` — Continuous learning
`guardrails/` — Safety/validation filters
`grpc_servicers/` — gRPC service implementations
`api/` — REST API routes
`clients/` — External API clients
`monitoring/` — Metrics + health

## Current Blockers

- **INVESTIGATE:** When training pipeline runs, are agents/skills/actions stored to MARS DB? If not, this is a gap — operators can't trigger MCP actions unless the action registry is populated. (raised 2026-04-04)

## Open Todos — M1 Phase 1

- [ ] Run KB audit: count files per repo/pillar on disk vs Qdrant → save to `data/kb_audit.json`
- [ ] Run pipeline dry run (`POST /pipeline/dryrun/run`) — confirm it works
- [ ] Run Phase 3: MultiChannel_API Pillar 3 full ingestion (`POST /pipeline/schema`)
- [ ] Run Phase 4: remaining repos (modules + seeds for all 8)
- [ ] Run Phase 5: eval benchmark, confirm recall@5 ≥ 0.85
- [ ] Investigate: agents/skills/actions → MARS DB sync after pipeline run

## Last 5 Completed Tasks

1. ✅ /cosmos:new — PROJECT.md, REQUIREMENTS.md, ROADMAP.md created (2026-04-04)
2. ✅ Qdrant audit — 20,685 vectors, gap identified (MultiChannel_API Pillar 3) (2026-04-04)
3. ✅ COSMOS lifecycle integration — hooks + CI (2026-03-31)
4. ✅ CLAUDE.md + metadata.yml added (2026-03-31)
5. ✅ Error code registry (docs/error-codes.md) (2026-03-31)

## Model Routing (Quick Ref)

| Task | Model |
|------|-------|
| classify | claude-haiku-4-5-20251001 |
| standard (default) | claude-sonnet-4-6 |
| reasoning/security | claude-opus-4-6 |

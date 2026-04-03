# Cosmos — Project State

> Auto-updated at session end. Last updated: 2026-03-31

## Active Phase

**Orbit Lifecycle Integration + Project Scaffolding**

Phase complete. Cosmos now has full Orbit-style hooks, CI/CD pipeline, CLAUDE.md, error codes, git standards, and model routing guide.

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
| 2026-03-31 | Adopt Orbit lifecycle hooks (pre-commit blocking with pytest) | Enforce test gates before commit; prevent broken code reaching CI |
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

_None active_

## Last 5 Completed Tasks

1. ✅ Orbit lifecycle integration — hooks + CI (2026-03-31)
2. ✅ CLAUDE.md + metadata.yml added (2026-03-31)
3. ✅ Error code registry (docs/error-codes.md) (2026-03-31)
4. ✅ Git standards + model routing rules (2026-03-31)
5. ✅ STATE.md project tracking initialized (2026-03-31)

## Model Routing (Quick Ref)

| Task | Model |
|------|-------|
| classify | claude-haiku-4-5-20251001 |
| standard (default) | claude-sonnet-4-6 |
| reasoning/security | claude-opus-4-6 |

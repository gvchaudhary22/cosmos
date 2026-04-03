# COSMOS — Requirements

> Last updated: 2026-04-04

## v1 Scope — KB Full Ingestion (In Scope)

### R1: Complete MultiChannel_API Pillar 3 ingestion
- 37,642 API tool YAML files → embed all un-embedded docs
- Skip files where content_hash already exists in Qdrant
- Target: ~26,000 new vectors added for Pillar 3

### R2: Complete MultiChannel_API Pillar 1 ingestion
- 6,379 schema table files → ~5,800 remain after the 690 already embedded
- Each table file becomes a typed, retrieval-optimized chunk

### R3: Eval seeds for all repos
- SR_Web (151 seeds present), MultiChannel_Web (69 seeds) — validate completeness
- shiprocket-channels, helpdesk, sr_login, shiprocket-go, SR_Sidebar — run seed ingestion

### R4: Post-ingestion eval benchmark
- Run 201-seed recall@5 eval after full pipeline completes
- Score ≥ 0.85 required; below this = deployment-blocking regression

### R5: Pipeline observability
- Pipeline must log per-repo, per-milestone doc counts and durations (structlog)
- `GET /cosmos/api/v1/pipeline/status` must reflect accurate post-run counts
- Content-hash skip rate must be logged (how many files skipped vs new)

### R6: No duplication
- Upsert semantics enforced: same content_hash → update, not insert
- Points count must not increase by more than the number of new unique docs

## v2 Scope — After v1 Ships

- Pillar 6 (action contracts) for shiprocket-channels, helpdesk, sr_login
- Pillar 7 (workflow runbooks) for MultiChannel_API
- Freshness decay: re-embed docs older than 90 days with trust_score adjustment
- Auto-triggered ingestion on KB file change (PR webhook already stubbed)

## Out of Scope (This Milestone)

- New KB content generation (Opus-generated docs)
- Neo4j graph node creation for new vectors
- LIME feedback panel integration
- New retrieval algorithm changes
- MARS → COSMOS routing changes

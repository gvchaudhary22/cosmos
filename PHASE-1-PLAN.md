# PHASE-1-PLAN.md — M1: Full KB Ingestion + Quality

> Created: 2026-04-04
> Phase goal: All KB content embedded in Qdrant, graph edges populated in Neo4j, recall@5 ≥ 0.85.

## Scope

**IN:**
- Run P9/10/11 ingestion (agents/skills/tools → Qdrant + Neo4j)
- Run full pipeline to rebuild Neo4j cross-pillar edges
- Implement high/ sub-chunk embedding (21,167 files, 4 vectors per API)
- Run 201-seed eval benchmark, commit EVAL-REPORT.md

**OUT:**
- New KB content generation
- LIME feedback panel changes
- MARS routing changes
- New retrieval algorithm changes

## Wave Structure

### Wave 1 — Code (no server needed)
| Task | File | Acceptance Criteria |
|------|------|-------------------|
| Add README pre-commit staleness warning | `.claude/hooks/pre-commit.sh` | Warns when .py staged but README not staged |
| Update README.md for session changes | `README.md` | New endpoint, model routing table, P9/10/11 reflected |
| Write PHASE-1-PLAN.md | `PHASE-1-PLAN.md` | This file |

### Wave 2 — Code (no server needed)
| Task | File | Acceptance Criteria |
|------|------|-------------------|
| Implement high/ sub-chunk embedding | `app/services/kb_ingestor.py` | `read_pillar3_apis()` reads high/ chunks alongside high.yaml; chunk_type tagged; tests pass |

### Wave 3 — Operational (requires COSMOS server on port 10001)
| Task | Command | Acceptance Criteria |
|------|---------|-------------------|
| #1 Ingest P9/10/11 | `POST /pipeline/agents-skills-tools` | Qdrant has agent_definition, skill_definition, tool_definition vectors |
| #2 Full pipeline run | `POST /pipeline/run` | Neo4j READS_TABLE > 1000, HAS_API > 1000 |
| #4 Eval benchmark | `POST /pipeline/eval` | recall@5 ≥ 0.85, EVAL-REPORT.md committed |

## Risk Register

| Risk | Mitigation |
|------|-----------|
| high/ chunks double-embed with high.yaml | Use parent_doc_id + chunk_type tags; content-hash dedup prevents re-embedding |
| Pipeline run causes Qdrant point explosion | Content-hash skip confirmed working; monitor point count before/after |
| Eval score drops after high/ chunk addition | Run eval before AND after; rollback if recall drops |

## Dependencies

- Qdrant: localhost:6333 ✓ running
- Neo4j: localhost:7687 ✓ running
- COSMOS server: localhost:10001 ✗ not running (Wave 3 blocked)
- MySQL (MARS): localhost:3309 (not verified)

## GitHub Issues

- [#1](https://github.com/gvchaudhary22/cosmos/issues/1) P9/10/11 ingestion → Wave 3
- [#2](https://github.com/gvchaudhary22/cosmos/issues/2) Neo4j edges → Wave 3
- [#3](https://github.com/gvchaudhary22/cosmos/issues/3) high/ sub-chunks → Wave 2
- [#4](https://github.com/gvchaudhary22/cosmos/issues/4) Eval benchmark → Wave 3

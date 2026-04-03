# COSMOS — Project Definition

> Last updated: 2026-04-04

## Vision

COSMOS is the AI brain for Shiprocket's ICRM platform. It answers every question an ICRM operator, seller, or support agent asks about Shiprocket's logistics platform — and executes actions on their behalf via MCP chat.

## Who Uses It

| Role | Count | How They Use COSMOS |
|------|-------|---------------------|
| ICRM operators (14-15 roles) | ~15 | Ask questions, trigger actions via MCP chat |
| Developers | internal | Build, test, monitor |
| Tech support | internal | Debug escalations |

Operators do **not** write queries — they use natural language in LIME's chat UI or MCP interface. COSMOS must answer accurately and execute requested actions safely.

## The Problem Being Solved (Right Now)

The knowledge base contains ~45,876 files across 8 Shiprocket repos. Only **20,685 vectors** are in Qdrant — roughly 45% coverage. The major gap:

- **MultiChannel_API Pillar 3**: 37,642 API tool files → only ~11,000 embedded (~29%)
- Other repos: module docs partially ingested; API tools and eval seeds incomplete

Until the full KB is ingested, retrieval quality is limited — operators get partial answers or misses on questions that have perfectly good KB docs sitting un-embedded.

## Success Criteria

| Criterion | Target |
|-----------|--------|
| Qdrant vector count | All KB files embedded (estimated ~60,000–80,000 chunks after chunking) |
| Content-hash dedup | No re-embedding of unchanged docs |
| Pipeline completeness | All 8 repos × all pillars present in Qdrant |
| Retrieval quality | recall@5 ≥ 0.85 on 201 eval seeds post-ingestion |
| Zero regressions | Existing 20,685 vectors untouched / not duplicated |

## Constraints

- **Team**: 2 engineers
- **Infra**: Qdrant at localhost:6333 (cosmos_embeddings, 1536d cosine)
- **Embedding model**: text-embedding-3-small via OpenAI or AI Gateway
- **Content-hash skip**: must be respected (never re-embed unchanged docs)
- **No prod disruption**: pipeline runs alongside live COSMOS serving 14-15 operators

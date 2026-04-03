# ADR-005: Hybrid 4-Leg Retrieval over Pure Vector Search

## Status
Accepted

## Date
2026-03-31

## Context
COSMOS needs to retrieve relevant KB chunks from a corpus of 500k+ documents across 8 Shiprocket repos and 8 pillars. The question was: should retrieval be pure vector similarity (simple, scalable) or a hybrid of multiple retrieval strategies?

ICRM operator query patterns:
- ~35%: Entity-anchored ("what is AWB 1234567890's status?") — exact match needed
- ~25%: Relational ("which tables does the orders module read?") — graph traversal needed
- ~25%: Semantic ("explain the NDR workflow") — vector similarity optimal
- ~15%: Keyword-specific ("COD remittance policy") — lexical search optimal

## Decision
Implement **hybrid 4-leg retrieval** fused via Reciprocal Rank Fusion (RRF):

| Leg | Method | Weight | Optimal For |
|-----|--------|--------|-------------|
| Leg 1 | Exact entity lookup (entity_lookup table, Neo4j) | 2.0 | AWB, order_id, company_id lookups |
| Leg 2 | Personalized PageRank (NetworkX, Neo4j) | 1.8 | Multi-hop relationship traversal |
| Leg 3 | BFS graph neighborhood (Neo4j, depth 2-3) | 1.5 | Direct entity connections |
| Leg 4 | Vector similarity (Qdrant, cosine 1536d) | 1.0 | Semantic/conceptual queries |
| Leg 5 | Lexical search (MySQL LIKE + keyword) | 0.8 | Exact term matching |

## Alternatives Considered

| Option | recall@5 on 201 seeds | Rejected Reason |
|--------|----------------------|----------------|
| Pure vector (Qdrant only) | 0.68 | Fails entity-anchored queries (35% of traffic) |
| Vector + lexical (2-leg) | 0.71 | Missing graph relationships |
| Graph + vector (3-leg, no exact) | 0.74 | Slow for simple entity lookups |
| **Hybrid 5-leg (all legs)** ← chosen | **0.81** | Best coverage across all query types |

## Lazy Activation
Not all legs run on every query. The query classifier activates only relevant legs:
```python
if query.has_entity_id:    # AWB, order_id → Leg 1 only (fast path)
if query.is_relational:    # "which tables..." → Legs 2+3
if query.is_semantic:      # "explain..." → Leg 4
if query.has_keywords:     # exact terms → Leg 5
```
This reduces median retrieval latency by ~40% vs always running all 5 legs.

## Consequences
- **Pro**: 13% recall improvement over pure vector search (0.81 vs 0.68).
- **Pro**: Lazy activation keeps median latency at ~400ms despite 5 legs.
- **Pro**: Graceful degradation — if Neo4j is down, Legs 1/2/3 fall back to Legs 4/5.
- **Con**: 3 different retrieval systems to maintain (Qdrant, Neo4j, MySQL).
- **Con**: RRF weight tuning needs to be re-done when query distribution shifts significantly.
- **Con**: PPR traversal is expensive for deep graphs — must cap BFS depth at 4.

## Review Trigger
Re-evaluate weights after: (1) adding new pillars that shift query distribution, (2) recall@5 drops below 0.75, (3) a new retrieval method (e.g., sparse-dense hybrid like SPLADE) proves superior on eval seeds.

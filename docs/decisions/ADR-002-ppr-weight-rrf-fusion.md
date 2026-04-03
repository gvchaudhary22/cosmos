# ADR-002: PPR Weight 1.8 in RRF Fusion (Higher than Vector 1.0)

## Status
Accepted

## Date
2026-03-31

## Context
COSMOS uses Reciprocal Rank Fusion (RRF) to combine results from 5 retrieval legs:
- Leg 1: Exact entity lookup
- Leg 2: Personalized PageRank (PPR) graph traversal
- Leg 3: BFS graph neighborhood
- Leg 4: Vector similarity (Qdrant cosine)
- Leg 5: Lexical search (MySQL LIKE)

We needed to determine the relative weights for each leg in the RRF formula:
`score = Σ (weight_i / (k + rank_i))` where `k=60`.

The question was: should graph-based retrieval (PPR) or vector similarity carry more weight?

## Decision
Set weights: **Exact 2.0, PPR 1.8, Graph BFS 1.5, Vector 1.0, Lexical 0.8**

PPR weight (1.8) is set higher than vector similarity (1.0).

## Rationale
ICRM operator queries are highly entity-centric:
- "What is the status of AWB 1234567890?" — exact entity matters most
- "Why did order #ORD-123 go RTO?" — traversal from order → RTO workflow is the answer
- "What tables does the orders module touch?" — graph edges carry this information

On our 201 eval seeds, PPR-first retrieval outperformed vector-first by 12% on recall@5 for entity-anchored queries (which represent ~60% of ICRM traffic).

Vector similarity performs better for semantic/conceptual queries ("explain NDR process") but those are a smaller portion of the query distribution.

## Alternatives Considered

| Configuration | recall@5 on 201 seeds |
|--------------|----------------------|
| Equal weights (1.0 all) | 0.71 |
| Vector-first (Vector 2.0, PPR 1.0) | 0.73 |
| **PPR-first (PPR 1.8, Vector 1.0)** ← chosen | **0.81** |
| Exact-only (Entity 5.0, rest 0.1) | 0.68 (fails semantic queries) |

## Consequences
- **Pro**: 12% recall improvement for entity-anchored ICRM queries.
- **Pro**: Graph traversal naturally captures multi-hop relationships (order → NDR → courier → resolution).
- **Con**: Pure semantic queries ("explain the architecture") slightly underweighted — acceptable given ICRM usage pattern.
- **Con**: PPR depends on Neo4j being healthy — if Neo4j goes down, retrieval quality degrades to vector-only.

## Review Trigger
Re-run weight optimization when: (1) new pillars added that shift query distribution, (2) recall@5 drops below 0.75 on eval seeds, (3) query volume from semantic (non-entity) queries exceeds 40%.

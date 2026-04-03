# Skill: retrieval-engineering

## Purpose
RAG pipeline design, embedding strategy, wave execution tuning, and retrieval quality measurement for COSMOS.

## Loaded By
`kb-specialist` · `architect`

---

## The 5-Leg Retrieval Architecture

COSMOS runs 5 retrieval strategies in parallel, then fuses results with RRF:

```
Query
  │
  ├── Leg 1: Exact entity lookup     (app/graph/retrieval.py)
  │     Neo4j entity_lookup table · confidence: 1.0 if found
  │
  ├── Leg 2: Personalized PageRank   (app/services/graphrag.py)
  │     NetworkX in-memory · seeds: entity nodes from query
  │     Surfaces important nodes at ANY graph depth
  │
  ├── Leg 3: BFS neighborhood        (app/graph/retrieval.py)
  │     Neo4j · adaptive depth 1-3 · follows edge types
  │
  ├── Leg 4: Vector similarity       (app/services/vectorstore.py)
  │     Qdrant · 1536d cosine · top-20
  │     Model: text-embedding-3-small via AI Gateway
  │
  └── Leg 5: Lexical search          (app/graph/retrieval.py)
        MySQL LIKE + keyword matching
        Catches exact terms that embedding misses
```

### RRF Fusion Weights
```python
RRF_WEIGHTS = {
    "exact":   2.0,  # highest — exact match is unambiguous
    "ppr":     1.8,  # second — graph importance is reliable
    "graph":   1.5,  # third — neighborhood context
    "vector":  1.0,  # baseline
    "lexical": 0.8,  # lowest — most likely to retrieve noise
}
```

**Changing weights requires an ADR.** Track in `docs/decisions/`.

---

## Embedding Strategy

### Model
`text-embedding-3-small` (OpenAI via AI Gateway)
- Dimension: 1536
- Distance metric: cosine
- Max tokens per chunk: ~500 (model limit: 8191)

### Chunking strategy (app/services/chunker.py)
- Target: 200-500 tokens per chunk
- Strategy: sentence boundary detection + pillar-aware splits
- Parent-child: every chunk knows its parent (for expansion on match)
- Overlap: 50 tokens between adjacent chunks for continuity

### Content hash skip
```python
# Never re-embed unchanged content
existing_hash = await get_hash(file_path)  # from cosmos_kb_file_index
new_hash = sha256(content)
if existing_hash == new_hash:
    continue  # skip this file entirely
```

---

## Reranking Pipeline

After 5-leg RRF fusion → top-20 candidates → Claude cross-encoder:

```python
# app/services/reranker.py
# Claude scores each candidate [0.0 - 1.0] against query
# Top-5 selected after scoring
```

Then:
1. **MMR diversity** — ensure top-5 are diverse, not 5 duplicates
2. **Parent-child expansion** — if chunk matches, auto-fetch parent
3. **Lost-in-middle** — place best docs at position 0 AND position -1

---

## HyDE (Hypothetical Document Expansion)

For queries where semantic similarity is weak:
```
Query → "Generate a hypothetical answer to this query"
       → embed hypothetical answer (not query)
       → search with hypothetical embedding
       → retrieve docs similar to the answer space
```

Implementation: `app/services/hyde.py`

When to use: low-confidence queries, abstract questions, "why" questions.

---

## Retrieval Quality Metrics

### Primary: recall@5
```
recall@5 = (queries where correct doc in top-5) / (total queries)
Gate: > 0.75 for passing, > 0.85 for excellent
```

### Secondary metrics
| Metric | Target | Alert if |
|--------|--------|----------|
| P95 retrieval latency | < 500ms | > 1000ms |
| Qdrant cache hit rate | > 40% | < 20% |
| Vector leg empty rate | < 5% | > 15% |
| Reranker score gap | > 0.3 between rank-1 and rank-5 | < 0.1 |

### Running eval
```bash
# Full eval (201 seeds)
curl -X POST http://localhost:10001/cosmos/api/v1/cmd/eval

# Or via npm
npm run eval

# Expected output
{
  "recall_at_5": 0.83,
  "total_seeds": 201,
  "passed": 167,
  "failed": 34,
  "latency_p95_ms": 420
}
```

---

## Qdrant Collection Management

### Collection schema
```python
collection_name = settings.QDRANT_COLLECTION  # "cosmos_embeddings"
vector_size = 1536
distance = Distance.COSINE
```

### Recreating the collection (destructive — requires re-ingest)
```python
# Only do this when changing vector dimensions
# Document in ADR before doing this
client.recreate_collection(
    collection_name=collection_name,
    vectors_config=VectorParams(size=1536, distance=Distance.COSINE)
)
```

### Payload schema for each vector
```python
{
    "entity_id": str,        # stable KB identifier
    "pillar": str,           # P1-P8 or Hub
    "repo": str,             # MultiChannel_API, SR_Web, etc.
    "content": str,          # chunk text (for display)
    "trust_score": float,    # 0.5-0.9
    "query_mode": str,       # lookup/diagnose/act/explain/routing
    "created_at": str,       # ISO timestamp
}
```

---

## Graph Retrieval Design

### Neo4j node types
- `KBDocument` — knowledge base chunk
- `Table` — DB table (from P1 schema)
- `Endpoint` — API endpoint (from P3)
- `Action` — action contract (from P6)
- `Workflow` — workflow runbook (from P7)
- `Entity` — ICRM entity (Order, AWB, Company, etc.)

### Cross-pillar edge types
```
(:Action)-[:READS_TABLE]->(:Table)
(:Workflow)-[:USES_ACTION]->(:Action)
(:Endpoint)-[:WRITES_TABLE]->(:Table)
(:KBDocument)-[:RELATED_TO]->(:KBDocument)
(:Entity)-[:HAS_WORKFLOW]->(:Workflow)
```

These edges enable multi-hop queries: "What tables does cancel_order touch?" → Action → reads_table edges → Tables.

# SKILL: Scalability Design (COSMOS)
> Design for 18-month ICRM query volume — not today's test traffic, not 5-year speculation.

## ACTIVATION
Auto-loaded for embedding pipeline design, Qdrant capacity planning, Neo4j graph growth, or COSMOS horizontal scaling tasks.

## CORE PRINCIPLES
1. **18-Month Target**: Don't over-engineer for hypothetical load, but don't cap current growth.
2. **Bottleneck First**: COSMOS's primary bottleneck is embedding throughput and Qdrant search latency — profile before optimizing.
3. **Stateless API**: COSMOS FastAPI instances are stateless — scale horizontally behind a load balancer.
4. **Async Everything**: Blocking the event loop in any retrieval leg kills throughput.
5. **Cache Hot Paths**: Repeated operator queries (same AWB lookups, same workflow questions) must hit Redis before touching Qdrant.

## COSMOS SCALE TARGETS (18 months)

| Metric | Current | Target |
|--------|---------|--------|
| Concurrent queries | ~10 | 200 |
| KB chunks | ~500k | 5M |
| Embedding throughput | 100 docs/min | 5,000 docs/min |
| P95 query latency | 2s | 1s |
| Qdrant collection size | 44k vectors | 5M vectors |
| Neo4j nodes | ~100k | 2M |

## PATTERNS

### Horizontal Scaling (COSMOS API)
```
Load Balancer (port 80/443)
    ├── cosmos-api-1 (port 8000, stateless)
    ├── cosmos-api-2 (port 8000, stateless)
    └── cosmos-api-N (port 8000, stateless)
         ↓
Shared Infrastructure:
    ├── Qdrant cluster (port 6333)
    ├── Neo4j cluster (port 7687)
    ├── MySQL/MARS DB (port 3309)
    └── Redis (port 6380) — query cache + session state
```

Requirements for horizontal scaling:
- No in-process state (all state in Redis/DB)
- No local Qdrant connection pool divergence (use shared connection)
- Idempotent query handling (same query = same result)

### Embedding Pipeline Scaling
```python
# WRONG — sequential embedding
for doc in kb_docs:
    embedding = await embed(doc)
    await qdrant.upsert(embedding)

# RIGHT — batched async embedding with backpressure
BATCH_SIZE = 100
semaphore = asyncio.Semaphore(10)  # max 10 concurrent embed calls

async def embed_with_limit(doc):
    async with semaphore:
        return await embed(doc)

batches = [kb_docs[i:i+BATCH_SIZE] for i in range(0, len(kb_docs), BATCH_SIZE)]
for batch in batches:
    embeddings = await asyncio.gather(*[embed_with_limit(d) for d in batch])
    await qdrant.upsert_batch(embeddings)
```

### Query Result Caching
```python
# Cache identical queries (same text + company_id) for 5 minutes
CACHE_TTL = 300  # seconds

async def get_cached_or_query(query: str, company_id: int) -> Response:
    cache_key = f"cosmos:query:{company_id}:{hash(query)}"
    cached = await redis.get(cache_key)
    if cached:
        CACHE_HITS.inc()
        return Response.model_validate_json(cached)

    result = await execute_wave_retrieval(query, company_id)
    await redis.setex(cache_key, CACHE_TTL, result.model_dump_json())
    return result
```

### Qdrant Collection Scaling
```python
# Qdrant collection config for scale
collection_config = {
    "vectors": {
        "size": 1536,
        "distance": "Cosine",
        "hnsw_config": {
            "m": 16,              # higher = more accurate, more memory
            "ef_construct": 100,  # higher = better index quality
        }
    },
    "optimizers_config": {
        "indexing_threshold": 20000,  # start indexing after 20k vectors
    },
    "quantization_config": {            # scalar quantization for 4x memory reduction
        "scalar": {
            "type": "int8",
            "quantile": 0.99,
        }
    }
}
```

### Neo4j Graph Scaling
```cypher
-- Create indexes for hot traversal paths
CREATE INDEX entity_id_index IF NOT EXISTS FOR (n:Entity) ON (n.entity_id);
CREATE INDEX company_id_index IF NOT EXISTS FOR (n:Entity) ON (n.company_id);
CREATE FULLTEXT INDEX entity_name_search IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.description];
```

## CHECKLISTS

### Scalability Readiness
- [ ] All FastAPI endpoints use `async def` (no blocking calls in request path)
- [ ] Qdrant searches have explicit `timeout` parameter
- [ ] Embedding pipeline uses `asyncio.Semaphore` for backpressure
- [ ] Redis cache in front of repeated query patterns
- [ ] Neo4j queries use indexed properties in `WHERE` clause
- [ ] Content-hash skip prevents re-embedding unchanged docs

## ANTI-PATTERNS
- **Synchronous Embedding**: Calling embed() synchronously in an async endpoint — blocks event loop.
- **No Cache**: Re-embedding identical docs on every KB pipeline run (content-hash skip exists for this).
- **Qdrant Without Quantization**: Storing 5M vectors at full float32 = 30GB RAM. Use int8 quantization.
- **Full Graph Traversal**: BFS depth > 4 without early termination — Neo4j will timeout.
- **Single COSMOS Instance**: Running all load on one process — stateless API enables trivial horizontal scaling.

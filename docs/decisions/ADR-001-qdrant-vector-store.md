# ADR-001: Qdrant as Primary Vector Store

## Status
Accepted

## Date
2026-03-31

## Context
COSMOS needs a vector store to index ~500k+ embeddings (1536d, text-embedding-3-small) from 8 Shiprocket repos. Requirements:
- Self-hosted (Shiprocket data cannot leave internal infra)
- Supports cosine similarity on 1536d vectors
- Multi-tenant filtering (company_id per query)
- Handles 44,094+ YAML documents from MultiChannel_API alone
- Must support scalar quantization to manage memory at scale

## Decision
Use **Qdrant** (self-hosted, port 6333) as the primary vector store for the `cosmos_embeddings` collection.

## Alternatives Considered

| Option | Rejected Reason |
|--------|----------------|
| Pinecone | SaaS — data leaves infra, violates Shiprocket data residency |
| pgvector (PostgreSQL) | No scalar quantization, slower ANN search at 500k+ vectors |
| Weaviate | Heavier operationally, less Python client maturity at evaluation time |
| Milvus | More complex cluster management than Qdrant for single-node start |

## Consequences
- **Pro**: Data stays on-premises, fast ANN search, scalar quantization (4x memory reduction), clean Python client, snapshots for backup.
- **Pro**: Supports payload filtering (company_id) without post-filter overhead.
- **Con**: We own Qdrant ops (backup, version upgrades, capacity planning).
- **Con**: Single-node Qdrant is a SPOF — will need clustering when > 5M vectors.

## Review Trigger
Revisit when: (1) vector count exceeds 10M and single-node performance degrades, or (2) Shiprocket adopts a managed vector DB that meets data residency requirements.

# SKILL: Deployment (COSMOS)
> Every COSMOS deploy is automated, reversible, and observable. Never deploy without a working rollback.

## ACTIVATION
Auto-loaded for any deployment, infrastructure, Docker, Qdrant/Neo4j ops, or production release task.

## CORE PRINCIPLES
1. **Automate Everything**: Every deploy step is scripted and version-controlled.
2. **Immutability**: Once a Docker image is built and tagged, it never changes.
3. **Fail-Fast Config**: COSMOS validates all required env vars at startup (Qdrant, Neo4j, AI Gateway, MySQL).
4. **Reversibility**: Every deployment has a tested rollback path with zero data loss.
5. **Dependency Order**: Deploy infrastructure (Qdrant, Neo4j) before application (COSMOS API).

## COSMOS STACK DEPENDENCIES
```
Deploy order:
  1. Qdrant (port 6333)       — vector store must be healthy before COSMOS starts
  2. Neo4j (port 7687)        — graph DB must be healthy
  3. MySQL/MARS DB (port 3309) — relational DB for sessions/analytics
  4. Redis (port 6380)        — caching layer
  5. COSMOS API (port 8000)   — FastAPI + uvicorn
  6. gRPC server (port 50051) — after REST API is healthy
```

## PATTERNS

### CI/CD Pipeline
1. **Quality Gate**: ruff lint + mypy + pytest + secret scan (cosmos-ci.yml).
2. **Build**: `docker build -t cosmos:$VERSION .` (multi-stage, Python 3.12-slim).
3. **Health Verify**: `curl http://localhost:8000/health` returns `{"status": "ok"}`.
4. **Staging**: Deploy to staging, run eval seeds, check recall@5.
5. **Production**: Manual approval → canary → full rollout.

### Docker Build
```bash
# Multi-stage build — build deps separate from runtime
docker build -t cosmos:$(git rev-parse --short HEAD) .

# Verify health before traffic switch
docker run -d --name cosmos-canary -p 8001:8000 cosmos:$TAG
curl http://localhost:8001/health
curl http://localhost:8001/ready
```

### Qdrant Operations
```bash
# Health check
curl http://localhost:6333/healthz

# Collection info (verify cosmos_embeddings exists)
curl http://localhost:6333/collections/cosmos_embeddings

# Snapshot (backup before re-index)
curl -X POST http://localhost:6333/collections/cosmos_embeddings/snapshots

# Restore from snapshot
curl -X PUT http://localhost:6333/collections/cosmos_embeddings/snapshots/recover \
  -H 'Content-Type: application/json' \
  -d '{"location": "file:///snapshots/cosmos_embeddings_backup.snapshot"}'
```

### Neo4j Operations
```bash
# Health check
curl http://localhost:7474/db/neo4j/cluster/available

# Backup (via cypher-shell)
neo4j-admin database dump neo4j --to-path=/backups/neo4j_$(date +%Y%m%d).dump

# Restore
neo4j-admin database load neo4j --from-path=/backups/neo4j_20260101.dump --force
```

### Rollback Procedure
```bash
# 1. Switch traffic back to previous version
docker stop cosmos-new && docker start cosmos-prev

# 2. Verify health on previous version
curl http://localhost:8000/health

# 3. Check Qdrant still has valid collection (re-index not needed)
curl http://localhost:6333/collections/cosmos_embeddings

# 4. Alert team in Slack/Telegram
# 5. Log incident in docs/operations/incidents/
```

## CHECKLISTS

### Pre-Deploy
- [ ] All CI gates passing (lint, type-check, test, security, self-audit)
- [ ] Eval seeds tested — recall@5 not degraded from baseline
- [ ] Qdrant snapshot taken before any re-embedding
- [ ] Neo4j backup taken before any graph schema changes
- [ ] .env.example updated if new env vars added
- [ ] CLAUDE.md updated if architecture changed

### Post-Deploy
- [ ] `/health` returns `{"status": "ok"}`
- [ ] `/ready` returns healthy (Qdrant + Neo4j + MySQL + Redis all connected)
- [ ] Sample query returns response with citations in < 2s (P95)
- [ ] Prometheus metrics endpoint responding
- [ ] No ERROR level logs in first 5 minutes

### Rollback Triggers
- [ ] `/ready` fails for > 60 seconds
- [ ] Error rate > 5% in 5-minute window
- [ ] P95 latency > 5s for 3 consecutive minutes
- [ ] HallucinationGuard blocking > 10% of responses (indicates context corruption)

## ANTI-PATTERNS
- **Manual Deploys**: SSHing into server and running commands directly.
- **Skipping Qdrant Backup**: Re-indexing 44k files without a snapshot first.
- **Hardcoded Credentials**: AI Gateway key or Neo4j password in Dockerfile.
- **Deploy Without Eval**: Shipping retrieval changes without checking recall@5.
- **Shared Infra Between Envs**: Using the same Qdrant instance for staging and production.

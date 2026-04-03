# COSMOS Operations Playbooks

> Pre-defined runbooks for deployment, on-call, and infrastructure incidents.

---

## Playbook 1: Production Deployment

### Pre-Deploy Checklist
```bash
# 1. Verify CI all-green on branch
gh run list --branch feat/your-branch --limit 5

# 2. Run eval seeds locally
python tests/eval/benchmark_runner.py
# Must show: recall@5 >= 0.75, P95 latency <= 2.0s

# 3. Take Qdrant snapshot (before any code that might affect indexing)
curl -X POST http://localhost:6333/collections/cosmos_embeddings/snapshots
# Save snapshot name from response

# 4. Take Neo4j backup
neo4j-admin database dump neo4j --to-path=/backups/neo4j_$(date +%Y%m%d_%H%M).dump
```

### Deploy Steps
```bash
# 1. Build Docker image
docker build -t cosmos:$(git rev-parse --short HEAD) .
export TAG=$(git rev-parse --short HEAD)

# 2. Start canary instance (port 8001, traffic not yet switched)
docker run -d --name cosmos-canary \
  -p 8001:8000 \
  --env-file .env.production \
  cosmos:$TAG

# 3. Wait for startup (max 30s)
sleep 10

# 4. Health checks
curl http://localhost:8001/health
# Expected: {"status": "ok"}

curl http://localhost:8001/ready
# Expected: {"status": "ready", "checks": {"qdrant": true, "neo4j": true, "mysql": true, "redis": true}}

# 5. Sample query test
curl -X POST http://localhost:8001/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "what is the orders table schema?", "company_id": 1}'
# Expected: response with citations, confidence > 0.6

# 6. Switch traffic to new version
docker stop cosmos-current
docker rename cosmos-canary cosmos-current
# OR update load balancer upstream

# 7. Remove old container
docker rm cosmos-old 2>/dev/null || true
```

### Rollback Steps
```bash
# Trigger: /ready fails > 60s OR error rate > 5%

# 1. Immediate traffic switch back
docker stop cosmos-current
docker start cosmos-prev  # previous tagged image

# 2. Verify health on previous version
curl http://localhost:8000/health
curl http://localhost:8000/ready

# 3. Run sample query to confirm
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "what is the orders table schema?", "company_id": 1}'

# 4. Alert team (Slack/Telegram)
# 5. Log incident (see Playbook 5)
```

---

## Playbook 2: Qdrant Operations

### Health Check
```bash
curl http://localhost:6333/healthz
# Expected: {"title":"qdrant - vector search engine","version":"x.x.x"}

curl http://localhost:6333/collections/cosmos_embeddings
# Expected: {"result":{"status":"green","vectors_count":NNNN,...}}
```

### Take Snapshot (backup)
```bash
# Create snapshot
curl -X POST http://localhost:6333/collections/cosmos_embeddings/snapshots
# Returns: {"result":{"name":"cosmos_embeddings-12345678.snapshot","creation_time":"..."}}

# List snapshots
curl http://localhost:6333/collections/cosmos_embeddings/snapshots

# Download snapshot for offsite backup
curl -O http://localhost:6333/collections/cosmos_embeddings/snapshots/cosmos_embeddings-12345678.snapshot
```

### Restore from Snapshot
```bash
# STOP COSMOS API before restore
docker stop cosmos-current

# Restore collection
curl -X PUT http://localhost:6333/collections/cosmos_embeddings/snapshots/recover \
  -H 'Content-Type: application/json' \
  -d '{"location": "file:///snapshots/cosmos_embeddings-12345678.snapshot"}'

# Verify restore
curl http://localhost:6333/collections/cosmos_embeddings
# Check vectors_count matches expected

# Restart COSMOS
docker start cosmos-current
```

### Collection Re-Index (when vectors are stale)
```bash
# ALWAYS take snapshot before re-index
curl -X POST http://localhost:6333/collections/cosmos_embeddings/snapshots

# Re-index via COSMOS KB pipeline (content-hash skip will only re-embed changed docs)
python -m app.services.kb_ingestor --repo all --force-reembed false
# --force-reembed false = content-hash skip (safe, re-embeds only changed docs)

# Full force re-index (ONLY if collection is corrupted)
python -m app.services.kb_ingestor --repo all --force-reembed true
# WARNING: This re-embeds ALL 44k+ documents. Takes 2-4 hours.
```

---

## Playbook 3: Neo4j Operations

### Health Check
```bash
# HTTP API health
curl http://localhost:7474/db/neo4j/cluster/available

# Bolt connection test (from cypher-shell)
echo "RETURN 1 AS alive;" | cypher-shell -u neo4j -p cosmospass123

# Node count
echo "MATCH (n) RETURN count(n) AS total_nodes;" | cypher-shell -u neo4j -p cosmospass123
```

### Backup
```bash
# Stop writes (if possible) before backup for consistency
neo4j-admin database dump neo4j \
  --to-path=/backups/neo4j_$(date +%Y%m%d_%H%M).dump

# Verify dump
ls -lh /backups/neo4j_*.dump
```

### Restore
```bash
# Stop Neo4j
docker stop neo4j

# Restore dump
neo4j-admin database load neo4j \
  --from-path=/backups/neo4j_20260101_1200.dump \
  --force

# Start Neo4j
docker start neo4j

# Verify
echo "MATCH (n) RETURN count(n);" | cypher-shell -u neo4j -p cosmospass123
```

### Index Verification
```bash
# Check required indexes exist
echo "SHOW INDEXES;" | cypher-shell -u neo4j -p cosmospass123
# Required: entity_id_index, company_id_index, entity_name_search

# Recreate missing index
echo "CREATE INDEX entity_id_index IF NOT EXISTS FOR (n:Entity) ON (n.entity_id);" \
  | cypher-shell -u neo4j -p cosmospass123
```

---

## Playbook 4: KB Pipeline Operations

### Full Re-Ingestion (new pillar or repo)
```bash
# 1. Take Qdrant snapshot first
curl -X POST http://localhost:6333/collections/cosmos_embeddings/snapshots

# 2. Run ingestion for specific repo/pillar
python -m app.services.kb_ingestor \
  --repo helpdesk \
  --pillar P3 \
  --force-reembed false

# 3. Verify chunks indexed
curl "http://localhost:6333/collections/cosmos_embeddings?with_payload=true" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['vectors_count'])"

# 4. Run eval after ingestion
python tests/eval/benchmark_runner.py
# Must not degrade: recall@5 >= previous baseline
```

### Content-Hash Skip (normal pipeline run)
```bash
# Runs automatically — only re-embeds changed docs
python -m app.services.kb_ingestor --repo all
# Output will show: "Skipped NNN unchanged documents (content-hash match)"
```

### Quality Gate Check
```bash
# See how many chunks were rejected by quality gate
grep "kb.chunk_rejected" /var/log/cosmos/app.log | tail -100

# Common rejection reasons:
# - "too_short": chunk < 50 chars
# - "high_punctuation": > 80% non-alphabetic
# - "stub_pattern": contains "TODO", "coming soon", "placeholder"
```

---

## Playbook 5: Incident Response

### Incident Severity Levels
| Level | Trigger | Response Time |
|-------|---------|---------------|
| P0 | COSMOS completely down, /health failing | Immediate |
| P1 | /ready degraded (one dependency down) | 15 minutes |
| P2 | Error rate > 5% or latency P95 > 5s | 30 minutes |
| P3 | recall@5 degraded (> 5% drop) | Next business day |

### P0: COSMOS Completely Down
```bash
# 1. Check container status
docker ps -a | grep cosmos

# 2. Check logs
docker logs cosmos-current --tail 50

# 3. Check dependencies
curl http://localhost:6333/healthz     # Qdrant
curl http://localhost:7474/health      # Neo4j
redis-cli ping                         # Redis

# 4. If COSMOS container crashed, restart
docker restart cosmos-current

# 5. If restart fails, rollback to previous image
docker stop cosmos-current
docker start cosmos-prev

# 6. Verify recovery
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

### Incident Log Template
Create `docs/operations/incidents/YYYY-MM-DD-short-description.md`:
```markdown
# Incident: YYYY-MM-DD — Short Description

## Timeline
- HH:MM UTC: Alert triggered
- HH:MM UTC: Engineer paged
- HH:MM UTC: Root cause identified
- HH:MM UTC: Mitigation applied
- HH:MM UTC: Recovery confirmed

## Impact
- Queries affected: NNN (estimated)
- Duration: NNN minutes
- Severity: P0/P1/P2/P3

## Root Cause
[What caused this]

## Resolution
[What fixed it]

## Prevention
[What change prevents recurrence]
```

---

## Playbook 6: On-Call Escalation

| Issue | First Contact | Escalate To |
|-------|--------------|-------------|
| Qdrant / Neo4j infra | On-call engineer | Platform team |
| AI Gateway errors | On-call engineer | Shiprocket AI team |
| MARS → COSMOS routing broken | On-call engineer | MARS team |
| KB quality degraded | Data engineer | KB specialist |
| Security incident (injection, leak) | Security engineer | CISO + MARS team |

# Operational Playbooks

Common runbooks for COSMOS operations. Each playbook follows: Diagnose → Verify → Act → Escalate.

---

## Playbook 1: COSMOS Won't Start

### Diagnose
```bash
npm start 2>&1 | head -50
# Look for [ERROR] lines in the first 20 lines of output
```

### Common causes and fixes

**Port in use**
```bash
lsof -ti :10001 | xargs kill -9
npm start
```

**Missing Python package**
```bash
# Error: ModuleNotFoundError: No module named 'X'
.venv/bin/pip install X
npm start
```

**API key missing**
```bash
# Error: Either OPENAI_API_KEY or AIGATEWAY_API_KEY is required
# Fix: check .env file
cat .env | grep -E "(AIGATEWAY|OPENAI)_API_KEY"
# If missing, add to .env:
echo "AIGATEWAY_API_KEY=your_key_here" >> .env
```

**MySQL DDL error**
```bash
# Error: (1101) BLOB, TEXT, GEOMETRY or JSON column can't have a default value
# Already fixed in codebase — if you see this, pull latest main
git pull origin main
```

### Verify
```bash
curl http://localhost:10001/cosmos/health
# Expected: {"status": "ok", ...}
```

---

## Playbook 2: recall@5 Drop Below Gate

### Diagnose
```bash
# Run eval to confirm
curl -X POST http://localhost:10001/cosmos/api/v1/cmd/eval
# Look for: "recall_at_5" < 0.75
```

### Common causes

**New KB ingestion broke existing chunks**
```bash
# Check for failed files in the index
mysql -u root -h 127.0.0.1 -P 3309 mars -e \
  "SELECT repo_id, COUNT(*) FROM cosmos_kb_file_index WHERE status=2 GROUP BY repo_id;"
```

**RRF weights changed without calibration**
```bash
# Check cosmos.config.json — compare with last good commit
git diff HEAD~1 cosmos.config.json | grep rrf_weights
```

**Qdrant collection was rebuilt with wrong dimension**
```bash
curl http://localhost:6333/collections/cosmos_embeddings
# Check: "vectors": {"size": 1536}
```

### Act
1. If failed KB files: re-run ingestion for failed files only
   ```bash
   curl -X POST http://localhost:10001/cosmos/api/v1/training-pipeline \
     -d '{"filter": "failed_only": true}'
   ```
2. If RRF weights changed: revert to last known-good weights in `cosmos.config.json`
3. If Qdrant wrong dimension: recreate collection and full re-ingest (document in ADR)

### Escalate
If recall@5 still < 0.75 after above fixes: file P1 issue, block deployment until resolved.

---

## Playbook 3: Hallucination Guard Triggering Too Often

### Diagnose
```bash
# Check hallucination block rate
mysql -u root -h 127.0.0.1 -P 3309 mars -e \
  "SELECT feedback_type, COUNT(*) FROM cosmos_feedback_traces 
   WHERE feedback_type='hallucination_blocked' 
   AND created_at > NOW() - INTERVAL 1 HOUR GROUP BY feedback_type;"
```

Normal rate: < 1% of requests. If > 5%: investigate.

### Common causes

**KB content has low trust scores**
```bash
# Check average trust score in Qdrant
curl http://localhost:6333/collections/cosmos_embeddings
# If count recently dropped: re-ingest
```

**Query domain not covered in KB**
- Operators asking about topics not in any pillar
- Fix: identify domain, write KB docs for that domain, re-ingest

**Reranker selecting wrong chunks**
- Check if reranker model changed or Opus quota exceeded
- If fallback to Sonnet reranking: restore Opus routing in `cosmos.config.json`

### Act
1. Check which entity IDs are being flagged as hallucinated
2. Trace back to which KB pillar should cover that entity
3. If pillar missing: trigger `/cosmos:train` after adding KB docs
4. If pillar exists but low trust: regenerate KB docs with Claude Opus + `trust_score: 0.9`

---

## Playbook 4: Kafka Consumer Lag

### Diagnose
```bash
# Check consumer group lag
.venv/bin/python -c "
import asyncio
from aiokafka.admin import AIOKafkaAdminClient
async def check():
    admin = AIOKafkaAdminClient(bootstrap_servers='localhost:9092')
    await admin.start()
    offsets = await admin.list_consumer_group_offsets('cosmos-workers')
    print(offsets)
asyncio.run(check())
"
```

### Common causes

**Consumer not started (KAFKA_ENABLED=false)**
```bash
grep KAFKA_ENABLED .env
# If false: set to true and restart
```

**Consumer crashed on malformed message**
```bash
# Check app logs for Kafka errors
grep "kafka" /tmp/cosmos*.log | grep ERROR
```

### Act
1. If consumer lag > 10,000 messages: scale consumer (increase partition count)
2. If consumer crashed: check `events/handlers.py` for the malformed message handler
3. If Kafka not available locally: set `KAFKA_ENABLED=false` for local dev

---

## Playbook 5: Qdrant Search Returning Empty Results

### Diagnose
```bash
# Check collection exists and has vectors
curl http://localhost:6333/collections/cosmos_embeddings
# Expected: "vectors_count" > 0

# Check Qdrant is healthy
curl http://localhost:6333/healthz
# Expected: "healthz check passed"
```

### Common causes

**Collection empty (never ingested)**
```bash
# Trigger full KB ingestion
curl -X POST http://localhost:10001/cosmos/api/v1/training-pipeline \
  -d '{"force_reingest": true}'
```

**Wrong collection name**
```bash
# Check QDRANT_COLLECTION in .env
grep QDRANT_COLLECTION .env
# Must match: cosmos_embeddings
```

**Embedding dimension mismatch**
```bash
# Verify vector size in collection
curl http://localhost:6333/collections/cosmos_embeddings | python -m json.tool | grep size
# Must be 1536
```

### Act
1. Verify Qdrant is running: `docker ps | grep qdrant`
2. If not running: `docker run -p 6333:6333 -p 6334:6334 qdrant/qdrant`
3. If collection empty: trigger ingestion pipeline
4. If dimension mismatch: recreate collection (document in ADR), full re-ingest

---

## Playbook 6: High Query Latency (P95 > 2s)

### Diagnose
```bash
# Check Prometheus metrics
curl http://localhost:10001/cosmos/metrics | grep query_latency
```

### Common causes and fixes

| Cause | Signal | Fix |
|-------|--------|-----|
| Neo4j BFS depth too high | Leg 3 latency > 500ms | Reduce BFS depth to 2 |
| PPR graph too large | Leg 2 latency > 800ms | Limit PPR to top-500 nodes |
| Qdrant cold cache | First queries slow | Keep Qdrant warm with health pings |
| Reranker rate limited | Opus 429 errors | Add retry with exponential backoff |
| MySQL connection pool exhausted | Pool timeout errors | Increase `DATABASE_POOL_SIZE` |

### Escalate
P95 > 5s → P1 incident. Engage devops agent to scale or optimize.

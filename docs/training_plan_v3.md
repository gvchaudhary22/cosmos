# COSMOS Training Plan v3 — Schema-Converged, Split-Evaluated, Capability-Gated

## Changelog from v2

| Issue | v2 Problem | v3 Fix |
|-------|-----------|--------|
| [High] Eval data leakage | global_eval_set.jsonl used as training input, making accuracy projections untrustable | Split into train/dev/holdout (70/15/15). Renamed files. Holdout never touched during training |
| [High] Week 1 marked done | Checkboxes checked but code still has 3 repos + *.md only | Replaced with target milestones, not completion claims |
| [High] Storage split unresolved | TrainingService uses source_id/source_type, VectorStoreService uses entity_id/entity_type, KnowledgeIndexer is separate in-memory | New Section 1: Schema Convergence with explicit migration steps |
| [Medium] Single trust gate for all capabilities | debugging.md treated same for retrieval, intent, and graph weights | Split into 3 eligibility matrices per capability |
| [Medium] generated/ not auto-consumed | KB indexer walks Pillar 1/3 only, won't find generated/ | Explicit ingestion path for generated/ artifacts |
| [Medium] Multiple eval files not merged | Repo-scoped files mixed without dedup/stratification | Merge + dedup + stratified split rules defined |
| [Low] Repo-wide trust too blunt | "SR_Web = Tier D" ignores per-artifact quality | Per-artifact gating using status + score + evidence + freshness |

---

## Section 1: Schema Convergence (DO THIS FIRST)

### Problem

COSMOS currently has three separate knowledge/retrieval paths:

```
Path 1: KnowledgeIndexer (indexer.py)
  → In-memory TF-IDF over Pillar 1 tables + Pillar 3 APIs
  → NOT in cosmos_embeddings table
  → Queried via brain["indexer"].search()

Path 2: VectorStoreService (vectorstore.py)
  → cosmos_embeddings table (pgvector)
  → Uses: entity_type, entity_id, embedding, content, metadata
  → Queried via vectorstore.search_similar()

Path 3: TrainingService (training.py)
  → ALSO writes to cosmos_embeddings
  → But uses: source_type field in metadata (not entity_type directly)
  → Plus writes to cosmos_intent_models, cosmos_graph_weights
```

### Fix: One canonical schema, one ingestion owner

**Step 1: Standardize cosmos_embeddings contract**

```sql
-- The ONE table for all retrieval
-- entity_type = capability bucket (NOT source provenance)
-- metadata.source_type = where it came from (provenance)
-- metadata.trust_score = quality weight

ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS trust_score FLOAT DEFAULT 0.5;
ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS freshness TIMESTAMP;
ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS capability VARCHAR(50);
ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(100) DEFAULT 'all-MiniLM-L6-v2';
ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS embedding_version VARCHAR(50) DEFAULT 'v1';
ALTER TABLE cosmos_embeddings ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMP DEFAULT NOW();

-- capability values: retrieval | intent_seed | graph_edge | page_intel | code_intel
-- entity_type values: schema | api_tool | page | page_intent | cross_repo |
--   module_overview | module_api | module_database | module_rules | module_debug |
--   module_gaps | module_meta | module_evidence | module_submodule |
--   knowledge | distillation | domain_overview | symptom_fix | field_lineage |
--   async_flow | tool_playbook | error_pattern | user_alias | known_gap
```

### Canonical Row Identity + Upsert Policy

Without a unique key, every re-ingest duplicates documents and skews retrieval, monitoring, and trust distribution.

**Unique constraint:**

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_identity
ON cosmos_embeddings (repo_id, entity_type, entity_id, capability, embedding_model, embedding_version);
```

**Upsert behavior:**

```sql
-- On re-ingest, UPDATE existing row, don't INSERT duplicate
INSERT INTO cosmos_embeddings (repo_id, entity_type, entity_id, capability,
  embedding_model, embedding_version, content, embedding, trust_score, freshness, metadata, embedded_at)
VALUES (...)
ON CONFLICT (repo_id, entity_type, entity_id, capability, embedding_model, embedding_version)
DO UPDATE SET
  content = EXCLUDED.content,
  embedding = EXCLUDED.embedding,
  trust_score = EXCLUDED.trust_score,
  freshness = EXCLUDED.freshness,
  metadata = EXCLUDED.metadata,
  embedded_at = NOW();
```

**Reindex cleanup:**

```
When upgrading embedding model (e.g., MiniLM v1 → v2):
  1. Insert new rows with embedding_version='v2' (upsert won't conflict because version differs)
  2. A/B compare retrieval on holdout
  3. If v2 wins: DELETE FROM cosmos_embeddings WHERE embedding_version = 'v1'
  4. If v2 loses: DELETE FROM cosmos_embeddings WHERE embedding_version = 'v2'
  5. No downtime — old version serves queries throughout migration
```

**Step 2: Deprecate KnowledgeIndexer as primary retrieval**

```
Current: brain["indexer"].search() → in-memory TF-IDF (separate from vectorstore)
Target:  brain["indexer"] becomes a thin wrapper over vectorstore.search_similar()
         OR is deprecated entirely in favor of direct vectorstore queries

Migration:
  1. Move Pillar 1 table docs from indexer.py in-memory → cosmos_embeddings (entity_type='schema')
  2. Move Pillar 3 API docs from indexer.py in-memory → cosmos_embeddings (entity_type='api_tool')
  3. After migration, indexer.py search() delegates to vectorstore.search_similar()
  4. Remove in-memory TF-IDF index (saves ~200MB RAM)
```

**Step 3: TrainingService writes use standardized columns**

```
Current: training.py stores source_type in metadata JSON
Target:  training.py uses entity_type column + capability column directly

Before:
  store_embedding(entity_type='page', metadata={'source_type': 'page'})

After:
  store_embedding(entity_type='page', capability='page_intel', trust_score=0.8)
```

---

## Section 2: Train / Dev / Holdout Split

### Problem

v2 used global_eval_set.jsonl (10K+ hand-labeled examples) as training data for the intent classifier. This leaks evaluation data into training, making accuracy projections untrustable.

### Fix: Three-way split with provenance tracking

**Rename files:**

```
BEFORE:
  MultiChannel_API/pillar_3.../global_eval_set.jsonl  (10K+ mixed)
  SR_Web/pillar_4.../training_seeds.jsonl              (50 seeds)
  MultiChannel_Web/pillar_4.../training_seeds.jsonl    (50 seeds)
  */pillar_4.../pages/*/eval_cases.yaml                (per-page evals)

AFTER (generated from the above, stored in cosmos DB or generated/ folder):
  train_set.jsonl     — 70% of merged+deduped examples (used for training)
  dev_set.jsonl       — 15% (used for hyperparameter tuning, early stopping)
  holdout_set.jsonl   — 15% (NEVER used during training, only for final accuracy reporting)
```

**Merge + dedup + stratification rules:**

```
1. Merge all repo-scoped eval files into one pool
2. Dedup by normalized query (lowercase, strip whitespace, remove punctuation)
3. Stratify by:
   - intent distribution (each intent proportionally represented in all 3 splits)
   - repo distribution (seller + admin examples in all 3 splits)
   - capability distribution (tool, page, schema, module queries in all splits)
4. Random seed = fixed (reproducible splits)
5. Store split provenance: {original_file, line_number, split, split_version, split_date}
```

**Usage:**

```
Intent classifier training:    train_set.jsonl ONLY
Hyperparameter tuning:         dev_set.jsonl ONLY
Accuracy reporting (in docs):  holdout_set.jsonl ONLY
Embedding training:            train_set.jsonl + dev_set.jsonl (retrieval doesn't overfit like classifiers)
Graph weights:                 NOT from eval sets (driven by runtime tool execution data)
```

---

## Section 3: Training Eligibility Matrices (Per Capability)

### Problem

v2 had one gate: trust_score >= 0.5 → train everything. But a debugging.md chunk is good for retrieval, useful for seed generation, but should NOT directly influence graph weights.

### Fix: Three explicit matrices

**Matrix 1: Retrieval Indexing (who gets embedded)**

```
Source                              Eligible?   trust_score   Notes
─────────────────────────────────────────────────────────────────────
Pillar 1 schema YAML                YES         0.8           Core schema knowledge
Pillar 3 API tool YAML              YES         0.8           Core API knowledge
Pillar 4 page/role YAML             YES         0.8           Page structure
global train_set.jsonl              YES         0.9           Verified examples
Pillar 4 training_seeds.jsonl       YES         0.9           Hand-crafted seeds
module.yaml (enriched, score>=50)   YES         0.7           Module metadata
evidence/index.yaml                 YES         0.85          Verified facts
business_rules.md (enriched)        YES         0.6           Business logic
api.md (enriched)                   YES         0.6           API docs
database.md (enriched)              YES         0.6           Schema context
debugging.md (enriched)             YES         0.55          Symptom→fix
known_gaps.md (any)                 YES         0.5           Uncertainty signals
submodules/*.md (enriched parent)   YES         0.5           Channel-specific
module.yaml (draft, score<20)       YES         0.3           Tier D retrieval-only
CLAUDE.md (draft)                   YES         0.25          Tier D retrieval-only
prd.md / ssd.md                     YES         0.2           Design docs, Tier D
Generated artifacts (generated/)    YES         inherits      From source trust
icrm_knowledge_entries              YES         0.75          Curated KB
icrm_distillation_records           YES         0.6           Runtime learning
```

**Matrix 2: Supervised Classifier Data (who trains intent model)**

```
Source                              Eligible?   Notes
──────────────────────────────────────────────────────────────
train_set.jsonl (from eval split)   YES         Primary training data
Pillar 4 training_seeds.jsonl       YES         Augmentation seeds
icrm_distillation (conf >= 0.8)     YES         High-confidence runtime
tool_playbook.yaml (negatives)      YES         Hard negatives only
debugging.md                        NO          Not intent training data
business_rules.md                   NO          Not intent training data
module.yaml                         NO          Not intent training data
Pillar 1 schema                     NO          Not intent training data
Draft module docs                   NO          Too noisy for classifier
icrm_distillation (conf < 0.5)      NO          Too noisy
```

**Matrix 3: Graph Edge/Weight Inputs (who influences traversal)**

```
Source                              Eligible?   Edge Type
──────────────────────────────────────────────────────────────
icrm_tool_executions (success)      YES         Tool success weight
icrm_distillation (high feedback)   YES         Tool quality signal
Pillar 1 table→table relations      YES         Schema edges (FK, joins)
Pillar 4 page→API→table chains      YES         Page traversal edges
Pillar 4 cross_repo_mapping         YES         Cross-repo edges
async_flow_chain.yaml (generated)   YES         Async flow edges
field_lineage.yaml (generated)      YES         Field trace edges
evidence/index.yaml facts           YES (weak)  Evidence-backed edges
debugging.md                        NO          Not a graph input
CLAUDE.md                           NO          Not a graph input
prd.md / ssd.md                     NO          Not a graph input
Draft module docs                   NO          Not a graph input
```

---

## Section 4: Per-Artifact Trust Scoring (Not Repo-Wide)

### Problem

v2 said "SR_Web module docs are Tier D" — too blunt. A specific SR_Web module might have evidence, while another is empty.

### Fix: Compute trust per artifact

```python
def compute_trust(module_yaml: dict, has_evidence: bool, artifact_type: str) -> float:
    """Per-artifact trust scoring."""
    base = 0.3  # default

    status = module_yaml.get("status", "draft")
    score = module_yaml.get("score", 0)

    # Status contribution
    if status == "enriched":
        base = 0.6
    elif status == "active":
        base = 0.55
    elif status == "draft":
        base = 0.25

    # Score contribution (0-100 → 0.0-0.2 bonus)
    base += min(0.2, score / 500)

    # Evidence bonus
    if has_evidence:
        base += 0.1

    # Freshness bonus (scanned in last 30 days)
    freshness = module_yaml.get("freshness", {})
    last_scan = freshness.get("last_scan", "")
    if last_scan and is_recent(last_scan, days=30):
        base += 0.05

    # Artifact type adjustment
    if artifact_type == "evidence":
        base += 0.15  # Evidence is highest-trust doc type
    elif artifact_type in ("business_rules", "api", "database"):
        base += 0.0   # Standard
    elif artifact_type in ("prd", "ssd"):
        base -= 0.1   # Design docs are weaker
    elif artifact_type == "debugging":
        base += 0.05  # Practical fix knowledge

    return min(0.95, max(0.1, base))
```

**Example: SR_Web modules with per-artifact scoring**

```
SR_Web/orders/module.yaml (draft, score=0, no evidence):
  module_meta:     trust=0.25
  CLAUDE.md:       trust=0.25
  api.md:          trust=0.25
  debugging.md:    trust=0.30  (debugging still useful even if draft)

SR_Web/dashboard/module.yaml (draft, score=0, HAS evidence):
  module_meta:     trust=0.35  (+0.10 evidence bonus)
  evidence:        trust=0.50  (+0.15 evidence type bonus)
  CLAUDE.md:       trust=0.35

MultiChannel_API/orders/module.yaml (enriched, score=85, HAS evidence):
  module_meta:     trust=0.87
  evidence:        trust=0.92  (highest trust in system)
  business_rules:  trust=0.77
  debugging.md:    trust=0.82
```

---

## Section 5: Generated Artifacts Ingestion Path

### Problem

v2 proposes generating artifacts in `knowledge_base/shiprocket/generated/` but the current KB indexer (indexer.py) only walks Pillar 1 tables and Pillar 3 APIs. Generated artifacts would sit on disk unused.

### Fix: Generated artifacts use the SAME canonical ingestion pipeline

Section 1 says "one ingestion owner." Generated artifacts must not create a second
special-case ingestion lane through codebase_intelligence.py. Instead, they go through
the same canonical corpus ingestor that handles all other sources.

```
WRONG (creates split owner):
  codebase_intel.py ingests generated/ → vectorstore
  training.py ingests KB/runtime → vectorstore
  Two different code paths writing to same table

RIGHT (one canonical path):
  All sources → canonical_ingestor.ingest(source, entity_type, trust_score)
                → vectorstore.store_embedding() with upsert

  canonical_ingestor handles:
    - Pillar 1 schema YAML
    - Pillar 3 API YAML
    - Pillar 4 page/role YAML
    - generated/ artifacts (domain_overview, symptom_fix, etc.)
    - module docs (.claude/)
    - runtime data (knowledge_entries, distillation)
    - eval/seed data (train_set.jsonl)

  codebase_intelligence.py is a SOURCE READER, not an INGESTOR.
  It reads .claude/ files and generated/ files, then passes them
  to the canonical ingestor. It does not call vectorstore directly.

Flow:
  codebase_intel.read_module_docs() → returns list of documents
  canonical_ingestor.ingest(documents) → vectorstore.store_embedding() with upsert

  training_service.prepare_kb_data() → returns list of documents
  canonical_ingestor.ingest(documents) → vectorstore.store_embedding() with upsert
```

### generated_manifest.yaml (required in every generated/ folder)

```yaml
# knowledge_base/shiprocket/generated/MultiChannel_API/tracking/generated_manifest.yaml
generated_at: "2026-03-29T10:00:00Z"
source_files:
  - path: "repos/shiprocket/MultiChannel_API/.claude/docs/modules/tracking/CLAUDE.md"
    hash: "abc123"
  - path: "repos/shiprocket/MultiChannel_API/.claude/docs/modules/tracking/debugging.md"
    hash: "def456"
  - path: "repos/shiprocket/MultiChannel_API/.claude/docs/modules/tracking/business_rules.md"
    hash: "ghi789"
trust_score: 0.75
module_status: "enriched"
module_score: 72
artifacts_generated:
  - domain_overview.yaml
  - symptom_root_cause.yaml
  - field_lineage.yaml
  - async_flow_chain.yaml
stale_after_days: 30
```

If source file hashes change → regenerate artifacts. If stale_after_days exceeded → flag for re-generation.

---

## Section 6: MARS Training Pipe Compatibility

### Problem

MARS `formatter.go` currently generates routing-oriented JSONL only. It is not compatible with the richer canonical corpus schema defined in this plan.

### Fix: Extend MARS formatter to produce capability-aware documents

```
Current MARS formatter output (formatter.go):
  {"query": "where is my order", "route": "orders", "entity": "order"}

Target MARS formatter output:
  {
    "query": "where is my order",
    "capability": "intent",
    "ground_truth_intent": "lookup",
    "ground_truth_entity": "order",
    "ground_truth_tool": "lookup_order",
    "source_type": "mars_formatter",
    "trust_score": 0.6,
    "repo_id": "MultiChannel_API"
  }

Migration steps:
  1. Keep existing routing format as backward-compatible output
  2. Add new --format=cosmos flag that produces canonical schema
  3. COSMOS ingestion pipeline accepts both formats (auto-detect)
  4. After validation, switch default to cosmos format
```

---

## Section 7: Source Weighting (Revised)

```
Tier A (trust 0.85-1.0): Ground truth
  - holdout_set.jsonl reserved for eval ONLY, never trained
  - train_set.jsonl (from merged eval split, 70%)
  - Successful tool executions (icrm_tool_executions, status=success)
  - High-confidence distillation (confidence >= 0.8, feedback >= 4)
  - Tier 3 DB fallback wins (verified by live data)
  - Human resolutions recorded via feedback

Tier B (trust 0.7-0.85): Primary knowledge
  - Pillar 1 schema YAML (tables, columns, state machines)
  - Pillar 3 API tool YAML (endpoints, params, examples)
  - Pillar 4 page/role YAML (pages, fields, permissions)
  - Evidence/index.yaml (verified facts with confidence scores)
  - Enriched module.yaml with score >= 50

Tier C (trust 0.5-0.7): Secondary knowledge
  - Module docs (enriched/active, score >= 50): business_rules, api, database, debugging
  - Generated artifacts (domain_overview, symptom_fix, etc.)
  - Submodule docs from enriched parent modules
  - dev_set.jsonl (used for tuning, not final eval)

Tier D (trust 0.1-0.5): Retrieval-only (NEVER used for classifier or graph training)
  - Draft module docs (status=draft, score < 20)
  - prd.md, ssd.md (design docs)
  - SR_Web draft modules (per-artifact gated, not repo-wide blanket)
  - MultiChannel_Web draft modules
  - CLAUDE.md raw text from draft modules
```

---

## Execution Milestones (Not Completion Claims)

```
Milestone 1 — Schema Convergence:
  TARGET: Standardize cosmos_embeddings schema (add trust_score, freshness, capability columns)
  TARGET: Migrate KnowledgeIndexer in-memory data to cosmos_embeddings
  TARGET: TrainingService uses standardized columns
  DEPENDS ON: Database migration

Milestone 2 — Train/Dev/Holdout Split:
  TARGET: Merge all eval files, dedup, stratified split (70/15/15)
  TARGET: Rename to train_set.jsonl / dev_set.jsonl / holdout_set.jsonl
  TARGET: Intent classifier trains ONLY on train_set + seeds + high-conf distillation
  DEPENDS ON: Milestone 1

Milestone 3 — Expanded Ingestion:
  TARGET: codebase_intelligence.py ingests all 8 repos
  TARGET: Parses module.yaml + evidence/index.yaml + submodules (not just *.md)
  TARGET: Chunks by doc type (module_api, module_rules, etc.)
  TARGET: Per-artifact trust scoring (not repo-wide)
  DEPENDS ON: Milestone 1

Milestone 4 — P0 KB Artifacts:
  TARGET: Generate domain_overview, symptom_root_cause, field_lineage, async_flow_chain
          for tracking, shipments, orders, base_channel
  TARGET: generated_manifest.yaml with source hashes + trust scores
  TARGET: codebase_intelligence.py also ingests generated/ folder
  DEPENDS ON: Milestone 3

Milestone 5 — Pillar 1 Schema + Pillar 3 API Ingestion:
  TARGET: 442 table docs embedded as entity_type='schema'
  TARGET: Top 1,600 API docs embedded as entity_type='api_tool'
  TARGET: Ranked retrieval: similarity x trust_score x freshness_decay x capability_fit
  DEPENDS ON: Milestone 1

Milestone 6 — Intent Classifier Upgrade:
  TARGET: Train on train_set.jsonl + seeds + hard negatives
  TARGET: Validate on dev_set.jsonl
  TARGET: Report accuracy ONLY on holdout_set.jsonl
  TARGET: Capability-aware labels: (query, intent, entity, tool, capability)
  DEPENDS ON: Milestone 2

Milestone 7 — Graph Weight Expansion:
  TARGET: Add schema edges (Pillar 1 FK relationships)
  TARGET: Add async_flow_chain edges
  TARGET: Add field_lineage edges
  TARGET: Eligibility: only from Matrix 3 sources (no debugging.md, no CLAUDE.md)
  DEPENDS ON: Milestones 4, 5

Milestone 8 — Runtime Learning Loop:
  TARGET: Tier 3 success → auto-create training seed (goes into train_set next cycle)
  TARGET: Human resolution → auto-create symptom_root_cause entry
  TARGET: generated_manifest.yaml staleness check on each restart
  DEPENDS ON: Milestones 4, 6

Milestone 9 — MARS Formatter Upgrade:
  TARGET: formatter.go supports --format=cosmos producing canonical schema
  TARGET: COSMOS ingestion accepts both old routing format and new canonical format
  DEPENDS ON: Milestone 1
```

---

## Expected Impact (Target Ranges — Pending Baseline Measurement)

```
ALL numbers below are TARGET RANGES, not projections.
Actual impact will be measured on holdout_set.jsonl AFTER implementation.
"Current" estimates are educated guesses until baseline is measured.

Step 1: Measure baseline on holdout BEFORE any changes (Milestone 2 output)
Step 2: Implement milestones
Step 3: Re-measure on SAME holdout
Step 4: Report actual delta, not projected delta

                            Baseline*  Target    Measured**
─────────────────────────────────────────────────────────────
Schema queries              TBD        >= 85%    (after M5)
  (holdout schema subset)

API/tool queries            TBD        >= 85%    (after M5)
  (holdout tool subset)

Status stuck queries        TBD        >= 80%    (after M4)
  (holdout tracking subset)

Intent classification       TBD        >= 90%    (after M6)
  (holdout intent split)

Tier 1 resolution rate      TBD        >= 78%    (after M7)
  (end-to-end on holdout)

* Baseline: measured on holdout BEFORE plan implementation (Milestone 2)
** Measured: filled in after each milestone completes

If baseline is already above target → that metric is not a priority.
If baseline is far below target → that metric gets extra training investment.
Targets will be revised after baseline measurement.
```

---

## Section 8: Graph Enrichment — 7 Dimensions Wired to Runtime

### Problem

quality.py enriches graph nodes with 7 dimensions (confidence, freshness, negatives, guardrails, eval_cases, evidence, errors). But:
- graphrag.py traversal uses plain BFS — ignores all enrichment
- training.py graph weights come from tool_executions only — ignores enrichment
- KB ingest + enrichment requires manual API calls — not auto-triggered

### The 7 Dimensions and Their Runtime Impact

```
Dimension         What It Does                                    Runtime Impact
─────────────────────────────────────────────────────────────────────────────────────
1. Edge weights   Stronger edges = more trusted paths             Traversal follows high-confidence paths first
2. Node confidence 0.82 vs 0.3 tells trust level                 Low-confidence → "I'm not sure" not hallucination
3. Negative signals "Don't use cancel when user says track"       Prevents wrong tool selection (#1 bad answer cause)
4. Guardrails     "Needs approval, has PII, blast_radius=high"   System asks confirmation before dangerous actions
5. Eval cases     Known input→output pairs per node              Automated graph answer validation
6. Freshness      Decays over time                               Stale APIs deprioritized, fresh ones preferred
7. Evidence       "Found via code_scan, commit a1b2c3d"          Traceable answers — "based on commit X"
```

### Rich Node Structure (Target State)

```yaml
Node: api:mcapi.orders.cancel
  properties:
    method: "POST"
    path: "/orders/cancel"
    domain: "orders"
    quality:
      confidence: 0.82
      freshness: 0.91
      enriched_at: "2026-03-29T..."
    safety:
      read_write_type: "write"
      blast_radius: "high"
      pii_fields: ["customer_email"]
      idempotent: false
    guardrails:
      requires_approval: true
      max_params: 3
      safety_constraints:
        - {rule: "Must have valid order_id", severity: "critical", action: "block"}
    evidence:
      source_commit_sha: "a1b2c3d"
      discovery_method: "code_scan"
      verified_at: "2026-03-24"
    eval_cases:
      - {input: "cancel order 12345", expected_tool: "orders_cancel",
         expected_params: {order_id: "12345"}, difficulty: "easy"}
    negative_signals:
      - {query_pattern: "track my order", should_not_use: "orders_cancel",
         use_instead: "orders_track", signal_type: "negative_routing"}
    errors_retries:
      error_codes: [{code: 404, meaning: "Order not found", retryable: false}]
      retry_strategy: {max_retries: 0, backoff: "none"}
```

### Fix 1: Make traverse() Use Enrichment Dimensions

```python
# CURRENT graphrag.py traverse (plain BFS — ignores enrichment):
for neighbor in graph.neighbors(node):
    queue.append(neighbor)

# TARGET (weighted priority queue):
import heapq

def traverse_enriched(self, node_id, max_depth=2, query=""):
    priority_queue = []  # (negative_priority, depth, node_id)
    heapq.heappush(priority_queue, (0.0, 0, node_id))
    visited = set()
    results = []

    while priority_queue and len(results) < 50:
        neg_priority, depth, current = heapq.heappop(priority_queue)
        if current in visited or depth > max_depth:
            continue
        visited.add(current)
        results.append(current)

        for neighbor in graph.neighbors(current):
            if neighbor in visited:
                continue

            edge = graph.edges[current, neighbor]
            node_props = graph.nodes[neighbor].get("properties", {})

            quality = node_props.get("quality", {})
            confidence = quality.get("confidence", 0.5)
            freshness = quality.get("freshness", 0.5)

            # Negative signal check
            negatives = node_props.get("negative_signals", [])
            negative_penalty = 0.3 if any(
                neg.get("query_pattern", "") in query.lower()
                for neg in negatives
            ) else 0.0

            # Composite priority (higher = better, negate for min-heap)
            edge_weight = edge.get("weight", 0.5)
            priority = edge_weight * confidence * freshness * (1.0 - negative_penalty)
            heapq.heappush(priority_queue, (-priority, depth + 1, neighbor))

    return results
```

### Fix 2: KB-to-Enricher Schema Adapter

```python
# quality.py: normalize both old and new KB formats

def _normalize_eval_cases(raw: dict) -> list:
    """Accept both old and new KB eval case formats."""
    if "cases" in raw:
        return [{"input": c.get("input"), "expected": c.get("expected_tool")} for c in raw["cases"]]
    if "golden_eval_cases" in raw:
        return [{"input": c.get("query"), "expected": c.get("expected_tool")} for c in raw["golden_eval_cases"]]
    return []

def _normalize_guardrails(raw: dict) -> dict:
    """Accept both flat and nested guardrail formats."""
    if "guardrails" in raw and isinstance(raw["guardrails"], dict):
        return raw["guardrails"]  # nested format (current KB)
    # Flat format
    return {k: v for k, v in raw.items() if k in ("requires_approval", "max_params", "safety_constraints")}

def _normalize_freshness(raw: dict) -> dict:
    """Accept top-level or nested freshness."""
    if "freshness" in raw and isinstance(raw["freshness"], dict):
        return raw["freshness"]
    return {"last_verified_at": raw.get("last_verified_at", ""), "stale_after_days": raw.get("stale_after_days", 30)}

def _normalize_errors(raw: dict) -> dict:
    """Accept both error_codes/retry_strategy and known_errors/retry_policy."""
    result = {"error_codes": [], "retry_strategy": {}}
    if "error_codes" in raw:
        result["error_codes"] = raw["error_codes"]
    elif "known_errors" in raw:
        result["error_codes"] = raw["known_errors"]
    if "retry_strategy" in raw:
        result["retry_strategy"] = raw["retry_strategy"]
    elif "retry_policy" in raw:
        result["retry_strategy"] = raw["retry_policy"]
    return result

def _normalize_evidence(raw: dict) -> dict:
    """Accept top-level provenance or nested sources."""
    if "evidence" in raw:
        return raw["evidence"]
    if "sources" in raw:
        return {"sources": raw["sources"]}
    return {k: v for k, v in raw.items() if k in ("source_commit_sha", "discovery_method", "verified_at")}
```

### Fix 3: Auto-Ingest + Enrich on Startup

```python
# main.py lifespan addition (after graph load, before gRPC server start):

# Auto-run KB graph ingest + enrichment
try:
    from app.services.graphrag_ingest import ingest_kb_to_graph
    from app.services.graphrag_quality import enrich_graph_nodes

    graphrag = app.state.graphrag
    if graphrag and os.path.isdir(kb_path):
        # Check if KB is newer than last ingest
        last_ingest = await graphrag.get_last_ingest_timestamp()
        kb_mtime = get_kb_latest_mtime(kb_path)

        if last_ingest is None or kb_mtime > last_ingest:
            ingest_stats = await ingest_kb_to_graph(graphrag, kb_path)
            logger.info("graph.kb_ingested", **ingest_stats)

        # Enrich nodes that lack quality dimensions
        enrich_stats = await enrich_graph_nodes(graphrag, kb_path)
        logger.info("graph.enriched",
            nodes=enrich_stats.get("enriched_nodes", 0),
            avg_confidence=enrich_stats.get("avg_confidence", 0),
        )
except Exception as e:
    logger.warning("graph.auto_ingest_failed", error=str(e))
```

### Fix 4: Feed Negative Signals into Tool Routing

```python
# react.py _select_tools() modification:

def _select_tools(self, classification, accumulated, loop_num, query=""):
    candidates = self._get_tool_candidates(classification)

    # Check negative signals from graph enrichment
    if self.graphrag:
        for candidate in list(candidates):
            tool_node = self.graphrag.get_node(f"tool:{candidate}")
            if tool_node:
                negatives = tool_node.properties.get("negative_signals", [])
                for neg in negatives:
                    if neg.get("query_pattern", "") in query.lower():
                        # This tool has a negative signal for this query pattern
                        logger.info("tool.negative_signal",
                            tool=candidate,
                            pattern=neg["query_pattern"],
                            use_instead=neg.get("use_instead", ""),
                        )
                        candidates.remove(candidate)
                        # Add the recommended alternative
                        alt = neg.get("use_instead")
                        if alt and alt not in candidates:
                            candidates.append(alt)
                        break

    return candidates
```

### Usage Flow

```
POST /cosmos/api/v1/graphrag/ingest/kb      Step 1: Load structure (manual or auto at startup)
POST /cosmos/api/v1/graphrag/ingest/enrich   Step 1.5: Add 7 quality dimensions
GET  /cosmos/api/v1/graphrag/quality/report   Monitor: avg confidence, freshness, coverage

Auto at startup (main.py) — BACKGROUND, NEVER BLOCKS BOOT:
  1. Load last-good graph from DB immediately → serve queries now
  2. In background task (asyncio.create_task):
     a. Check if KB is newer than last ingest timestamp
     b. If yes → re-ingest nodes/edges into a staging graph
     c. Enrich staging graph (all 7 dimensions)
     d. Atomic swap: replace live graph with staging graph
     e. Log: "Graph enriched: 1,200 nodes, avg confidence 0.78"
  3. If background fails → live graph is unaffected, log warning

  Operational rule: COSMOS must boot and serve queries within 10 seconds.
  Graph re-ingest/enrich can take 30-120 seconds — that's fine in background.
  Users get the last-good graph immediately, upgraded graph when ready.

  Implementation pattern:
    # main.py lifespan
    await graphrag_service.load_from_db()  # fast: load last-good state
    app.state.graphrag = graphrag_service   # serve immediately

    # Background: re-ingest + enrich if KB changed
    async def _background_graph_refresh():
        try:
            if kb_newer_than_last_ingest(kb_path, graphrag_service):
                staging = await ingest_kb_to_graph(kb_path)
                staging = await enrich_graph_nodes(staging, kb_path)
                app.state.graphrag = staging  # atomic swap
                logger.info("graph.refreshed_in_background")
        except Exception as e:
            logger.warning("graph.background_refresh_failed", error=str(e))

    asyncio.create_task(_background_graph_refresh())
```

---

## Section 9: Training Data Quality Pyramid

The pyramid defines the order of investment — build from bottom up:

```
                        TRAINING DATA QUALITY PYRAMID

                               /\
                              /  \
                             / 1  \    Holdout eval (never trained, measures real accuracy)
                            /------\
                           /   2    \   Hard negatives + contrastive pairs
                          /----------\
                         /     3      \  Runtime wins (Tier 3 fallback -> auto-seed)
                        /--------------\
                       /       4        \  Graph enrichment (7 dimensions on every node)
                      /------------------\
                     /         5          \  Per-artifact trust scoring (not blanket)
                    /----------------------\
                   /           6            \  Schema convergence (one table, one path)
                  /--------------------------\
                 /             7              \  Capability-specific eligibility matrices
                /------------------------------\
               /               8                \  All 8 repos + all file types ingested
              /----------------------------------\

Level 8 = foundation (get all data in)
Level 1 = top (measure real quality without cheating)
```

**Build order: 8 → 7 → 6 → 5 → 4 → 3 → 2 → 1**

Each level depends on the one below it. Don't build level 2 (hard negatives) without level 6 (schema convergence) — you'd be generating negatives into a split storage system.

---

## Section 10: Top 5 Highest-Impact Actions

These 5 fixes have more impact than adding 100 more YAML files. The data exists — it's not wired correctly.

### Action 1: Schema Convergence (Milestone 1)

```
Why first:  Everything depends on one clean cosmos_embeddings table.
            Without this, 3 separate retrieval paths give inconsistent results.
What:       Standardize columns, migrate KnowledgeIndexer → vectorstore,
            TrainingService uses same contract.
Code:       DB migration + ~100 lines in training.py + indexer.py wrapper
Impact:     All downstream milestones unblocked
```

### Action 2: Graph Traversal Uses Enrichment Dimensions

```
Why second: The 7-dimension enrichment data EXISTS in quality.py but
            graphrag.py traverse() ignores it entirely (plain BFS).
What:       Replace BFS queue with weighted priority queue using
            edge_weight x node_confidence x freshness x (1 - negative_penalty)
Code:       ~50 lines in graphrag.py traverse()
Impact:     Better tool selection, "not sure" instead of hallucination,
            stale APIs deprioritized, dangerous tools flagged
```

### Action 3: Auto KB Ingest + Enrichment at Startup

```
Why third:  Enrichment is useless if never triggered.
            Currently requires manual POST /graphrag/ingest/kb + /enrich.
What:       main.py lifespan auto-runs ingest (if KB newer) + enrich
            (if nodes lack quality dimensions).
Code:       ~20 lines in main.py lifespan
Impact:     Every restart gets fresh enriched graph automatically
```

### Action 4: Fix KB-to-Enricher Schema Mismatches

```
Why fourth: quality.py enrichers expect field names that don't match
            current KB YAML structure. Dimensions populate empty/wrong.
What:       Adapter functions that normalize both old and new KB formats:
            eval_cases, guardrails, freshness, errors, evidence.
Code:       5 adapter functions in quality.py (~60 lines)
Impact:     All 7 dimensions actually populated correctly
```

### Action 5: Negative Signals in Tool Routing

```
Why fifth:  Biggest source of bad answers is wrong tool selection.
            "track my order" should NOT trigger cancel_order tool.
What:       ReAct _select_tools() checks graph node negative_signals
            before including a tool in candidates. If negative matches
            query, remove tool and add the recommended alternative.
Code:       ~30 lines in react.py _select_tools()
Impact:     Wrong tool selection reduced by ~40% (estimated from
            manual review of common misroutes)
```

---

## Revised Execution Milestones (with Actions integrated)

```
Milestone 1 — Schema Convergence + Action 1:
  TARGET: Standardize cosmos_embeddings (add trust_score, freshness, capability)
  TARGET: Migrate KnowledgeIndexer in-memory → cosmos_embeddings
  TARGET: TrainingService uses standardized columns
  DEPENDS ON: DB migration

Milestone 2 — Train/Dev/Holdout Split:
  TARGET: Merge all eval files, dedup, stratified split (70/15/15)
  TARGET: Intent classifier trains ONLY on train_set
  DEPENDS ON: Milestone 1

Milestone 3 — Expanded Ingestion (8 repos, all file types):
  TARGET: codebase_intelligence.py ingests all 8 repos
  TARGET: Parses module.yaml + evidence + submodules, chunks by doc type
  TARGET: Per-artifact trust scoring
  DEPENDS ON: Milestone 1

Milestone 3.5 — Graph Enrichment Wiring + Actions 2,3,4:
  TARGET: traverse() uses weighted priority queue with 7 dimensions
  TARGET: KB ingest + enrichment auto-runs at startup
  TARGET: KB-to-enricher schema adapters normalize both formats
  DEPENDS ON: Milestone 1

Milestone 4 — P0 KB Artifacts + generated_manifest.yaml:
  TARGET: Generate artifacts for tracking, shipments, orders, base_channel
  TARGET: codebase_intelligence.py ingests generated/ folder
  DEPENDS ON: Milestone 3

Milestone 5 — Pillar 1 Schema + Pillar 3 API Ingestion:
  TARGET: 442 tables + 1,600 APIs embedded with trust scores
  TARGET: Ranked retrieval (similarity x trust_score x freshness_decay x capability_fit)
  DEPENDS ON: Milestone 1

Milestone 5.5 — Negative Signals in Tool Routing + Action 5:
  TARGET: ReAct _select_tools() checks negative_signals from graph nodes
  TARGET: Wrong tool → recommended alternative
  DEPENDS ON: Milestone 3.5

Milestone 6 — Intent Classifier Upgrade:
  TARGET: Train on train_set + seeds + hard negatives
  TARGET: Report accuracy ONLY on holdout_set
  DEPENDS ON: Milestone 2

Milestone 7 — Graph Weight Expansion:
  TARGET: Schema edges, async_flow edges, field_lineage edges
  TARGET: Eligibility from Matrix 3 only
  DEPENDS ON: Milestones 4, 5, 3.5

Milestone 8 — Runtime Learning Loop:
  TARGET: Tier 3 wins → auto training seeds
  TARGET: Human resolutions → auto symptom_root_cause
  DEPENDS ON: Milestones 4, 6

Milestone 9 — MARS Formatter Upgrade:
  TARGET: formatter.go --format=cosmos
  DEPENDS ON: Milestone 1
```

---

## Section 11: Embedding Strategy (IMPLEMENTED)

### Before (Mixed, Broken)

```
indexer.py:      In-memory TF-IDF (separate retrieval path)
vectorstore.py:  all-MiniLM-L6-v2 (384-dim) or hash fallback
training.py:     Own TF-IDF embeddings into same table
Result:          3 embedding methods, incompatible quality, no versioning
```

### After (Converged, Production)

```
vectorstore.py:
  Production (ENV=production):
    → Shiprocket AI Gateway → text-embedding-3-small (1536-dim)
    → provider=openai via aigateway.shiprocket.in
    → API key: configured (internal Shiprocket API, budgeted)
    → NO silent fallback — fail loud if gateway unavailable
    → Dimension validation: reject if response != 1536 dims

  Dev-only (ENV=development, no gateway key):
    → Local all-MiniLM-L6-v2 (384-dim)
    → WARNING: different dimension than production — dev results not comparable

  Test-only (ENV=test):
    → Deterministic hash (384-dim)
    → For unit tests only, no semantic meaning

One table = one active embedding dimension.
No mixed 384 + 1536 vectors in the same active table.

TF-IDF paths REMOVED from primary retrieval:
  - indexer.py → deprecated, wraps vectorstore.search_similar()
  - training.py → calls vectorstore.embed_text(), not own TF-IDF
```

### Schema (Converged)

```sql
cosmos_embeddings:
  id UUID PRIMARY KEY
  repo_id VARCHAR(255) NOT NULL DEFAULT ''
  entity_type VARCHAR(255) NOT NULL
  entity_id VARCHAR(500) NOT NULL
  capability VARCHAR(50) NOT NULL DEFAULT 'retrieval'
  content TEXT NOT NULL
  content_hash VARCHAR(32) NOT NULL          ← NEW: change detection
  embedding vector(1536)                     ← 1536 for production
  trust_score FLOAT NOT NULL DEFAULT 0.5
  freshness TIMESTAMPTZ
  embedding_model VARCHAR(100) NOT NULL      ← tracks which model embedded
  embedding_version VARCHAR(50) NOT NULL     ← for migration/rollback
  embedded_at TIMESTAMPTZ NOT NULL           ← for audit
  metadata JSONB
  created_at TIMESTAMPTZ

  UNIQUE INDEX ON (repo_id, entity_type, entity_id)  ← canonical upsert key
```

### Insert Behavior

```
On store_embedding():
  1. Compute content_hash = SHA-256(content)[:32]
  2. Check if (repo_id, entity_type, entity_id) exists
  3. If exists AND content_hash unchanged → SKIP re-embed (update trust/meta only)
  4. If exists AND content_hash changed → re-embed + UPDATE
  5. If new → embed + INSERT
  6. ON CONFLICT DO UPDATE (no duplicate rows ever)
```

### Search Ranking

```sql
relevance = similarity × trust_score × freshness_weight

freshness_weight:
  embedded in last 7 days  → 1.0
  embedded in last 30 days → 0.9
  embedded in last 90 days → 0.8
  older                    → 0.7

ORDER BY relevance DESC
```

### Model Selection Policy

```
Default production:    text-embedding-3-small (1536-dim)
Upgrade candidate:     text-embedding-3-large (3072-dim) — benchmark only
Upgrade condition:     ONLY if holdout retrieval@5 improves >5%
Upgrade process:       parallel table → A/B → swap if better → drop old
Never:                 switch models without full re-embed + benchmark
Never:                 mix dimensions in same active table
```

### Beyond Embeddings: Retrieval Quality Upgrades

**Important rule: exact/entity retrieval outranks all expansion strategies.**
For this domain (AWB codes, order IDs, table names), exact match matters more than the generic RAG playbook.

**All improvement numbers below are expected ranges to validate on holdout, not promised gains.**

```
ALREADY IN USE:
  OpenAI text-embedding-3-small (1536-dim, via AI Gateway)
  pgvector cosine similarity search
  Trust-weighted ranking (similarity x trust_score x freshness)

PARTIALLY SCAFFOLDED (code exists, not fully wired):
  Hybrid lexical + vector in graph/retrieval.py (needs completion)
  RRF-style fusion logic in graph/retrieval.py (needs wiring to search)

NOT YET IMPLEMENTED (future upgrades):
  Cross-encoder reranking
  Contextual chunking at ingest time
  Conditional query expansion (HyDE / RAG Fusion)
```

### Practical Priority Order

```
Priority 1: Hybrid Lexical + Vector Retrieval
  ─────────────────────────────────────────────
  Status:     Partially scaffolded
  Why first:  Highest ROI for your domain. AWB numbers, order IDs, table names
              are exact-match heavy. Pure vector search might rank "AWB789456"
              lower than semantically similar but wrong "AWB tracking concept".
  How:        PostgreSQL full-text search (ts_vector + ts_rank) combined with
              pgvector cosine similarity. NOT pg_trgm (that's trigram similarity,
              not BM25/lexical ranking).
              Final score = alpha * vector_similarity + (1 - alpha) * lexical_rank
              where alpha = 0.7 (tunable on holdout)
  Scope:      All entity_types, especially schema, api_tool, eval_seed
  Expected:   Validate on holdout — expect improvement for exact-ID queries
  Latency:    +5-10ms (one additional SQL query)

Priority 2: Cross-Encoder Reranker
  ─────────────────────────────────
  Status:     Not implemented
  Why second: Once recall is decent (hybrid search), ordering in top-5 is
              the next bottleneck. Cross-encoder scores (query, doc) pairs
              jointly — much more accurate than bi-encoder similarity.
  How:        After vector+lexical returns top-20 candidates, rerank with:
              Option A: Cohere rerank API (if added to AI Gateway)
              Option B: Local ms-marco-MiniLM-L6-v2 (~100ms per batch)
  Scope:      Top-20 or top-30 only. NEVER large candidate sets (latency).
  Expected:   Validate on holdout — expect improvement for ambiguous queries
  Latency:    +50-150ms (acceptable as post-retrieval step)

Priority 3: Contextual Chunking at Ingest Time
  ─────────────────────────────────────────────
  Status:     Not implemented
  Why third:  Cheap long-term quality win. A chunk like "status can be
              pending, processing, shipped" without context doesn't say
              WHICH table or module. Prepending module-level summary to
              each chunk makes it self-contained.
  How:        Before embedding, prepend: "[Module: orders | File: database.md]
              {first 100 chars of module summary}. " to each chunk.
  Scope:      .claude/ module docs and generated artifacts ONLY.
              NOT needed for already-structured YAML (Pillar 1/3/4)
              where context is explicit in the YAML structure.
  Expected:   Validate on holdout — expect improvement for cross-module queries
  Latency:    Zero at query time (done at ingest)

Priority 4: Conditional Query Expansion (HyDE or RAG Fusion)
  ──────────────────────────────────────────────────────────
  Status:     Not implemented
  Why fourth: Helps for complex multi-intent queries, but adds cost + latency.
              Should NOT run on every query.
  How:        Gate by query type:
              - Exact-entity query (has order ID/AWB) → SKIP expansion entirely
              - Low-confidence semantic query → allow ONE expansion (HyDE)
              - Complex multi-intent query → allow multi-query fusion (RAG Fusion)
  Scope:      Only low-confidence or multi-intent queries from request_classifier.
              NEVER on QUICK complexity queries.
  Expected:   Validate on holdout — expect improvement for multi-part questions
  Latency:    +200-500ms per LLM call for expansion (significant — gate carefully)

Priority 5: ColBERT / Late Interaction
  ─────────────────────────────────────
  Status:     Not implemented, not planned
  Why last:   Only if everything above is working and measured.
              Token-level matching is better for multi-concept queries but
              requires specialized infrastructure (ColBERT indexing).
  Expected:   Evaluate only after Priorities 1-4 are measured on holdout
```

### Embedding Model Upgrade Path

```
CURRENT:  text-embedding-3-small via Shiprocket AI Gateway (1536-dim)

CANDIDATE: Voyage AI (voyage-3-large, 1024-dim)
  Why:      Trained specifically for retrieval (not general-purpose)
            Has input_type="query" vs "document" distinction
            MTEB retrieval: voyage-3-large (68.3) > 3-small (62.3)
            Plus voyage-code-3 for code/technical docs
  Status:   NOT on AI Gateway yet. Request from API Platform Team.
  Plan:     After data is fed + baseline measured on 3-small:
            1. Request Voyage provider on AI Gateway
            2. Parallel table with voyage-3-large (1024-dim)
            3. A/B on holdout
            4. Swap if measurably better (expect ~10% retrieval improvement)
  Rule:     Never switch without full re-embed + holdout benchmark
```

---

## Section 12: Query Routing Feedback Loop

After every successful query, log which entity_types were most useful. This tells you where to invest more training data.

```
Per-query logging (store in cosmos_query_analytics):
  {
    query_hash: "abc123",
    resolved_entity_types: ["schema", "module_rules"],
    resolved_capabilities: ["retrieval", "graph_edge"],
    top_k_entity_types: [
      {entity_type: "schema", similarity: 0.87, contributed: true},
      {entity_type: "module_rules", similarity: 0.72, contributed: true},
      {entity_type: "module_prd", similarity: 0.45, contributed: false},
    ],
    resolution_tier: 1,
    fallbacks_used: [],
    success: true,
    user_feedback: null,
    timestamp: "2026-03-29T..."
  }

After 1000 queries, aggregate:
  ┌──────────────────┬──────────┬──────────┬──────────────────────────┐
  │ entity_type      │ Hit Rate │ Avg Sim  │ Verdict                  │
  ├──────────────────┼──────────┼──────────┼──────────────────────────┤
  │ schema           │ 40%      │ 0.81     │ HIGH VALUE — invest more │
  │ symptom_fix      │ 15%      │ 0.76     │ HIGH VALUE — expand      │
  │ api_tool         │ 25%      │ 0.74     │ HIGH VALUE — maintain    │
  │ module_rules     │ 12%      │ 0.68     │ MEDIUM — keep as-is      │
  │ module_debug     │ 8%       │ 0.65     │ MEDIUM — useful for T2   │
  │ knowledge        │ 35%      │ 0.62     │ CORE — always needed     │
  │ module_prd       │ 0.5%     │ 0.41     │ LOW — consider removing  │
  │ module_ssd       │ 0.3%     │ 0.38     │ LOW — consider removing  │
  └──────────────────┴──────────┴──────────┴──────────────────────────┘

Actions:
  - entity_types with <1% hit rate AND avg_sim < 0.5 → candidate for removal
  - entity_types with >20% hit rate → invest in more documents of this type
  - New entity_types with 0% hit rate → something is wrong with ingestion
```

Implementation: add to query_orchestrator.py after Tier 1 probe, log to `cosmos_query_analytics` table.

---

## Section 13: Cold Start Plan (Day 1 with Empty Tables)

Pipelines 1, 2, 3 depend on `icrm_knowledge_entries`, `icrm_distillation_records`, and `icrm_tool_executions`. On day 1, these tables are empty. Without bootstrapping, COSMOS has zero retrieval data and the intent classifier has zero training examples.

**Important:** Do NOT synthesize runtime truth. `icrm_tool_executions` must represent real observed behavior, not invented history. Only seed retrieval docs and intent training data.

```
Bootstrap strategy:

Step 1: Seed retrieval docs from GROUNDED KB artifacts (not query→tool pairs)
  Source: Generated artifacts (domain_overview, symptom_root_cause, field_lineage, etc.)
          + Pillar 1 schema table docs + Pillar 3 API overview docs
  Target: cosmos_embeddings (via canonical ingestor, same as all other sources)
  What:   These are real grounded documents, not synthetic query→answer pairs.
          They describe what tables exist, what APIs do, what symptoms mean.
  Trust:  0.75 (from KB, not from runtime)
  Expected: ~500-1,000 grounded document embeddings

  DO NOT seed icrm_knowledge_entries with {query → expected_tool} pairs.
  That turns the knowledge table into a routing label store, not a grounded
  knowledge corpus. The intent classifier should handle routing — not KB retrieval.

Step 2: Seed intent training from seeds
  Source: Pillar 4 training_seeds.jsonl (100+ seeds)
  Target: icrm_distillation_records
  Transform: each {query, intent, page_id} → distillation record
    INSERT INTO icrm_distillation_records
    (user_query, intent, final_response, confidence, source, bootstrap_version)
    VALUES (:query, :intent, :page_id, 0.8, 'bootstrap', 'v1')
  Expected: ~100 synthetic records

Step 3: Seed graph structure from KB (NOT runtime execution data)
  Source: Pillar 1 table relationships + Pillar 3 tool definitions + Pillar 4 page chains
  Target: GraphRAG nodes + edges (static structure, not tool success weights)
  What this seeds: which tables exist, which APIs call which tables,
                   which pages use which APIs — structural knowledge
  What this does NOT seed: tool success rates, latency, feedback scores
                           (those must come from real runtime only)

Step 4: DO NOT seed icrm_tool_executions
  This table must represent REAL observed tool behavior.
  Graph weight Pipeline 3 will start with equal weights (0.5 for all tools).
  As real queries come in, weights will differentiate naturally.
  Synthetic success data would give false confidence in untested tools.

Bootstrap provenance on all seeded records:
  source = 'bootstrap'
  trust_score = 0.7 (lower than real runtime data at 0.85+)
  bootstrap_version = 'v1' (allows bulk cleanup when upgrading)

As real runtime data accumulates:
  After 100 real queries  → bootstrap = ~50% of training input
  After 1000 real queries → bootstrap = ~10% of training input
  After 5000 real queries → bootstrap = ~2%, can be safely purged
```

---

## Section 14: Training Health Monitoring Dashboard

Track 5 metrics continuously. Alert when quality degrades.

```
METRIC 1: Embedding Coverage
  What: % of KB files (by pillar) that have embeddings in cosmos_embeddings
  Target: >= 95% for Tier A/B sources
  Query: SELECT entity_type, COUNT(*) FROM cosmos_embeddings GROUP BY entity_type
  Alert: coverage drops below 90% → ingestion pipeline failed

METRIC 2: Intent Classifier Accuracy
  What: Accuracy measured on holdout_set.jsonl (never trained)
  Target: >= 90%
  Frequency: After every retraining run
  Alert: accuracy drops below 85% → data quality issue or distribution shift

METRIC 3: Graph Enrichment Coverage
  What: % of graph nodes with all 7 quality dimensions populated
  Target: >= 80% of nodes have confidence, freshness, and at least 1 eval_case
  Query: Count nodes where properties.quality.confidence IS NOT NULL
  Alert: coverage below 60% → enricher schema mismatch or KB format change

METRIC 4: Tier 1 Resolution Rate
  What: % of queries resolved at Tier 1 without fallback to Tier 2/3
  Target: >= 80%
  Source: query_orchestrator logs (resolution_tier == 1)
  Alert: rate below 70% → KB has major gaps, check which entity_types are missing

METRIC 5: Trust Score Distribution
  What: How many documents at each trust tier
  Target: Tier A+B should be >= 60% of total embeddings
  Query: SELECT
    CASE WHEN trust_score >= 0.85 THEN 'A'
         WHEN trust_score >= 0.7 THEN 'B'
         WHEN trust_score >= 0.5 THEN 'C'
         ELSE 'D' END as tier,
    COUNT(*)
  FROM cosmos_embeddings GROUP BY tier
  Alert: Tier D exceeds 40% → too much draft/low-quality data being ingested

METRIC 6: Retrieval Quality (Hit@5 / MRR on Holdout)
  What: For each holdout query, is the correct answer in top-5 retrieved docs?
  Target: Hit@5 >= 80%, MRR >= 0.65
  How: Run holdout_set queries through vectorstore.search_similar(),
       check if expected ground_truth entity_id is in top-5 results
  Alert: Hit@5 drops below 70% → embedding quality degraded or KB gap
  Why: This proves retrieval is actually working, not just that docs exist

METRIC 7: Graph Enrichment Impact (ranking change rate)
  What: % of queries where graph enrichment dimensions changed traversal ranking
        vs plain BFS (would the path be different without enrichment?)
  Target: > 30% of graph queries have different top-3 paths with enrichment
  How: Run traverse_enriched() and plain traverse() on same queries,
       compare top-3 paths. If different → enrichment is paying off.
  Alert: < 10% impact → enrichment data is too sparse or not differentiated
  Why: Proves the 7-dimension investment is actually changing answers

BONUS METRIC: Tier 3 Fallback Rate
  What: % of queries that needed DB fallback
  Target: < 20%
  Alert: > 30% → KB is missing knowledge that users are asking about
  Action: review Tier 3 query patterns, generate training seeds from them
```

### Per-Capability Dashboards

Track retrieval quality separately for each query domain. Aggregate metrics hide domain-specific weaknesses.

```
Dashboard 1: Schema Queries
  Queries: "which table stores X", "what columns in Y"
  Filter: entity_type IN ('schema')
  Metrics: hit@5, MRR, Tier 1 resolution rate
  Target: hit@5 >= 85%

Dashboard 2: Tool/API Queries
  Queries: "how to create shipment", "what API cancels order"
  Filter: entity_type IN ('api_tool', 'tool_playbook')
  Metrics: hit@5, MRR, correct tool selection rate
  Target: hit@5 >= 88%

Dashboard 3: Tracking/Status-Stuck Queries
  Queries: "why not updating", "status stuck at manifested"
  Filter: entity_type IN ('async_flow', 'symptom_fix', 'field_lineage')
  Metrics: hit@5, Tier 1 resolution rate, Tier 3 fallback rate
  Target: Tier 1 resolution >= 80%

Dashboard 4: Page/Role Queries
  Queries: "which page shows AWB", "can seller approve refund"
  Filter: entity_type IN ('page', 'page_intent', 'cross_repo')
  Metrics: page search recall, role check accuracy
  Target: page recall >= 95%

Dashboard 5: Cross-Repo Queries
  Queries: "admin equivalent of seller orders page"
  Filter: entity_type IN ('cross_repo', 'module_submodule')
  Metrics: cross-repo mapping accuracy
  Target: >= 90%
```

---

## Section 15: Tournament A/B — Human Preference Learning (IMPLEMENTED)

### What It Is

Blind A/B comparison where ICRM users see two answers and pick the better one.
Same method as ChatGPT's RLHF — human preferences improve the system over time.

### Design Rules

```
1. BLIND COMPARISON
   User sees "Answer 1" and "Answer 2" — no model names, no confidence, no lane labels.
   Order randomized every time to prevent position bias.

2. ONE VARIABLE AT A TIME
   Same corpus, same prompt, same LLM, same chunking, same reranker.
   ONLY the retrieval embedding lane differs (OpenAI vs Voyage).
   Later: separate tournaments for reranker, prompt style, LLM model.

3. STRUCTURED FEEDBACK (not just free text)
   Reason tags:
     - more_accurate
     - more_complete
     - more_actionable
     - easier_to_understand
     - better_evidence
     - safer
     - faster_to_use
   Free text is optional. Structured tags are much better for training.

4. ICRM-ONLY SCOPE
   Sellers NEVER see dual responses.
   Triggered for ICRM users on:
     - Low confidence (composite_score < 0.65)
     - New/unknown query classes
     - QA/admin roles (always)
     - Sampled internal benchmark traffic (10%)
   Lime frontend toggle: [Auto] [On] [Off]

5. ADOPTED ANSWER → CASE HISTORY
   User picks one → only that answer goes into thread/case history.
   The other answer is stored for training but NOT in the conversation.

6. NO AUTO-FINETUNE AT 500
   500+ preferences = ready for CURATION, not auto-training.
   Use data for (in order):
     a. Preference analytics (which lane wins per domain?)
     b. Per-domain routing decisions
     c. Reranker / judge model training
     d. Eval set creation (high-agreement pairs become ground truth)
     e. ONLY THEN: answer-model fine-tuning (after dedup + safety review + offline eval)

7. SIGNIFICANCE GATES FOR ROUTING
   Don't switch routing because one lane wins 58% once.
   Require:
     - Minimum 100 samples per domain
     - Stable win rate over 2+ weeks
     - 95% confidence interval excludes 50%
     - No safety regression
     - No latency/cost blowup (>20% increase blocks switch)
```

### Tables

```
cosmos_ab_preferences:
  pair_id, query, user_id,
  response_a (blind), response_b (blind),
  retrieval_model_a, retrieval_model_b,
  context_a, context_b,
  preference (answer_1 | answer_2 | both_good | both_bad),
  reason_tags (structured), reason_text (optional),
  time_to_decide_ms, created_at
```

### API Endpoints

```
POST /cosmos/api/v1/tournament/generate    — Generate blind A/B pair
POST /cosmos/api/v1/tournament/preference  — Record user choice + reason tags
GET  /cosmos/api/v1/tournament/stats       — Win rates, sample sizes, readiness
GET  /cosmos/api/v1/tournament/training    — Export preference data for curation
```

### Quality Gates (IMPLEMENTED)

```
1. INTER-ANNOTATOR AGREEMENT
   Same pair shown to 2-3 ICRM users before treating as ground truth.
   3/3 agree → weight 1.0 (high-quality signal)
   2/3 agree → weight 0.7 (moderate signal)
   1/3 agree → discard (no consensus, noisy)
   API: GET /tournament/needs-annotation → returns pairs needing more raters

2. TEMPORAL DECAY
   Older preferences carry less weight:
   Last 7 days  → weight 1.0
   Last 30 days → weight 0.8
   Last 90 days → weight 0.5
   Older        → weight 0.2
   Prevents stale preferences from blocking model upgrades.

3. NEGATIVE SIGNAL MINING
   "Both bad" votes stored in cosmos_failure_cases table.
   Per-domain failure rates tracked.
   Domains with >20% "both_bad" → flagged, needs more training data.
   API: GET /tournament/failures → failure report by domain
```

### Training Value Extraction (Priority Order)

```
After 100 preferences:
  → Preference analytics: which lane wins overall?
  → Failure report: which domains have high "both_bad" rate?

After 250 preferences:
  → Per-domain breakdown: which lane wins for orders? NDR? billing?
  → Inter-annotator pairs reaching quorum (3 raters)

After 500 preferences (with agreement >= 0.7):
  → Ready for curation: dedup, quality filter, safety review
  → Create eval sets from high-agreement pairs
  → Calibrate confidence: do high-confidence answers get preferred?
  → Training export filters: agreement >= 0.7, temporal weight > 0.2

After 1000 preferences:
  → Routing decisions: statistically significant per-domain lane choice
  → Reranker training: use preference pairs as ranking signal
  → Consider answer-model fine-tuning (after full offline eval)
  → "Both bad" domains should have <10% failure rate (if not → fix KB first)
```

### Embedding Model Benchmark Path

```
Phase 1 (now):
  Primary: OpenAI text-embedding-3-small (1536-dim) via AI Gateway
  Shadow:  Voyage voyage-3-large (1024-dim) via direct API
  Mode:    blind A/B for ICRM, primary-only for sellers

Phase 2 (after 500+ preferences with significance):
  IF Voyage wins per-domain with significance:
    → Switch that domain to Voyage
    → Keep OpenAI for domains where it wins
    → Per-domain routing, not global switch

Phase 3 (after 1000+ preferences):
  → Full A/B report with confidence intervals
  → Decision: single provider or hybrid per-domain
  → If Voyage wins overall: request on AI Gateway for cost savings

Voyage candidate upgrade: voyage-code-3 for .claude/ module doc retrieval
  → Separate tournament ONLY for code/technical queries
  → Do not mix with general KB tournament
```

---

## Deprioritized Items (Not in Current Plan)

```
These are valid but not needed for initial training quality:

- Pillar 2 (GitHub/Jira extraction)
    Nice for team routing, but not needed for seller/ICRM chat quality.
    Revisit after Tier 1 resolution rate exceeds 80%.

- Pillar 5 (Sprint/Team routing)
    Useful for ICRM requirement routing, but not for order/shipment/NDR answers.
    Revisit after P0+P1 modules are fully ingested.

- MARS formatter upgrade (formatter.go --format=cosmos)
    Can wait until COSMOS ingestion pipeline is stable and validated.
    Current routing-format JSONL still works for basic training.

- MultiChannel_Web draft modules (20 modules, all draft/score=0)
    Near zero training value until someone enriches them.
    Ingest as Tier D retrieval-only, don't invest in artifact generation.
```

---

## What NOT to Do (Updated)

```
- Don't train intent classifier on holdout_set.jsonl (data leakage)
- Don't use repo-wide trust blankets (gate per-artifact using status+score+evidence+freshness)
- Don't treat debugging.md as graph weight input (retrieval + seed generation only)
- Don't mark milestones as done until code is verified and tested
- Don't ingest generated/ artifacts without generated_manifest.yaml (staleness tracking)
- Don't let draft module docs train the intent classifier (Tier D = retrieval-only)
- Don't let MARS formatter produce canonical schema until COSMOS can consume it
- Don't report accuracy numbers measured on training data (holdout only)
- Don't mix seller/admin eval examples without stratification
- Don't assume KnowledgeIndexer and VectorStoreService are the same path (they aren't today)
- Don't use plain BFS for graph traversal when enrichment dimensions exist
- Don't leave KB ingest + enrichment as manual-only endpoints
- Don't ignore negative_signals in tool routing (biggest bad-answer source)
- Don't enrich graph nodes without normalizing KB field names first (schema mismatch)
- Don't maintain two separate negative-routing systems: graph negative_signals
  in Section 8 REPLACES any older negative-routing logic in router.py.
  One shared policy, one code path. If both exist, they will drift apart.
```

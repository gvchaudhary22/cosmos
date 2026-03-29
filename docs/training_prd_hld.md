# COSMOS Training Pipelines — PRD + HLD

## Overview

6 training pipelines ingest 92,595 YAML files + runtime data + codebase docs into a unified vector store, intent model, and graph weight system that powers the three-tier query architecture.

```
Knowledge Base (92,595 YAML)          Runtime Data (PostgreSQL)        Codebase Docs (.claude/)
├── Pillar 1: Schema (8,158)          ├── icrm_knowledge_entries       ├── MultiChannel_API/
├── Pillar 3: API/Tools (84,271)      ├── icrm_distillation_records    ├── SR_Web/
├── Pillar 4: Page/Role (166)         ├── icrm_tool_executions         └── shiprocket-go/
│                                      │
▼                                      ▼                                ▼
Pipeline 1: Embedding                  Pipeline 2: Intent Classifier    Pipeline 6: Module Doc
Pipeline 4: Page-Role Embedding        Pipeline 3: Graph Weights
Pipeline 5: Cross-Repo Navigation
```

---

## Pipeline 1: Embedding Training

### PRD

**Problem:** COSMOS needs to find relevant knowledge when a user asks a question. Without embeddings, it has no way to match "where is my order" to the correct KB entries about order tracking.

**Goal:** Convert all knowledge base entries and historical distillation records into 384-dimensional vector embeddings stored in pgvector, enabling sub-100ms semantic similarity search.

**Users:** All chat users (sellers + ICRM agents). Every Tier 1 query hits this.

**Success Metric:**
- top-5 retrieval recall >= 80% (correct KB entry in top 5 results)
- Search latency < 100ms (p95)
- Embedding coverage: 100% of knowledge entries + last 5000 distillation records

### HLD

```
Input Sources:
  ├── icrm_knowledge_entries (enabled=true)
  │   Fields: id, question, answer, category
  │   Content: question + " " + answer
  │
  └── icrm_distillation_records (confidence >= 0.6, limit 5000)
      Fields: id, user_query, intent, final_response
      Content: user_query + " " + final_response

Processing:
  1. Load all eligible records from PostgreSQL
  2. Tokenize content (lowercase, alphanumeric, >2 chars)
  3. Build vocabulary + compute IDF weights
  4. Generate 384-dim TF-IDF embedding per document
  5. L2-normalize each vector

Storage:
  Table: cosmos_embeddings
  ├── entity_type = 'knowledge' (from KB entries)
  ├── entity_type = 'distillation' (from distillation records)
  ├── embedding = vector(384)
  ├── content = original text
  └── metadata = {category, intent, source_id}

Query-time Usage:
  vectorstore.search_similar(
    query="where is my order",
    entity_type=None,  # search across all types
    limit=5,
    threshold=0.3
  )
  → Returns top-5 most similar KB/distillation entries

Trigger:
  POST /training/embeddings
  { "repo_id": "SR_Web" }  // optional filter

Schedule: Weekly (or after bulk KB import)
Duration: ~2-5 min for 5000 records
```

---

## Pipeline 2: Intent Classifier Training

### PRD

**Problem:** Every incoming query must be classified by intent (lookup, explain, act, report, navigate) and entity (order, shipment, ndr, billing, etc.) before any pipeline runs. Rule-based classification covers 70% of cases. The remaining 30% need a trained model.

**Goal:** Train a centroid-based nearest-neighbor intent classifier using historical distillation records, achieving >= 85% accuracy on known intents.

**Users:** Every chat query passes through the intent classifier. It's the first thing that runs.

**Success Metric:**
- Overall accuracy >= 85% (cross-validated)
- Per-intent accuracy >= 70% (no intent below this)
- Classification latency < 5ms (in-memory, no DB hit)

### HLD

```
Input Sources:
  └── icrm_distillation_records
      Filter: intent IS NOT NULL, intent != 'unknown', confidence >= 0.5
      Limit: 10,000 most recent
      Fields: user_query, intent, confidence

Processing:
  1. Tokenize all queries (lowercase, >2 chars)
  2. Group by intent (require >= 3 samples per intent)
  3. Build vocabulary (cap at 5,000 words for storage)
  4. Compute IDF weights across full corpus
  5. For each intent: compute centroid vector (average of all query vectors)
  6. L2-normalize centroids
  7. Leave-one-out cross-validation:
     - For each sample: remove it, recompute centroid, classify it
     - Track per-intent accuracy

Storage:
  Table: cosmos_intent_models
  ├── version = timestamp
  ├── model_data = JSON {
  │     vocabulary: {word: index},
  │     idf_weights: {word: float},
  │     centroids: {intent: sparse_vector},
  │     config: {max_vocab: 5000, min_samples: 3}
  │   }
  └── metrics = {overall_accuracy, per_intent_accuracy, vocab_size}

Query-time Usage:
  1. Tokenize incoming query
  2. Compute TF-IDF vector using stored vocabulary + IDF
  3. Cosine similarity against each centroid
  4. Return highest-similarity intent + confidence

  Fallback: if confidence < 0.5 → classify_with_ai() (Claude Haiku)

Trigger:
  POST /training/intent-classifier
  { "repo_id": "SR_Web" }

Schedule: Weekly (or when distillation records grow by 1000+)
Duration: ~30-60 sec for 10,000 records
```

---

## Pipeline 3: Graph Weight Optimization

### PRD

**Problem:** The GraphRAG traversal follows edges between nodes (order → API → webhook → courier). Not all edges are equally useful. Tools that succeed more often and get better feedback should have higher edge weights, so the graph prioritizes reliable paths.

**Goal:** Compute per-tool execution weights based on success rate, user feedback, confidence, and latency. These weights influence graph traversal ordering.

**Users:** Every Tier 1 deep stage (GraphRAG traverse) uses these weights. Affects which paths are explored first.

**Success Metric:**
- Weighted tools with >100 executions
- Top-10 tools have weight > 0.6
- Graph traversal relevance improves by 10% (measured via sandbox eval)

### HLD

```
Input Sources:
  ├── icrm_tool_executions (limit 50,000)
  │   Fields: tool_name, status (success/failed), duration_ms, error_message
  │
  └── icrm_distillation_records (limit 20,000)
      Fields: tools_used, confidence, feedback_score, cost_usd, tokens

Processing:
  1. Aggregate per-tool from execution records:
     - success_count, failed_count → success_rate
     - total_latency_ms → avg_latency
  2. Aggregate per-tool from distillation records:
     - total_uses, total_confidence, total_feedback (normalized 1-5)
     - total_cost_usd
  3. Compute composite weight per tool:
     weight = (0.4 × success_rate)
            + (0.3 × avg_feedback / 5.0)
            + (0.2 × avg_confidence)
            - (0.1 × latency_penalty)
     latency_penalty = min(1.0, avg_latency / 5000)
  4. Normalize to [0, 1]

Storage:
  Table: cosmos_graph_weights
  ├── version = timestamp
  ├── weights = JSON {tool_name: {weight, success_rate, avg_feedback, ...}}
  └── metrics = {tools_weighted, total_executions, top_10_tools}

Query-time Usage:
  GraphRAG traverse: when choosing which edge to follow,
  multiply edge.weight × tool_weight[edge.tool_name]
  → Higher-weighted tools explored first

Trigger:
  POST /training/graph-weights
  { "repo_id": "SR_Web" }

Schedule: Daily (tool performance changes frequently)
Duration: ~15-30 sec for 50,000 records
```

---

## Pipeline 4: Page-Role Embedding Training

### PRD

**Problem:** When a seller asks "where can I see my AWB code" or an ICRM agent asks "can ops_lead approve refunds", COSMOS needs to search across page metadata, field definitions, and role permissions. General embeddings (Pipeline 1) don't capture page-specific structure.

**Goal:** Generate specialized embeddings for Pillar 4 page intelligence: page metadata, fields, actions, role permissions, and eval cases. Enable "which page shows X" and "can role Y do Z" queries.

**Users:** Tier 1 page-role probe pipeline. Also ICRM agents checking permissions.

**Success Metric:**
- All 20 pages (10 SR_Web + 10 MultiChannel_Web) embedded
- Page search recall >= 90% on eval cases
- Role permission queries answered correctly >= 95%

### HLD

```
Input Sources:
  └── Pillar 4 Knowledge Base (166 YAML files)
      ├── SR_Web/pillar_4_page_role_intelligence/
      │   ├── pages/ (10 pages × 8 YAMLs each = 80 files)
      │   ├── catalog.yaml
      │   ├── role_matrix.yaml
      │   └── cross_repo_mapping.yaml + training_seeds.jsonl
      └── MultiChannel_Web/pillar_4_page_role_intelligence/
          └── (same structure, 83 files)

      Per page loaded:
        page_meta.yaml → route, component, module, domain, page_type, roles
        fields.yaml → field names, types, sources
        actions.yaml → action names, descriptions
        role_permissions.yaml → per-role CRUD permissions
        eval_cases.yaml → query/expected_intent pairs

Processing:
  1. Load all PageDocuments via PageIntelligenceService
  2. Build training document per page:
     text = f"Page {page_id} at route {route}, component {component}, "
            f"module {module}, domain {domain}, type {page_type}. "
            f"Fields: {field_labels}. Actions: {action_labels}. "
            f"Roles: {roles}."
  3. Generate 384-dim TF-IDF embeddings
  4. Store page embeddings as entity_type='page'
  5. Extract eval_cases → parse query + expected_intent
  6. Store intent entries as entity_type='page_intent'

Storage:
  Table: cosmos_embeddings
  ├── entity_type = 'page'
  │   metadata = {repo, domain, page_type, route, component}
  │   content = page summary text
  │
  └── entity_type = 'page_intent'
      metadata = {intent, page_id, domain}
      content = eval case query text

Query-time Usage:
  # "Which page shows AWB codes?"
  search_similar(query="AWB code", entity_type="page", limit=3)
  → Returns: seller.shipments.tracking (similarity=0.87)

  # Role check
  page_intelligence.get_role_permissions(role="seller", page_id="seller.orders.detail")
  → {has_access: true, permissions: {read: true, update: false}}

Trigger:
  POST /training/page-role
  { "repo_id": "SR_Web" }

Schedule: On KB update (Pillar 4 files change)
Duration: ~10-20 sec for 20 pages
```

---

## Pipeline 5: Cross-Repo Navigation Training

### PRD

**Problem:** Shiprocket has two panels: seller (SR_Web) and admin (MultiChannel_Web/ICRM). When an ICRM agent investigates a seller's issue, they need to know: "the seller sees order status on seller.orders.detail, which maps to icrm.orders.search on the admin side, which has additional fields like internal_sync_status."

**Goal:** Generate embeddings that link equivalent pages across repos, including field-level differences (what admin sees that seller doesn't).

**Users:** ICRM agents who investigate cross-system issues. Also the cross-repo probe in Tier 1.

**Success Metric:**
- All 8 cross-repo mappings (seller ↔ admin pages) embedded
- Cross-repo lookup accuracy >= 95%
- Field diff correctly identifies admin-only fields

### HLD

```
Input Sources:
  └── cross_repo_mapping.yaml (in each repo's pillar_4 folder)
      Per mapping:
        source_repo, source_page_id
        target_repo, target_page_id
        shared_api (common API endpoints)
        shared_db (common DB tables/columns)
        additional_admin_fields (fields only admin sees)
        additional_admin_actions (actions only admin can perform)
        notes

Processing:
  1. Load cross_repo_mappings from both repos
  2. Build training document per mapping:
     text = f"Page {source_page_id} in {source_repo} maps to "
            f"{target_page_id} in {target_repo}. "
            f"Admin has additional fields: {additional_fields}. "
            f"Admin has additional actions: {additional_actions}. "
            f"{notes}"
  3. Generate 384-dim TF-IDF embeddings
  4. Store as entity_type='cross_repo'

Storage:
  Table: cosmos_embeddings
  ├── entity_type = 'cross_repo'
  │   metadata = {
  │     source_repo, source_page_id,
  │     target_repo, target_page_id,
  │     type: 'cross_repo_mapping'
  │   }
  └── content = mapping description text

Query-time Usage:
  # "What's the admin equivalent of seller.orders?"
  search_similar(query="seller orders admin", entity_type="cross_repo")
  → Returns: seller.orders.detail ↔ icrm.orders.search

  # Cross-repo probe in orchestrator
  page_intelligence.get_cross_repo_mapping("seller.orders.detail")
  → {target_page: "icrm.orders.search", additional_fields: [...]}

Trigger:
  POST /training/cross-repo
  { "repo_id": "SR_Web" }

Schedule: On KB update (cross_repo_mapping.yaml changes)
Duration: ~5-10 sec for 8 mappings
```

---

## Pipeline 6: Module Doc Ingestion (Codebase Intelligence)

### PRD

**Problem:** When Tier 1 (brain) can't answer, Tier 2 needs code-level context to refine the query. The .claude/docs/modules/ directories contain structured documentation about every module in the codebase: business rules, database schemas, API contracts, debugging guides.

**Goal:** Pre-index all module documentation into the vector store so Tier 2 can retrieve relevant code knowledge without live filesystem access.

**Users:** Tier 2 fallback path. Only hit when Tier 1 composite score < 0.7.

**Success Metric:**
- All module docs from 3 repos indexed
- Tier 2 retrieval finds relevant module in top-3 results >= 80% of the time
- Zero filesystem access at query time

### HLD

```
Input Sources:
  └── .claude/docs/modules/ directories
      ├── MultiChannel_API/.claude/docs/modules/
      │   24 modules: orders, shipments, courier, ndr, billing,
      │   channels, cod, customer, dashboard, discrepancy,
      │   escalation, international, kyc, login, notifications,
      │   products, reports, returns, settings, srcron, tracking,
      │   warehouse, awb_refund, assignment
      │
      ├── SR_Web/.claude/docs/modules/
      │   16 modules: orders, ndr, returns, settings, dashboard,
      │   bulk-action, manage-skus, packaging, rate-calculator,
      │   recharge, refer-and-earn, setupchannels, user-profile,
      │   activity-logs, addreturns, apiuser
      │
      └── shiprocket-go/.claude/docs/modules/
          4 modules: models, actions, channels, grifts

      File types: *.md (CLAUDE.md, api.md, database.md, business_rules.md, etc.)

Processing:
  1. For each repo → each module → each .md file:
     a. Read file content
     b. Skip if < 50 chars
     c. Chunk into max 2,000 char segments (paragraph boundaries)
     d. For each chunk:
        - entity_id = "{repo}:{module}:{filename}:chunk_{i}"
        - Extract DB table references (grep against 20 known tables)
        - Extract API endpoint patterns (/v1/..., /api/...)
        - Store with entity_type='module_doc'
  2. Build metadata per chunk:
     {module, file, chunk_index, tables: [...], apis: [...]}

Storage:
  Table: cosmos_embeddings
  ├── entity_type = 'module_doc'
  │   repo_id = repo name
  │   content = "[{repo}/{module}] {chunk_text}"
  └── metadata = {module, file, chunk_index, tables, apis}

Query-time Usage:
  # Tier 2 code intelligence retrieval
  codebase_intel.retrieve(
    query="order manifest stuck webhook",
    intents=[{intent: "explain", entity: "shipment"}],
    top_k=5
  )
  → Returns: MultiChannel_API:shipments module docs
  → Extracts: tables=[shipments, tracking_events], apis=[/v1/shipments/manifest]
  → Builds refined query for brain retry

  # DB template matching
  codebase_intel._match_db_template("order status", intents)
  → Returns: "order_by_id" template

Trigger: Automatic at COSMOS startup (lifespan init)
Schedule: On every restart (or when .claude/ docs change)
Duration: ~30-60 sec for ~44 modules
```

---

## Training Execution Order

```
FIRST RUN (initial setup):
  1. Pipeline 6: Module Doc Ingestion   ← runs at startup automatically
  2. Pipeline 1: Embedding Training     ← base vector store
  3. Pipeline 2: Intent Classifier      ← needs distillation data
  4. Pipeline 3: Graph Weights          ← needs tool execution data
  5. Pipeline 4: Page-Role Embedding    ← needs Pillar 4 YAML
  6. Pipeline 5: Cross-Repo Navigation  ← needs Pillar 4 mappings

ONGOING (scheduled):
  Daily:   Pipeline 3 (graph weights — tool performance changes fast)
  Weekly:  Pipeline 1 (embeddings — KB grows slowly)
  Weekly:  Pipeline 2 (intent classifier — new intents emerge weekly)
  On-change: Pipeline 4 + 5 (page/role — only when YAML files change)
  On-restart: Pipeline 6 (module docs — always fresh at startup)

AFTER TIER 3 LEARNING FEEDBACK:
  When Kafka LEARNING_INSIGHT events accumulate (>50 per week):
  → Re-run Pipeline 1 (new knowledge from DB fallback patterns)
  → Re-run Pipeline 2 (new intent patterns from fallback queries)
```

---

## Storage Summary

```
┌─────────────────────────────────────────────────────────┐
│ Table: cosmos_embeddings (pgvector)                      │
│                                                           │
│ entity_type values:                                       │
│ ├── 'knowledge'      ← Pipeline 1 (KB entries)          │
│ ├── 'distillation'   ← Pipeline 1 (historical queries)  │
│ ├── 'page'           ← Pipeline 4 (page metadata)       │
│ ├── 'page_intent'    ← Pipeline 4 (eval cases)          │
│ ├── 'cross_repo'     ← Pipeline 5 (cross-repo maps)     │
│ └── 'module_doc'     ← Pipeline 6 (codebase docs)       │
│                                                           │
│ Total estimated rows: ~15,000-25,000 embeddings          │
│ Vector dimension: 384 (all-MiniLM-L6-v2)                │
│ Index: pgvector ivfflat or hnsw on embedding column      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Table: cosmos_intent_models                              │
│ ← Pipeline 2 (centroid vectors + vocabulary)             │
│ Rows: 1 per training run (latest version active)         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Table: cosmos_graph_weights                              │
│ ← Pipeline 3 (per-tool weights)                          │
│ Rows: 1 per training run (latest version active)         │
└─────────────────────────────────────────────────────────┘
```

---

## API Endpoints

```
POST /cosmos/api/v1/training/embeddings         ← Pipeline 1
POST /cosmos/api/v1/training/intent-classifier   ← Pipeline 2
POST /cosmos/api/v1/training/graph-weights       ← Pipeline 3
POST /cosmos/api/v1/training/page-role           ← Pipeline 4
POST /cosmos/api/v1/training/cross-repo          ← Pipeline 5
(Pipeline 6 runs at startup, no API needed)

GET  /cosmos/api/v1/training/jobs                ← List all training jobs
GET  /cosmos/api/v1/training/jobs/:id            ← Job status + metrics
GET  /cosmos/api/v1/training/jobs/:id/watch      ← SSE progress stream

Via MARS (Temporal durable workflow):
POST /api/v1/temporal/training
{ "pipeline_type": "embeddings", "repo_id": "SR_Web" }
```

---

## Monitoring & Alerts

```
Metrics to track:
  ├── embedding_count by entity_type (should grow over time)
  ├── intent_classifier_accuracy (should stay > 85%)
  ├── graph_weight_distribution (no tool should have weight < 0.1)
  ├── pipeline_duration_ms (alert if > 10 min)
  ├── tier3_fallback_rate (should decrease as KB improves)
  └── tier3_learning_events (should trigger re-training)

Alerts:
  ├── intent_accuracy < 80%  → re-train immediately
  ├── embedding_count drops   → KB import failed
  ├── graph weights all < 0.3 → tool execution issues
  ├── tier3_rate > 30%        → KB has major gaps
  └── pipeline failed          → check DB connection
```

# COSMOS Training Plan v2 — Unified Corpus, Capability-Aware, Source-Weighted

## What Changed from v1

v1 had 6 separate pipelines each pulling from different sources. This created:
- Multiple knowledge paths (TF-IDF in indexer.py + embeddings in vectorstore + graph weights)
- Only 3 repos ingested (MultiChannel_API, SR_Web, shiprocket-go), missing 5 more
- Only `*.md` files ingested, missing structured `module.yaml`, `evidence/index.yaml`
- Eval data (10K+ labeled examples) not used for training
- Pillar 1 (8,158 schema files) completely ignored
- 27/57 module docs are draft — no quality gating
- No source weighting (draft docs treated same as verified KB)

v2 fixes all of this.

---

## Architecture: One Canonical Corpus Schema

Every training document — regardless of source — is normalized to this schema before storage:

```yaml
# Canonical Training Document
query_or_task: "What is the COD settlement timeline?"
capability: "business_rule"    # intent | tool | page | module | schema | cross_repo | business_rule | error_fix
ground_truth: "COD remittance is processed T+5 business days from delivery confirmation"
evidence_refs:
  - "MultiChannel_API/billing/business_rules.md:line42"
  - "pillar_1_schema/tables/cod_remittances/_meta.yaml"
source_type: "kb_pillar3"      # kb_pillar1 | kb_pillar3 | kb_pillar4 | module_doc | eval_set | runtime | distillation
trust_score: 0.9               # 0.0-1.0 (see Source Weighting below)
freshness: "2026-03-19"        # last verified date
negative_examples:
  - "COD is settled immediately"
  - "COD settlement takes 30 days"
```

### Source Weighting (4 Tiers)

```
Tier A (trust 0.9-1.0): Ground truth
  - Reviewed KB eval cases (global_eval_set.jsonl, pillar 4 eval_cases.yaml)
  - Successful tool executions (icrm_tool_executions where status=success)
  - High-confidence distillation (confidence >= 0.8)
  - Tier 3 DB fallback wins (verified by live data)

Tier B (trust 0.7-0.9): Primary knowledge
  - Pillar 1 schema YAML (tables, columns, state machines)
  - Pillar 3 API tool YAML (endpoints, params, examples)
  - Pillar 4 page/role YAML (pages, fields, permissions)
  - Evidence/index.yaml files (verified facts with confidence scores)

Tier C (trust 0.5-0.7): Secondary knowledge
  - Module docs with status=enriched or status=active AND score >= 50
  - business_rules.md, api.md, database.md from enriched modules
  - debugging.md (symptom -> fix patterns)
  - Submodule docs from enriched parent modules

Tier D (trust 0.1-0.4): Weak supervision / retrieval-only
  - Module docs with status=draft or score < 50
  - prd.md, ssd.md (design docs, not operational truth)
  - Raw CLAUDE.md text chunks
  - SR_Web module docs (mostly draft, score=0)
```

### Quality Gate: What Gets Trained vs Retrieval-Only

```
trust_score >= 0.5 → TRAIN (embed + intent train + graph weight)
trust_score 0.1-0.5 → RETRIEVAL ONLY (embed for Tier 2 fallback, but don't
                       train intent classifier or graph weights on it)
trust_score < 0.1 → SKIP (not ingested at all)
```

---

## Expanded Source Inventory

### Repos to Ingest (8 repos, was 3)

```
CURRENTLY INGESTED (codebase_intelligence.py):
  ├── MultiChannel_API     (24 modules, 7 module.yaml, 6 evidence)
  ├── SR_Web               (16 modules, 4 module.yaml, 4 evidence)
  └── shiprocket-go        (4 modules, 0 module.yaml)

MISSING — ADD THESE:
  ├── MultiChannel_Web     (20 modules, 20 module.yaml — all draft, use as Tier D)
  ├── shiprocket-channels  (13 modules, 13 module.yaml — 7 enriched, high value)
  ├── SR_Sidebar           (5 modules, 5 module.yaml — active)
  ├── sr_login             (5 modules, 5 module.yaml — enriched)
  └── helpdesk             (3 modules, 3 module.yaml — active)
```

### File Types to Ingest (was: *.md only)

```
CURRENTLY INGESTED:
  ├── *.md files (text chunks)

MISSING — ADD THESE:
  ├── module.yaml          (57 files — structured metadata, trust scores)
  ├── evidence/index.yaml  (15 files — verified facts with confidence)
  ├── prd.md               (54 files — product requirements)
  ├── ssd.md               (55 files — system design)
  ├── debugging.md         (81 files — symptom→fix patterns)
  ├── submodules/*.md      (176 files — channel-specific knowledge)
  └── TOTAL NEW: 438 structured files
```

---

## Priority-Ordered KB Expansion

### P0: Add First (highest query-resolution impact)

```
1. MultiChannel_API/tracking
   Why: "status not updated", "where is my shipment", courier scan timeline,
        webhook lag, sync delay — the #1 support query category
   Files: CLAUDE.md, api.md, database.md, business_rules.md, debugging.md,
          module.yaml (enriched, score TBD), evidence/index.yaml
   Generate: async_flow_chain.yaml (webhook→courier→status), field_lineage.yaml
             (tracking_status from push_api to panel), symptom_root_cause.yaml
             (status stuck → webhook retry pending)

2. MultiChannel_API/shipments
   Why: manifest, pickup, courier handoff, shipment state changes
   Files: CLAUDE.md, api.md, database.md, business_rules.md, debugging.md
   Generate: domain_overview.yaml, tool_playbook.yaml (manifest creation),
             error_catalog.yaml (manifest failures)

3. MultiChannel_API/orders
   Why: core lifecycle, already richest docs (score=85, enriched)
   Files: module.yaml, CLAUDE.md, api.md, database.md, business_rules.md,
          debugging.md, evidence/index.yaml, 7 submodules (amazon, shopify, etc.)
   Generate: domain_overview.yaml, symptom_root_cause.yaml,
             field_lineage.yaml (order_id → awb → tracking)

4. shiprocket-channels/base_channel
   Why: real order-ingestion pipeline, rollback behavior, inventory allocation,
        channel-side creation flow — usually missing from normal KB
   Files: module.yaml (enriched), CLAUDE.md, api.md, database.md,
          business_rules.md, debugging.md, evidence/index.yaml,
          submodules/ (product_mapper, inventory, webhooks)
   Generate: async_flow_chain.yaml (channel→order→shipment),
             error_catalog.yaml (channel sync failures)
```

### P1: Add Next

```
5. MultiChannel_API/ndr
   Why: NDR is major support domain, needs root-cause + action paths
   Generate: symptom_root_cause.yaml, tool_playbook.yaml (reattempt vs RTO)

6. MultiChannel_API/billing
   Why: billing disputes, COD, wallet, remittance, fee confusion
   Generate: domain_overview.yaml, field_lineage.yaml (charges breakdown)

7-10. MultiChannel_Web: orders, ndr, billing, shipments
   Why: ICRM/admin-side investigations, cross-repo answers
   Note: All draft (score=0), ingest as Tier D retrieval-only
   Generate: page_admin_mapping.yaml (link to Pillar 4 cross-repo)
```

### P2: Add After That

```
11-12. SR_Web: orders, ndr
   Why: seller-language/page-language answers
   Note: draft quality, use carefully as Tier D

13. shiprocket-channels: amazon, shopify, woocommerce
   Why: marketplace/channel-specific order behavior
   Note: enriched quality, Tier C
```

---

## What to Extract from Each Module Folder

For each module, don't dump raw markdown. Generate these **normalized KB artifacts**:

### 1. domain_overview.yaml
```yaml
domain: orders
repo: MultiChannel_API
summary: "Order creation, lifecycle management, 23 marketplace channels"
tables: [orders, order_products, shipments, channels]
apis: [POST /v1/orders, GET /v1/orders/:id, PUT /v1/orders/:id/cancel]
dependencies: [courier_assignment, billing, tracking, notification]
status_states: [pending, processing, shipped, delivered, cancelled, rto]
owner: bharat.bhushan@shiprocket.com
trust_score: 0.85
```

**Source:** module.yaml + CLAUDE.md

### 2. symptom_root_cause.yaml
```yaml
symptoms:
  - symptom: "Order status stuck at manifested"
    root_causes:
      - cause: "Courier hasn't picked up"
        check: "SELECT pickup_date FROM shipments WHERE awb_code = :awb"
        fix: "Wait 24-48h or contact courier for pickup reattempt"
        confidence: 0.9
      - cause: "Webhook from courier not received"
        check: "Check tracking_events for latest scan"
        fix: "Webhook will auto-retry. Status updates within 4-6 hours"
        confidence: 0.8
  - symptom: "Tracking not updating on seller panel"
    root_causes:
      - cause: "Courier push API timeout"
        check: "ELK: search courier.push_api timeout for AWB"
        fix: "Auto-retry in progress. Resolves within 2-4 hours"
        confidence: 0.85
```

**Source:** debugging.md + business_rules.md + known_gaps.md

### 3. field_lineage.yaml
```yaml
fields:
  - field: tracking_status
    page: seller.shipments.tracking
    api: GET /v1/tracking/:awb
    webhook: courier.push_api → tracking_events table
    db_column: tracking_events.status
    db_table: tracking_events
    refresh: "Pushed by courier via webhook, not polled"
    delay_reason: "Courier API timeout or queue lag"
  - field: awb_code
    page: seller.orders.detail
    api: POST /v1/manifest/generate
    db_column: shipments.awb_code
    db_table: shipments
    generated_by: "Courier assignment API during manifest creation"
```

**Source:** database.md + api.md + Pillar 1 schema + Pillar 4 field_trace_chain

### 4. async_flow_chain.yaml
```yaml
flows:
  - name: order_to_delivery
    steps:
      - step: 1
        action: "Order created (panel/API/channel sync)"
        table: orders
        status: "pending"
      - step: 2
        action: "Manifest generated, AWB assigned"
        table: shipments
        status: "manifested"
        async: "Courier API call, can timeout"
      - step: 3
        action: "Courier pickup scan"
        table: tracking_events
        status: "picked_up"
        async: "Webhook from courier push_api"
        delay_note: "Can lag 2-6 hours if courier system slow"
      - step: 4
        action: "In-transit scans"
        table: tracking_events
        status: "in_transit"
        async: "Multiple webhook pushes per hub"
      - step: 5
        action: "Delivered scan"
        table: tracking_events
        status: "delivered"
        triggers: ["COD settlement T+5", "seller notification"]
```

**Source:** business_rules.md + database.md + CLAUDE.md workflow sections

### 5. tool_playbook.yaml
```yaml
tools:
  - tool: lookup_order
    when_to_use: "Seller asks about specific order status, details, or tracking"
    required_params: [order_id OR awb_code, company_id]
    approval_mode: none
    side_effects: none
    common_failures:
      - "Order not found → seller may have wrong ID format"
      - "401 → token expired, needs refresh"
    do_not_use_for: "Bulk order queries (use orders_list instead)"
  - tool: cancel_order
    when_to_use: "Seller explicitly requests cancellation"
    required_params: [order_id, company_id]
    approval_mode: "seller_confirmation_required"
    side_effects: ["AWB deallocation", "inventory restock", "refund trigger"]
    restrictions: "Cannot cancel after manifest generation"
```

**Source:** api.md + business_rules.md + Pillar 3 tool definitions

### 6. error_catalog.yaml
```yaml
errors:
  - error: "Manifest generation failed"
    api: POST /v1/manifest/generate
    causes:
      - "Courier serviceability check failed for pincode"
      - "AWB stock exhausted for selected courier"
      - "Order weight exceeds courier limit"
    seller_message: "Unable to generate manifest. Please check delivery pincode and order weight."
    admin_action: "Check courier serviceability rules and AWB allocation"
  - error: "Channel sync failed"
    api: POST /channels/:id/sync
    causes:
      - "Channel auth token expired"
      - "Product SKU mismatch between channel and Shiprocket"
      - "Rate limit exceeded on channel API"
    seller_message: "Order sync is temporarily paused. It will resume automatically."
    admin_action: "Check channel settings for auth refresh"
```

**Source:** debugging.md + known_gaps.md + api.md error responses

### 7. user_aliases.yaml
```yaml
aliases:
  - canonical: "AWB number"
    variations: ["tracking number", "awb", "docket number", "consignment number",
                  "awb code", "tracking id", "waybill", "awb no"]
    hinglish: ["awb number kya hai", "tracking number do", "docket number batao"]
  - canonical: "order status"
    variations: ["where is my order", "order kahan hai", "order ka status",
                  "order update", "shipping status", "delivery status"]
  - canonical: "NDR"
    variations: ["non-delivery", "failed delivery", "delivery failed",
                  "not delivered", "undelivered", "reattempt",
                  "delivery nahi hua", "order wapas aaya"]
  - canonical: "weight discrepancy"
    variations: ["weight dispute", "wrong weight", "overcharged weight",
                  "weight difference", "weight mismatch",
                  "weight galat hai", "zyada weight laga diya"]
```

**Source:** manual curation + distillation record analysis

---

## Training by Capability (Not One Model Blob)

### Capability 1: Retrieval Corpus
```
What: Unified embedding index for Tier 1 vector search
Sources:
  Tier A: reviewed KB evals + successful distillation (trust 0.9+)
  Tier B: Pillar 1 schema + Pillar 3 APIs + Pillar 4 pages (trust 0.7-0.9)
  Tier C: enriched module docs + business rules (trust 0.5-0.7)
  Tier D: draft docs (retrieval-only, trust 0.1-0.4)
  NEW: domain_overview, symptom_root_cause, field_lineage, async_flow_chain,
       tool_playbook, error_catalog, user_aliases

entity_types in cosmos_embeddings:
  knowledge, distillation, schema, api_tool, page, page_intent,
  cross_repo, module_doc, business_rule, domain_overview,
  symptom_fix, field_lineage, async_flow, tool_playbook,
  error_pattern, user_alias, known_gap

Weighted search: similarity × trust_score = final_relevance
```

### Capability 2: Intent/Router
```
What: Classify query → intent + entity + tool
Sources:
  PRIMARY: global_eval_set.jsonl (10K+ hand-labeled, Tier A)
  AUGMENT: Pillar 4 training_seeds.jsonl (100 seeds, Tier A)
  AUGMENT: Pillar 4 eval_cases.yaml (50+ per repo, Tier A)
  AUGMENT: tool_playbook.yaml → generate negative examples
           ("cancel order" → intent=act NOT intent=lookup)
  BASELINE: icrm_distillation_records (Tier A for high-conf, Tier C for low)

Hard negatives (critical for confusing domains):
  - "order status" (lookup) vs "cancel order" (action)
  - "seller.orders" (SR_Web) vs "icrm.orders" (MultiChannel_Web)
  - "tracking stuck" (sync issue) vs "tracking delayed" (courier issue)
  - "billing charge" (lookup) vs "refund request" (action)
```

### Capability 3: Graph Weights
```
What: Edge weights for GraphRAG traversal
Sources:
  Tool outcomes: icrm_tool_executions (success rate, latency)
  KB relations: Pillar 1 table→table relationships
  Cross-repo edges: Pillar 4 cross_repo_mapping
  Page edges: Pillar 4 page→API→table chains
  NEW: async_flow_chain edges (webhook→courier→status)
  NEW: field_lineage edges (field→API→DB column)
```

### Capability 4: Page Intelligence
```
What: "Which page shows X?" and "Can role Y do Z?"
Sources:
  PRIMARY: Pillar 4 YAML (reviewed, Tier B)
  AUGMENT: enriched SR_Web/MultiChannel_Web module docs (Tier C)
  SKIP: draft SR_Web module docs (Tier D, retrieval-only)
```

### Capability 5: Code Intelligence (Tier 2 fallback)
```
What: Module-level knowledge for query refinement
Sources: .claude/docs/modules/ across ALL 8 repos
Chunked by type (NOT mixed into same embedding):
  - entity_type='module_overview' (from CLAUDE.md)
  - entity_type='module_api' (from api.md)
  - entity_type='module_database' (from database.md)
  - entity_type='module_rules' (from business_rules.md)
  - entity_type='module_debug' (from debugging.md)
  - entity_type='module_gaps' (from known_gaps.md)
  - entity_type='module_meta' (from module.yaml — structured, not text)
  - entity_type='module_evidence' (from evidence/index.yaml)
  - entity_type='module_submodule' (from submodules/*.md)

Quality gate: module.yaml status + score determines trust_score
  enriched + score >= 50 → Tier C (train + retrieve)
  active → Tier C
  draft or score < 50 → Tier D (retrieve-only)
  infrastructure_only → SKIP
```

---

## Ingestion Pipeline Design

### Phase 1: KB Artifacts Generator (run once, update on KB change)

```
Input: .claude/docs/modules/ from all 8 repos
Output: Normalized YAML artifacts in knowledge_base/shiprocket/generated/

For each module (P0 first, then P1, then P2):
  1. Read module.yaml → extract trust_score, status, dependencies
  2. Quality gate: skip if status=draft AND no evidence/
  3. For enriched/active modules, generate:
     - domain_overview.yaml
     - symptom_root_cause.yaml (from debugging.md)
     - field_lineage.yaml (from database.md + Pillar 1 + Pillar 4)
     - async_flow_chain.yaml (from business_rules.md + CLAUDE.md)
     - tool_playbook.yaml (from api.md + business_rules.md)
     - error_catalog.yaml (from debugging.md + known_gaps.md)
  4. Store in knowledge_base/shiprocket/generated/{repo}/{module}/
```

### Phase 2: Unified Ingestion Pipeline (replaces fragmented training)

```
Single pipeline that ingests ALL sources with trust scoring:

Step 1: Ingest Pillar 1 schema (entity_type='schema', trust=0.8)
Step 2: Ingest Pillar 3 API tools (entity_type='api_tool', trust=0.8)
Step 3: Ingest Pillar 4 pages (entity_type='page', trust=0.8)
Step 4: Ingest generated artifacts (domain_overview, symptom_fix, etc.)
Step 5: Ingest module docs by type (chunked, not mixed)
Step 6: Ingest eval sets (global_eval_set.jsonl, training_seeds.jsonl)
Step 7: Ingest runtime data (knowledge_entries, distillation)

Each document stored with:
  - entity_type (specific capability)
  - trust_score (from source tier)
  - freshness (last verified date)
  - repo_id
  - module (if applicable)
  - negative_examples (if available)
```

### Phase 3: Intent Classifier Retraining

```
Merge into unified training set:
  1. global_eval_set.jsonl (10K+, trust=0.95)
  2. Pillar 4 training_seeds.jsonl (100+, trust=0.9)
  3. Pillar 4 eval_cases.yaml (50+ per repo, trust=0.9)
  4. icrm_distillation_records (high-conf only, trust=0.7)
  5. Generated hard negatives from tool_playbook.yaml

Train with capability-aware labels:
  (query, intent, entity, tool, capability)
  NOT just (query, intent)
```

### Phase 4: Runtime Learning Loop

```
Tier 3 DB fallback succeeds:
  → Create new eval case automatically
  → Create new training seed
  → Tag as trust=0.85 (verified by live data)
  → Feed into next retraining cycle

Human resolution recorded:
  → Create symptom_root_cause entry
  → Tag as trust=0.95 (human-verified)
  → Highest-value training data
```

---

## Execution Order

```
Week 1:
  [x] Expand codebase_intelligence.py to ingest all 8 repos
  [x] Add module.yaml + evidence/index.yaml parsing (not just *.md)
  [x] Quality-gate: skip draft docs from training, keep for retrieval
  [x] Generate P0 artifacts: tracking, shipments, orders, base_channel

Week 2:
  [ ] Ingest Pillar 1 schema into embeddings (entity_type='schema')
  [ ] Ingest global_eval_set.jsonl into intent classifier
  [ ] Add training_seeds.jsonl + eval_cases.yaml to intent training
  [ ] Generate P1 artifacts: ndr, billing, MultiChannel_Web modules

Week 3:
  [ ] Chunk module docs by type (not mixed)
  [ ] Add trust-weighted search (similarity × trust_score)
  [ ] Build hard negative pairs for intent classifier
  [ ] Generate user_aliases.yaml and error_catalog.yaml

Week 4:
  [ ] Wire Tier 3 fallback wins into automatic eval case creation
  [ ] Build async_flow_chain for top 5 domains
  [ ] Build field_lineage for top 50 high-value fields
  [ ] Unify indexer.py + vectorstore into single retrieval path
```

---

## Expected Impact

```
                            Current    After v2 Plan
─────────────────────────────────────────────────────
Schema queries              ~40%       ~90%
  "which table stores AWB?"
  (Pillar 1 ingested)

API/tool queries            ~50%       ~95%
  "how to create shipment?"
  (Pillar 3 + tool_playbook)

Status stuck queries        ~45%       ~90%
  "why not updating?"
  (tracking module + async_flow_chain)

Billing disputes            ~35%       ~85%
  "why was I overcharged?"
  (billing module + error_catalog)

NDR resolution              ~40%       ~88%
  "what to do about NDR?"
  (ndr module + symptom_root_cause)

Intent classification       ~85%       ~93%
  (global_eval_set + hard negatives)

Cross-module queries        ~55%       ~82%
  "how orders link to billing?"
  (field_lineage + domain_overview)

Tier 1 resolution rate      ~60%       ~83%
  (fewer Tier 2/3 fallbacks)

Channel-specific queries    ~25%       ~75%
  "how does Shopify order sync work?"
  (shiprocket-channels + submodules)
```

---

## What NOT to Do

```
- Don't dump all 57 module.yaml files blindly (27 are draft)
- Don't treat SR_Web module docs as gold (score=0, draft)
- Don't store secrets, raw logs, PII, or full query dumps
- Don't ingest duplicate text without provenance and trust_score
- Don't mix all module doc types into one embedding text
- Don't let Tier D docs influence intent classifier training
- Don't let LLM-generated SQL hit the slave (template-only)
- Don't ingest MultiChannel_Web as Tier B (it's all draft → Tier D)
```

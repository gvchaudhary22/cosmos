# COSMOS Knowledge Base Goals

## What This Document Is
This is the single source of truth for what the Knowledge Base must achieve. Every KB decision — what to create, how to structure, how to embed, how to retrieve — traces back to these goals.

---

## Goal 1: Every ICRM Question Has a Grounded Answer

**The KB must contain enough structured knowledge that no ICRM operator question goes unanswered by guessing.**

What this means:
- If an operator asks "why is this order stuck at status 3?" — the KB must have the status constant (3 = READY_TO_SHIP), the valid transitions from status 3, and the operator playbook for diagnosis.
- If a seller asks "when will my COD payment come?" — the KB must have remittance cycle rules (T+2 early, T+8 standard), hold reasons, and the billing workflow.
- If the KB doesn't have the answer, COSMOS says "I don't know" — it never fabricates.

How to measure:
- Run 201 ICRM eval seeds → recall@5 must be > 85%.
- Low-confidence traces (< 0.5) must decrease month over month.

---

## Goal 2: Knowledge Is Structured for Retrieval, Not for Reading

**Every KB document is written for embedding and vector search, not for human documentation.**

What this means:
- Each chunk is 200-500 tokens with ONE clear concept.
- Rendered text, not raw YAML dumps. Typed renderers convert structured data into clean search-optimized text.
- Tagged with `query_mode` (lookup / diagnose / act / explain) so retrieval can filter by intent type.
- Tagged with `pillar`, `domain`, `capability`, `trust_score` for payload-filtered search in Qdrant.

Bad chunk (raw dump):
```
{'overview': {'api': {'id': 'mcapi.v1.orders.create.post', 'method': 'POST' ...}}}
```

Good chunk (rendered):
```
POST /api/v1/orders/create | Controller: CustomController@store | Domain: orders | Intent: orders_create | Risk: medium | Approval: confirm
```

---

## Goal 3: Every Action and Workflow Is a First-Class KB Entity

**The KB doesn't just describe what exists — it describes what to DO and what HAPPENS.**

P1 answers: "orders table has 50 columns"
P3 answers: "POST /api/v1/orders/create exists"
**P6 answers: "When you create an order, these preconditions must be met, these side effects happen, this is how to rollback"**
**P7 answers: "When an order gets stuck, follow this state machine: order_placed → validated → address_verified → ready_to_ship"**

Each action contract (P6) has 11 files:
- index, contract, intent_map, permissions, data_access, execution_graph, failure_modes, observability, examples, eval_cases, rollback

Each workflow runbook (P7) has 13 files:
- index, overview, state_machine, entrypoints, decision_matrix, action_map, data_flow, ui_map, async_map, operator_playbook, user_language, exception_paths, eval_cases

---

## Goal 4: Multi-Repo Coverage — Not Just MultiChannel_API

**All 8 Shiprocket repos must have KB coverage proportional to their ICRM query volume.**

Current state:
| Repo | YAML Files | Pillars | Coverage |
|------|-----------|---------|----------|
| MultiChannel_API | 44,094 | 8/8 | Complete |
| SR_Web | 452 | 3/8 | Partial |
| MultiChannel_Web | 264 | 3/8 | Partial |
| shiprocket-channels | 132 | 1/8 | Minimal |
| helpdesk | 77 | 1/8 | Minimal |
| shiprocket-go | 23 | 1/8 | Minimal |
| sr_login | 23 | 1/8 | Minimal |
| SR_Sidebar | 22 | 1/8 | Minimal |

Target: Every repo that ICRM operators ask about must have at minimum: P3 (APIs), P6 (key actions), and P7 (key workflows).

Priority:
1. shiprocket-channels — channel sync issues are top-5 ICRM query category
2. helpdesk — ticket escalation workflows directly impact ICRM operators
3. sr_login — auth failures are common seller complaints

---

## Goal 5: The Graph Connects Everything

**KB docs don't exist in isolation — every doc is a node in a knowledge graph with typed edges to related docs.**

An orders action contract is connected to:
- `action → table` (reads_table, writes_table) → which DB tables it touches
- `action → api` (calls_api) → which API endpoint it uses
- `workflow → action` (uses_action) → which workflow triggers this action
- `action → job` (dispatches_job) → which async jobs it starts
- `domain → action` (belongs_to_domain) → which business domain owns it

Edge weights are semantic:
- belongs_to_domain = 2.0 (strongest grouping)
- writes_table = 1.8 (important side effect)
- uses_action = 1.7 (workflow dependency)
- calls_api = 1.6 (API dependency)
- reads_table = 1.5 (data dependency)

PPR (Personalized PageRank) uses these weights to find important nodes at ANY depth — not just 2-hop BFS.

---

## Goal 6: Negative Routing Prevents Wrong Answers

**The KB must explicitly teach COSMOS what NOT to do — which tool to NOT use for which query.**

Examples:
- "cancel order" ≠ "cancel shipment" — different APIs, different side effects, different preconditions
- "COD ka paisa kab milega" → billing/remittance, NOT refund
- "mera order RTO ho gaya" → NDR/RTO workflow, NOT order creation
- "courier wala nahi aaya" → pickup failure, NOT delivery failure

Current: 100 cross-domain negative routing examples.
Target: 200+ examples covering every domain confusion pair, including Hinglish variants.

---

## Goal 7: Claude Opus 4.6 Generates the Best KB Content

**When generating new KB content, use Claude Opus 4.6 with this prompt:**

```
You are generating knowledge base content for Shiprocket's ICRM AI copilot.

RULES:
1. Write for EMBEDDING, not for humans. 200-500 tokens per chunk.
2. One concept per chunk. Never merge topics.
3. Use Shiprocket terminology: AWB, NDR, RTO, COD, ICRM, MCAPI.
4. Include Hinglish: "order cancel karo", "pickup kyun nahi hua".
5. Include negative examples: "NOT the same as X."
6. Link to pillars: "See pillar_1_schema/tables/orders"
7. For actions: preconditions, side effects, rollback, approval.
8. For workflows: state machine, transitions, operator playbook.
9. For eval cases: positive, ambiguous, unsafe, regression.
10. Quality > quantity. One excellent doc > ten mediocre ones.
```

Quality is #1. Not cost. Not speed. The best possible knowledge for the best possible ICRM response.

---

## Goal 8: Continuous Evaluation and Learning

**Every pipeline run measures KB quality. Every low-confidence query improves the KB.**

Eval pipeline:
1. 201 ICRM eval seeds run after every training pipeline execution
2. Measure: recall@1, recall@5, recall@10 per category
3. Measure: action_match (correct action contract retrieved?)
4. Measure: field_precision (correct table.column for field queries?)

Feedback loop:
1. Low-confidence traces (< 0.5) auto-generate improvement candidates
2. Types: missing_action_candidate, missing_kb_coverage, add_negative_example, add_clarification_rule
3. Staged in `_feedback_staging/` for human review
4. On approve → creates KB YAML → triggers training pipeline → new knowledge embedded

Target: recall@5 > 90% across all categories. Zero domains with recall < 70%.

---

## Goal 9: Entity Hubs Bridge All Pillars

**For each business entity (orders, shipments, billing, etc.), one 500-800 token summary merges all pillars.**

An entity hub for "orders" contains:
- Schema: key tables + important columns (from P1)
- APIs: read/write endpoints (from P3)
- Actions: available action contracts (from P6)
- Workflows: state machine states (from P7)
- Field traces: page field → API → table.column (from P4)

Why: When LangGraph needs complete context in one retrieval hit, the entity hub provides it. Individual pillar chunks are too narrow; entity hubs give the full picture.

Current: 10 entity hubs (orders, shipments, billing, ndr, pickup, returns, settings, courier, support, channels).
Target: Entity hubs for every domain that ICRM operators ask about.

---

## Goal 10: Production-Ready Embedding Pipeline

**The training pipeline must handle 50,000+ docs efficiently with zero data loss.**

Pipeline chain:
```
KB YAML files
  → kb_ingestor reads (11 readers, typed renderers, query_mode tagging)
  → content_hash check (skip unchanged docs — saves 99% on re-run)
  → embed_text via AI Gateway (text-embedding-3-small, 1536d)
  → Qdrant upsert (deterministic point ID, payload filtering)
  → Neo4j graph build (MERGE nodes + edges, entity_lookup)
  → Eval benchmark (201 seeds, recall@5 per category)
  → Feedback loop (stage improvements from low-confidence traces)
```

Rules:
- First run: ~30 min (embed all docs)
- Re-run with no changes: ~30 sec (content_hash skip)
- Re-run with 50 changed docs: ~1 min (only changed docs re-embedded)
- Never embed junk: quality gate rejects < 50 chars, stubs, low-alpha content
- P6/P7 docs with empty fields get fallback content, never silently skipped

---

## Summary: The KB Exists to Answer These 5 Question Types

| Type | Example | Pillar |
|------|---------|--------|
| **Lookup** | "What columns does orders table have?" | P1, P3, P4 |
| **Diagnose** | "Why is this shipment stuck?" | P7 (workflow + state machine + operator playbook) |
| **Act** | "Cancel this order" | P6 (action contract + preconditions + side effects) |
| **Explain** | "How does RTO prediction work?" | P6 (execution_graph) + P5 (module docs) |
| **Route** | "Is this a cancel-order or cancel-shipment?" | P8 (negative routing) |

If the KB can answer all 5 types with > 85% recall, COSMOS delivers production-grade ICRM responses.

# Skill: knowledge-base

## Purpose
KB ingestion, pillar management, quality gating, and content generation for the COSMOS knowledge base.

## Loaded By
`kb-specialist`

---

## KB Structure

### The 8 Repos
```
knowledge_base/shiprocket/
  MultiChannel_API/    → 44,094 YAML files — PRIMARY (all 8 pillars)
  SR_Web/              → Seller web panel (P1, P4, P5)
  MultiChannel_Web/    → ICRM admin panel (P1, P4, P5)
  shiprocket-channels/ → Channel integrations
  helpdesk/            → Support ticket system
  shiprocket-go/       → Go microservices
  sr_login/            → Authentication
  SR_Sidebar/          → UI sidebar
```

### The 8 Pillars
| Pillar | Directory | What it answers |
|--------|-----------|----------------|
| P1: Schema | `pillar_1_schema/` | "What data exists?" — tables, columns, status values |
| P3: APIs & Tools | `pillar_3_apis_tools/` | "What API can I call?" — endpoints with tool mapping |
| P4: Pages & Fields | `pillar_4_pages_fields/` | "Where is this field?" — UI → API → DB traces |
| P5: Module Docs | `pillar_5_module_docs/` | "What code handles this?" — controllers, services |
| P6: Action Contracts | `pillar_6_action_contracts/` | "What should I do?" — 25 actions × 11 files |
| P7: Workflow Runbooks | `pillar_7_workflow_runbooks/` | "Why did this happen?" — 9 workflows × 13 files |
| P8: Negative Routing | `pillar_8_negative_routing/` | "Don't confuse X with Y" — disambiguation |
| Hub | `hub/` | "Give me everything about X" — cross-pillar summaries |

---

## Quality Gate

A chunk passes quality gate if:
- `len(content.strip()) >= 50`
- Punctuation ratio < 80%
- Does not match stub patterns: `TODO`, `placeholder`, `N/A`, `[TBD]`
- Has a valid `entity_id` (stable identifier)
- Has a `query_mode`: `lookup` | `diagnose` | `act` | `explain` | `routing`

Trust scores:
- `0.9` — human-verified content
- `0.7` — auto-generated content
- `0.5` — Claude-generated (unverified)

Freshness decay:
- Docs > 90 days old: weight multiplied by 0.7 in retrieval scoring

---

## YAML Document Format

```yaml
# Standard KB doc format
entity_id: "pillar_3_apis_tools/endpoints/cancel_order"
pillar: "P3"
repo: "MultiChannel_API"
title: "Cancel Order API"
query_mode: "act"
trust_score: 0.9
training_ready: true

content: |
  [200-500 tokens of retrieval-optimized text]
  [One concept per chunk — never merge multiple topics]
  [Include: endpoint path, method, params, response, preconditions]
  [Include: Hinglish variants where relevant]
  [Include: negative examples — "This is NOT the same as X"]
  [Cross-link: "See pillar_1_schema/tables/orders for column details"]

negative_phrases:
  - "This is NOT for cancelling shipments (use /shipment/cancel)"
  - "Does not handle COD remittance cancellation"

related:
  - "pillar_1_schema/tables/orders"
  - "pillar_6_action_contracts/cancel_order/index"
```

---

## Chunk Writing Guidelines

### Write for embedding, not for humans
- Each chunk should answer ONE specific retrieval query
- Include the question the chunk should answer as a comment: `# Q: How do I cancel an order?`
- Dense, precise language — not documentation prose

### Include real Shiprocket terminology
- AWB, NDR, RTO, COD, ICRM, MCAPI, channel sync
- Status codes: 104 (cancelled), 7 (delivered), 9 (out for delivery)
- Hinglish: "order cancel karo", "pickup kyun nahi hua", "NDR resolve kaise karein"

### Negative examples (P8 pattern — use everywhere)
```yaml
content: |
  ...
  NOTE: This action is for [X]. Do NOT use this for [Y].
  If user asks "[common misphrase]", redirect to [correct action].
```

---

## Ingestion Pipeline Checklist

Before triggering ingestion:
- [ ] `KB_PATH` set in `.env` and points to valid directory
- [ ] `AIGATEWAY_API_KEY` set (for text-embedding-3-small)
- [ ] Qdrant running on `:6333` (`curl http://localhost:6333/healthz`)
- [ ] Neo4j running on `:7687`
- [ ] MySQL running on `:3309`

After ingestion:
- [ ] Check `cosmos_kb_file_index` for failed files (`status=2`)
- [ ] Run eval: `recall@5 > 0.75`
- [ ] Check Qdrant collection count matches expected doc count

---

## Claude Prompt for KB Content Generation

When using Claude Opus to generate KB content:
```
You are generating knowledge base content for Shiprocket's ICRM AI copilot (COSMOS/RocketMind).

Rules:
1. Write for EMBEDDING (200-500 tokens per chunk, one concept per chunk).
2. Include real Shiprocket terminology: AWB, NDR, RTO, COD, ICRM, MCAPI.
3. Include Hinglish variants: "order cancel karo", "pickup kyun nahi hua".
4. Include negative examples and disambiguation.
5. Cross-link to related pillars using stable entity_ids.
6. For P6 action contracts: preconditions, side effects, rollback, approval mode.
7. For P7 runbooks: state machine, valid transitions, operator playbook.
8. trust_score: 0.9 for verified, 0.7 for auto-generated.
9. training_ready: true.
10. Quality > quantity. One excellent doc beats ten mediocre ones.
```

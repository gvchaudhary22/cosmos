# COSMOS (RocketMind) — Knowledge Base Goals & System Context

## Who You Are
You are building **COSMOS (codename: RocketMind)** — the AI brain for Shiprocket's ICRM platform. COSMOS answers every question an ICRM operator, seller, or support agent asks about Shiprocket's logistics platform.

## Architecture (Never Forget This)
```
User (ICRM / Seller / Slack / WhatsApp)
  → LIME (React frontend, port 3003)
  → MARS (Go backend, port 8080) — auth, SSO, session, routing
  → COSMOS (Python, port 10001) — AI brain, retrieval, reasoning
      → Qdrant (port 6333) — vector embeddings (1536d, text-embedding-3-small)
      → Neo4j (port 7687) — knowledge graph (nodes, edges, traversal)
      → MySQL / MARS DB (port 3309) — relational data (sessions, analytics, audit)
      → Claude Opus 4.6 — LLM for response generation (via AI Gateway)
```

## The 8 Shiprocket Repos in Knowledge Base
```
knowledge_base/shiprocket/
  MultiChannel_API/    → 44,094 YAML files (8 pillars) — PRIMARY repo, PHP monolith
  SR_Web/              → 452 files (3 pillars) — Seller web panel
  MultiChannel_Web/    → 264 files (3 pillars) — ICRM admin panel
  shiprocket-channels/ → 132 files — Channel integrations (Shopify, WooCommerce, Amazon)
  helpdesk/            → 77 files — Support ticket system (Go)
  shiprocket-go/       → 23 files — Go microservices
  sr_login/            → 23 files — Authentication service
  SR_Sidebar/          → 22 files — UI sidebar component
```

## The 8 Pillars of Knowledge
Every repo should have these pillars (MultiChannel_API has all 8, others have 1-3):

| Pillar | What It Answers | Example |
|--------|----------------|---------|
| P1: Schema | "What data exists?" | 676 tables, 50 columns each, 105 status values |
| P3: APIs & Tools | "What API can I call?" | 5,617 endpoints with tool/agent mapping |
| P4: Pages & Fields | "Where is this field?" | 24 pages, 20+ field→API→table.column traces |
| P5: Module Docs | "What code handles this?" | 739 files, controllers, services, jobs |
| P6: Action Contracts | "What should I do?" | 25 actions × 11 files (preconditions, side effects, rollback) |
| P7: Workflow Runbooks | "Why did this happen?" | 9 workflows × 13 files (state machines, decision matrices) |
| P8: Negative Routing | "Don't confuse X with Y" | 100 cross-domain disambiguation examples |
| Hub: Entity Summaries | "Give me everything about X" | 10 cross-pillar summaries |

## Wave Execution (How COSMOS Processes a Query)
```
Query arrives from MARS
  ↓
Claude Query Intelligence (Opus 4.6 analyzes query → enriched search plan)
  ↓
Query Decomposition (multi-part → sub-queries)
  ↓
Wave 1: 5-Leg Parallel Retrieval
  Leg 1: Exact entity lookup (entity_lookup table in Neo4j)
  Leg 2: Personalized PageRank (NetworkX, seeds from entity+intent)
  Leg 3: BFS graph neighborhood (Neo4j, adaptive depth)
  Leg 4: Vector similarity (Qdrant, 1536d cosine)
  Leg 5: Lexical search (MySQL LIKE + keyword matching)
  ↓
RRF Fusion (weighted: exact 2.0, PPR 1.8, graph 1.5, vector 1.0, lexical 0.8)
  ↓
Wave 2: Deep GraphRAG (conditional, for complex queries)
  ↓
Wave 3: LangGraph Adaptive Retrieve + Neo4j Chain Scoring
  ↓
Wave 4: Neo4j Weighted Dijkstra (strongest relationship paths)
  ↓
Claude Cross-Encoder Reranking (top-20 → Claude scores relevance)
  ↓
MMR Diversity (ensure diverse evidence, not 5 similar docs)
  ↓
Parent-Child Chunking (auto-fetch parent when child matches)
  ↓
Lost-in-Middle Prevention (best evidence first AND last)
  ↓
Citation Markup [1] [2] [3]
  ↓
Wave 5: RIPER (Research → Innovate → Plan → Execute → Review)
  ↓
RALPH Self-Correction (grounding check, intent coverage)
  ↓
HallucinationGuard (BLOCK if 3+ fabricated IDs)
  ↓
Confidence Gate (< 0.3 → "I don't know")
  ↓
Response with citations
```

## CRITICAL RULES (Response Quality > Cost > Speed)

1. **Quality is #1.** Never compromise response accuracy for speed or cost.
2. **Use Claude Opus 4.6** for all LLM operations (query intelligence, reranking, response generation).
3. **Every fact must come from KB.** The LLM synthesizes — the KB provides the facts.
4. **Cite sources.** Every response must reference [1] [2] [3] from retrieved context.
5. **Say "I don't know"** when confidence < 0.3. Never guess.
6. **No hallucination.** 8 layers prevent fabricated data. BLOCK at 3+ ungrounded entities.
7. **Hinglish is pre-translated** at LIME/MARS layer. COSMOS receives clean English.
8. **Content-hash skip** on re-run. Never re-embed unchanged docs.

## Knowledge Base Quality Standards

### What Makes a Good KB Doc (for embedding)
- **200-500 tokens** per chunk (not too short = vague, not too long = diluted)
- **One concept per chunk** (not a merged dump of 5 different topics)
- **Rendered, not raw YAML** (typed renderers convert YAML → clean retrieval text)
- **Tagged with query_mode** (lookup / diagnose / act / explain / routing)
- **Cross-linked via stable IDs** (pillar_1_schema/tables/orders, not free text)

### What Makes a Good Action Contract (P6)
- 11 files per action: index, contract, intent_map, permissions, data_access, execution_graph, failure_modes, observability, examples, eval_cases, rollback
- **intent_map** must have English + Hinglish + ICRM shorthand + negative phrases
- **execution_graph** must be step-by-step (validate → check → execute → side_effects)
- **eval_cases** must have positive, ambiguous, unsafe, wrong_tenant, regression tests

### What Makes a Good Workflow Runbook (P7)
- 13 files per workflow: index, overview, state_machine, entrypoints, decision_matrix, action_map, data_flow, ui_map, async_map, operator_playbook, user_language, exception_paths, eval_cases
- **state_machine** must have allowed transitions + invalid transitions
- **operator_playbook** must have: diagnose → verify → act → escalate
- **user_language** must have seller wording + ICRM wording + Hinglish variants

## Goals for Knowledge Base Improvement

### Goal 1: Opus-Generated High-Quality KB Docs
Use Claude Opus 4.6 to generate KB content that is:
- **Retrieval-optimized** (written for embedding, not for humans reading docs)
- **Semantically precise** (one clear concept per chunk, no ambiguity)
- **Example-rich** (real ICRM operator phrasing, not generic)
- **Cross-linked** (every doc references related tables, APIs, actions, workflows)

### Goal 2: Multi-Repo Coverage
Extend full 8-pillar coverage from MultiChannel_API to ALL repos:
- shiprocket-channels: needs P3 (APIs), P6 (actions for channel sync)
- helpdesk: needs P3 (ticket APIs), P6 (escalation actions)
- shiprocket-go: needs P3 (Go service APIs), P5 (module docs)
- sr_login: needs P6 (auth actions), P7 (login flow workflow)

### Goal 3: Embedding Quality Over Quantity
- **Don't embed everything.** Only embed docs that help: lookup, diagnose, act, explain.
- **Quality gate**: reject < 50 chars, > 80% punctuation, stub patterns.
- **Trust score**: high-quality docs get trust_score 0.9, auto-generated get 0.5.
- **Freshness decay**: docs > 90 days old get 0.7 weight.

### Goal 4: Graph-First Retrieval
- Every KB doc becomes a graph node in Neo4j
- Cross-pillar edges: action → table (reads_table), workflow → action (uses_action)
- PPR (Personalized PageRank) finds important nodes at ANY depth
- Entity lookup for instant exact-match (AWB, order_id, company_id)

### Goal 5: Continuous Learning
- **Feedback loop**: low-confidence traces → staged KB improvements
- **Eval benchmark**: 201 ICRM eval seeds, measure recall@5 after every pipeline run
- **Auto-actions**: missing_action_candidate, add_negative_example, add_clarification_rule
- **Human review**: approve/reject staged improvements in LIME feedback panel

### Goal 6: Production-Grade Response
- **Factuality prompt**: 10 rules injected into every Claude call
- **Source attribution**: every chunk tagged with [pillar:entity_id trust=0.9]
- **Grounding check**: response terms must appear in retrieved context (≥ 30%)
- **Citation in response**: [1] [2] [3] markers so operators can verify claims
- **Confidence gating**: < 0.3 = refuse, 0.3-0.6 = uncertainty marker, > 0.6 = confident

## Current Scores

| Component | Score |
|-----------|-------|
| Knowledge Base (content) | 9.8 / 10 |
| Training Pipeline (ingestion) | 9.5 / 10 |
| Wave Execution (retrieval) | 9.5 / 10 |
| Anti-Hallucination (quality) | 9.5 / 10 |
| LIME (frontend) | 8.5 / 10 |
| MARS (platform) | 9.0 / 10 |
| COSMOS (AI brain) | 9.5 / 10 |

## Prompt for Claude to Generate KB Content

When asking Claude Opus 4.6 to generate KB content, use this system prompt:

```
You are generating knowledge base content for Shiprocket's ICRM AI copilot (COSMOS/RocketMind).

RULES:
1. Write for EMBEDDING, not for humans. Each chunk should be 200-500 tokens of focused, retrieval-optimized text.
2. One concept per chunk. Never merge multiple topics into one document.
3. Include real Shiprocket terminology: AWB, NDR, RTO, COD, ICRM, MCAPI, channel sync.
4. Include Hinglish variants where relevant: "order cancel karo", "pickup kyun nahi hua".
5. Include negative examples: "This is NOT the same as X. If user asks Y, use Z instead."
6. Link to other pillars using stable IDs: "See pillar_1_schema/tables/orders for column details."
7. For action contracts: always include preconditions, side effects, rollback, approval mode.
8. For workflow runbooks: always include state machine, valid transitions, operator playbook.
9. For eval cases: include positive, ambiguous, unsafe, and regression test cases.
10. Quality > quantity. One excellent doc beats ten mediocre ones.

FORMAT:
- YAML structure matching the pillar template
- All fields populated (no empty arrays or TODO placeholders)
- trust_score: 0.9 for human-verified, 0.7 for auto-generated
- training_ready: true
```

## File Paths (for reference)
```
KB Root:        /Users/gauravchaudhary/Documents/project/marsproject/mars/knowledge_base/shiprocket/
COSMOS Code:    /Users/gauravchaudhary/Documents/project/marsproject/cosmos/app/
MARS Code:      /Users/gauravchaudhary/Documents/project/marsproject/mars/
LIME Code:      /Users/gauravchaudhary/Documents/project/marsproject/lime/src/
Qdrant:         http://localhost:6333 (collection: cosmos_embeddings)
Neo4j:          bolt://localhost:7687 (neo4j/cosmospass123)
MySQL (MARS):   localhost:3309 (root/1fN82Avd7TT5Bad2, database: mars)
AI Gateway:     https://aigateway.shiprocket.in (key: 69cd4c62bfdaeb3be524b572)
```

# COSMOS — AI Brain for Shiprocket ICRM

**COSMOS** (codename: **RocketMind**) is the Python AI inference engine that powers Shiprocket's ICRM platform. Every question an ICRM operator, seller, or support agent asks about Shiprocket's logistics platform — order status, NDR handling, AWB tracking, pickup failures, channel sync — is answered by COSMOS.

---

## Table of Contents

- [Platform Architecture](#platform-architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Module Guide](#module-guide)
- [Query Execution Pipeline](#query-execution-pipeline)
- [Knowledge Base](#knowledge-base)
- [Anti-Hallucination System](#anti-hallucination-system)
- [API Reference](#api-reference)
- [gRPC Services](#grpc-services)
- [Getting Started](#getting-started)
- [Running Tests](#running-tests)
- [Model Routing](#model-routing)
- [Event System (Kafka)](#event-system-kafka)
- [Continuous Learning](#continuous-learning)
- [Git Standards](#git-standards)

---

## Platform Architecture

```
User (ICRM / Seller / Slack / WhatsApp)
  │
  ▼
LIME  (React — port 3003)
  │   Frontend chat, feedback panel, operator UI
  │
  ▼
MARS  (Go — port 8080)
  │   Auth · SSO · Session management · Request routing
  │   Hinglish pre-translation (COSMOS receives clean English)
  │
  ▼
COSMOS  (Python — port 10001)          ← YOU ARE HERE
  │
  ├── Claude Opus 4.6        (via AI Gateway)  — LLM inference + reranking
  ├── text-embedding-3-small (via AI Gateway)  — 1536d vector embeddings
  ├── Qdrant     :6333       — vector similarity store
  ├── Neo4j      :7687       — knowledge graph (nodes + edges)
  ├── MySQL      :3309       — sessions, audit, eval seeds (MARS DB)
  ├── Kafka      :9094       — event streaming (order webhooks, feedback)
  └── S3   ap-southeast-1   — KB YAML sync, training exports, backups
```

COSMOS is the only service that talks to Claude. MARS handles everything upstream (auth, session, routing). LIME handles everything downstream (rendering, feedback collection).

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI 0.115 (async) |
| Language | Python 3.12 |
| LLM | Claude Opus 4.6 / Sonnet 4.6 / Haiku 4.5 via Anthropic SDK 0.40 |
| Vector DB | Qdrant (1536d cosine, `text-embedding-3-small`) |
| Graph DB | Neo4j 5 (bolt driver, Personalized PageRank, BFS, Dijkstra) |
| Relational DB | MySQL via SQLAlchemy asyncio + aiomysql |
| Graph library | NetworkX 3.3 (in-memory PPR, graph scoring) |
| LangGraph | 0.2.74 — adaptive retrieval pipeline orchestration |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) via AI Gateway |
| Comms | REST (FastAPI) + gRPC (grpcio 1.78) |
| Event bus | Kafka (aiokafka, SASL/SCRAM-SHA-512 for staging) |
| Observability | structlog + Prometheus metrics + OpenTelemetry tracing |
| Testing | pytest + pytest-asyncio |
| Lint / Type | ruff + mypy |

---

## Project Structure

```
cosmos/
├── app/
│   ├── main.py                 # FastAPI entrypoint — lifespan, wiring, startup
│   ├── config.py               # All env vars (pydantic BaseSettings)
│   │
│   ├── api/                    # REST API layer
│   │   ├── routes.py           # Router registration
│   │   └── endpoints/          # One file per domain (chat, brain, training, …)
│   │
│   ├── brain/                  # Core RAG orchestration
│   │   ├── pipeline.py         # Main retrieval + generation pipeline
│   │   ├── router.py           # Query routing (KB / code / DB / action)
│   │   ├── indexer.py          # Document indexing + graph node creation
│   │   ├── cache.py            # SemanticCache (embedding-based dedup)
│   │   ├── graph.py            # In-memory graph operations
│   │   ├── grel.py             # GREL: Graph Retrieval Engine Layer
│   │   ├── hierarchy.py        # Parent-child chunk hierarchy
│   │   ├── tournament.py       # TournamentEngine (multi-model voting)
│   │   ├── wiring.py           # Wire all brain components together
│   │   └── setup.py            # Brain factory (create_brain)
│   │
│   ├── engine/                 # Inference engine + AI tools
│   │   ├── react.py            # ReActEngine (Reason + Act loop)
│   │   ├── riper.py            # RIPER: Research→Innovate→Plan→Execute→Review
│   │   ├── ralph.py            # RALPH: self-correction + grounding check
│   │   ├── wave_executor.py    # 5-leg parallel Wave retrieval
│   │   ├── classifier.py       # Intent classifier (Haiku)
│   │   ├── llm_client.py       # LLM client (API / CLI / hybrid mode)
│   │   ├── model_router.py     # Task → model routing (Haiku/Sonnet/Opus)
│   │   ├── confidence.py       # Confidence scoring + gating (< 0.3 = refuse)
│   │   ├── grounding.py        # Grounding check (response terms in context)
│   │   ├── planner.py          # Multi-step query decomposition
│   │   ├── proactive_monitor.py# Background anomaly detection (15 min cycle)
│   │   ├── codebase_intelligence.py # Tier 2: code retrieval (pre-indexed)
│   │   ├── safe_query_executor.py   # Tier 3: safe live DB queries via MARS
│   │   ├── cost_tracker.py     # Per-session cost tracking + budget enforcement
│   │   ├── audit.py            # Action audit trail
│   │   ├── approval.py         # Approval-mode gate for destructive actions
│   │   └── circuit_breaker.py  # Upstream failure circuit breaker
│   │
│   ├── services/               # Business logic + data services
│   │   ├── query_orchestrator.py    # Hybrid Query Orchestrator (master router)
│   │   ├── vectorstore.py      # Qdrant vector store service
│   │   ├── neo4j_graph.py      # Neo4j graph operations
│   │   ├── training_pipeline.py# Master KB ingestion orchestrator
│   │   ├── kb_ingestor.py      # YAML → chunks → embeddings
│   │   ├── kb_watcher.py       # Watchdog: incremental re-ingest on YAML change
│   │   ├── kb_file_index.py    # Content-hash tracker (skip unchanged files)
│   │   ├── canonical_ingestor.py    # Canonical KB doc format ingestor
│   │   ├── chunker.py          # 200-500 token chunking with parent-child
│   │   ├── reranker.py         # Claude cross-encoder reranking (top-20)
│   │   ├── hyde.py             # HyDE: hypothetical document expansion
│   │   ├── embedding_backends.py    # Embedding provider abstraction
│   │   ├── feedback_loop.py    # Feedback → staged KB improvements
│   │   ├── kb_feedback_consumer.py  # RALPH→KB Kafka feedback consumer
│   │   ├── graphrag.py         # GraphRAG: graph-augmented retrieval
│   │   ├── page_intelligence.py# Pillar 4: page/field/UI intelligence
│   │   ├── neighbor_expander.py# Graph neighborhood expansion
│   │   ├── wave_trace.py       # Wave execution trace logging
│   │   ├── kb_eval.py          # Eval benchmark runner (201 seeds)
│   │   ├── s3_client.py        # S3: KB sync + training export + backup
│   │   ├── workflow_settings.py# CosmosSettingsCache (tunable weights)
│   │   └── sandbox.py          # Safe action sandbox (dry-run mode)
│   │
│   ├── graph/                  # Low-level graph + retrieval
│   │   ├── retrieval.py        # 5-leg retrieval + RRF fusion
│   │   ├── ingest.py           # Graph node/edge ingestion
│   │   ├── context.py          # Context window assembly
│   │   ├── strategy.py         # Retrieval strategy selection
│   │   ├── quality.py          # Quality scoring
│   │   └── langgraph_pipeline.py  # LangGraph adaptive retrieval chain
│   │
│   ├── guardrails/             # Safety + compliance filters
│   │   ├── setup.py            # create_guardrail_pipeline factory
│   │   ├── base.py             # Base guardrail class
│   │   ├── rules.py            # Core safety rules
│   │   ├── kb_guardrails.py    # KB safety index (blast_radius, PII, approval)
│   │   ├── mars_safety.py      # MARS-specific safety rules
│   │   ├── advanced_guards.py  # HallucinationGuard, ConfidenceGate
│   │   ├── compliance_guards.py# GDPR / PII compliance
│   │   └── context_tagger.py   # Tag context chunks with trust metadata
│   │
│   ├── clients/                # External API clients
│   │   ├── mcapi.py            # MultiChannel API (apiv2.shiprocket.in)
│   │   ├── elk.py              # Elasticsearch / ELK client
│   │   ├── mars.py             # MARS Go backend HTTP client
│   │   ├── sso_auth.py         # Shiprocket SSO auth client
│   │   ├── voyage_client.py    # Voyage AI embeddings (fallback)
│   │   └── auth_aware_client.py# JWT-forwarding HTTP client
│   │
│   ├── tools/                  # AI tool implementations (ReAct tool use)
│   │   ├── registry.py         # ToolRegistry
│   │   ├── setup.py            # create_tool_registry factory
│   │   ├── read_tools.py       # Read-only tools (search, lookup, explain)
│   │   └── write_tools.py      # Write tools (cancel, reattempt — approval-gated)
│   │
│   ├── learning/               # Continuous learning module
│   │   ├── continuous.py       # Continuous learning orchestrator
│   │   ├── feedback.py         # Feedback ingestion + classification
│   │   ├── knowledge.py        # Knowledge update from feedback
│   │   ├── auto_actions.py     # Auto KB improvements (missing_action, etc.)
│   │   ├── dpo_pipeline.py     # DPO training data generation
│   │   ├── analytics.py        # Learning analytics + metrics
│   │   └── collector.py        # Training trace collector
│   │
│   ├── events/                 # Kafka event handlers
│   │   ├── kafka_bus.py        # EventBus + Topic registry
│   │   ├── handlers.py         # query_completed, learning_insight, feedback, kb_updated
│   │   └── order_handler.py    # WooCommerce order webhook handler
│   │
│   ├── grpc_servicers/         # gRPC service implementations (port 50051)
│   │   ├── training_servicer.py     # TriggerEmbeddingTraining
│   │   ├── vectorstore_servicer.py  # VectorStore CRUD
│   │   ├── graphrag_servicer.py     # GraphRAG queries
│   │   ├── sandbox_servicer.py      # Sandbox execution
│   │   └── report_servicer.py       # Report generation
│   │
│   ├── db/                     # Database layer
│   │   └── session.py          # SQLAlchemy async session, init_db, close_db
│   │
│   ├── middleware/             # FastAPI middleware
│   │   ├── metrics.py          # Prometheus metrics middleware
│   │   └── rate_limiter.py     # HTTP rate limiter (60 req/min default)
│   │
│   ├── monitoring/             # Observability
│   │   ├── metrics.py          # Prometheus metric definitions
│   │   └── otel_tracing.py     # OpenTelemetry tracer setup
│   │
│   └── grpc_gen/               # Generated gRPC protobuf stubs (do not edit)
│
├── tests/                      # pytest test suite (mirrors app/ structure)
├── docs/                       # Architecture docs, error codes, KB plans
├── data/                       # Local training data exports
├── .claude/
│   ├── hooks/                  # Orbit lifecycle hooks (pre-commit, stop, etc.)
│   └── rules/                  # Coding conventions (model-routing.md, git-standards.md)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── metadata.yml                # Orbit IDP contract (platform.shiprocket.com/v1alpha1)
├── .env                        # Local secrets (never commit)
├── .env.example                # Full annotated config reference
└── CLAUDE.md                   # AI assistant instructions for this repo
```

---

## Module Guide

### `app/brain/` — RAG Orchestration Core

The brain wires together all retrieval and generation components at startup (`setup.py` → `wiring.py`). The central pipeline (`pipeline.py`) receives a query, runs multi-leg retrieval, fuses results, reranks, and generates the response.

**Key concepts:**
- `TournamentEngine` — runs the same query through multiple models/strategies, picks the best response by confidence + grounding score
- `SemanticCache` — embedding-based query dedup; identical-intent queries skip retrieval
- `GREL (GRELEngine)` — Graph Retrieval Engine Layer; orchestrates Neo4j traversal strategies
- `BrainRouter` — routes queries to one of 4 tiers: KB RAG / Codebase / Safe DB / Action

### `app/engine/` — Inference Engine

Houses all the AI reasoning machinery:

| Component | Role |
|---|---|
| `ReActEngine` | Reason + Act loop; uses tool_registry for live data fetches |
| `RIPER` | 5-phase reasoning: Research → Innovate → Plan → Execute → Review |
| `RALPH` | Self-correction: checks response against source context, flags gaps |
| `WaveExecutor` | Runs 5 retrieval legs in parallel (see Pipeline section) |
| `IntentClassifier` | Haiku-powered: routes query to right tier before heavy retrieval |
| `ConfidenceGate` | Score < 0.3 → refuse with "I don't know" |
| `GroundingChecker` | ≥ 30% of response terms must appear in retrieved context |
| `CostTracker` | Hard stops at session ($1) and daily ($50) budget ceilings |
| `ProactiveMonitor` | Background loop (every 15 min) — detects anomalies, surfaces alerts |

### `app/services/` — Data + Pipeline Services

| Service | Role |
|---|---|
| `QueryOrchestrator` | Master hybrid orchestrator: Tier 1 (KB) → Tier 2 (code) → Tier 3 (DB) |
| `VectorStoreService` | Qdrant wrapper: upsert, search, delete, ensure_schema |
| `TrainingPipeline` | KB ingestion master: reads YAMLs → chunks → embeds → indexes |
| `KBIngestor` | Per-pillar YAML reader with quality gate (rejects stubs, < 50 chars) |
| `KBWatcher` | Watchdog on KB_PATH; triggers incremental re-ingest on file change |
| `KBFileIndex` | Content-hash table in MySQL; skips re-embedding unchanged files |
| `Chunker` | 200-500 token chunks, parent-child hierarchy, pillar-aware splitting |
| `Reranker` | Claude cross-encoder: scores top-20 chunks by relevance to query |
| `HyDE` | Hypothetical Document Expansion: generate fake answer → embed → search |
| `GraphRAG` | Loads full KB graph into memory; PPR + BFS traversal |
| `PageIntelligence` | Pillar 4: maps UI fields → API endpoints → DB columns |
| `FeedbackLoop` | Low-confidence traces → staged KB improvements |
| `S3Client` | KB sync (read/write), training export (write), embedding backup (write) |

### `app/guardrails/` — Safety Pipeline

Every LLM call passes through the guardrail pipeline before and after generation:

1. **`kb_guardrails.py`** — Loads `blast_radius`, `PII_fields`, `approval_mode` from KB P6 action contracts
2. **`advanced_guards.py`** — `HallucinationGuard`: blocks if 3+ entity IDs in response are not in retrieved context
3. **`compliance_guards.py`** — GDPR/PII: redacts sensitive fields from response
4. **`mars_safety.py`** — Shiprocket-specific rules (no order data from wrong tenant, etc.)
5. **`rules.py`** — Core safety rules (no SQL injection, no credential exposure)

### `app/learning/` — Continuous Learning

COSMOS learns from every interaction:
- Low-confidence responses trigger `auto_actions.py` → `missing_action_candidate`, `add_negative_example`, `add_clarification_rule`
- Operator feedback (thumbs up/down, corrections) flows through Kafka → `feedback.py` → `knowledge.py`
- DPO training pairs (preferred/rejected responses) generated by `dpo_pipeline.py` → exported to S3

---

## Query Execution Pipeline

```
Query arrives from MARS
  │
  ▼
1. IntentClassifier (Haiku)
   → classify: lookup / diagnose / act / explain / routing
  │
  ▼
2. Query Decomposition (Planner)
   → multi-part queries split into sub-queries
  │
  ▼
3. Wave 1: 5-Leg Parallel Retrieval
   ├── Leg 1: Exact entity lookup      (entity_lookup table, Neo4j)
   ├── Leg 2: Personalized PageRank    (NetworkX, seeds from entity + intent)
   ├── Leg 3: BFS graph neighborhood   (Neo4j, adaptive depth 1-3)
   ├── Leg 4: Vector similarity        (Qdrant, 1536d cosine, top-20)
   └── Leg 5: Lexical search           (MySQL LIKE + keyword match)
  │
  ▼
4. RRF Fusion (weighted scores)
   exact=2.0 · PPR=1.8 · graph=1.5 · vector=1.0 · lexical=0.8
  │
  ▼
5. Wave 2: Deep GraphRAG (conditional — only for complex/multi-hop queries)
  │
  ▼
6. Wave 3: LangGraph Adaptive Retrieve + Neo4j Chain Scoring
  │
  ▼
7. Wave 4: Neo4j Weighted Dijkstra (strongest relationship paths)
  │
  ▼
8. Claude Cross-Encoder Reranking (top-20 → Claude scores relevance → top-5)
  │
  ▼
9. MMR Diversity Filter (ensure 5 different docs, not 5 duplicates)
  │
  ▼
10. Parent-Child Chunk Expansion (auto-fetch parent when child matches)
  │
  ▼
11. Lost-in-Middle Prevention (best evidence: positions 1 AND last in context)
  │
  ▼
12. Citation Markup [1] [2] [3] injected into context
  │
  ▼
13. Wave 5: RIPER reasoning (Research → Innovate → Plan → Execute → Review)
  │
  ▼
14. RALPH Self-Correction (grounding check, intent coverage, gap detection)
  │
  ▼
15. HallucinationGuard (BLOCK if 3+ entity IDs not in source context)
  │
  ▼
16. ConfidenceGate
    < 0.3 → "I don't know, please contact support"
    0.3–0.6 → response + uncertainty marker
    > 0.6 → confident response with citations
  │
  ▼
Response with [1] [2] [3] citations → MARS → LIME
```

---

## Knowledge Base

The KB lives at `KB_PATH` (configured in `.env`) and is structured across 8 Shiprocket repos and 8 pillars:

```
knowledge_base/shiprocket/
  MultiChannel_API/    → 44,094 YAML files — PRIMARY (all 8 pillars)
  SR_Web/              → Seller web panel (P1, P4, P5)
  MultiChannel_Web/    → ICRM admin panel (P1, P4, P5)
  shiprocket-channels/ → Channel integrations (Shopify, WooCommerce, Amazon)
  helpdesk/            → Support ticket system
  shiprocket-go/       → Go microservices
  sr_login/            → Authentication service
  SR_Sidebar/          → UI sidebar component
```

**The 8 Pillars:**

| Pillar | What It Answers |
|---|---|
| P1: Schema | "What data exists?" — 676 tables, 50 columns, 105 status values |
| P3: APIs & Tools | "What API can I call?" — 5,617 endpoints |
| P4: Pages & Fields | "Where is this field in the UI?" — 24 pages, field→API→table traces |
| P5: Module Docs | "What code handles this?" — 739 files, controllers, services |
| P6: Action Contracts | "What should I do?" — 25 actions × 11 files each |
| P7: Workflow Runbooks | "Why did this happen?" — 9 workflows × 13 files each |
| P8: Negative Routing | "Don't confuse X with Y" — 100 disambiguation examples |
| Hub: Entity Summaries | "Give me everything about X" — cross-pillar summaries |

**Chunk quality gates** — a chunk is rejected if:
- Content < 50 characters
- > 80% punctuation / boilerplate
- Matches stub patterns (`TODO`, `placeholder`, `N/A`)

**Content-hash skip** — `KBFileIndex` tracks SHA-256 of every ingested file. Re-running the pipeline skips unchanged files entirely.

---

## Anti-Hallucination System

COSMOS has 8 layers preventing fabricated data:

| Layer | Mechanism |
|---|---|
| 1 | **Every fact from KB** — LLM synthesizes, KB provides facts |
| 2 | **Factuality prompt** — 10 rules injected into every Claude call |
| 3 | **Source attribution** — every chunk tagged `[pillar:entity_id trust=0.9]` |
| 4 | **GroundingChecker** — ≥ 30% of response terms must appear in context |
| 5 | **RALPH** — self-correction pass before final response |
| 6 | **HallucinationGuard** — BLOCK if 3+ ungrounded entity IDs |
| 7 | **ConfidenceGate** — < 0.3 → refuse with "I don't know" |
| 8 | **Citation markers** — [1] [2] [3] so operators can verify every claim |

---

## API Reference

Base path: `/cosmos/api/v1`

| Endpoint | Method | Description |
|---|---|---|
| `/cosmos/health` | GET | Health check + component status |
| `/cosmos/metrics` | GET | Prometheus metrics |
| `/v1/chat` | POST | Standard RAG chat (KB retrieval) |
| `/v1/hybrid-chat` | POST | Hybrid chat (KB + code + DB tiers) |
| `/v1/brain/query` | POST | Direct brain query with wave trace |
| `/v1/brain/index` | POST | Index documents into the brain |
| `/v1/sessions` | GET/POST | Session management |
| `/v1/feedback` | POST | Submit operator feedback |
| `/v1/knowledge` | GET/POST | KB document CRUD |
| `/v1/training` | POST | Trigger training pipeline |
| `/v1/training-pipeline` | GET/POST | Full KB ingestion pipeline |
| `/v1/vectorstore` | GET/POST/DELETE | Qdrant collection management |
| `/v1/graphrag` | POST | GraphRAG query |
| `/v1/page-intelligence` | GET/POST | UI page / field intelligence |
| `/v1/learning` | GET | Learning insights + analytics |
| `/v1/actions` | POST | Execute actions (approval-gated) |
| `/v1/agents` | POST | Agent forge + agent registry |
| `/v1/tools` | GET/POST | Tool registry |
| `/v1/costs` | GET | Cost tracking dashboard |
| `/v1/tournament` | POST | A/B model tournament |
| `/v1/sandbox` | POST | Dry-run action sandbox |
| `/v1/report` | POST | Report agent |
| `/v1/cosmos-settings` | GET/PUT | Tunable retrieval weights |
| `/v1/admin` | GET/POST | Admin operations |

---

## gRPC Services

Port: `50051`

| Service | Proto | Description |
|---|---|---|
| `TrainingService` | `TriggerEmbeddingTraining` | Trigger KB ingestion from MARS |
| `VectorStoreService` | CRUD | Qdrant collection management |
| `GraphRAGService` | `Query` | Graph-augmented retrieval |
| `SandboxService` | `Execute` | Safe action sandbox |
| `ReportService` | `Generate` | Report generation |

Proto definitions: `app/grpc_gen/` (generated — do not edit)

---

## Getting Started

### Prerequisites

- Python 3.12
- Qdrant running on `:6333`
- Neo4j running on `:7687`
- MySQL running on `:3309` (MARS DB)
- Kafka (optional for local dev — set `KAFKA_ENABLED=false`)

### Local Setup

```bash
# Clone and enter
cd cosmos

# Create virtualenv
python3.12 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — fill in REQUIRED values (DB passwords, API keys)

# Start COSMOS
uvicorn app.main:app --reload --port 10001
```

### Verify startup

```bash
curl http://localhost:10001/cosmos/health
```

Expected:
```json
{
  "status": "ok",
  "brain": "loaded",
  "vectorstore": "ready",
  "neo4j": "connected",
  "kafka": "running"
}
```

### Docker (local infra only)

```bash
# Start Qdrant + Neo4j + Redis
docker compose up -d

# Run COSMOS directly (recommended for dev — hot reload)
uvicorn app.main:app --reload --port 10001
```

---

## Running Tests

```bash
# All tests
python -m pytest tests/ -x -q --tb=short

# Specific module
python -m pytest tests/test_brain.py -v
python -m pytest tests/test_retrieval.py -v

# With coverage
python -m pytest tests/ --cov=app --cov-report=term-missing
```

**Before every commit (enforced by pre-commit hook):**
```bash
python -m pytest tests/ -x -q   # must pass
ruff check app/                   # no lint errors
mypy app/ --ignore-missing-imports # no type errors
```

---

## Model Routing

COSMOS routes tasks to the right Claude model based on cost vs. capability:

| Task | Model | Cost Factor |
|---|---|---|
| Intent classification, routing | `claude-haiku-4-5-20251001` | 1× |
| Code generation, API endpoints, tests | `claude-sonnet-4-6` | 5× |
| KB content generation, graph schema | `claude-opus-4-6` | 25× |
| Security / guardrails review | `claude-opus-4-6` | 25× |
| Cross-encoder reranking | `claude-opus-4-6` | 25× |

Rule: Opus is reserved for < 10% of requests. Sonnet is the default. Haiku for fast triage.

`LLM_MODE` in `.env` controls how Claude is called:
- `cli` — uses the local `claude` binary (Claude Max plan, zero API cost, recommended for local dev)
- `api` — direct Anthropic API (requires `ANTHROPIC_API_KEY`)
- `hybrid` — cli for long reasoning tasks, api for short classification

---

## Event System (Kafka)

COSMOS is both a Kafka consumer and producer:

**Consumes:**

| Topic | Handler | Description |
|---|---|---|
| `cosmos.query_completed` | `handle_query_completed` | Log trace, update analytics |
| `cosmos.learning_insight` | `handle_learning_insight` | Ingest learning signal |
| `cosmos.feedback_submitted` | `handle_feedback` | Process operator feedback |
| `cosmos.kb_updated` | `handle_kb_updated` | Trigger incremental KB re-ingest |
| `sc_webhook_orders_wc` | `handle_order_webhook` | WooCommerce order events from Channels |

**For local dev** (no Kafka available):
```env
KAFKA_ENABLED=false
KAFKA_SECURITY_PROTOCOL=PLAINTEXT
```

---

## Continuous Learning

COSMOS improves itself from every interaction:

```
Operator gives feedback (thumbs down / correction)
  │
  ▼
Kafka: cosmos.feedback_submitted
  │
  ▼
FeedbackLoop classifies: wrong_answer / missing_knowledge / hallucination
  │
  ▼
AutoActions generates staged KB improvement:
  missing_action_candidate → draft P6 action contract
  add_negative_example     → add disambiguation entry
  add_clarification_rule   → update intent_map
  │
  ▼
Human review in LIME feedback panel (approve / reject)
  │
  ▼
Approved → merged into KB → re-indexed → eval benchmark run
```

**Eval benchmark:** 201 ICRM operator seeds in MySQL. `KBEval` measures `recall@5` after every pipeline run. Score < 0.85 blocks deployment.

---

## Git Standards

Branch naming:
```
feat/NNN-short-description      # New feature
fix/NNN-short-description       # Bug fix
arch/NNN-short-description      # Architecture change
chore/NNN-short-description     # Deps, tooling
refactor/NNN-short-description  # No behavior change
```

Commit format:
```
<type>(<scope>): <what was done> (#NNN)

Types:  feat · fix · arch · refactor · test · docs · chore · perf · security
Scopes: brain · engine · graph · learning · api · grpc · guardrails · db · monitoring · ci
```

Example:
```
feat(brain): add 5-leg parallel wave retrieval with RRF fusion (#42)
fix(engine): handle Qdrant timeout with exponential backoff (#56)
arch(guardrails): add HallucinationGuard with entity grounding check (#70)
```

Rules:
- Always cut branch from latest `develop` — never commit directly to `main` or `develop`
- Squash merge only
- All CI gates must pass before requesting review

---

## Current System Scores

| Component | Score |
|---|---|
| Knowledge Base (content) | 9.8 / 10 |
| Training Pipeline (ingestion) | 9.5 / 10 |
| Wave Execution (retrieval) | 9.5 / 10 |
| Anti-Hallucination (quality) | 9.5 / 10 |
| COSMOS (AI brain) | 9.5 / 10 |

---

## Key Contacts

- **Team:** AI Platform (`platform.shiprocket.com/team: ai-platform`)
- **Orbit version:** 2.8.1
- **Issues / PRs:** `gvchaudhary22/cosmos`
- **Docs:** `docs/` — error codes, KB architecture PRD, training plans, decision records

<div align="center">
  <h1>COSMOS — AI Brain for Shiprocket ICRM</h1>
  <p><strong>Production-grade, multi-wave RAG engine powering every AI answer on the Shiprocket platform.</strong></p>

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-UNLICENSED-red.svg)](#)
[![Team](https://img.shields.io/badge/team-ai--platform-black)](#)

</div>

---

## What is COSMOS?

COSMOS (codename: **RocketMind**) is the Python AI inference engine that powers Shiprocket's ICRM platform. Every question an ICRM operator, seller, or support agent asks — order status, NDR handling, AWB tracking, pickup failures, channel sync — is answered by COSMOS.

**Key capabilities:** 5-leg parallel wave retrieval · GraphRAG + PPR traversal · Claude cross-encoder reranking · 8-layer anti-hallucination · KB ingestion pipeline · RIPER reasoning · RALPH self-correction · continuous learning from feedback · cost-governed model routing

COSMOS is the only service in the stack that talks directly to Claude. MARS handles everything upstream (auth, session, routing). LIME handles everything downstream (rendering, feedback collection).

### Architecture

```
User (ICRM / Seller / Slack / WhatsApp)
  │
  ▼
LIME  (React — port 3003)
  │   Frontend chat, feedback panel, operator UI
  │
  ▼
MARS  (Go — port 8080)
  │   Auth · SSO · Session · Request routing
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
  └── S3   ap-south-1        — KB YAML sync, training exports, backups
```

---

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/architecture.md](docs/architecture.md) | Control plane, three pillars, wave execution model, model routing, Nexus, Sentinel CI |
| [docs/concepts.md](docs/concepts.md) | Agents, skills, workflows, STATE.md, hooks, Agent Forge, KB |
| [docs/token-optimization.md](docs/token-optimization.md) | Six-layer token strategy, cost estimates, model profiles |
| [docs/security-model.md](docs/security-model.md) | Integrity verification, hook safety, prompt injection defense, SCA, OWASP mapping |
| [docs/runtime-adapters.md](docs/runtime-adapters.md) | Claude Code (native), Codex (stable), other runtime adapter contracts |
| [docs/playbooks.md](docs/playbooks.md) | Runbooks for startup failures, recall drops, hallucination spikes, Kafka lag |
| [docs/playbooks/self-orchestration.md](docs/playbooks/self-orchestration.md) | How COSMOS uses its own workflows to evolve itself safely |
| [docs/evals.md](docs/evals.md) | Eval framework, recall@5 methodology, CI gate, EVAL-REPORT format |
| [docs/eval-dataset.md](docs/eval-dataset.md) | 201 ICRM seed queries for regression testing |
| [docs/mcp-guide.md](docs/mcp-guide.md) | MCP server integration for Claude Code and Cursor |
| [docs/error-codes.md](docs/error-codes.md) | ERR-COSMOS-NNN registry with runbooks |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to add agents, skills, KB content, API endpoints |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting, threat model, security gates |
| [rocketmind.registry.json](rocketmind.registry.json) | Machine-readable agent + skill + workflow registry |
| [cosmos.config.json](cosmos.config.json) | Runtime config: model routing, RRF weights, wave settings |
| [rocketmind.config.schema.json](rocketmind.config.schema.json) | JSON schema for config validation |
| [cosmos.integration.json](cosmos.integration.json) | COSMOS Nexus integration — registers COSMOS in multi-repo orchestration |
| [templates/rocketmind.base.md](templates/rocketmind.base.md) | Source template for CLAUDE.md and INSTRUCTIONS.md |
| [.claude/agents/](.claude/agents/) | 11 specialist agent definitions |
| [.claude/skills/](.claude/skills/) | 19 reusable process skills |
| [.claude/hooks/](.claude/hooks/) | Lifecycle and safety gate hooks |
| [.claude/commands/cosmos/](.claude/commands/cosmos/) | /cosmos:* slash command surface (16 individual commands) |

---

## Install

**Prerequisites:** Python 3.12, Qdrant on `:6333`, Neo4j on `:7687`, MySQL on `:3309`.

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
npm start                              # via npm (recommended)
# or directly:
uvicorn app.main:app --reload --port 10001
```

Verify: `curl http://localhost:10001/cosmos/health`

**Local infra (Docker):**

```bash
# Start Qdrant + Neo4j + Redis
docker compose up -d
```

---

## Slash commands

```
/cosmos:new         Start a new project from scratch
/cosmos:plan        Research + spec + task breakdown
/cosmos:build       Execute phase with parallel wave architecture
/cosmos:verify      Test + UAT + review
/cosmos:ship        PR + deploy + release
/cosmos:next        Auto-detect and run the next step
/cosmos:quick       Ad-hoc task with full quality guarantees
/cosmos:riper       Research→Innovate→Plan→Execute→Review
/cosmos:forge       Build a new specialized agent on demand
/cosmos:review      Code + architecture review
/cosmos:audit       Security audit (OWASP/STRIDE)
/cosmos:debug       Systematic root-cause debugging
/cosmos:resume      Reload STATE.md after compaction or new session
/cosmos:progress    Current project status
/cosmos:help        All commands and usage
```

All `/cosmos:*` commands route through RocketMind's workflow engine. Use in a Claude Code session.

---

## Documentation

| Doc | Contents |
|-----|----------|
| [CLAUDE.md](CLAUDE.md) | AI assistant instructions, architecture rules, completion gate |
| [.claude/rules/model-routing.md](.claude/rules/model-routing.md) | Task → model routing table (Haiku / Sonnet / Opus) |
| [.claude/rules/git-standards.md](.claude/rules/git-standards.md) | Branch naming, commit format, PR rules |
| [docs/](docs/) | Architecture docs, error codes (ERR-COSMOS-NNN), KB plans |
| [.env.example](.env.example) | Full annotated config reference |
| [rocketmind.registry.json](rocketmind.registry.json) | Agent + skill + workflow registry |

---

## Project Structure

```
cosmos/
├── app/
│   ├── main.py                      # FastAPI entrypoint — lifespan, wiring, startup
│   ├── config.py                    # All env vars (pydantic BaseSettings + dotenv)
│   │
│   ├── api/
│   │   ├── routes.py                # Router registration — wires all endpoint modules
│   │   └── endpoints/
│   │       ├── chat.py              # /v1/chat — standard RAG chat
│   │       ├── hybrid_chat.py       # /v1/hybrid-chat — KB + code + DB tiers
│   │       ├── brain.py             # /v1/brain/query — direct brain access
│   │       ├── training_pipeline.py # /v1/training-pipeline — KB ingestion
│   │       ├── vectorstore.py       # /v1/vectorstore — Qdrant management
│   │       ├── graphrag.py          # /v1/graphrag — GraphRAG query
│   │       ├── feedback.py          # /v1/feedback — operator feedback
│   │       ├── actions.py           # /v1/actions — approval-gated writes
│   │       ├── cosmos_cmd.py        # /cosmos/api/v1/cmd/* — RocketMind commands
│   │       ├── cosmos_settings.py   # /v1/cosmos-settings — tunable weights
│   │       ├── costs.py             # /v1/costs — cost tracking dashboard
│   │       ├── tournament.py        # /v1/tournament — A/B model tournament
│   │       └── sandbox.py           # /v1/sandbox — dry-run action sandbox
│   │
│   ├── brain/                       # RAG orchestration core
│   │   ├── setup.py                 # create_brain() factory
│   │   ├── wiring.py                # Wire all brain components together
│   │   ├── pipeline.py              # Main retrieval + generation pipeline
│   │   ├── router.py                # BrainRouter — KB / code / DB / action tiers
│   │   ├── indexer.py               # Document indexing + graph node creation
│   │   ├── cache.py                 # SemanticCache — embedding-based dedup
│   │   ├── graph.py                 # In-memory graph operations
│   │   ├── grel.py                  # GREL: Graph Retrieval Engine Layer
│   │   ├── hierarchy.py             # Parent-child chunk hierarchy
│   │   ├── tournament.py            # TournamentEngine — multi-model voting
│   │   └── ...
│   │
│   ├── engine/                      # Inference engine + AI reasoning
│   │   ├── react.py                 # ReActEngine — Reason + Act loop
│   │   ├── riper.py                 # RIPER: Research→Innovate→Plan→Execute→Review
│   │   ├── ralph.py                 # RALPH: self-correction + grounding check
│   │   ├── wave_executor.py         # 5-leg parallel Wave retrieval
│   │   ├── classifier.py            # IntentClassifier (Haiku)
│   │   ├── llm_client.py            # LLM client (api / cli / hybrid)
│   │   ├── model_router.py          # Task → model routing
│   │   ├── confidence.py            # ConfidenceGate (< 0.3 = refuse)
│   │   ├── grounding.py             # GroundingChecker (≥ 30% term overlap)
│   │   ├── planner.py               # Multi-step query decomposition
│   │   ├── proactive_monitor.py     # Background anomaly detection (15 min)
│   │   ├── cost_tracker.py          # Session ($1) + daily ($50) budget enforcement
│   │   ├── audit.py                 # Action audit trail
│   │   ├── approval.py              # Approval-mode gate for destructive actions
│   │   └── circuit_breaker.py       # Upstream failure circuit breaker
│   │
│   ├── services/                    # Data + pipeline services
│   │   ├── query_orchestrator.py    # Master hybrid orchestrator
│   │   ├── vectorstore.py           # Qdrant wrapper: upsert, search, delete
│   │   ├── neo4j_graph.py           # Neo4j graph operations
│   │   ├── training_pipeline.py     # KB ingestion master orchestrator
│   │   ├── kb_ingestor.py           # YAML → chunks → embeddings
│   │   ├── kb_watcher.py            # Watchdog: incremental re-ingest on file change
│   │   ├── kb_file_index.py         # Content-hash tracker (skip unchanged files)
│   │   ├── chunker.py               # 200-500 token chunks + parent-child hierarchy
│   │   ├── reranker.py              # Claude cross-encoder (top-20 → top-5)
│   │   ├── hyde.py                  # HyDE: hypothetical document expansion
│   │   ├── graphrag.py              # GraphRAG: full-graph PPR + BFS traversal
│   │   ├── feedback_loop.py         # Low-confidence traces → staged KB improvements
│   │   ├── s3_client.py             # KB sync, training export, embedding backup
│   │   ├── workflow_settings.py     # CosmosSettings (tunable retrieval weights)
│   │   └── ...
│   │
│   ├── graph/                       # Low-level retrieval engine
│   │   ├── retrieval.py             # 5-leg retrieval + RRF fusion
│   │   ├── ingest.py                # Graph node/edge ingestion
│   │   ├── context.py               # Context window assembly
│   │   ├── strategy.py              # Retrieval strategy selection
│   │   ├── quality.py               # Quality scoring
│   │   └── langgraph_pipeline.py    # LangGraph adaptive retrieval chain
│   │
│   ├── guardrails/                  # Safety pipeline
│   │   ├── setup.py                 # create_guardrail_pipeline() factory
│   │   ├── kb_guardrails.py         # blast_radius, PII, approval_mode from KB P6
│   │   ├── advanced_guards.py       # HallucinationGuard, ConfidenceGate
│   │   ├── compliance_guards.py     # GDPR/PII compliance
│   │   ├── mars_safety.py           # Shiprocket-specific tenant safety rules
│   │   └── rules.py                 # Core safety rules
│   │
│   ├── clients/                     # External API clients
│   │   ├── mcapi.py                 # MultiChannel API (apiv2.shiprocket.in)
│   │   ├── mars.py                  # MARS Go backend HTTP client
│   │   ├── elk.py                   # Elasticsearch / ELK client
│   │   └── sso_auth.py              # Shiprocket SSO auth client
│   │
│   ├── learning/                    # Continuous learning module
│   │   ├── continuous.py            # Continuous learning orchestrator
│   │   ├── feedback.py              # Feedback ingestion + classification
│   │   ├── knowledge.py             # Knowledge update from feedback
│   │   ├── auto_actions.py          # Auto KB improvements
│   │   └── dpo_pipeline.py          # DPO training data generation
│   │
│   ├── events/                      # Kafka event handlers
│   │   ├── kafka_bus.py             # EventBus + topic registry
│   │   ├── handlers.py              # query_completed, learning_insight, feedback, kb_updated
│   │   └── order_handler.py         # WooCommerce order webhook handler
│   │
│   ├── grpc_servicers/              # gRPC service implementations (port 50051)
│   │   ├── training_servicer.py     # TriggerEmbeddingTraining
│   │   ├── vectorstore_servicer.py  # VectorStore CRUD
│   │   ├── graphrag_servicer.py     # GraphRAG queries
│   │   └── sandbox_servicer.py      # Sandbox execution
│   │
│   └── db/
│       └── session.py               # SQLAlchemy async session, init_db, close_db
│
├── tests/                           # pytest test suite (mirrors app/ structure)
├── docs/                            # Architecture docs, error codes, KB plans
├── .claude/
│   ├── hooks/                       # RocketMind lifecycle hooks
│   ├── rules/                       # model-routing.md, git-standards.md
│   └── commands/                    # /cosmos:* slash command definitions
├── rocketmind.registry.json         # Agent + skill + workflow registry
├── requirements.txt
├── docker-compose.yml
├── metadata.yml                     # IDP contract
└── CLAUDE.md                        # AI assistant instructions
```

---

## Query Execution Pipeline

```
HTTP POST /v1/hybrid-chat  (from MARS)
  │
  ▼
api/endpoints/hybrid_chat.py
  │
  ▼
services/query_orchestrator.py  ←── QueryOrchestrator (master router)
  │
  ├── Tier 1: KB RAG  ──────────────────────────────────────────────┐
  │     brain/router.py  (BrainRouter.classify_tier)                │
  │       │                                                          │
  │       ▼                                                          │
  │     engine/classifier.py  (IntentClassifier — Haiku)            │
  │       │   lookup / diagnose / act / explain / routing            │
  │       ▼                                                          │
  │     engine/planner.py  (multi-part → sub-queries)               │
  │       │                                                          │
  │       ▼                                                          │
  │     engine/wave_executor.py  ──── 5-leg parallel retrieval ─────┤
  │       │  ├── Leg 1  graph/retrieval.py  entity_lookup (Neo4j)   │
  │       │  ├── Leg 2  services/graphrag.py  PPR (NetworkX)        │
  │       │  ├── Leg 3  graph/retrieval.py  BFS (Neo4j)             │
  │       │  ├── Leg 4  services/vectorstore.py  cosine (Qdrant)    │
  │       │  └── Leg 5  graph/retrieval.py  LIKE (MySQL)            │
  │       │                                                          │
  │       ▼                                                          │
  │     graph/retrieval.py  (RRF fusion — weighted merge)           │
  │       │                                                          │
  │       ▼                                                          │
  │     graph/langgraph_pipeline.py  (Wave 3: LangGraph chain)      │
  │       │                                                          │
  │       ▼                                                          │
  │     services/reranker.py  (Claude cross-encoder: top-20 → top-5)│
  │       │                                                          │
  │       ▼                                                          │
  │     brain/hierarchy.py  (parent-child chunk expansion)          │
  │       │                                                          │
  │       ▼                                                          │
  │     engine/riper.py  (Wave 5: Research→Innovate→Plan→Execute→Review)
  │       │                                                          │
  │       ▼                                                          │
  │     engine/ralph.py  (self-correction + grounding check)        │
  │                                                                  │
  ├── Tier 2: Codebase Intelligence                                  │
  │     engine/codebase_intelligence.py  (pre-indexed code search)  │
  │                                                                  │
  └── Tier 3: Safe DB Query                                          │
        engine/safe_query_executor.py  (MARS DB via clients/mars.py)│
  │                                                                  ┘
  ▼
guardrails/setup.py  (create_guardrail_pipeline)
  ├── guardrails/kb_guardrails.py      blast_radius, PII, approval_mode
  ├── guardrails/advanced_guards.py    HallucinationGuard
  ├── guardrails/compliance_guards.py  GDPR/PII redaction
  └── guardrails/mars_safety.py        tenant isolation
  │
  ▼
engine/confidence.py  (ConfidenceGate)
  < 0.3 → refuse    0.3–0.6 → uncertain    > 0.6 → confident
  │
  ▼
Response with [1][2][3] citations → MARS → LIME
```

---

## KB Ingestion Pipeline

```
KB_PATH (YAML files on disk / S3)
  │
  ▼
services/kb_watcher.py           Watchdog — detects file changes
  │
  ▼
services/kb_file_index.py        Content-hash check (skip unchanged)
  │  cosmos_kb_file_index table in MySQL
  │
  ▼
services/training_pipeline.py   TrainingPipeline (master orchestrator)
  │
  ├── services/kb_ingestor.py    Read YAML → validate → extract fields
  │
  ├── services/canonical_ingestor.py  Canonical KB doc format
  │
  ├── services/chunker.py        200-500 token chunks + parent-child
  │    app/services/chunker.py   split by pillar type (P1/P3/P6/P7/Hub)
  │
  ├── services/embedding_backends.py  Call AI Gateway (text-embedding-3-small)
  │
  ├── services/vectorstore.py    Upsert into Qdrant (1536d cosine)
  │
  └── graph/ingest.py            Create Neo4j nodes + cross-pillar edges
        │  reads_table, uses_action, belongs_to_workflow
        │
        ▼
      services/neo4j_graph.py    Neo4j bolt driver

Quality gate (in kb_ingestor.py):
  ✗ reject  content < 50 chars
  ✗ reject  > 80% punctuation / boilerplate
  ✗ reject  stub patterns (TODO, placeholder, N/A)
  ✓ accept  trust_score: 0.9 (human-verified) | 0.5 (auto-generated)
```

---

## Continuous Learning Flow

```
Operator gives feedback (thumbs down / correction in LIME)
  │
  ▼
Kafka: cosmos.feedback_submitted
  │
  ▼
events/handlers.py  handle_feedback()
  │
  ▼
learning/feedback.py  FeedbackIngestion.classify()
  │   wrong_answer / missing_knowledge / hallucination
  │
  ▼
learning/auto_actions.py  AutoActions.generate()
  ├── missing_action_candidate  → draft P6 action contract
  ├── add_negative_example      → add disambiguation entry (P8)
  └── add_clarification_rule    → update intent_map
  │
  ▼
services/feedback_loop.py  persist to cosmos_feedback_traces
  │
  ▼
Human review in LIME feedback panel (approve / reject)
  │
  ▼
Approved → services/training_pipeline.py  (re-ingest)
  │
  ▼
services/kb_eval.py  KBEval.run()  recall@5 on 201 seeds
  score < 0.85 → deployment blocked
```

---

## Knowledge Base

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

**The 11 Pillars:**

| Pillar | What It Answers |
|--------|----------------|
| P1: Schema | "What data exists?" — 676 tables, 50 columns, 105 status values |
| P3: APIs & Tools | "What API can I call?" — 5,617 endpoints |
| P4: Pages & Fields | "Where is this field in the UI?" — 24 pages, field→API→table traces |
| P5: Module Docs | "What code handles this?" — 739 files, controllers, services |
| P6: Action Contracts | "What should I do?" — 25 actions × 11 files each |
| P7: Workflow Runbooks | "Why did this happen?" — 9 workflows × 13 files each |
| P8: Negative Routing | "Don't confuse X with Y" — 100 disambiguation examples |
| P9: Agent Definitions | "Which agent handles this?" — agent capabilities, routing, scope |
| P10: Skill Definitions | "What skills are available?" — skill inputs, outputs, contracts |
| P11: Tool Definitions | "What tools can be called?" — tool signatures, side effects, permissions |
| Hub: Entity Summaries | "Give me everything about X" — cross-pillar summaries |

P9/P10/P11 are ingested via `POST /cosmos/api/v1/pipeline/agents-skills-tools`. Graph nodes use `NodeType.skill` with edges `EdgeType.agent_has_skill` and `EdgeType.skill_calls_tool` (defined in `graphrag_models.py`).

---

## Why Use COSMOS

### With COSMOS

- Every query passes through 5-leg retrieval fusion — the best evidence surfaces regardless of how the question is phrased
- 8-layer anti-hallucination guarantees every claim has a source; the system refuses rather than guesses
- GraphRAG + PPR finds connections across pillars — a question about an API can surface the DB column, the UI field, and the action contract simultaneously
- Continuous learning from operator feedback — low-confidence responses automatically generate KB improvement candidates
- Cost governance enforces model routing — Haiku for classification, Sonnet for code, Opus only for heavy reasoning (< 10% of requests)

### Without COSMOS

- Operators get raw LLM responses with no grounding — hallucination risk on every complex query
- No knowledge graph means multi-hop questions (API → table → UI → action) require multiple manual lookups
- No feedback loop means errors repeat — the system never improves from corrections
- No confidence gating means the system appears confident even when it is guessing

---

## Anti-Hallucination System

| Layer | Mechanism |
|-------|-----------|
| 1 | Every fact from KB — LLM synthesizes, KB provides facts |
| 2 | Factuality prompt — 10 rules injected into every Claude call |
| 3 | Source attribution — every chunk tagged `[pillar:entity_id trust=0.9]` |
| 4 | GroundingChecker — ≥ 30% of response terms must appear in context |
| 5 | RALPH — self-correction pass before final response |
| 6 | HallucinationGuard — BLOCK if 3+ ungrounded entity IDs |
| 7 | ConfidenceGate — < 0.3 → refuse with "I don't know" |
| 8 | Citation markers — [1] [2] [3] so operators can verify every claim |

---

## Model Routing

| Task | Model | Cost Factor |
|------|-------|-------------|
| Intent classification only (`classify()`) | `claude-haiku-4-5-20251001` | 1× |
| High-confidence lookup / navigate queries | `claude-sonnet-4-6` | 5× |
| act / report / explain queries (always) | `claude-opus-4-6` | 25× |
| KB content generation, graph schema | `claude-opus-4-6` | 25× |
| Security / guardrails review | `claude-opus-4-6` | 25× |
| Cross-encoder reranking | `claude-opus-4-6` | 25× |

**Quality-first routing rule:** Opus is the default for any query that produces a response an operator acts on (`act`, `report`, `explain`). Sonnet is used only when intent is `lookup` or `navigate` and confidence is high. Haiku is used exclusively for the initial `classify()` call.

`LLM_MODE` in `.env`:
- `cli` — local `claude` binary (Claude Max plan, zero API cost — recommended for dev)
- `api` — Anthropic API (requires `ANTHROPIC_API_KEY`)
- `hybrid` — cli for long reasoning, api for short classification

---

## API Reference

Base path: `/cosmos/api/v1`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/cosmos/health` | GET | Health check + component status |
| `/cosmos/metrics` | GET | Prometheus metrics |
| `/v1/chat` | POST | Standard RAG chat |
| `/v1/hybrid-chat` | POST | Hybrid chat (KB + code + DB tiers) |
| `/v1/brain/query` | POST | Direct brain query with wave trace |
| `/v1/training-pipeline` | GET/POST | KB ingestion pipeline |
| `/cosmos/api/v1/pipeline/agents-skills-tools` | POST | Pillar 9/10/11 ingestion (agent, skill, tool definitions) |
| `/v1/vectorstore` | GET/POST/DELETE | Qdrant collection management |
| `/v1/graphrag` | POST | GraphRAG query |
| `/v1/feedback` | POST | Submit operator feedback |
| `/v1/actions` | POST | Execute actions (approval-gated) |
| `/v1/cosmos-settings` | GET/PUT | Tunable retrieval weights |
| `/v1/costs` | GET | Cost tracking dashboard |
| `/v1/tournament` | POST | A/B model tournament |
| `/v1/sandbox` | POST | Dry-run action sandbox |

---

## Running Tests

```bash
# All tests
npm test
# or: python -m pytest tests/ -x -q --tb=short

# With coverage
npm run test:coverage

# Lint + type check
npm run lint
npm run typecheck
```

**Before every commit (enforced by pre-commit hook):**
```
pytest tests/ -x -q    # must pass
ruff check app/         # no lint errors
mypy app/               # no type errors
secret_scan             # no secrets in staged files
```

---

## Sample Eval Set

201 ICRM operator seeds in MySQL (`cosmos_eval_seeds`). `KBEval` measures `recall@5` after every pipeline run. Score < 0.85 blocks deployment.

Run: `npm run eval` or `POST /cosmos/api/v1/cmd/eval`

---

## Key Contacts

- **Team:** AI Platform (`platform.shiprocket.com/team: ai-platform`)
- **RocketMind version:** 1.0.0
- **Issues / PRs:** `gvchaudhary22/cosmos`
- **Docs:** `docs/` — error codes, KB architecture PRD, training plans, decision records

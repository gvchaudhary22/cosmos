# COSMOS Architecture

## Control Plane

COSMOS separates orchestration policy from runtime implementation via three layers:

```
rocketmind.registry.json     ← machine-readable source of truth (agents, skills, workflows)
cosmos.config.json           ← runtime config (model routing, hooks, RRF weights, git)
CLAUDE.md                    ← session orchestrator for Claude-compatible runtimes
.claude/agents/              ← specialist agent definitions
.claude/skills/              ← reusable process frameworks (lazy-loaded)
.claude/hooks/               ← lifecycle and safety gates
.claude/commands/            ← /cosmos:* command surface
.cosmos/state/               ← STATE.md persistence
```

The same control plane config works across runtimes. Claude Code uses it natively; see `docs/runtime-adapters.md` for other runtimes.

---

## Three Pillars

### Pillar 1: Control Plane
Routes requests, enforces safety, defines agent roles.

Core files: `rocketmind.registry.json`, `cosmos.config.json`, `CLAUDE.md`

```
Incoming request
  │
  ▼
Intent classifier (Haiku) → domain + complexity
  │
  ├── match ≥ 0.60 → dispatch to matched agent
  └── no match → trigger forge agent
```

### Pillar 2: Execution Layer
Specialist agents and skills that perform the actual work.

```
.claude/agents/         11 agents (architect, engineer, strategist, ...)
.claude/skills/         19 skills (tdd, architecture, riper, ...)
.cosmos/extensions/     userland agents (project-specific, gitignored)
```

Each agent loads only the skills it needs (lazy-loaded). An agent calling `/cosmos:build` loads `engineer.md` + `tdd.md` + `debugging.md` — not all 19 skills.

### Pillar 3: Persistence Layer
Makes long-running work resumable; every change traceable.

```
.cosmos/state/STATE.md    project state — active phase, waves, decisions
.claude/hooks/            lifecycle hooks — pre-commit, pre-compact, stop
git history               immutable audit trail
```

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
  │   Auth · SSO · Session · Request routing
  │   Hinglish pre-translation (COSMOS receives clean English)
  │
  ▼
COSMOS  (Python — port 10001)          ← AI BRAIN
  │
  ├── Claude Opus/Sonnet/Haiku (via AI Gateway)
  ├── Qdrant :6333   — vector similarity (1536d cosine)
  ├── Neo4j  :7687   — knowledge graph (PPR, BFS, Dijkstra)
  ├── MySQL  :3309   — relational (sessions, audit, eval seeds)
  ├── Kafka  :9094   — event streaming (webhooks, feedback)
  └── S3             — KB sync, training exports, backups
```

---

## Wave Execution Model

Work is broken into dependency-ordered waves. Tasks within a wave run in parallel. Each task runs in a fresh coroutine — no accumulated state from prior tasks.

```python
# app/engine/wave_executor.py

executor = WaveExecutor()
executor.add_wave("retrieval", [
    WaveTask("exact_lookup",   exact_lookup_factory),
    WaveTask("ppr_search",     ppr_search_factory),
    WaveTask("bfs_search",     bfs_search_factory),
    WaveTask("vector_search",  vector_search_factory),
    WaveTask("lexical_search", lexical_search_factory),
])
executor.add_wave("rerank", [
    WaveTask("rrf_fusion",   rrf_fusion_factory),
    WaveTask("cross_encoder", cross_encoder_factory),
])
executor.add_wave("generate", [
    WaveTask("riper_reason",   riper_factory),
    WaveTask("ralph_check",    ralph_factory),
])
result = await executor.execute(context)
```

### Parallelism levels
| Mode | How |
|------|-----|
| Default (single session) | Waves sequential, tasks within wave parallel via `asyncio.gather` |
| Git worktrees | True parallelism via `.claude/skills/git-worktree.md` |
| External orchestrator | Anthropic API called concurrently per subagent |

`recommended_parallel_sessions: 8` in `cosmos.config.json`.

---

## Query Execution Flow (File-Level)

```
POST /v1/hybrid-chat  (from MARS)
  │
  app/api/endpoints/hybrid_chat.py
  │
  app/services/query_orchestrator.py
  │
  ├── app/engine/classifier.py         IntentClassifier (Haiku)
  ├── app/engine/planner.py            Query decomposition
  ├── app/engine/wave_executor.py      5-leg parallel retrieval
  │     ├── app/graph/retrieval.py     Legs 1, 3, 5
  │     ├── app/services/graphrag.py   Leg 2 (PPR)
  │     └── app/services/vectorstore.py Leg 4
  ├── app/graph/retrieval.py           RRF fusion
  ├── app/graph/langgraph_pipeline.py  LangGraph adaptive chain
  ├── app/services/reranker.py         Claude cross-encoder
  ├── app/brain/hierarchy.py           Parent-child expansion
  ├── app/engine/riper.py              RIPER reasoning (Wave 5)
  ├── app/engine/ralph.py              Self-correction
  ├── app/guardrails/advanced_guards.py HallucinationGuard
  └── app/engine/confidence.py         ConfidenceGate
  │
  Response with [1][2][3] citations
```

---

## Model Routing

Tasks are routed to the minimum sufficient model. IDs configured in `cosmos.config.json` → `models.routing`.

| Alias | Model | Used for | Cost |
|-------|-------|----------|------|
| `classify` | claude-haiku-4-5-20251001 | Intent routing, triage | 1× |
| `standard` | claude-sonnet-4-6 | Code generation, standard tasks | 5× |
| `reasoning` | claude-opus-4-6 | Architecture, KB generation | 25× |
| `security` | claude-opus-4-6 | Threat modeling, guardrails | 25× |

Rule: Opus < 10% of requests. Never hardcode model IDs — always use aliases from config.

---

## Nexus Mode (Multi-Repo Orchestration)

COSMOS reads from 8 KB repos. Nexus mode classifies query domain first → routes to relevant repos:

```
Query: "Why is WooCommerce order not syncing?"
  │
  NexusRouter (app/brain/router.py)
  │
  ├── domain: "channel_sync" → search shiprocket-channels/
  ├── domain: "api_contract" → search MultiChannel_API/
  └── domain: "escalation"   → search helpdesk/
```

This improves retrieval precision by avoiding cross-domain noise.

---

## Kernel vs Userland

```
Kernel (committed to repo):
  .claude/agents/     11 core agents
  .claude/skills/     19 core skills
  app/                Python inference engine

Userland (gitignored, project-specific):
  .cosmos/extensions/agents/     ICRM-specific agents
  .cosmos/extensions/skills/     ICRM-specific skills
```

When a userland agent encodes a genuinely reusable pattern, tag it `promotion_candidate: true` and promote to kernel via `/cosmos:forge`.

---

## Sentinel CI

Gates enforced before merge (`.github/workflows/cosmos-ci.yml`):

| Gate | Tool | Threshold |
|------|------|-----------|
| Lint | ruff | zero errors |
| Type check | mypy | zero errors |
| Unit tests | pytest | all pass |
| Secret scan | custom | zero hits |
| Recall@5 | KBEval | > 0.75 (if retrieval changes) |
| OWASP dependency | safety | zero critical CVEs |

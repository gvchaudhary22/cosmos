# COSMOS Core Concepts

## Agents

An agent is a specialist AI profile loaded for a specific domain. Each agent has:
- **Role** — what it does and when it's called
- **Domains** — topic areas it covers (`ENGINEERING`, `PRODUCT`, `REVIEW`, `RESEARCH`, `OPERATIONS`, `SYNTHESIS`)
- **Triggers** — natural language patterns that activate it
- **Skills** — process frameworks it loads (lazy-loaded, not always in context)
- **Output contracts** — what artifacts it produces and their format
- **Completion gate** — what must be true before the agent declares done

Agent files live in `.claude/agents/`. The registry at `rocketmind.registry.json` indexes all agents.

### Agent roster
| Agent | Domain | Primary role |
|-------|--------|-------------|
| `architect` | ENGINEERING + SYNTHESIS | System design, ADRs, data models |
| `engineer` | ENGINEERING | Code implementation, tests, fixes |
| `strategist` | PRODUCT + SYNTHESIS | Planning, roadmap, STATE.md |
| `reviewer` | REVIEW | Code review, ship gate |
| `security-engineer` | REVIEW + ENGINEERING | Threat model, guardrail design |
| `devops` | OPERATIONS | Deploy, infra, CI/CD |
| `data-engineer` | ENGINEERING + OPERATIONS | KB pipeline, Kafka, embeddings |
| `qa-engineer` | REVIEW + ENGINEERING | Test strategy, eval benchmark |
| `kb-specialist` | ENGINEERING + RESEARCH | KB content, pillar management |
| `researcher` | RESEARCH | Feasibility, trade-off analysis |
| `forge` | SYNTHESIS | Creates new agents on demand |

### Routing logic
```
match_score = (trigger_overlap × 0.4) + (domain_match × 0.4) + (skill_relevance × 0.2)
if match_score < 0.60: activate forge
```

---

## Skills

A skill is a reusable process framework — a set of instructions, patterns, and checklists that an agent loads when it needs a specific capability. Skills are lazy-loaded: only the skills needed for the current task are in context.

Skill files live in `.claude/skills/`.

### Skill roster
| Skill | Purpose | Loaded by |
|-------|---------|-----------|
| `tdd` | Test-driven development | engineer, reviewer, qa-engineer |
| `architecture` | System design patterns, ADRs | architect, data-engineer |
| `planning` | Phase decomposition, STATE.md | strategist |
| `brainstorming` | Option generation, spec extraction | strategist, researcher |
| `debugging` | Async Python root cause analysis | engineer |
| `review` | Severity-ranked code review | reviewer, security-engineer |
| `deployment` | Deploy safety, rollback | devops |
| `observability` | Logs, metrics, tracing | devops, data-engineer |
| `security-and-identity` | Threat model, tenant isolation | architect, security-engineer |
| `context-management` | Token strategy, lazy loading | all |
| `riper` | 5-phase structured reasoning | all |
| `git-worktree` | Parallel development | engineer, strategist |
| `scalability` | Capacity planning | architect, data-engineer |
| `ai-systems` | Agent design, prompt engineering | forge, architect |
| `reflection` | RALPH self-correction | reviewer, engineer |
| `knowledge-base` | KB structure, quality gates | kb-specialist |
| `retrieval-engineering` | RAG, wave execution, RRF | kb-specialist, architect |
| `python-async` | Async patterns, pitfalls | engineer |
| `debugging` | Root cause + RALPH | engineer |

---

## Workflows

A workflow is a named sequence of agent interactions triggered by a `/cosmos:*` command. Workflows are defined in `rocketmind.registry.json` → `workflows[]`.

### Lifecycle workflows
```
/cosmos:new      → strategist: PROJECT.md + ROADMAP.md + STATE.md
/cosmos:plan     → strategist + architect: PHASE-N-PLAN.md
/cosmos:build    → engineer: source files + tests (autonomous)
/cosmos:verify   → reviewer + qa-engineer: PHASE-N-UAT.md
/cosmos:ship     → reviewer: release summary + STATE.md update
/cosmos:next     → strategist: auto-detect next action
```

### Utility workflows
```
/cosmos:quick    → engineer: focused ad-hoc task
/cosmos:riper    → researcher + strategist + engineer + reviewer
/cosmos:forge    → forge: new agent definition
/cosmos:review   → reviewer + security-engineer: code review
/cosmos:audit    → security-engineer: security audit
/cosmos:debug    → engineer: root cause + fix + regression test
/cosmos:resume   → strategist: reconstruct context after compaction
/cosmos:progress → strategist: current project status
```

### COSMOS-specific workflows
```
/cosmos:train    → kb ingestion + embedding pipeline (autonomous)
/cosmos:eval     → 201-seed recall@5 benchmark (audit)
/cosmos:help     → command reference
```

---

## STATE.md

STATE.md is the persistence backbone of COSMOS. Every `/cosmos:*` command updates it.

Location: `.cosmos/state/STATE.md`

```markdown
# COSMOS — Project State

## Active Project
[project name and goal]

## Current Phase
[phase N — name — status]

## Active Wave
[wave name — tasks in progress]

## Last 5 Completed Tasks
1. [date] [command] — [task] → [outcome]

## Decisions Log
| Date | Command | Decision | Rationale |

## Blockers
[list with dates and owners]

## Clarification Requests
[outstanding questions]

## Agent Sessions
| Agent | Status | Wave | Output |
```

### Resume from compaction
When context is lost (compaction, new session):
1. Read `.cosmos/state/STATE.md`
2. Run `git log --oneline -10`
3. Check `.claude/session-state/` for pre-compact snapshot
4. Continue from last completed task

---

## Hooks

Hooks are shell scripts that run at lifecycle events. They enforce non-negotiable quality gates.

| Hook | Trigger | Blocking? |
|------|---------|-----------|
| `pre-commit.sh` | Before every commit | YES — blocks on lint/test/secret failures |
| `pre-tool-use.sh` | Before bash tool | YES — blocks destructive patterns |
| `pre-compact.sh` | Before context compact | No — saves snapshot |
| `post-tool-use.sh` | After bash tool | No — logs tool usage |
| `stop.sh` | Session end | No — logs session, warns uncommitted |

Hooks live in `.claude/hooks/`. Set `COSMOS_HOOKS_DISABLED=1` to bypass (use only in emergencies).

---

## Agent Forge

When no existing agent covers a request with ≥ 60% confidence, the `forge` agent activates:

1. Extracts the domain of the request
2. Designs a new agent spec
3. Writes `.claude/agents/[name].md` (kernel) or `.cosmos/extensions/agents/[name].md` (userland)
4. Updates `rocketmind.registry.json`

### Kernel vs Userland
- **Kernel**: reusable across projects → committed to this repo
- **Userland**: COSMOS/ICRM-specific → stored in `.cosmos/extensions/` (gitignored)

---

## Knowledge Base

The KB is the factual foundation. Every response must be grounded in KB content.

- **8 repos** covering Shiprocket's full platform
- **8 pillars** per repo (P1 schema → P8 negative routing)
- **44,094+ YAML files** in MultiChannel_API alone
- **Ingested into**: Qdrant (vectors) + Neo4j (graph) + MySQL (metadata)

KB quality gate: chunks < 50 chars, > 80% punctuation, or stub patterns are rejected.

Trust scores: `0.9` (human-verified) · `0.7` (auto-generated) · `0.5` (Claude-generated)

# COSMOS Commands
> RocketMind-powered orchestration commands running inside COSMOS wave execution.
> Use in Claude Code session: `/cosmos:<command>`

## STATE.md Update Protocol

> Standing rule — applies to every COSMOS command, every session

STATE.md is COSMOS's memory. Update it automatically whenever any of the following occur:

| Trigger | What to write |
|---------|---------------|
| Task completed | Move item from Todos to Last 5 Completed Tasks |
| Decision made | Add row to Decisions Log with date, decision, rationale |
| New task created | Add to appropriate phase/milestone in Todos |
| Blocker encountered | Add to Todos with `BLOCKED:` prefix and reason |
| Blocker resolved | Remove from Todos, add resolution to Decisions Log |
| Milestone shipped | Update Active Milestone, Current Version |
| Session produces significant context | Update Project Context if phase/milestone changed |

**Rules:**
- Never wait to be asked. Update STATE.md as part of completing any task.
- If STATE.md does not exist, create it before any other work.
- STATE.md lives at `.claude/session-state/STATE.md`

---

## Runtime Status Blocks

**Start banner** (emit at the beginning of plan/build/quick commands):

```
━━━ COSMOS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Domain:     {DOMAIN}
  Complexity: {COMPLEXITY}
  Agent:      {AGENT}
  Mode:       {COLLABORATIVE|AUTONOMOUS|AUDIT}
  Phase:      {active phase or "none"}
  Branch:     {branch name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Completion footer** (append at end of every command):

```
---
## Recommended Next Command

**Primary**: {next command}
**Why**: {one sentence}

**Alternatives**:
- {alt 1}
- {alt 2}
```

---

# Command: /cosmos:discover

> Validate the problem, target user, and go/no-go case before committing to build

## PROCESS

Load `skills/brainstorming.md`. Then:

1. Read the stated problem, target user hypothesis, and any existing context artifacts.
2. Dispatch:
   - `researcher` for problem validation, market reality, and opportunity sizing
   - `designer` for user framing, UX assumptions, and validation angles
3. Produce `DISCOVERY.md` with:
   - Problem framing
   - User insights and assumptions
   - Opportunity sizing
   - Open questions and validation gaps
   - Go / no-go recommendation
4. Surface blockers or missing evidence instead of pretending the problem is validated.
5. If the recommendation is go, hand off to `/cosmos:new`.

After completion → `Run /cosmos:new — turn validated discovery into requirements and roadmap.`

---

# Command: /cosmos:clarify

> Surface and resolve pending clarification requests that are blocking autonomous execution

## PROCESS

1. Read `.claude/session-state/STATE.md`.
2. Parse the `## Clarification Requests` section.
3. Show all `[OPEN]` `CLARIFICATION_REQUESTED` entries in a structured queue.
4. If called with a resolution, mark the matching entry `[RESOLVED]`.
5. If any open requests remain, keep workflow state blocked.
6. If no open requests remain, recommend `/cosmos:next`.

**Clarification event schema in STATE.md:**
```
[OPEN] id: clarify-001 | requested_by: engineer | question: Which KB path? | reason: Missing required input | requested_at: 2026-04-04T00:00:00Z
[RESOLVED] id: clarify-001 | resolution: Use knowledge_base/shiprocket/MultiChannel_API | resolved_by: operator | resolved_at: 2026-04-04T00:05:00Z
```

**Rule:** If ambiguity blocks safe execution, the active agent must emit a `CLARIFICATION_REQUESTED` event and stop tool execution until it is resolved.

---

# Command: /cosmos:new

> Initialize a brand new project from scratch

## PROCESS

1. Ask up to 5 targeted questions to understand scope, users, constraints, and success criteria.
2. Spawn researcher subagent for domain landscape.
3. Produce:
   - `PROJECT.md` — vision, goals, constraints, success criteria
   - `REQUIREMENTS.md` — v1/v2/out-of-scope with rationale
   - `ROADMAP.md` — phases mapped to requirements
   - `.claude/session-state/STATE.md` — initial state document
4. Present roadmap for approval. Adjust based on feedback.
5. Once approved → output: `Project initialized. Run /cosmos:plan 1 to plan Phase 1.`

---

# Command: /cosmos:plan [N]

> Research + spec + task breakdown for phase N

## PROCESS

**Emit start banner with:** `Complexity: PHASE`, `Agent: strategist + architect`

Load `skills/planning.md`. If N not specified, use next unplanned phase from ROADMAP.md.

1. Read `.claude/session-state/STATE.md` + `ROADMAP.md` + `REQUIREMENTS.md`
2. Spawn researcher subagent for this phase's domain
3. Design wave execution for phase N (which tasks can parallelize?)
4. Produce `PHASE-{N}-PLAN.md` with:
   - Phase goal (one sentence)
   - Scope: IN and OUT
   - Wave-structured task list with acceptance criteria per task
   - Architecture decisions and trade-offs
   - Dependencies (infra, external services, other phases)
   - Risk register (top 3 risks + mitigations)
5. Verify plan against requirements
6. Update STATE.md: set current_phase, status = planning_complete

After completion → `Run /cosmos:build {N} to execute this phase.`

---

# Command: /cosmos:build [N]

> Execute phase N using parallel wave architecture

## PROCESS

**Emit start banner with:** `Complexity: PHASE`, `Agent: engineer`

Read `PHASE-{N}-PLAN.md`. For each wave, dispatch subagents in parallel. After each wave:

```
━━━ Wave {N} Complete ━━━━━━━━━━━━━━━━━
  ✓ {task 1} — committed
  ✓ {task 2} — committed
  ✗ {task 3} — BLOCKED: {reason}
  Next: Wave {N+1}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

After all waves:
- Run verification: does codebase deliver everything Phase N promised?
- Output `PHASE-{N}-VERIFICATION.md`
- Update STATE.md with phase completion

After completion → `Run /cosmos:verify {N} to run UAT.`

---

# Command: /cosmos:verify [N]

> Human + automated verification of phase N deliverables

## PROCESS

1. Extract testable deliverables from `PHASE-{N}-PLAN.md`
2. Run: `python -m pytest tests/ -x -q`, `ruff check app/`, `mypy app/ --ignore-missing-imports`
3. Present each user-facing deliverable for UAT with pass/fail
4. For failures: spawn debug subagent for root cause + fix
5. Output `PHASE-{N}-UAT.md` with results

After completion → `Run /cosmos:ship {N} if UAT passed.`

---

# Command: /cosmos:ship [N]

> Create PR, deploy, update release state

## PROCESS

Load `skills/deployment.md`. Requires `PHASE-{N}-UAT.md` to exist and pass.

1. Run reviewer subagent across all phase changes. Block on CRITICAL findings.
2. Update `CHANGELOG.md`
3. Create PR with: what was built, how to test, infra/config changes
4. Tag release: `v{milestone}.{phase}`
5. Update STATE.md: phase marked shipped

After completion → `Run /cosmos:plan — begin next phase.`

---

# Command: /cosmos:next

> Auto-detect current state and recommend the next logical step

## PROCESS

Read STATE.md + ROADMAP.md. Decision table (first matching rule wins):

| STATE.md signal | Recommendation |
|-----------------|---------------|
| No project initialized | `/cosmos:new` |
| Phase planned, not built | `/cosmos:build N` |
| Phase built, not verified | `/cosmos:verify N` |
| Phase verified, not shipped | `/cosmos:ship N` |
| All phases complete | `/cosmos:plan` for next milestone |
| Otherwise | Show status and ask |

---

# Command: /cosmos:quick <task>

> Ad-hoc task with full quality guarantees

## PROCESS

**Emit start banner with:** `Complexity: QUICK`

1. Classify: which agent handles this?
2. Confirm issue + branch discipline before edits
3. Define single task XML:
   ```xml
   <task type="...">
     <n>...</n>
     <files>...</files>
     <action>...</action>
     <verify>...</verify>
     <done>...</done>
   </task>
   ```
4. Execute with relevant skill loaded
5. Run tests, lint, type check
6. Commit, update STATE.md

After completion → emit next command recommendation.

---

# Command: /cosmos:riper <task>

> Structured RIPER analysis: Research → Innovate → Plan → Execute → Review

## PROCESS

Load `skills/riper.md` and `skills/reflection.md`. Run through all 5 phases:

1. **Research** — gather all relevant context, constraints, and prior art
2. **Innovate** — generate 3+ solution options with trade-offs
3. **Plan** — choose approach, define steps, identify risks
4. **Execute** — implement the plan
5. **Review** — verify against original intent, check for regressions

If Execute fails 3 times with the same error → halt and request human help via `/cosmos:review`.

---

# Command: /cosmos:forge <description>

> Build a new specialized agent for a task no current agent covers

## PROCESS

Load `.claude/agents/` to check existing agents. If no agent covers it (>60% fit):

1. Analyze task description to identify domain
2. Design new agent using blueprint in `.claude/agents/`
3. Write to `.claude/agents/{name}.md`
4. Register in `rocketmind.registry.json`
5. Dispatch the task to the new agent

---

# Command: /cosmos:review

> Full structured code + architecture review

## PROCESS

Load `agents/reviewer.md`. Spawn reviewer subagent with:
- All changed files since last ship
- ARCH.md (architectural alignment)
- REQUIREMENTS.md (spec compliance)

Output: structured review with CRITICAL/HIGH/MEDIUM/LOW findings.
CRITICAL findings must be fixed before next ship.

---

# Command: /cosmos:audit

> Security + quality deep audit (OWASP/STRIDE)

## PROCESS

Spawn security-engineer subagent:
1. OWASP Top 10 scan
2. Dependency vulnerability check (`pip-audit` or safety)
3. Secrets/credentials in codebase check
4. Auth/authz coverage check
5. Prompt injection vectors (AI-specific)

Output: `SECURITY-AUDIT.md` with findings by severity.

---

# Command: /cosmos:debug <issue description>

> Systematic 4-phase root cause debugging

## PROCESS

Load `skills/debugging.md`. Then:

1. **Reproduce** — write a failing test that captures the bug
2. **Isolate** — binary search the call stack
3. **Root cause** — 5 whys analysis
4. **Fix** — root cause fix + regression test + related code scan

Output: root cause analysis, fix diff, regression test, STATE.md update.

---

# Command: /cosmos:resume

> Reload project state and continue after compaction or new session

## PROCESS

1. Read `.claude/session-state/STATE.md`. Also read `pre-compact-snapshot.md` if it exists.
2. Run `git log --oneline -5` to confirm last committed state.
3. Output status summary:
   - Active milestone + phase
   - Last 5 completed tasks
   - Open todos for current milestone
   - Any blockers
4. Infer and output the Next Command block using the decision table from `/cosmos:next`.

---

# Command: /cosmos:progress

> Current project status — where are we, what's next, what's blocked

## PROCESS

Read STATE.md + ROADMAP.md. Output:

```
Project: COSMOS — AI Brain for Shiprocket ICRM
Milestone: {M} — {name}
Phase status:
  ✅ Phase 1 — {name} (shipped)
  🔄 Phase 2 — {name} (building, Wave 2 of 3)
  ⏳ Phase 3 — {name} (not started)

Blockers: {list or "none"}
Next action: /cosmos:build 2 to complete Wave 3
```

---

# Command: /cosmos:train [KB_PATH]

> Run KB ingestion pipeline — embed, graph, eval

## PROCESS

1. If KB_PATH not specified, use path from `CLAUDE.md` or `cosmos.config.json`
2. Call `POST /v1/training-pipeline` or run `scripts/train.py`
3. Report: files processed, embeddings created, graph nodes added, quality gate results
4. Run `/cosmos:eval` after training to measure recall@5 impact

---

# Command: /cosmos:eval

> Run 201 ICRM eval seeds, measure recall@5, produce EVAL-REPORT.md

## PROCESS

1. Call `POST /cosmos/api/v1/cmd/eval` or run `npm run eval`
2. Measure recall@5 on all 201 seed queries
3. Score < 0.85 → flag as deployment-blocking regression
4. Output: `EVAL-REPORT.md` with per-query results, overall score, regression diff vs last run

---

# Command: /cosmos:help

> Show all available commands, agents, and usage guide

## PROCESS

Display this file in full. No further action needed.

---

## Available Agents

| Agent | Domains | Triggers |
|-------|---------|----------|
| architect | ENGINEERING, SYNTHESIS | system design, tech selection, architecture review, design |
| engineer | ENGINEERING | implement, build, code, debug, refactor, fix |
| strategist | PRODUCT, SYNTHESIS | plan, roadmap, milestones, project, phase |
| reviewer | REVIEW | review, audit, quality gate, ship gate, check |
| security-engineer | REVIEW, ENGINEERING | security review, threat model, audit, auth changes, owasp |
| devops | OPERATIONS | deploy, monitor, ci/cd, infra, pipeline |
| data-engineer | ENGINEERING, OPERATIONS | etl, kafka, stream processing, pipeline, data |
| qa-engineer | REVIEW, ENGINEERING | test strategy, test plan, qa, quality assurance, regression |
| kb-specialist | ENGINEERING, RESEARCH | knowledge base, kb, embedding, retrieval, pillar, ingest |
| researcher | RESEARCH | research, compare, feasibility, investigate, unknown |
| forge | SYNTHESIS | no matching agent, forge new agent, create agent, new specialist |

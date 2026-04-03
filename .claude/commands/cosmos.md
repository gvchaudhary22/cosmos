# COSMOS Commands
> RocketMind-powered orchestration commands running inside COSMOS wave execution.
> Use in Claude Code session: `/cosmos:<command>`

## Workflow Commands

### `/cosmos:new`
> Command: `/cosmos:new` | Mode: `collaborative`

**Inputs:** scope, users, constraints

**Outputs:** PROJECT.md, ROADMAP.md, STATE.md

**Agents:** strategist

---

### `/cosmos:plan`
> Command: `/cosmos:plan` | Mode: `collaborative`

**Inputs:** STATE.md, ROADMAP.md

**Outputs:** PHASE-N-PLAN.md

**Agents:** strategist, architect

---

### `/cosmos:build`
> Command: `/cosmos:build` | Mode: `autonomous`

**Inputs:** PHASE-N-PLAN.md, ARCH.md, STATE.md

**Outputs:** source files, tests, SUMMARY.md

**Agents:** engineer

---

### `/cosmos:verify`
> Command: `/cosmos:verify` | Mode: `audit`

**Inputs:** PHASE-N-PLAN.md, build outputs

**Outputs:** PHASE-N-UAT.md

**Agents:** reviewer, qa-engineer

---

### `/cosmos:ship`
> Command: `/cosmos:ship` | Mode: `audit`

**Inputs:** PHASE-N-UAT.md, CHANGELOG.md

**Outputs:** release summary, STATE.md update

**Agents:** reviewer

---

### `/cosmos:next`
> Command: `/cosmos:next` | Mode: `collaborative`

**Inputs:** STATE.md, ROADMAP.md

**Outputs:** next action

**Agents:** strategist

---

### `/cosmos:quick`
> Command: `/cosmos:quick` | Mode: `collaborative`

**Inputs:** task description

**Outputs:** focused task result, verification

**Agents:** engineer

---

### `/cosmos:riper`
> Command: `/cosmos:riper` | Mode: `collaborative`

**Inputs:** task description, context

**Outputs:** RIPER analysis

**Agents:** researcher, strategist, engineer, reviewer

---

### `/cosmos:forge`
> Command: `/cosmos:forge` | Mode: `collaborative`

**Inputs:** agent description

**Outputs:** new agent spec, registry update

**Agents:** forge

---

### `/cosmos:review`
> Command: `/cosmos:review` | Mode: `audit`

**Inputs:** changed files, ARCH.md

**Outputs:** review report

**Agents:** reviewer, security-engineer

---

### `/cosmos:audit`
> Command: `/cosmos:audit` | Mode: `audit`

**Inputs:** changed files, dependency graph

**Outputs:** security audit report

**Agents:** security-engineer

---

### `/cosmos:debug`
> Command: `/cosmos:debug` | Mode: `collaborative`

**Inputs:** bug description, failing test

**Outputs:** root cause analysis, fix, regression test

**Agents:** engineer

---

### `/cosmos:resume`
> Command: `/cosmos:resume` | Mode: `collaborative`

**Inputs:** STATE.md, git log

**Outputs:** reconstructed context

**Agents:** strategist

---

### `/cosmos:progress`
> Command: `/cosmos:progress` | Mode: `audit`

**Inputs:** STATE.md

**Outputs:** project status summary

**Agents:** strategist

---

### `/cosmos:train`
> Command: `/cosmos:train` | Mode: `autonomous`

**Inputs:** KB_PATH, kb files

**Outputs:** embeddings, graph nodes, eval score

---

### `/cosmos:eval`
> Command: `/cosmos:eval` | Mode: `audit`

**Inputs:** 201 eval seeds

**Outputs:** recall@5 score, EVAL-REPORT.md

---

### `/cosmos:help`
> Command: `/cosmos:help` | Mode: `collaborative`

**Outputs:** command reference

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

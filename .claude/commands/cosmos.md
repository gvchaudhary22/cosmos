# COSMOS Commands

> Orbit-powered slash commands for COSMOS. All commands route through COSMOS wave execution.

## Available Commands

### `/cosmos:discover`
> Maps to `/orbit:discover`

**Inputs:** problem statement, target user hypothesis

**Outputs:** DISCOVERY.md

**Agents:** researcher, designer

### `/cosmos:new-project`
> Maps to `/orbit:new-project`

**Inputs:** scope, users, constraints, existing systems

**Outputs:** PROJECT.md, REQUIREMENTS.md, ROADMAP.md, STATE.md

### `/cosmos:plan`
> Maps to `/orbit:plan`

**Inputs:** STATE.md, ROADMAP.md, REQUIREMENTS.md

**Outputs:** PHASE-{N}-PLAN.md

### `/cosmos:build`
> Maps to `/orbit:build`

**Inputs:** PHASE-{N}-PLAN.md, ARCH.md, STATE.md

**Outputs:** task outputs, SUMMARY.md, PHASE-{N}-VERIFICATION.md

### `/cosmos:verify`
> Maps to `/orbit:verify`

**Inputs:** PHASE-{N}-PLAN.md, build outputs

**Outputs:** PHASE-{N}-UAT.md

### `/cosmos:ship`
> Maps to `/orbit:ship`

**Inputs:** PHASE-{N}-UAT.md, review output, CHANGELOG.md, README.md

**Outputs:** release summary, CHANGELOG.md update, documentation updates, STATE.md update

**Agents:** reviewer, technical-writer

### `/cosmos:launch`
> Maps to `/orbit:launch`

**Inputs:** release artifacts, target audience, launch channels

**Outputs:** LAUNCH-PLAN.md, GTM-CHECKLIST.md, ANNOUNCEMENT-DRAFT.md

**Agents:** launch-planner, technical-writer

### `/cosmos:quick`
> Maps to `/orbit:quick`

**Inputs:** task description

**Outputs:** focused task result, verification, state update

### `/cosmos:forge`
> Maps to `/orbit:forge`

**Inputs:** task description

**Outputs:** new agent file, registry update

### `/cosmos:review`
> Maps to `/orbit:review`

**Inputs:** changed files, ARCH.md, REQUIREMENTS.md

**Outputs:** review report

### `/cosmos:audit`
> Maps to `/orbit:audit`

**Inputs:** changed files, dependency graph

**Outputs:** security audit report

### `/cosmos:eval`
> Maps to `/orbit:eval`

**Inputs:** README.md, registry, runtime adapters, workflow docs

**Outputs:** EVAL-REPORT.md, eval-report.json

**Agents:** reviewer

### `/cosmos:resume`
> Maps to `/orbit:resume`

**Inputs:** STATE.md, pre-compact snapshot, git log

**Outputs:** reconstructed context

### `/cosmos:next`
> Maps to `/orbit:next`

**Inputs:** STATE.md, ROADMAP.md

**Outputs:** next action

### `/cosmos:progress`
> Maps to `/orbit:progress`

**Inputs:** STATE.md, ROADMAP.md

**Outputs:** project status summary

### `/cosmos:map-codebase`
> Maps to `/orbit:map-codebase`

**Inputs:** repo tree, source files, STATE.md

**Outputs:** CODEBASE-MAP.md

### `/cosmos:monitor`
> Maps to `/orbit:monitor`

**Inputs:** health endpoints, metrics, alerts

**Outputs:** HEALTH-REPORT.md

### `/cosmos:debug`
> Maps to `/orbit:debug`

**Inputs:** bug description, failing test or reproduction

**Outputs:** root cause analysis, fix, regression test

### `/cosmos:deploy`
> Maps to `/orbit:deploy`

**Inputs:** environment, release artifacts

**Outputs:** deployment summary

### `/cosmos:rollback`
> Maps to `/orbit:rollback`

**Inputs:** failed deployment, release metadata

**Outputs:** rollback summary

### `/cosmos:milestone`
> Maps to `/orbit:milestone`

**Inputs:** shipped phases, state, release tags

**Outputs:** milestone archive

### `/cosmos:help`
> Maps to `/orbit:help`

**Inputs:** none

**Outputs:** command reference

### `/cosmos:riper`
> Maps to `/orbit:riper`

**Inputs:** task description, context

**Outputs:** RIPER analysis

**Agents:** researcher, strategist, engineer, reviewer

### `/cosmos:worktree`
> Maps to `/orbit:worktree`

**Inputs:** parallel task plan

**Outputs:** worktree setup guidance

### `/cosmos:cost`
> Maps to `/orbit:cost`

**Inputs:** session usage

**Outputs:** token and cost estimate

### `/cosmos:promote`
> Maps to `/orbit:promote`

**Inputs:** local patterns, agents, skills

**Outputs:** core repository PR, registry update

### `/cosmos:ask`
> Maps to `/orbit:ask`

**Inputs:** question about project state

**Outputs:** answer from STATE.md or context.db with source citation

**Agents:** strategist

### `/cosmos:clarify`
> Maps to `/orbit:clarify`

**Inputs:** pending clarification requests, operator answer

**Outputs:** clarification queue, clarification resolution

**Agents:** strategist


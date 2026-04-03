# Skill: planning

## Purpose
Phase decomposition, milestone definition, wave ordering, and STATE.md management for multi-step COSMOS work.

## Loaded By
`strategist`

---

## Phase Planning Process

### Step 1: Scope the phase
- What is the observable outcome of this phase?
- What is the acceptance criteria? (measurable)
- What are the dependencies on other phases?

### Step 2: Decompose into tasks
- Each task is independently completable
- Each task has a clear owner agent
- Each task produces a specific artifact or code change

### Step 3: Order into waves
```
Wave 1 (parallel): tasks with no dependencies
Wave 2 (parallel): tasks that depend only on Wave 1 outputs
Wave 3 (sequential): integration + verify + commit
```

### Step 4: Write PHASE-N-PLAN.md
Location: `.cosmos/state/PHASE-N-PLAN.md` or project root.

---

## PHASE-N-PLAN.md Template

```markdown
# Phase N: [Name]

**Goal:** [One-sentence observable outcome]
**Start date:** YYYY-MM-DD
**Status:** planned | in-progress | complete

## Acceptance Criteria
- [ ] [measurable criterion 1]
- [ ] [measurable criterion 2]
- [ ] recall@5 > 0.75 (if KB changes)
- [ ] pytest passes
- [ ] No [ERROR] level startup messages

## Wave Execution Plan

### Wave 1 (parallel)
| Task | Agent | Output | Est. complexity |
|------|-------|--------|-----------------|
| [task 1] | engineer | [file] | small/medium/large |
| [task 2] | data-engineer | [file] | small/medium/large |

### Wave 2 (parallel, after Wave 1)
| Task | Agent | Output | Depends on |
|------|-------|--------|-----------|

### Wave 3 (sequential)
| Step | Agent | Action |
|------|-------|--------|
| 1 | reviewer | code review |
| 2 | qa-engineer | run eval benchmark |
| 3 | engineer | commit + STATE.md update |

## Dependencies
- Depends on Phase [N-1]: [what specifically]
- Blocks Phase [N+1]: [what specifically]

## Risks
| Risk | Probability | Mitigation |
|------|-------------|-----------|
```

---

## STATE.md Management

### Update triggers
Update STATE.md after EVERY completed command or wave.

### Update protocol
```markdown
## Last 5 Completed Tasks
1. [YYYY-MM-DD] /cosmos:[cmd] — [task description] → [outcome/artifact]
2. ...

## Decisions Log
| Date | Command | Decision | Rationale |
|------|---------|----------|-----------|
| YYYY-MM-DD | /cosmos:[cmd] | [decision] | [why] |
```

### Blocker format
```markdown
## Blockers
- [YYYY-MM-DD] [blocker description] — blocked by: [person/system] — unblocks: [task]
```

---

## COSMOS Priority Matrix

When tasks compete for ordering:

| Priority | Criterion | Examples |
|----------|-----------|---------|
| P0 | Production breakage | startup errors, hallucination guard fails |
| P1 | Accuracy regression | recall@5 drops below gate |
| P2 | New capability | new pillar, new agent, new retrieval leg |
| P3 | Quality improvement | better chunking, improved prompts |
| P4 | Refactor / cleanup | no behavior change |

Always work P0 → P4. Never start P3 work while P1 is open.

---

## Milestone Definition

Good milestone: **specific, observable, measurable**
- ✓ "recall@5 > 0.80 on 201 seeds after MultiChannel_API P6 ingest"
- ✓ "P95 query latency < 1.5s under 50 concurrent users"
- ✗ "improved retrieval quality" (not measurable)
- ✗ "faster responses" (not specific)

# Skill: riper

## Purpose
Structured five-phase reasoning for complex COSMOS tasks: Research → Innovate → Plan → Execute → Review.

## Loaded By
`all` (any agent can invoke RIPER for complex tasks)

---

## The Five Phases

### R — Research
**Goal:** Understand before acting. Never start coding during Research.

Activities:
- Read the relevant source files (don't rely on memory)
- Check `docs/decisions/` for prior ADRs on this topic
- Run `git log --oneline app/[relevant-module]/` to see recent changes
- Identify what already exists that can be reused
- Surface all assumptions (list explicitly)

Output: Research summary — "Here is what I found..."

**Gate:** Do not advance to Innovate until you can answer: "What exists? What is the gap? What are the constraints?"

---

### I — Innovate
**Goal:** Generate options. Still no code.

Activities:
- Generate 2–3 distinct approaches (use `brainstorming` skill)
- Evaluate each on: correctness · performance · maintainability · cost
- Identify risks for each option
- Select the recommended approach with explicit rationale

Output: Options matrix + recommendation

**Gate:** Do not advance to Plan until one option is selected with documented rationale.

---

### P — Plan
**Goal:** Produce a concrete, step-by-step implementation plan.

Activities:
- List files to create, modify, and delete
- Define wave structure (what runs in parallel, what is sequential)
- Write acceptance criteria (measurable)
- Identify test cases needed

Output: Implementation plan (task list with file names)

**Gate:** Do not advance to Execute until the plan is reviewable. Every step must name a specific file.

---

### E — Execute
**Goal:** Implement the plan exactly. No deviations without returning to Plan.

Activities:
- Implement in dependency order (interfaces before implementations)
- Write tests alongside implementation (TDD)
- Run `pytest` after each logical unit
- If blocked: return to Plan, update it, then continue

Output: Code changes, test files, passing tests

**Gate:** Do not advance to Review until all planned changes are complete and `pytest` passes.

---

### R — Review
**Goal:** Verify correctness, safety, and completeness.

Activities:
- Apply the `review` skill checklist (P0 → P3 findings)
- Run `ruff check app/` — zero errors required
- Run eval benchmark if retrieval changes: `recall@5 > 0.75`
- Update `STATE.md` with completed tasks
- Update `CHANGELOG.md`

Output: Review report + STATE.md update + commit

**Gate:** Only mark RIPER complete when Review passes with no P0/P1 findings.

---

## RIPER State Machine

```
[RESEARCH] → [INNOVATE] → [PLAN] → [EXECUTE] → [REVIEW] → DONE
                 ↑___________↑         ↑____________↑
                 (blocked → back)      (blocked → back)
```

**Never skip phases.** Skipping Research leads to wasted implementation. Skipping Plan leads to uncontrolled scope.

---

## RIPER for COSMOS: Phase-Specific Notes

### Research phase — COSMOS signals
- Check `recall@5` baseline before any retrieval change
- Check startup log for existing warnings before adding features
- Read `rocketmind.registry.json` before forging new agents

### Innovate phase — COSMOS constraints
- Any new retrieval leg must integrate with existing RRF weights
- Any new guardrail must not break the confidence gate (< 0.3 = refuse)
- Any new DB table must follow the MySQL schema conventions

### Execute phase — COSMOS patterns
```python
# Always async
async def my_function() -> MyResult:
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(text(sql), params)
            await session.commit()
            return result
        except Exception as e:
            await session.rollback()
            logger.error("my_function.failed", error=str(e), error_code="ERR-COSMOS-NNN")
            raise
```

### Review phase — COSMOS gates
- `pytest tests/ -x -q` — must pass
- `ruff check app/` — zero errors
- `recall@5 > 0.75` — if retrieval changed
- Zero `[ERROR]` in `npm start` output

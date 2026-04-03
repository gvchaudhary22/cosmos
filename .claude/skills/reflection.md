# Skill: reflection

## Purpose
RALPH-style self-correction loop — verify your own output before finalizing. Used by engineer and reviewer to catch gaps before handing off.

## Loaded By
`reviewer` · `engineer`

---

## RALPH Self-Correction Protocol

RALPH = **R**easoning **A**udit **L**oop for **P**ost-generation **H**armonization

### Trigger condition
Run RALPH after every LLM response before returning to the caller.

### The three checks

#### Check 1: Intent Coverage
Did the response actually answer the question asked?
```
Original query: [what was asked]
Response covers: [what was answered]
Gap: [what was asked but not answered]
```
If gap is non-empty → regenerate or append.

#### Check 2: Grounding
Are all factual claims traceable to retrieved context?
```
Claim: "[claim in response]"
Source: context chunk [N] → [yes/no]
```
Rule: ≥ 30% of non-stopword response terms must appear in retrieved context.
If grounding < 30% → flag with uncertainty marker or refuse.

#### Check 3: Hallucination Detection
Are any entity IDs, API endpoints, or status codes in the response NOT in the context?
```
Entity: "[order_id / AWB / table_name / endpoint]"
In context: [yes/no]
```
Rule: 3+ ungrounded entities → BLOCK response entirely.

---

## Code Self-Review Loop

When an engineer writes code, apply these checks before declaring done:

### Step 1: Re-read the implementation
Read every file you changed. Does it do what was planned?

### Step 2: Check against the plan
```
Plan step 1: [planned change] → [implemented] ✓/✗
Plan step 2: [planned change] → [implemented] ✓/✗
```
For any ✗: fix before proceeding.

### Step 3: Run the linter
```bash
.venv/bin/ruff check app/ --select E,W,F
```
Zero errors required.

### Step 4: Run the tests
```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```
All must pass.

### Step 5: Check for regressions
If retrieval changed:
```bash
# Check recall@5 hasn't dropped
curl -X POST http://localhost:10001/cosmos/api/v1/cmd/eval
```
Threshold: `recall@5 > 0.75`

---

## Reflection Anti-Patterns

| Anti-pattern | What happens | What to do |
|---|---|---|
| "It looks right to me" | Subtle bugs ship | Always run tests, not eyeballs |
| "The test is wrong" | Tests are the spec | Fix the code, not the test (unless test is genuinely wrong) |
| "I'll fix it in the next PR" | It never gets fixed | Fix it now or file a P2 issue |
| "The error doesn't reproduce locally" | Environment mismatch | Document the condition in a test |

---

## COSMOS-Specific Reflection Checks

### After writing KB ingestor changes
- [ ] Quality gate still rejects stubs (`len(content) < 50`)
- [ ] Chunk size still in 200-500 token range
- [ ] `content_hash` skip still works (run pipeline twice, second should skip all)
- [ ] `trust_score` set correctly (0.9 for human-verified, 0.7 for auto)

### After writing retrieval changes
- [ ] All 5 legs still called in `WaveExecutor`
- [ ] RRF weights unchanged unless intentional (document in ADR if changed)
- [ ] MMR diversity filter still applied
- [ ] Parent-child expansion still triggered on child match

### After writing guardrail changes
- [ ] `ConfidenceGate` lower bound still ≥ 0.3
- [ ] `HallucinationGuard` entity count threshold still 3
- [ ] Tenant `company_id` filter still applied on all DB queries

### After writing API endpoint changes
- [ ] Endpoint registered in `app/api/routes.py`
- [ ] Auth header forwarded to MARS
- [ ] Rate limiter active
- [ ] Response format matches existing endpoints (consistent JSON structure)

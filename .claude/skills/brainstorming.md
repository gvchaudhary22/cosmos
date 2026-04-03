# Skill: brainstorming

## Purpose
Structured option generation, assumption surfacing, and spec extraction for COSMOS tasks. Used before planning or research to ensure the right question is being answered.

## Loaded By
`strategist` · `researcher`

---

## Spec Extraction (from vague requests)

When a request is ambiguous, extract the spec before acting:

### The 5 Questions
1. **What is the observable success state?** (what does done look like)
2. **Who is the primary user?** (ICRM operator / seller / support agent / platform engineer)
3. **What are the hard constraints?** (latency, cost, accuracy, safety)
4. **What already exists?** (don't rebuild what's there)
5. **What is NOT in scope?** (explicit exclusions prevent scope creep)

### COSMOS-specific context to extract
- Which pillar(s) does this affect? (P1–P8 or Hub)
- Which repo(s) in the KB? (MultiChannel_API / SR_Web / etc.)
- Does it change the retrieval pipeline? (affects recall@5)
- Does it change the guardrail pipeline? (affects safety)
- Does it require a DB migration? (MySQL / Qdrant collection / Neo4j schema)

---

## Option Generation Protocol

For any non-trivial decision, generate at least 3 options:

```markdown
## Option A: [Conservative / Minimal change]
Description: [what it does]
Effort: [S/M/L]
Risk: [low/medium/high]
Trade-off: [what you give up]

## Option B: [Balanced / Recommended]
Description: [what it does]
Effort: [S/M/L]
Risk: [low/medium/high]
Trade-off: [what you give up]

## Option C: [Ambitious / Full solution]
Description: [what it does]
Effort: [S/M/L]
Risk: [low/medium/high]
Trade-off: [what you give up]
```

Default recommendation: **Option B** unless there is a strong reason for A or C.

---

## Assumption Surfacing

Before starting any non-trivial task, list assumptions explicitly:

```markdown
## Assumptions
- [ ] MySQL is running on :3309 with the MARS DB schema
- [ ] Qdrant collection `cosmos_embeddings` exists with 1536d vectors
- [ ] KB_PATH is set and points to valid YAML files
- [ ] [specific assumption about this task]
```

Mark assumptions as verified (`[x]`) before execution starts.
If an assumption cannot be verified, convert it to a **blocker**.

---

## Brainstorming Anti-Patterns

| Anti-pattern | Why it fails | What to do instead |
|---|---|---|
| "Just use the existing approach" | Assumes current is optimal | Generate at least 2 alternatives before deciding |
| "We need to refactor everything" | Scope creep | Identify the minimal change that achieves the goal |
| "Let's benchmark all options" | Analysis paralysis | Evaluate on 3-4 criteria, time-box to 30 min |
| "The user knows what they want" | Unstated constraints exist | Run 5 Questions before starting |

---

## COSMOS Domain Vocabulary

Use precise terms when brainstorming COSMOS features:

| Term | Meaning |
|------|---------|
| wave | parallel task execution unit in `WaveExecutor` |
| leg | one retrieval strategy (exact/PPR/BFS/vector/lexical) |
| pillar | KB document category (P1–P8 + Hub) |
| chunk | 200-500 token retrieval unit from KB |
| trust_score | quality signal (0.9 human / 0.7 auto / 0.5 generated) |
| confidence | output of `ConfidenceGate` (0.0–1.0) |
| grounding | % of response terms present in retrieved context |
| entity_id | stable identifier for KB cross-linking |
| recall@5 | fraction of queries where correct doc in top-5 results |

# Skill: ai-systems

## Purpose
Agent system design, prompt engineering, capability scoping, and AI pipeline architecture for COSMOS.

## Loaded By
`forge` · `architect`

---

## Agent System Design Principles

### Capability Scoping
An agent's capability is defined by:
1. **Domain** — what topics it can reason about
2. **Triggers** — natural language patterns that activate it
3. **Skills** — what process frameworks it has access to
4. **Output contracts** — what artifacts it produces and their format

Rule: **One agent, one domain.** An agent that does everything does nothing well.

### Routing Architecture
```
Incoming request
  │
  ▼
Intent classifier (Haiku) → domain + complexity
  │
  ▼
Registry lookup (rocketmind.registry.json)
  │
  ├── match ≥ 0.60 → dispatch to matched agent
  └── no match → trigger forge agent
```

Confidence scoring:
```
score = (trigger_overlap × 0.4) + (domain_match × 0.4) + (skill_relevance × 0.2)
threshold = 0.60  # forge_threshold in cosmos.config.json
```

---

## Prompt Engineering for COSMOS

### System prompt structure
```
[Role definition — who is the agent]
[Context — what does it know about COSMOS]
[Constraints — what it must never do]
[Output format — what it produces]
[Few-shot examples — 2-3 representative cases]
```

### Factuality prompt (injected into every Claude call)
```
Rules for this response:
1. Every factual claim must appear in the retrieved context.
2. If you cannot find the answer in the context, say "I don't know."
3. Cite sources as [1] [2] [3] — numbers map to retrieved chunks.
4. Entity IDs (order_id, AWB, company_id) must appear verbatim from context.
5. Never fabricate API endpoints, table names, or status codes.
6. Never guess Shiprocket-specific values (status=104, COD limits, etc.).
7. If confidence < 0.3, refuse with "I don't have enough information."
8. Distinguish between "policy" (always true) and "state" (may have changed).
9. For actions: state preconditions before recommending.
10. For errors: state the diagnostic step before the fix.
```

### Anti-hallucination techniques
1. **Grounding check**: ≥ 30% of response terms must appear in retrieved context
2. **Entity ID validation**: entity IDs in response cross-checked against context
3. **RALPH pass**: self-correction — does response match query intent?
4. **HallucinationGuard**: block if 3+ entity IDs not in source context

---

## COSMOS AI Pipeline Architecture

### Wave executor design principles
- Each wave is **dependency-ordered** (wave N+1 waits for wave N)
- Tasks within a wave are **parallel** (asyncio.gather)
- Each task gets a **fresh coroutine** (no shared mutable state)
- Progress is emitted via **async callback** (SSE-compatible)

### Tool use design
Tools in COSMOS are defined in `app/tools/registry.py`:
- **Read tools** (`read_tools.py`): search, lookup, explain — always safe
- **Write tools** (`write_tools.py`): cancel, reattempt — always approval-gated

Tool definition format (Anthropic SDK format):
```python
{
    "name": "search_orders",
    "description": "Search orders by AWB, order_id, or company_id",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search term"},
            "company_id": {"type": "string", "description": "Tenant identifier"}
        },
        "required": ["query", "company_id"]
    }
}
```

### ReAct loop pattern (app/engine/react.py)
```
Thought: [what to do next]
Action: [tool_name]
Action Input: [tool arguments]
Observation: [tool result]
... (repeat until answer found)
Final Answer: [response with citations]
```

Max iterations: 5 (configurable). Timeout: 30s per action.

---

## Agent Forge Process

When forging a new agent:

1. **Domain check** — does this domain already exist in the registry?
2. **Kernel vs userland** — is this reusable (kernel) or ICRM-specific (userland)?
3. **Write spec** — use the agent template in `forge.md`
4. **Write file** — `.claude/agents/[name].md` (kernel) or `.cosmos/extensions/agents/[name].md` (userland)
5. **Register** — add to `rocketmind.registry.json`
6. **Test triggers** — verify no overlap with existing agents (> 40% trigger overlap = conflict)

---

## LLM Mode Configuration

COSMOS supports three modes (`LLM_MODE` in `.env`):

| Mode | How | When to use |
|------|-----|-------------|
| `cli` | Local `claude` binary | Local dev (zero API cost) |
| `api` | Anthropic API | Staging/production |
| `hybrid` | cli for long tasks, api for fast calls | Mixed environments |

Model selection in `app/engine/model_router.py`:
```python
TASK_TO_MODEL = {
    "classify":   "claude-haiku-4-5-20251001",  # 1× cost
    "standard":   "claude-sonnet-4-6",           # 5× cost
    "reasoning":  "claude-opus-4-6",             # 25× cost
    "security":   "claude-opus-4-6",             # 25× cost
    "rerank":     "claude-opus-4-6",             # 25× cost
}
```

Rule: Opus for < 10% of requests. Sonnet default. Haiku for triage only.

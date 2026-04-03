# Contributing to COSMOS

COSMOS is an internal platform maintained by the Shiprocket AI Platform team. This guide covers how to add skills, agents, KB content, and code changes.

---

## Development Setup

```bash
git clone [repo] && cd cosmos
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in required values

# Start local infra
docker compose up -d  # Qdrant + Neo4j + Redis

# Start COSMOS
npm start

# Verify
curl http://localhost:10001/cosmos/health
```

---

## Branch and Commit Standards

### Branch naming
```
feat/NNN-short-description      # New feature
fix/NNN-short-description       # Bug fix
arch/NNN-short-description      # Architecture change
chore/NNN-short-description     # Deps, tooling, config
refactor/NNN-short-description  # No behavior change
docs/NNN-short-description      # Documentation only
```

Always cut from latest `develop`. Never commit directly to `main` or `develop`.

### Commit format
```
<type>(<scope>): <what was done> (#NNN)

Types:  feat · fix · arch · refactor · test · docs · chore · perf · security
Scopes: brain · engine · graph · learning · api · grpc · guardrails · db · monitoring · ci
```

### Examples
```
feat(engine): add RIPER v2 with adaptive phase depth (#142)
fix(graph): handle missing Neo4j edge type gracefully (#156)
arch(guardrails): add company_id check to all MCP tool responses (#170)
```

---

## How to Add a Skill

1. Create `.claude/skills/[name].md` following the skill template
2. Add to `rocketmind.registry.json` → `skills[]`
3. Update the agents that should load the new skill (`loaded_by`)
4. Document in `docs/concepts.md` → Skills table

### Skill template
```markdown
# Skill: [name]

## Purpose
[One sentence: what this skill enables]

## Loaded By
`[agent1]` · `[agent2]`

---

## [Section 1: Core concept]
[Content]

## [Section 2: Patterns]
[Checklists, templates, examples]
```

---

## How to Add an Agent

1. Create `.claude/agents/[name].md` following the agent template (see `forge.md`)
2. Add to `rocketmind.registry.json` → `agents[]`
3. Verify triggers don't overlap > 40% with existing agents
4. Decide: kernel (commit) or userland (`.cosmos/extensions/`)
5. Document in `docs/concepts.md` → Agent roster table

**Userland agents** (ICRM-specific): place in `.cosmos/extensions/agents/` and add to `.gitignore`.

---

## How to Add KB Content

See `.claude/skills/knowledge-base.md` for full KB authoring guidelines.

Quick reference:
1. Write YAML doc following the standard format (entity_id, pillar, content, trust_score)
2. Place in correct pillar directory under `KB_PATH/shiprocket/[repo]/`
3. Run ingestion: `curl -X POST http://localhost:10001/cosmos/api/v1/training-pipeline`
4. Run eval: `recall@5` must stay ≥ 0.75
5. Commit KB changes separately from code changes

---

## How to Add an API Endpoint

1. Create `app/api/endpoints/[name].py`
2. Register in `app/api/routes.py`
3. Write tests in `tests/test_[name].py`
4. Add to API Reference table in `README.md`

Checklist for new endpoints:
- [ ] Auth header forwarded to MARS
- [ ] `company_id` extracted and validated
- [ ] Rate limiter active (inherits from middleware)
- [ ] Structured error response: `{"error": "...", "code": "ERR-COSMOS-NNN"}`
- [ ] Prometheus metric emitted

---

## How to Update the Error Code Registry

`docs/error-codes.md` contains all `ERR-COSMOS-NNN` codes.

When adding a new error:
1. Find the next available number in the registry
2. Add entry: code, component, meaning, likely cause, remediation
3. Add logging in code: `logger.error("event.name", error_code="ERR-COSMOS-NNN")`

---

## Pull Request Process

1. Create branch from `develop`
2. Make changes + write tests
3. Run pre-commit gate: `npm test && npm run lint`
4. If retrieval changes: run eval `recall@5 > 0.75`
5. Open PR with:
   - Summary (what changed and why)
   - Test plan (what was verified)
   - Issues linked
6. Request review from AI Platform team
7. Squash merge after approval

### PR body template
```markdown
## Summary
- [bullet: what changed]
- [bullet: why it was needed]

## Test plan
- [ ] `pytest tests/ -x -q` passes
- [ ] `ruff check app/` passes
- [ ] recall@5 > 0.75 (if retrieval changed)
- [ ] Tested locally: [describe manual test]

## Issues
Closes #NNN
```

---

## What Not to Do

- Do not hardcode model IDs — use aliases from `cosmos.config.json`
- Do not call `os.environ.get()` in service code — use `settings.*`
- Do not use synchronous I/O in async functions
- Do not write to `.env` or commit secrets
- Do not bypass the pre-commit hook (`--no-verify`)
- Do not merge directly to `main` or `develop`
- Do not change `HallucinationGuard` or `ConfidenceGate` thresholds without an ADR

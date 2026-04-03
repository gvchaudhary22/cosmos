# Cosmos — MARS AI Brain (Python/FastAPI)

## Overview

Cosmos is the AI inference and routing brain for the MARS platform. It provides the intelligence layer — routing requests to appropriate models, managing knowledge graphs, orchestrating multi-model pipelines, and running the ML/AI workloads that power MARS's decision-making.

## Tech Stack

- **Framework**: FastAPI (async)
- **Language**: Python 3.12
- **AI SDK**: Anthropic Python SDK
- **DB**: SQLAlchemy (async), migrations via Alembic
- **Task Queue**: Background tasks or Celery
- **Comms**: gRPC (grpc_server.py, grpc_servicers/), REST API
- **Port**: 8001

## Project Structure

```
cosmos/
├── app/
│   ├── main.py              # FastAPI entrypoint
│   ├── config.py            # Settings (pydantic BaseSettings)
│   ├── api/                 # REST API routes
│   ├── brain/               # Core AI orchestration logic
│   ├── clients/             # External API clients (Anthropic, etc.)
│   ├── db/                  # Database models and session
│   ├── engine/              # Inference engine
│   ├── events/              # Event handling
│   ├── graph/               # Knowledge graph
│   ├── grpc_gen/            # Generated gRPC stubs
│   ├── grpc_server.py       # gRPC server entrypoint
│   ├── grpc_servicers/      # gRPC service implementations
│   ├── guardrails/          # Safety/validation filters
│   ├── learning/            # Continuous learning module
│   ├── middleware/          # FastAPI middleware
│   ├── monitoring/          # Metrics and health
│   ├── services/            # Business logic services
│   └── tools/               # AI tool implementations
├── tests/                   # pytest test suite
├── docs/                    # Architecture and design docs
├── .claude/
│   ├── hooks/               # cosmos-style lifecycle hooks
│   ├── rules/               # Coding conventions and standards
│   └── session-state/       # STATE.md + session snapshots
├── .github/workflows/       # CI/CD (cosmos-ci.yml)
├── requirements.txt         # Python dependencies
├── metadata.yml             # IDP contract
└── CLAUDE.md                # This file
```

## Commands

```bash
# Start server
uvicorn app.main:app --reload --port 8001

# Run tests
python -m pytest tests/ -x -q --tb=short

# Lint
ruff check app/ tests/
mypy app/ --ignore-missing-imports

# Format
ruff format app/ tests/
```

## Model Routing

See `.claude/rules/model-routing.md` for full guide.

| Task | Model | Alias |
|------|-------|-------|
| Request classification | claude-haiku-4-5 | classify |
| Standard implementation | claude-sonnet-4-6 | standard |
| Architecture/reasoning | claude-opus-4-6 | reasoning |
| Security review | claude-opus-4-6 | security |

## Error Codes

See `docs/error-codes.md` for the full ERR-COSMOS-* registry with runbooks.

## Architecture Rules

- All AI calls go through `app/clients/` — never call Anthropic SDK directly in services
- Async everywhere — use `async def` and `await` throughout
- Use `app/config.py` (pydantic BaseSettings) for all environment config
- Error codes: log with `error_code=ERR-COSMOS-NNN` for alertability
- Tests: `tests/` mirrors `app/` structure (e.g., `app/brain/router.py` → `tests/brain/test_router.py`)
- No secrets in code — use env vars via config.py

## Git Standards

See `.claude/rules/git-standards.md` for branch naming and commit format.

## Hooks (Cosmos Lifecycle)

| Hook | Trigger | Action |
|------|---------|--------|
| pre-commit.sh | Before commit | pytest + secret scan (BLOCKING) |
| stop.sh | Session end | Log session, warn uncommitted |
| pre-compact.sh | Before context compact | Git snapshot to session-state/ |
| pre-tool-use.sh | Before tool call | Block dangerous patterns |
| post-tool-use.sh | After tool call | Log to tool-usage.log |

## Completion Gate

Before declaring any task done:
1. `python -m pytest tests/ -x -q` — all tests pass
2. `ruff check app/` — no lint errors
3. `mypy app/ --ignore-missing-imports` — no type errors
4. No secrets in staged files
5. CLAUDE.md updated if new modules/endpoints added

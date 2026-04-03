# Cosmos Model Routing Guide

Cost-optimized model selection for all AI tasks in Cosmos.

## Routing Table

| Task Domain | Model | Alias | Cost Factor |
|-------------|-------|-------|-------------|
| Request classification, intent routing | `claude-haiku-4-5-20251001` | `classify` | 1x |
| Standard Python implementation | `claude-sonnet-4-6` | `standard` | 5x |
| API endpoint coding, test writing | `claude-sonnet-4-6` | `standard` | 5x |
| ML pipeline design, model architecture | `claude-opus-4-6` | `reasoning` | 25x |
| Security/guardrails review | `claude-opus-4-6` | `security` | 25x |
| Knowledge graph schema design | `claude-opus-4-6` | `reasoning` | 25x |

## Decision Rules

**Haiku** — fast, cheap, ~20x cost reduction:
- Classifying incoming request type
- Simple validation or routing decisions
- Health check reasoning

**Sonnet** (default):
- Writing FastAPI endpoints
- Writing pytest tests
- Debugging with tracebacks
- gRPC servicer implementation
- Standard ML pipeline code

**Opus** — reserve for < 10% of requests:
- Knowledge graph schema design
- Multi-model orchestration architecture
- Guardrails policy design
- Security vulnerability analysis
- Performance bottleneck root cause

## In Code (app/clients/)

```python
MODEL_ROUTING = {
    "classify":  "claude-haiku-4-5-20251001",
    "standard":  "claude-sonnet-4-6",
    "reasoning": "claude-opus-4-6",
    "security":  "claude-opus-4-6",
}

TASK_TO_PROFILE = {
    "intent_classification": "classify",
    "code_generation":       "standard",
    "test_generation":       "standard",
    "api_design":            "reasoning",
    "security_review":       "security",
    "architecture":          "reasoning",
}

def route_model(task: str) -> str:
    profile = TASK_TO_PROFILE.get(task, "standard")
    return MODEL_ROUTING[profile]
```

## In AI Sessions (Claude Code)

```bash
# Classification / quick tasks → /model claude-haiku-4-5-20251001
# Standard Python work        → /model claude-sonnet-4-6  (default)
# Architecture / security     → /model claude-opus-4-6
```

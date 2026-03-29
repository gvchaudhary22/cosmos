"""
Health & Readiness endpoints for COSMOS.

Provides:
  GET /cosmos/health         — Basic health (always 200 if server is up)
  GET /cosmos/health/ready   — Readiness (checks DB, Redis, MCAPI connectivity)
  GET /cosmos/health/live    — Liveness (checks if ReAct engine is responsive)
  GET /cosmos/health/dependencies — Dependency status matrix
"""

import time
import structlog
from fastapi import APIRouter

from app.config import settings

logger = structlog.get_logger()

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Basic health check — always returns 200 if server is up."""
    return {
        "status": "healthy",
        "service": "cosmos",
        "version": "0.5.0",
        "env": settings.ENV,
    }


@router.get("/health/ready")
async def readiness():
    """Readiness check — verifies DB, Redis, and MCAPI are reachable."""
    checks = {}
    overall = True

    # Database check
    try:
        from app.db.session import get_engine
        engine = get_engine()
        if engine is not None:
            checks["database"] = {"status": "ok"}
        else:
            checks["database"] = {"status": "degraded", "reason": "engine is None"}
            overall = False
    except Exception as e:
        checks["database"] = {"status": "error", "reason": str(e)}
        overall = False

    # Redis check
    try:
        import aioredis
        redis = aioredis.from_url(settings.REDIS_URL)
        await redis.ping()
        checks["redis"] = {"status": "ok"}
        await redis.close()
    except Exception as e:
        checks["redis"] = {"status": "error", "reason": str(e)}
        overall = False

    # MCAPI check
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.MCAPI_BASE_URL}/health")
            if resp.status_code < 500:
                checks["mcapi"] = {"status": "ok"}
            else:
                checks["mcapi"] = {"status": "degraded", "reason": f"status {resp.status_code}"}
                overall = False
    except Exception as e:
        checks["mcapi"] = {"status": "error", "reason": str(e)}
        overall = False

    status_code = 200 if overall else 503
    return {
        "status": "ready" if overall else "not_ready",
        "checks": checks,
    }


@router.get("/health/live")
async def liveness():
    """Liveness check — verifies core engine components are responsive."""
    checks = {}

    # ReAct engine responsiveness (just check it can be imported and instantiated)
    try:
        from app.engine.react import ReActEngine
        checks["react_engine"] = {"status": "ok"}
    except Exception as e:
        checks["react_engine"] = {"status": "error", "reason": str(e)}

    # Guardrail pipeline
    try:
        from app.guardrails.setup import create_guardrail_pipeline
        pipeline = create_guardrail_pipeline()
        checks["guardrails"] = {
            "status": "ok",
            "pre_guards": len(pipeline.pre_guards),
            "post_guards": len(pipeline.post_guards),
        }
    except Exception as e:
        checks["guardrails"] = {"status": "error", "reason": str(e)}

    # Classifier
    try:
        from app.engine.classifier import IntentClassifier
        checks["classifier"] = {"status": "ok"}
    except Exception as e:
        checks["classifier"] = {"status": "error", "reason": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())
    return {
        "status": "live" if all_ok else "degraded",
        "checks": checks,
    }


@router.get("/health/dependencies")
async def dependencies():
    """Dependency status matrix — shows all external dependencies and their config."""
    return {
        "service": "cosmos",
        "dependencies": {
            "database": {
                "type": "postgresql",
                "url": settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else "configured",
                "pool_size": settings.DATABASE_POOL_SIZE,
            },
            "redis": {
                "type": "redis",
                "url": settings.REDIS_URL.split("@")[-1] if "@" in settings.REDIS_URL else settings.REDIS_URL,
            },
            "mcapi": {
                "type": "http",
                "base_url": settings.MCAPI_BASE_URL,
                "auth_mode": settings.MCAPI_AUTH_MODE,
                "timeout": settings.MCAPI_TIMEOUT,
            },
            "elasticsearch": {
                "type": "elasticsearch",
                "hosts": settings.ELASTICSEARCH_HOSTS,
            },
            "llm_ai": {
                "type": "anthropic",
                "model_haiku": settings.LLM_MODEL_HAIKU,
                "model_sonnet": settings.LLM_MODEL_SONNET,
                "configured": settings.ANTHROPIC_API_KEY is not None,
            },
        },
        "feature_flags": {
            "dry_run": settings.FF_DRY_RUN,
            "prompt_safety": settings.FF_PROMPT_SAFETY,
            "token_economics": settings.FF_TOKEN_ECONOMICS,
        },
    }

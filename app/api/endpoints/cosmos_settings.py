"""
Cosmos Workflow Settings API.

GET  /cosmos/api/v1/settings          — Read current effective settings
PUT  /cosmos/api/v1/settings          — Update settings (Lime UI → Cosmos Postgres cache)
POST /cosmos/api/v1/settings/preset   — Apply a named preset
"""

import structlog
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.workflow_settings import WorkflowSettings

logger = structlog.get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SettingsPayload(BaseModel):
    quality_mode: str = Field("balanced", pattern="^(max_quality|balanced|cost_optimized)$")
    force_complex: bool = False
    model_preference: str = Field("auto", pattern="^(auto|opus|sonnet|haiku)$")
    ignore_cost_budget: bool = False
    wave1_confidence_threshold: float = Field(0.75, ge=0.0, le=1.0)
    tier1_respond_threshold: float = Field(0.70, ge=0.0, le=1.0)
    probe_timeout_sec: int = Field(10, ge=1, le=300)
    deep_timeout_sec: int = Field(20, ge=1, le=600)
    pipeline1_enabled: bool = True
    pipeline2_enabled: bool = True
    pipeline3_enabled: bool = True
    pipeline4_enabled: bool = True
    pipeline5_enabled: bool = True
    enable_ralph: bool = True
    enable_riper: bool = True
    enable_hyde: bool = False
    max_context_tokens: int = Field(8000, ge=500, le=200000)


class PresetPayload(BaseModel):
    preset: str = Field(..., pattern="^(max_quality|balanced|cost_optimized)$")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def get_settings(request: Request):
    """Return the current in-memory (effective) settings."""
    cache = getattr(request.app.state, "settings_cache", None)
    if cache is None:
        return WorkflowSettings.balanced().to_dict()
    return cache.get().to_dict()


@router.put("")
async def update_settings(request: Request, payload: SettingsPayload):
    """Persist new settings to Postgres and refresh the in-memory cache."""
    cache = getattr(request.app.state, "settings_cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="Settings cache not initialized")

    new_settings = WorkflowSettings.from_dict(payload.dict())
    await cache.update(new_settings)
    logger.info(
        "cosmos_settings.updated",
        quality_mode=new_settings.quality_mode,
        model=new_settings.model_preference,
    )
    return new_settings.to_dict()


@router.post("/preset")
async def apply_preset(request: Request, payload: PresetPayload):
    """Apply a named preset, overwriting all settings."""
    cache = getattr(request.app.state, "settings_cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="Settings cache not initialized")

    preset_map = {
        "max_quality": WorkflowSettings.max_quality,
        "balanced": WorkflowSettings.balanced,
        "cost_optimized": WorkflowSettings.cost_optimized,
    }
    preset_fn = preset_map.get(payload.preset, WorkflowSettings.balanced)
    new_settings = preset_fn()
    await cache.update(new_settings)
    logger.info("cosmos_settings.preset_applied", preset=payload.preset)
    return new_settings.to_dict()

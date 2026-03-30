"""
Cosmos Workflow Settings — three-layer architecture.

Layer 1: Mars MySQL (source of truth, managed by Lime UI / Mars API).
Layer 2: Cosmos Postgres single-row cache table (resilience — survives Mars downtime).
Layer 3: In-memory singleton (0ns per-request reads, refreshed every 60s).

This module owns Layer 3 (in-memory) and the refresh loop.
WorkflowSettingsRepo (workflow_settings_repo.py) owns Layer 2 (Postgres).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
import structlog

logger = structlog.get_logger()

# Default refresh period for in-memory cache (seconds).
_REFRESH_INTERVAL_SEC = 60


@dataclass
class WorkflowSettings:
    """Effective Cosmos pipeline settings for a single request cycle."""

    # Quality & cost
    quality_mode: str = "balanced"          # max_quality | balanced | cost_optimized
    force_complex: bool = False             # treat every query as complex
    model_preference: str = "auto"          # auto | opus | sonnet | haiku
    ignore_cost_budget: bool = False        # skip budget gate

    # GREL thresholds
    wave1_confidence_threshold: float = 0.75  # skip Wave 2 when Wave 1 >= this
    tier1_respond_threshold: float = 0.70     # TierPolicy respond threshold

    # Timeouts (seconds)
    probe_timeout_sec: int = 10
    deep_timeout_sec: int = 20

    # Pipeline toggles (Pipelines 1-5)
    pipeline1_enabled: bool = True   # decision_tree
    pipeline2_enabled: bool = True   # tfidf_rag
    pipeline3_enabled: bool = True   # hybrid_retrieval
    pipeline4_enabled: bool = True   # tool_use (Wave 2)
    pipeline5_enabled: bool = True   # full_reasoning (Wave 2)

    # Extra quality levers
    enable_ralph: bool = True
    enable_riper: bool = True
    enable_hyde: bool = True
    max_context_tokens: int = 8000

    # Wave 3: LangGraph stateful reasoning (runs after W1+W2, before LLM assembly)
    wave3_langgraph_enabled: bool = True    # stateful multi-step reasoning on merged context
    wave3_max_iterations: int = 3           # max reasoning loop iterations
    wave3_timeout_sec: int = 15             # hard timeout for Wave 3

    # Wave 4: Neo4j targeted graph traversal (runs after W3, uses refined entities)
    wave4_neo4j_enabled: bool = True        # targeted BFS from W3-extracted entities
    wave4_max_depth: int = 3               # graph traversal depth
    wave4_timeout_sec: int = 10             # hard timeout for Wave 4

    # Phase 3: Shadow lane mode for W3/W4
    # When True: W3+W4 run in parallel but results are ONLY logged for comparison,
    # not injected into LLM context. Enables MRR comparison before promoting to default.
    wave3_shadow_mode: bool = False  # log-only, no context injection
    wave4_shadow_mode: bool = False  # log-only, no context injection

    @classmethod
    def max_quality(cls) -> "WorkflowSettings":
        """Preset: quality first, cost ignored."""
        return cls(
            quality_mode="max_quality",
            force_complex=True,
            model_preference="opus",
            ignore_cost_budget=True,
            wave1_confidence_threshold=0.95,
            tier1_respond_threshold=0.90,
            probe_timeout_sec=20,
            deep_timeout_sec=40,
            pipeline1_enabled=True,
            pipeline2_enabled=True,
            pipeline3_enabled=True,
            pipeline4_enabled=True,
            pipeline5_enabled=True,
            enable_ralph=True,
            enable_riper=True,
            enable_hyde=True,
            max_context_tokens=16000,
            wave3_langgraph_enabled=True,
            wave3_max_iterations=3,
            wave3_timeout_sec=20,
            wave4_neo4j_enabled=True,
            wave4_max_depth=4,
            wave4_timeout_sec=15,
        )

    @classmethod
    def balanced(cls) -> "WorkflowSettings":
        """Preset: balanced quality / cost tradeoff (factory default)."""
        return cls()  # all defaults are balanced

    @classmethod
    def cost_optimized(cls) -> "WorkflowSettings":
        """Preset: minimise token spend."""
        return cls(
            quality_mode="cost_optimized",
            force_complex=False,
            model_preference="haiku",
            ignore_cost_budget=False,
            wave1_confidence_threshold=0.60,
            tier1_respond_threshold=0.55,
            probe_timeout_sec=8,
            deep_timeout_sec=15,
            pipeline1_enabled=True,
            pipeline2_enabled=True,
            pipeline3_enabled=False,
            pipeline4_enabled=False,
            pipeline5_enabled=False,
            enable_ralph=True,
            enable_riper=False,
            enable_hyde=False,
            max_context_tokens=4000,
            wave3_langgraph_enabled=False,  # too expensive for cost mode
            wave4_neo4j_enabled=False,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowSettings":
        """Construct from a plain dict (e.g. fetched from Postgres JSONB column)."""
        return cls(
            quality_mode=d.get("quality_mode", "balanced"),
            force_complex=bool(d.get("force_complex", False)),
            model_preference=d.get("model_preference", "auto"),
            ignore_cost_budget=bool(d.get("ignore_cost_budget", False)),
            wave1_confidence_threshold=float(d.get("wave1_confidence_threshold", 0.75)),
            tier1_respond_threshold=float(d.get("tier1_respond_threshold", 0.70)),
            probe_timeout_sec=int(d.get("probe_timeout_sec", 10)),
            deep_timeout_sec=int(d.get("deep_timeout_sec", 20)),
            pipeline1_enabled=bool(d.get("pipeline1_enabled", True)),
            pipeline2_enabled=bool(d.get("pipeline2_enabled", True)),
            pipeline3_enabled=bool(d.get("pipeline3_enabled", True)),
            pipeline4_enabled=bool(d.get("pipeline4_enabled", True)),
            pipeline5_enabled=bool(d.get("pipeline5_enabled", True)),
            enable_ralph=bool(d.get("enable_ralph", True)),
            enable_riper=bool(d.get("enable_riper", True)),
            enable_hyde=bool(d.get("enable_hyde", False)),
            max_context_tokens=int(d.get("max_context_tokens", 8000)),
            wave3_langgraph_enabled=bool(d.get("wave3_langgraph_enabled", False)),
            wave3_max_iterations=int(d.get("wave3_max_iterations", 3)),
            wave3_timeout_sec=int(d.get("wave3_timeout_sec", 15)),
            wave4_neo4j_enabled=bool(d.get("wave4_neo4j_enabled", False)),
            wave4_max_depth=int(d.get("wave4_max_depth", 3)),
            wave4_timeout_sec=int(d.get("wave4_timeout_sec", 10)),
        )

    def to_dict(self) -> dict:
        return {
            "quality_mode": self.quality_mode,
            "force_complex": self.force_complex,
            "model_preference": self.model_preference,
            "ignore_cost_budget": self.ignore_cost_budget,
            "wave1_confidence_threshold": self.wave1_confidence_threshold,
            "tier1_respond_threshold": self.tier1_respond_threshold,
            "probe_timeout_sec": self.probe_timeout_sec,
            "deep_timeout_sec": self.deep_timeout_sec,
            "pipeline1_enabled": self.pipeline1_enabled,
            "pipeline2_enabled": self.pipeline2_enabled,
            "pipeline3_enabled": self.pipeline3_enabled,
            "pipeline4_enabled": self.pipeline4_enabled,
            "pipeline5_enabled": self.pipeline5_enabled,
            "enable_ralph": self.enable_ralph,
            "enable_riper": self.enable_riper,
            "enable_hyde": self.enable_hyde,
            "max_context_tokens": self.max_context_tokens,
            "wave3_langgraph_enabled": self.wave3_langgraph_enabled,
            "wave3_max_iterations": self.wave3_max_iterations,
            "wave3_timeout_sec": self.wave3_timeout_sec,
            "wave4_neo4j_enabled": self.wave4_neo4j_enabled,
            "wave4_max_depth": self.wave4_max_depth,
            "wave4_timeout_sec": self.wave4_timeout_sec,
        }


class CosmosSettingsCache:
    """
    In-memory singleton cache (Layer 3).

    Holds the current effective settings and refreshes from Postgres every
    `refresh_interval_sec` seconds using an asyncio background task.
    """

    def __init__(
        self,
        repo,  # WorkflowSettingsRepo instance
        refresh_interval_sec: int = _REFRESH_INTERVAL_SEC,
    ) -> None:
        self._repo = repo
        self._refresh_interval = refresh_interval_sec
        self._settings: WorkflowSettings = WorkflowSettings.balanced()
        self._last_refresh: float = 0.0
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Load settings from Postgres on startup and start the refresh loop."""
        await self._refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("cosmos_settings_cache.initialized", settings=self._settings.to_dict())

    async def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    def get(self) -> WorkflowSettings:
        """Return current in-memory settings — zero latency."""
        return self._settings

    async def refresh_now(self) -> WorkflowSettings:
        """Force an immediate refresh from Postgres."""
        await self._refresh()
        return self._settings

    async def update(self, settings: WorkflowSettings) -> None:
        """Persist new settings to Postgres and update the in-memory cache."""
        await self._repo.upsert(settings)
        async with self._lock:
            self._settings = settings
            self._last_refresh = time.monotonic()

    # --- private ---

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            try:
                await self._refresh()
            except Exception as exc:
                logger.warning("cosmos_settings_cache.refresh_failed", error=str(exc))

    async def _refresh(self) -> None:
        try:
            settings = await self._repo.load()
            async with self._lock:
                self._settings = settings
                self._last_refresh = time.monotonic()
            logger.debug("cosmos_settings_cache.refreshed", quality_mode=settings.quality_mode)
        except Exception as exc:
            logger.warning(
                "cosmos_settings_cache.load_failed",
                error=str(exc),
                fallback="keeping current settings",
            )

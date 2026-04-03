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
        """Construct from a plain dict (e.g. fetched from Postgres JSON column)."""
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


import json
import os
from datetime import datetime

# Shadow promotion thresholds (Phase 5a)
# W3 (Neo4j) is promoted when graph-heavy query MRR lifts by ≥0.05
_W3_MRR_LIFT_GATE = 0.05
_W3_P95_LATENCY_MS = 5000   # combined W2+W3 p95 must stay under 5s
# W4 (LangGraph) is promoted when complex-query MRR lifts by ≥0.05
_W4_MRR_LIFT_GATE = 0.05
_W4_P95_LATENCY_MS = 8000   # combined W2+W4 p95 must stay under 8s
_W4_TRIGGER_RATE_MAX = 0.20  # if W4 fires on >20% of queries, root cause is W2 gap


async def promote_shadow_lanes_if_ready(
    settings_cache: "CosmosSettingsCache",
    benchmark_path: str = "cosmos/data/benchmark_results.json",
    promotion_log_path: str = "cosmos/data/shadow_promotion.json",
) -> dict:
    """Check promotion gates for W3 (Neo4j) and W4 (LangGraph) shadow lanes.

    Reads benchmark_results.json produced by benchmark_runner.py after each
    training run.  When both MRR lift AND latency gates pass, sets
    wave3_shadow_mode=False (or wave4_shadow_mode=False) via the settings cache
    so the lane is promoted to primary for ALL subsequent requests.

    Returns a dict describing what was promoted (if anything).
    """
    if not os.path.exists(benchmark_path):
        logger.info("shadow_promotion.no_benchmark_file", path=benchmark_path)
        return {"promoted": [], "reason": "no benchmark file"}

    try:
        with open(benchmark_path) as f:
            results = json.load(f)
    except Exception as exc:
        logger.warning("shadow_promotion.parse_error", error=str(exc))
        return {"promoted": [], "reason": f"parse error: {exc}"}

    current: WorkflowSettings = settings_cache.get()
    promoted = []
    reasons = {}

    # ── W3 (Neo4j) gate ─────────────────────────────────────────────────────
    if current.wave3_shadow_mode:
        w3 = results.get("wave3_neo4j", {})
        w3_mrr_lift: float = float(w3.get("mrr_lift_graph_queries", 0.0))
        w3_p95_ms: float = float(w3.get("p95_latency_ms_w2_w3", 9999.0))
        w3_regression: bool = bool(w3.get("tool_accuracy_regression", True))

        if w3_mrr_lift >= _W3_MRR_LIFT_GATE and w3_p95_ms <= _W3_P95_LATENCY_MS and not w3_regression:
            new_settings = WorkflowSettings(**{
                **vars(current),
                "wave3_shadow_mode": False,
            })
            await settings_cache.update(new_settings)
            promoted.append("wave3_neo4j")
            reasons["wave3_neo4j"] = (
                f"mrr_lift={w3_mrr_lift:.3f} >= {_W3_MRR_LIFT_GATE}, "
                f"p95={w3_p95_ms:.0f}ms <= {_W3_P95_LATENCY_MS}ms, "
                "no regressions"
            )
            logger.info("shadow_promotion.wave3_promoted",
                        mrr_lift=w3_mrr_lift, p95_ms=w3_p95_ms)
        else:
            reasons["wave3_neo4j"] = (
                f"NOT promoted: mrr_lift={w3_mrr_lift:.3f} (need {_W3_MRR_LIFT_GATE}), "
                f"p95={w3_p95_ms:.0f}ms (need <={_W3_P95_LATENCY_MS}ms), "
                f"regression={w3_regression}"
            )

    # ── W4 (LangGraph) gate ─────────────────────────────────────────────────
    if current.wave4_shadow_mode:
        w4 = results.get("wave4_langgraph", {})
        w4_mrr_lift: float = float(w4.get("mrr_lift_complex_queries", 0.0))
        w4_p95_ms: float = float(w4.get("p95_latency_ms_w2_w4", 9999.0))
        w4_trigger_rate: float = float(w4.get("trigger_rate", 1.0))

        if (w4_mrr_lift >= _W4_MRR_LIFT_GATE
                and w4_p95_ms <= _W4_P95_LATENCY_MS
                and w4_trigger_rate <= _W4_TRIGGER_RATE_MAX):
            # Refresh settings (W3 may have been promoted just above)
            current = settings_cache.get()
            new_settings = WorkflowSettings(**{
                **vars(current),
                "wave4_shadow_mode": False,
            })
            await settings_cache.update(new_settings)
            promoted.append("wave4_langgraph")
            reasons["wave4_langgraph"] = (
                f"mrr_lift={w4_mrr_lift:.3f} >= {_W4_MRR_LIFT_GATE}, "
                f"p95={w4_p95_ms:.0f}ms <= {_W4_P95_LATENCY_MS}ms, "
                f"trigger_rate={w4_trigger_rate:.2%} <= {_W4_TRIGGER_RATE_MAX:.0%}"
            )
            logger.info("shadow_promotion.wave4_promoted",
                        mrr_lift=w4_mrr_lift, p95_ms=w4_p95_ms,
                        trigger_rate=w4_trigger_rate)
        else:
            reasons["wave4_langgraph"] = (
                f"NOT promoted: mrr_lift={w4_mrr_lift:.3f} (need {_W4_MRR_LIFT_GATE}), "
                f"p95={w4_p95_ms:.0f}ms (need <={_W4_P95_LATENCY_MS}ms), "
                f"trigger_rate={w4_trigger_rate:.2%} (need <={_W4_TRIGGER_RATE_MAX:.0%})"
            )

    # Write promotion log
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "promoted": promoted,
        "reasons": reasons,
    }
    try:
        history = []
        if os.path.exists(promotion_log_path):
            with open(promotion_log_path) as f:
                history = json.load(f)
        history.append(log_entry)
        with open(promotion_log_path, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as exc:
        logger.warning("shadow_promotion.log_write_error", error=str(exc))

    return log_entry


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

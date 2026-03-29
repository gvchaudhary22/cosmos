"""
Model Router for COSMOS Phase 4.

Routes queries to the minimum-sufficient model tier based on
intent, confidence, and complexity signals. Three tiers:

  HAIKU  — classification, simple lookups        (~$0.0001/query)
  SONNET — standard reasoning, tool use          (~$0.003/query)
  OPUS   — complex multi-step reasoning          (~$0.01/query)
"""

import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from cosmos.app.engine.classifier import Intent

logger = structlog.get_logger()


class ModelTier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


@dataclass
class ModelProfile:
    name: str
    model_id: str
    tier: ModelTier
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_tokens: int
    strengths: List[str]


PROFILES: Dict[ModelTier, ModelProfile] = {
    ModelTier.HAIKU: ModelProfile(
        name="Haiku",
        model_id="claude-haiku-4-5-20251001",
        tier=ModelTier.HAIKU,
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.005,
        max_tokens=8192,
        strengths=["classification", "extraction", "simple_qa"],
    ),
    ModelTier.SONNET: ModelProfile(
        name="Sonnet",
        model_id="claude-sonnet-4-6",
        tier=ModelTier.SONNET,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        max_tokens=8192,
        strengths=["reasoning", "tool_use", "code", "analysis"],
    ),
    ModelTier.OPUS: ModelProfile(
        name="Opus",
        model_id="claude-opus-4-6",
        tier=ModelTier.OPUS,
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        max_tokens=4096,
        strengths=["complex_reasoning", "multi_step", "ambiguous", "security"],
    ),
}


class ModelRouter:
    """Routes queries to minimum-sufficient model."""

    def __init__(self) -> None:
        self._usage: Dict[ModelTier, int] = {t: 0 for t in ModelTier}

    def route(
        self,
        intent: Intent,
        confidence: float,
        complexity_signals: Dict = None,
    ) -> ModelProfile:
        """
        Route to the cheapest model that can handle the task.

        Routing rules:
        - LOOKUP with high confidence (>0.8) -> HAIKU
        - NAVIGATE -> HAIKU
        - EXPLAIN with single entity -> SONNET
        - ACT (any write action) -> SONNET minimum
        - REPORT with aggregation -> SONNET
        - Multi-intent / low confidence (<0.5) -> OPUS
        - Complex EXPLAIN (multi-entity, causal reasoning) -> OPUS
        - Any query with security implications -> OPUS
        """
        signals = complexity_signals or {}

        # Security always gets Opus
        if signals.get("security", False):
            return self._select(ModelTier.OPUS)

        # Low confidence -> Opus for better reasoning
        if confidence < 0.5:
            return self._select(ModelTier.OPUS)

        # Multi-intent -> Opus
        sub_intents = signals.get("sub_intents", [])
        if len(sub_intents) >= 1:
            return self._select(ModelTier.OPUS)

        # Complex EXPLAIN (multi-entity or causal)
        if intent == Intent.EXPLAIN:
            entity_count = signals.get("entity_count", 1)
            if entity_count > 1 or signals.get("causal", False):
                return self._select(ModelTier.OPUS)
            return self._select(ModelTier.SONNET)

        # LOOKUP with high confidence -> Haiku
        if intent == Intent.LOOKUP and confidence > 0.8:
            return self._select(ModelTier.HAIKU)

        # NAVIGATE -> Haiku
        if intent == Intent.NAVIGATE:
            return self._select(ModelTier.HAIKU)

        # ACT -> Sonnet minimum
        if intent == Intent.ACT:
            return self._select(ModelTier.SONNET)

        # REPORT -> Sonnet
        if intent == Intent.REPORT:
            return self._select(ModelTier.SONNET)

        # UNKNOWN with ok confidence -> Sonnet
        if intent == Intent.UNKNOWN:
            return self._select(ModelTier.SONNET)

        # Default: LOOKUP with moderate confidence, etc.
        return self._select(ModelTier.SONNET)

    def estimate_cost(
        self, tier: ModelTier, input_tokens: int, output_tokens: int
    ) -> float:
        """Estimate cost in USD for a query."""
        profile = PROFILES[tier]
        input_cost = (input_tokens / 1000) * profile.cost_per_1k_input
        output_cost = (output_tokens / 1000) * profile.cost_per_1k_output
        return round(input_cost + output_cost, 6)

    def get_usage_stats(self) -> Dict:
        """Return model usage breakdown."""
        total = sum(self._usage.values())
        return {
            "total_queries": total,
            "by_tier": dict(self._usage),
            "distribution": {
                t.value: (self._usage[t] / total * 100 if total > 0 else 0.0)
                for t in ModelTier
            },
        }

    def _select(self, tier: ModelTier) -> ModelProfile:
        self._usage[tier] += 1
        logger.info("model_router.selected", tier=tier.value)
        return PROFILES[tier]

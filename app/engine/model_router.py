"""
Model Router for COSMOS Phase 4.

Quality-first routing: uses Opus for all substantive tasks, Haiku only
for pure classification. Three tiers:

  HAIKU  — intent/entity classification only     (~$0.0001/query)
  SONNET — (reserved; not used in default routing)
  OPUS   — everything else: lookup, explain, act, report, navigate,
            unknown, low-confidence, multi-intent (~$0.01/query)
"""

import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from app.engine.classifier import Intent

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
        Quality-first routing: almost everything goes to Opus.

        Routing rules (quality-first policy):
        - ALL intents (LOOKUP, EXPLAIN, ACT, REPORT, NAVIGATE, UNKNOWN) -> OPUS
        - Low confidence (<0.5) -> OPUS
        - Multi-intent -> OPUS
        - Security signals -> OPUS
        - HAIKU is never selected here; use route_classify() for classification tasks.

        Callers that explicitly need Haiku for classification should call
        route_classify() instead.
        """
        # Everything goes to Opus — quality over cost.
        return self._select(ModelTier.OPUS)

    def route_classify(
        self,
        intent: Intent,
        confidence: float,
        complexity_signals: Dict = None,
    ) -> ModelProfile:
        """
        Route a pure classification / entity-extraction task to Haiku.

        This method exists exclusively for the classifier pipeline, where
        speed and low cost matter more than deep reasoning. All other callers
        should use route().
        """
        return self._select(ModelTier.HAIKU)

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

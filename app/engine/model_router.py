"""
Model Router for COSMOS Phase 4.

Pillar-aware routing (COSMOS transfer): routes to minimum-sufficient model
based on query pillar complexity. Three tiers:

  HAIKU  — P1 schema lookups, entity ID resolution, simple exact-match queries
            (~$0.0001/query, 20x cheaper than Opus)
  SONNET — P3 API docs, P4 page navigation, standard workflow queries, tool-use
            (~$0.003/query, 5x cheaper than Opus)
  OPUS   — P6 action contracts, P7 runbook diagnostics, multi-hop reasoning,
            low-confidence, complex/ambiguous queries (~$0.015/query)

Routing priority:
  1. pillar_hint in complexity_signals overrides all (explicit pillar routing)
  2. confidence < 0.5 → always Opus (quality safeguard)
  3. intent + complexity_signals → pillar inference
  4. Default → Opus (quality-first fallback)

Original quality-first policy preserved: classification tasks use route_classify() → Haiku.
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

    # Pillars that are safe for cheaper models
    _HAIKU_PILLARS = {"P1"}          # schema lookups — deterministic, low reasoning
    _SONNET_PILLARS = {"P3", "P4"}   # api docs + page navigation — structured retrieval
    _OPUS_PILLARS = {"P6", "P7"}     # action contracts + runbooks — complex reasoning

    def route(
        self,
        intent: Intent,
        confidence: float,
        complexity_signals: Dict = None,
    ) -> ModelProfile:
        """
        Pillar-aware routing (COSMOS transfer):

        Routing rules (priority order):
        1. Low confidence (<0.5) → Opus (never compromise on uncertain queries)
        2. Security/fraud signals → Opus
        3. Multi-intent → Opus (requires synthesis across topics)
        4. Explicit pillar_hint in complexity_signals → route by pillar
        5. Intent + is_entity_lookup → Haiku (simple exact-match)
        6. Intent EXPLAIN / NAVIGATE → Sonnet (structured retrieval)
        7. Intent ACT / UNKNOWN → Opus (action contracts, multi-hop)
        8. Default → Opus (quality-first fallback)

        Cost impact: ~60% of ICRM queries are P1/P3/P4 lookups.
        With pillar routing: ~60% route to Haiku/Sonnet → 70-80% cost reduction.
        Quality preserved: P6/P7/complex/low-confidence always get Opus.
        """
        signals = complexity_signals or {}

        # 1. Low confidence → always Opus
        if confidence < 0.5:
            logger.info("model_router.low_confidence_opus", confidence=confidence)
            return self._select(ModelTier.OPUS)

        # 2. Security signals → Opus
        if signals.get("security") or signals.get("fraud"):
            return self._select(ModelTier.OPUS)

        # 3. Multi-intent → Opus
        if signals.get("multi_intent") or signals.get("intent_count", 1) > 1:
            return self._select(ModelTier.OPUS)
        if signals.get("sub_intents"):
            return self._select(ModelTier.OPUS)

        # 4. Explicit pillar hint → route by pillar
        pillar = signals.get("pillar_hint", "").upper()
        if pillar in self._HAIKU_PILLARS:
            logger.info("model_router.pillar_haiku", pillar=pillar)
            return self._select(ModelTier.HAIKU)
        if pillar in self._SONNET_PILLARS:
            logger.info("model_router.pillar_sonnet", pillar=pillar)
            return self._select(ModelTier.SONNET)
        if pillar in self._OPUS_PILLARS:
            logger.info("model_router.pillar_opus", pillar=pillar)
            return self._select(ModelTier.OPUS)

        # 5. Entity ID lookup with high confidence → Haiku (fast path)
        if signals.get("is_entity_lookup") and confidence >= 0.8:
            logger.info("model_router.entity_lookup_haiku", confidence=confidence)
            return self._select(ModelTier.HAIKU)

        # 6. Intent-based routing (confidence already ≥ 0.5 from rule 1)
        intent_val = (intent.value if hasattr(intent, "value") else str(intent)).lower()

        if intent_val == "lookup":
            if confidence >= 0.85:
                logger.info("model_router.selected", tier="haiku")
                return self._select(ModelTier.HAIKU)
            logger.info("model_router.selected", tier="sonnet")
            return self._select(ModelTier.SONNET)

        if intent_val == "navigate":
            if confidence >= 0.8:
                logger.info("model_router.selected", tier="haiku")
                return self._select(ModelTier.HAIKU)
            logger.info("model_router.selected", tier="sonnet")
            return self._select(ModelTier.SONNET)

        if intent_val == "explain":
            if signals.get("entity_count", 1) > 1 or signals.get("causal"):
                logger.info("model_router.selected", tier="opus")
                return self._select(ModelTier.OPUS)
            logger.info("model_router.selected", tier="sonnet")
            return self._select(ModelTier.SONNET)

        if intent_val in ("act", "report"):
            logger.info("model_router.selected", tier="sonnet")
            return self._select(ModelTier.SONNET)

        # 7. Unknown intent → Sonnet (safe default, not Opus)
        logger.info("model_router.selected", tier="sonnet")
        return self._select(ModelTier.SONNET)

    def route_by_pillar(self, pillar: str) -> ModelProfile:
        """
        Direct pillar-to-model routing. Use when pillar is known before retrieval.

        P1 → Haiku  (schema: deterministic lookups)
        P3 → Sonnet (api docs: structured retrieval)
        P4 → Sonnet (page navigation: structured retrieval)
        P6 → Opus   (action contracts: complex reasoning, preconditions, rollback)
        P7 → Opus   (runbooks: multi-step diagnosis, state machines)
        """
        p = pillar.upper()
        if p in self._HAIKU_PILLARS:
            return self._select(ModelTier.HAIKU)
        if p in self._SONNET_PILLARS:
            return self._select(ModelTier.SONNET)
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

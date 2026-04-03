"""
Model Router for COSMOS Phase 4.

Quality-first policy: correctness > speed > cost.
ICRM operators make real logistics decisions — a wrong answer costs money.

  HAIKU  — ONLY for pure classification / entity-extraction tasks (route_classify())
            Never used for response generation.
  SONNET — High-confidence P1/P3/P4 lookups where answer is deterministic.
            (~$0.003/query)
  OPUS   — All action intents, all report intents, all unknown intents,
            low-confidence, complex/ambiguous, multi-hop, P6/P7.
            Default fallback. (~$0.015/query)

Routing priority:
  1. confidence < 0.6 → always Opus (raised from 0.5 for quality)
  2. Security/fraud/multi-intent → Opus
  3. intent=act or intent=report → Opus (actions have real side effects)
  4. Explicit pillar_hint P6/P7 → Opus
  5. Explicit pillar_hint P3/P4, high confidence → Sonnet
  6. Explicit pillar_hint P1, very high confidence → Sonnet
  7. Entity lookup, very high confidence → Sonnet (not Haiku)
  8. intent=lookup/navigate, very high confidence → Sonnet
  9. Default → Opus

Classification tasks use route_classify() → Haiku (no response generation).
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
        Quality-first routing — correctness > speed > cost.

        Routing rules (priority order):
        1. Low confidence (<0.6) → Opus
        2. Security/fraud signals → Opus
        3. Multi-intent → Opus
        4. intent=act or intent=report → Opus (actions have real logistics side effects)
        5. Explicit pillar_hint P6/P7 → Opus
        6. Explicit pillar_hint P3/P4, confidence ≥ 0.8 → Sonnet
        7. Explicit pillar_hint P1, confidence ≥ 0.9 → Sonnet (pure schema lookup)
        8. Entity lookup, confidence ≥ 0.9 → Sonnet
        9. intent=lookup/navigate, confidence ≥ 0.9 → Sonnet
        10. intent=explain → Opus (causal reasoning needs depth)
        11. Default → Opus
        """
        signals = complexity_signals or {}

        # 0. Force classify path (from LLMClient.classify()) → Haiku
        if signals.get("_force_classify"):
            return self._select(ModelTier.HAIKU)

        # 1. Low confidence → always Opus (raised: 0.5 → 0.6)
        if confidence < 0.6:
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

        intent_val = (intent.value if hasattr(intent, "value") else str(intent)).lower()

        # 4. Action/report intents → always Opus
        # Operators performing actions on orders/shipments need correct preconditions,
        # side effects, and rollback info. Wrong answer = real logistics damage.
        if intent_val in ("act", "report"):
            logger.info("model_router.act_report_opus", intent=intent_val)
            return self._select(ModelTier.OPUS)

        # 5. Explicit pillar hint
        pillar = signals.get("pillar_hint", "").upper()
        if pillar in self._OPUS_PILLARS:
            logger.info("model_router.pillar_opus", pillar=pillar)
            return self._select(ModelTier.OPUS)
        if pillar in self._SONNET_PILLARS and confidence >= 0.8:
            logger.info("model_router.pillar_sonnet", pillar=pillar)
            return self._select(ModelTier.SONNET)
        if pillar in self._HAIKU_PILLARS and confidence >= 0.9:
            # P1 schema: Sonnet minimum for response generation (Haiku only in classify path)
            logger.info("model_router.pillar_p1_sonnet", pillar=pillar)
            return self._select(ModelTier.SONNET)

        # 6. High-confidence structured lookups → Sonnet
        if signals.get("is_entity_lookup") and confidence >= 0.9:
            logger.info("model_router.entity_lookup_sonnet", confidence=confidence)
            return self._select(ModelTier.SONNET)

        if intent_val == "lookup" and confidence >= 0.9:
            logger.info("model_router.selected", tier="sonnet", intent=intent_val)
            return self._select(ModelTier.SONNET)

        if intent_val == "navigate" and confidence >= 0.9:
            logger.info("model_router.selected", tier="sonnet", intent=intent_val)
            return self._select(ModelTier.SONNET)

        # 7. Explain → Opus (multi-entity causal reasoning needs Opus depth)
        if intent_val == "explain":
            logger.info("model_router.selected", tier="opus", intent=intent_val)
            return self._select(ModelTier.OPUS)

        # 8. Default → Opus (quality-first fallback)
        logger.info("model_router.default_opus", intent=intent_val, confidence=confidence)
        return self._select(ModelTier.OPUS)

    def route_by_pillar(self, pillar: str) -> ModelProfile:
        """
        Direct pillar-to-model routing. Use when pillar is known before retrieval.

        P1 → Sonnet (schema lookups: Haiku removed — response generation needs reliability)
        P3 → Sonnet (api docs: structured retrieval)
        P4 → Sonnet (page navigation: structured retrieval)
        P6 → Opus   (action contracts: complex reasoning, preconditions, rollback)
        P7 → Opus   (runbooks: multi-step diagnosis, state machines)
        Default → Opus
        """
        p = pillar.upper()
        if p in self._SONNET_PILLARS or p in self._HAIKU_PILLARS:
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

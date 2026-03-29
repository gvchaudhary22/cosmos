"""
Tier Policy — Multi-signal gate for deciding when to escalate between tiers.

NOT confidence-only. A 0.55 answer with exact AWB + live tool evidence
can be safer than a 0.75 KB-only guess.

Signals:
  - answer_confidence: 0.0-1.0 from ReAct/pipeline
  - evidence_count: number of pipelines that contributed data
  - live_data_used: True if tools called real APIs (not just KB)
  - entity_resolved: True if the specific entity (order, AWB) was found
  - freshness_mode: kb_only | live_tool | db_verified

Tier gates:
  Tier 1 → respond:  composite_score >= 0.7
  Tier 1 → Tier 2:   composite_score 0.3-0.7
  Tier 1 → Tier 3:   composite_score < 0.3 (brain knows nothing)
  Tier 2 → respond:  composite_score >= 0.6
  Tier 2 → Tier 3:   composite_score < 0.6
  Tier 3 → respond:  composite_score >= 0.5
  Tier 3 → escalate: composite_score < 0.5
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import structlog

logger = structlog.get_logger()


class FreshnessMode(str, Enum):
    KB_ONLY = "kb_only"           # Answer from knowledge base only
    LIVE_TOOL = "live_tool"       # Answer includes live API data
    DB_VERIFIED = "db_verified"   # Answer confirmed by DB query


class TierDecision(str, Enum):
    RESPOND = "respond"           # Good enough, send to user
    ESCALATE_TIER2 = "tier2"      # Go to code intelligence
    ESCALATE_TIER3 = "tier3"      # Go to DB fallback (skip Tier 2)
    ESCALATE_HUMAN = "human"      # Give up, route to human agent


@dataclass
class TierSignals:
    """Multi-signal input for tier gate evaluation."""
    answer_confidence: float = 0.0
    evidence_count: int = 0          # pipelines that found data
    live_data_used: bool = False      # tools hit real APIs
    entity_resolved: bool = False     # specific entity found
    freshness_mode: FreshnessMode = FreshnessMode.KB_ONLY
    tools_used: List[str] = None
    intents_addressed: int = 0        # how many of N intents were covered
    total_intents: int = 1

    def __post_init__(self):
        if self.tools_used is None:
            self.tools_used = []


@dataclass
class TierGateResult:
    """Output of tier gate evaluation."""
    decision: TierDecision
    composite_score: float
    signals_summary: dict
    reason: str


@dataclass
class ResponseMetadata:
    """Metadata attached to every response for debugging and analytics."""
    resolution_tier: int              # 1, 2, or 3
    freshness_mode: str               # kb_only | live_tool | db_verified
    confidence: float
    composite_score: float
    used_fallbacks: List[str]         # ["tier2_code_intel", "tier3_db_tool"]
    evidence_count: int
    entity_resolved: bool
    intents_addressed: int
    total_intents: int
    tools_used: List[str]
    tiers_visited: List[int]          # [1] or [1,2] or [1,2,3]
    cost_estimate_usd: float = 0.0


class TierPolicy:
    """
    Multi-signal gate that decides tier escalation.

    Composite score formula:
      score = (confidence * 0.35)
            + (evidence_ratio * 0.20)
            + (freshness_bonus * 0.15)
            + (entity_bonus * 0.15)
            + (intent_coverage * 0.15)
    """

    # Tier gate thresholds
    TIER1_RESPOND = 0.70
    TIER1_SKIP_TO_TIER3 = 0.30
    TIER2_RESPOND = 0.60
    TIER3_RESPOND = 0.50

    # Cost estimates per tier (USD)
    TIER1_COST = 0.002
    TIER2_COST = 0.012
    TIER3_COST = 0.001

    def compute_score(self, signals: TierSignals) -> float:
        """Compute composite score from multiple signals."""
        # Confidence (0-1) — weight: 35%
        conf = min(1.0, max(0.0, signals.answer_confidence))

        # Evidence ratio (0-1) — weight: 20%
        # More pipelines contributing = higher confidence in answer
        evidence_ratio = min(1.0, signals.evidence_count / 5.0)

        # Freshness bonus (0-1) — weight: 15%
        freshness_map = {
            FreshnessMode.KB_ONLY: 0.3,
            FreshnessMode.LIVE_TOOL: 0.8,
            FreshnessMode.DB_VERIFIED: 1.0,
        }
        freshness = freshness_map.get(signals.freshness_mode, 0.3)

        # Entity resolution bonus (0 or 1) — weight: 15%
        entity = 1.0 if signals.entity_resolved else 0.0

        # Intent coverage (0-1) — weight: 15%
        if signals.total_intents > 0:
            intent_cov = signals.intents_addressed / signals.total_intents
        else:
            intent_cov = 1.0

        composite = (
            conf * 0.35
            + evidence_ratio * 0.20
            + freshness * 0.15
            + entity * 0.15
            + intent_cov * 0.15
        )

        return round(min(1.0, composite), 3)

    def evaluate_tier1(self, signals: TierSignals) -> TierGateResult:
        """Evaluate whether Tier 1 result is good enough or needs escalation."""
        score = self.compute_score(signals)

        summary = {
            "confidence": signals.answer_confidence,
            "evidence_count": signals.evidence_count,
            "live_data": signals.live_data_used,
            "entity_resolved": signals.entity_resolved,
            "freshness": signals.freshness_mode.value,
            "intent_coverage": f"{signals.intents_addressed}/{signals.total_intents}",
        }

        # Bug 1 fix: KB-only answers for entity-specific queries are unreliable.
        # If a specific entity (order, AWB, shipment) was resolved but no live data was
        # fetched, the KB cannot authoritatively answer — force escalation to Tier 2
        # to get real-time data, regardless of how high the KB confidence appears.
        if (
            score >= self.TIER1_RESPOND
            and signals.freshness_mode == FreshnessMode.KB_ONLY
            and signals.entity_resolved
            and not signals.live_data_used
        ):
            score = self.TIER1_RESPOND - 0.01  # nudge below threshold
            logger.info(
                "tier_policy.tier1_kb_entity_forced_escalation",
                original_score=round(score + 0.01, 3),
                capped_score=round(score, 3),
                reason="entity_resolved_requires_live_data",
            )

        if score >= self.TIER1_RESPOND:
            decision = TierDecision.RESPOND
            reason = f"Tier 1 sufficient (score={score:.2f} >= {self.TIER1_RESPOND})"
        elif score < self.TIER1_SKIP_TO_TIER3:
            decision = TierDecision.ESCALATE_TIER3
            reason = f"Tier 1 too weak (score={score:.2f} < {self.TIER1_SKIP_TO_TIER3}), skip to Tier 3"
        else:
            decision = TierDecision.ESCALATE_TIER2
            reason = f"Tier 1 partial (score={score:.2f}), escalate to Tier 2"

        logger.info("tier_policy.tier1", decision=decision.value, score=score, **summary)
        return TierGateResult(decision=decision, composite_score=score, signals_summary=summary, reason=reason)

    def evaluate_tier2(self, signals: TierSignals) -> TierGateResult:
        """Evaluate whether Tier 2 retry result is good enough."""
        score = self.compute_score(signals)

        summary = {
            "confidence": signals.answer_confidence,
            "evidence_count": signals.evidence_count,
            "freshness": signals.freshness_mode.value,
        }

        if score >= self.TIER2_RESPOND:
            decision = TierDecision.RESPOND
            reason = f"Tier 2 sufficient (score={score:.2f} >= {self.TIER2_RESPOND})"
        else:
            decision = TierDecision.ESCALATE_TIER3
            reason = f"Tier 2 still weak (score={score:.2f} < {self.TIER2_RESPOND}), go to Tier 3"

        logger.info("tier_policy.tier2", decision=decision.value, score=score)
        return TierGateResult(decision=decision, composite_score=score, signals_summary=summary, reason=reason)

    def evaluate_tier3(self, signals: TierSignals) -> TierGateResult:
        """Evaluate whether Tier 3 DB result is good enough or must escalate to human."""
        score = self.compute_score(signals)

        if score >= self.TIER3_RESPOND:
            decision = TierDecision.RESPOND
            reason = f"Tier 3 sufficient (score={score:.2f} >= {self.TIER3_RESPOND})"
        else:
            decision = TierDecision.ESCALATE_HUMAN
            reason = f"All tiers exhausted (score={score:.2f} < {self.TIER3_RESPOND}), escalate to human"

        logger.info("tier_policy.tier3", decision=decision.value, score=score)
        return TierGateResult(decision=decision, composite_score=score, signals_summary={}, reason=reason)

    def estimate_cost(self, tiers_visited: List[int]) -> float:
        """Estimate total cost based on tiers used."""
        cost = 0.0
        if 1 in tiers_visited:
            cost += self.TIER1_COST
        if 2 in tiers_visited:
            cost += self.TIER2_COST
        if 3 in tiers_visited:
            cost += self.TIER3_COST
        return round(cost, 4)

    def build_metadata(
        self,
        resolution_tier: int,
        signals: TierSignals,
        composite_score: float,
        tiers_visited: List[int],
        used_fallbacks: List[str],
    ) -> ResponseMetadata:
        """Build response metadata for every answer."""
        return ResponseMetadata(
            resolution_tier=resolution_tier,
            freshness_mode=signals.freshness_mode.value,
            confidence=signals.answer_confidence,
            composite_score=composite_score,
            used_fallbacks=used_fallbacks,
            evidence_count=signals.evidence_count,
            entity_resolved=signals.entity_resolved,
            intents_addressed=signals.intents_addressed,
            total_intents=signals.total_intents,
            tools_used=signals.tools_used or [],
            tiers_visited=tiers_visited,
            cost_estimate_usd=self.estimate_cost(tiers_visited),
        )

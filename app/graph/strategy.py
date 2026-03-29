"""
GREL Strategy E — Hybrid Graph Retrieval.

Wraps HybridRetriever + ContextAssembler into the GREL strategy interface
so it runs alongside Strategies A-D during the GATHER phase.

This strategy contributes:
  - Relevant API docs, table schemas, tool metadata from the typed graph
  - Token-budgeted context ready for LLM synthesis
  - Entity resolution via exact lookup
  - Relationship chains showing how entities connect

Cost: $0 (pure DB queries, no LLM calls)
Latency: ~50-200ms depending on graph size
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.brain.tournament import StrategyName, StrategyResult
from app.engine.tier_policy import FreshnessMode, TierPolicy, TierSignals
from app.graph.context import ContextAssembler
from app.graph.retrieval import HybridRetriever, hybrid_retriever

# Shared TierPolicy instance for composite scoring
_tier_policy = TierPolicy()


async def hybrid_retrieval_strategy(
    query: str,
    intent: str,
    entity: str,
    entity_id: Optional[str] = None,
    retriever: Optional[HybridRetriever] = None,
    max_context_tokens: int = 4000,
) -> StrategyResult:
    """GREL Strategy E: Hybrid graph retrieval.

    Runs 4-leg parallel retrieval, fuses with RRF, assembles
    token-budgeted context, and returns as a StrategyResult.
    """
    start = time.monotonic()
    _retriever = retriever or hybrid_retriever

    try:
        result = await _retriever.retrieve(
            query=query,
            intent=intent if intent else None,
            entity=entity if entity else None,
            entity_id=entity_id,
            top_k=10,
            max_depth=2,
        )

        # Assemble context
        assembler = ContextAssembler(max_tokens=max_context_tokens)
        ctx = assembler.assemble(result)

        # Determine tool suggestion from top-ranked node
        tool_used = None
        params: Dict[str, Any] = {}
        for rn in result.ranked_nodes:
            node = rn.node
            if node.node_type.value == "tool":
                tool_used = node.label
                params = node.properties
                break
            if node.node_type.value == "api_endpoint":
                tool_used = node.properties.get("candidate_tool") or node.label
                params = {
                    "method": node.properties.get("method", ""),
                    "path": node.properties.get("path", ""),
                    "domain": node.properties.get("domain", ""),
                }
                break

        # Confidence based on retrieval quality
        confidence = _compute_confidence(result)

        latency = (time.monotonic() - start) * 1000

        return StrategyResult(
            strategy=StrategyName.HYBRID_RETRIEVAL,
            answer=ctx.text,
            confidence=confidence,
            tool_used=tool_used,
            params_extracted=params,
            latency_ms=latency,
            cost_usd=0.0,  # Pure DB queries, no LLM
            tokens_used=ctx.token_estimate,
        )

    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return StrategyResult(
            strategy=StrategyName.HYBRID_RETRIEVAL,
            answer="",
            confidence=0.0,
            latency_ms=latency,
            error=str(e),
        )


def _compute_confidence(result) -> float:
    """Compute confidence via TierPolicy's 5-signal composite score.

    Signals fed from retrieval result:
      - answer_confidence: base quality from leg agreement + top score
      - evidence_count: number of legs that returned hits
      - entity_resolved: exact_lookup leg found the entity
      - freshness_mode: KB_ONLY (pure graph retrieval, no live API)
      - intents_addressed: 1 if we have nodes, 0 otherwise
    """
    if not result.ranked_nodes:
        return 0.0

    # Derive a raw answer_confidence from retrieval signals
    top = result.ranked_nodes[0]
    leg_count = len(top.sources)
    raw_conf = min(0.3 + (leg_count * 0.15), 0.9)
    if "exact_lookup" in top.sources:
        raw_conf = min(raw_conf + 0.1, 0.95)
    if len(result.ranked_nodes) < 3:
        raw_conf *= 0.8

    signals = TierSignals(
        answer_confidence=raw_conf,
        evidence_count=result.evidence_count,
        entity_resolved=result.entity_resolved,
        freshness_mode=FreshnessMode.KB_ONLY,
        intents_addressed=1 if result.ranked_nodes else 0,
        total_intents=1,
    )

    return _tier_policy.compute_score(signals)


def create_hybrid_strategy_fn(
    retriever: Optional[HybridRetriever] = None,
    max_context_tokens: int = 4000,
):
    """Factory that returns an async strategy function matching GREL's interface.

    Usage in wiring:
        engine.register_strategy(
            StrategyName.HYBRID_RETRIEVAL,
            create_hybrid_strategy_fn(),
        )
    """
    async def _strategy(query, intent, entity, entity_id=None):
        return await hybrid_retrieval_strategy(
            query=query,
            intent=intent,
            entity=entity,
            entity_id=entity_id,
            retriever=retriever,
            max_context_tokens=max_context_tokens,
        )
    return _strategy

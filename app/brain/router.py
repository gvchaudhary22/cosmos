"""
3-Tier Intelligent Router — COSMOS Brain.

Replaces pure RAG with a layered approach that is faster, cheaper, and
more accurate:

  Tier 1: Decision Tree (~60% of queries) — O(1), $0, <5ms
    Exact/fuzzy match on intent_tags + domain from knowledge_base YAML.
    Handles well-structured queries like "show order 12345", "track shipment AWB123".

  Tier 2: Tool-Use with Domain Scoping (~30%) — low cost, ~200ms
    Scope tools to the matched domain (50-200 tools), natively selects tool.
    Uses Anthropic tool_use format uses structured tool definitions.

  Tier 3: Full Reasoning with Few-Shot (~10%) — higher cost, ~1s
    Full ReAct loop with few-shot examples from examples.yaml.
    For complex multi-step queries that need reasoning.

Each tier has a confidence gate — if confidence is high enough, skip higher tiers.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.brain.indexer import KBDocument, KnowledgeIndexer


class RoutingTier(str, Enum):
    DECISION_TREE = "tier1_decision_tree"
    TOOL_USE = "tier2_tool_use"
    FULL_REASONING = "tier3_full_reasoning"
    ESCALATE = "escalate"


@dataclass
class RouteResult:
    """Result of the routing decision."""

    tier: RoutingTier
    selected_api: Optional[KBDocument] = None
    selected_tools: List[KBDocument] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    extracted_params: Dict[str, Any] = field(default_factory=dict)

    # Cost tracking
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    # For Tier 2: Claude tool definitions to send
    tool_definitions: List[dict] = field(default_factory=list)

    # For Tier 3: few-shot examples to include
    few_shot_examples: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tier 1: Decision Tree — exact/fuzzy match from knowledge_base
# ---------------------------------------------------------------------------

# Map COSMOS intents to knowledge_base tool_assignment read_write_type
_INTENT_TO_RW = {
    "lookup": "read",
    "explain": "read",
    "report": "read",
    "navigate": "read",
    "act": "write",
}

# Map COSMOS entities to knowledge_base domains
_ENTITY_TO_DOMAIN = {
    "order": "orders",
    "shipment": "shipping",
    "return": "returns",
    "payment": "payments",
    "ndr": "ndr",
    "billing": "billing",
    "wallet": "wallet",
    "customer": "customers",
    "seller": "sellers",
}

# Common action verbs mapped to tool_candidate patterns
_ACTION_TO_TOOL_PATTERN = {
    "cancel": "cancel",
    "refund": "refund",
    "track": "track",
    "reattempt": "reattempt",
    "escalate": "escalate",
    "block": "block",
    "unblock": "unblock",
    "update": "update",
    "reship": "reship",
    "reassign": "reassign",
}


class DecisionTreeRouter:
    """Tier 1: O(1) routing from pre-built lookup tables.

    Built from knowledge_base tool_agent_tags.yaml fields:
      - intent_tags.primary → maps to intent
      - tool_assignment.tool_group → maps to entity/domain
      - tool_assignment.read_write_type → filters read/write
      - routing_hints.prefer_when / avoid_when → condition matching
    """

    def __init__(self, indexer: KnowledgeIndexer):
        self._indexer = indexer
        # Pre-built lookup tables
        self._by_intent_domain: Dict[str, List[KBDocument]] = {}
        self._by_tool_candidate: Dict[str, KBDocument] = {}
        self._by_tool_group: Dict[str, List[KBDocument]] = {}
        self._built = False

    def build_routing_table(self) -> int:
        """Build lookup tables from indexed documents. Returns entry count."""
        if not self._indexer.is_indexed:
            return 0

        self._by_intent_domain.clear()
        self._by_tool_candidate.clear()
        self._by_tool_group.clear()

        count = 0
        for doc_id in list(self._indexer._documents.keys()):
            doc = self._indexer._documents[doc_id]
            if doc.doc_type != "api":
                continue

            # Index by primary intent tag
            for tag in doc.intent_tags:
                key = f"{tag}:{doc.domain}"
                self._by_intent_domain.setdefault(key, []).append(doc)
                # Also index without domain for broader matches
                self._by_intent_domain.setdefault(tag, []).append(doc)

            # Index by tool_candidate name
            if doc.tool_candidate:
                self._by_tool_candidate[doc.tool_candidate] = doc

            # Index by tool_group (domain)
            if doc.domain:
                self._by_tool_group.setdefault(doc.domain, []).append(doc)

            count += 1

        self._built = True
        return count

    def route(
        self, intent: str, entity: str, entity_id: Optional[str], query: str
    ) -> RouteResult:
        """Try to route using decision tree.

        Returns RouteResult with confidence > 0.8 if exact match found.
        """
        if not self._built:
            return RouteResult(
                tier=RoutingTier.DECISION_TREE,
                confidence=0.0,
                reasoning="Routing table not built",
            )

        intent_lower = intent.lower()
        domain = _ENTITY_TO_DOMAIN.get(entity.lower(), entity.lower())
        rw_type = _INTENT_TO_RW.get(intent_lower, "read")

        # Strategy 1: Direct intent:domain match
        key = f"{intent_lower}_{domain}" if domain else intent_lower
        candidates = []

        # Try exact intent tag match with domain
        for tag_key, docs in self._by_intent_domain.items():
            if domain and domain in tag_key and intent_lower in tag_key:
                candidates.extend(docs)
            elif intent_lower in tag_key and not domain:
                candidates.extend(docs)

        # Filter by read/write type
        if candidates:
            filtered = [
                d for d in candidates if d.read_write_type == rw_type
            ]
            if filtered:
                candidates = filtered

        # Strategy 2: Action verb → tool_candidate match
        if not candidates:
            action = self._extract_action(query)
            if action and action in _ACTION_TO_TOOL_PATTERN:
                pattern = _ACTION_TO_TOOL_PATTERN[action]
                for tc_name, doc in self._by_tool_candidate.items():
                    if pattern in tc_name.lower():
                        candidates.append(doc)

        # Strategy 3: Domain-only match (broad)
        if not candidates and domain:
            domain_docs = self._by_tool_group.get(domain, [])
            if domain_docs:
                # Filter by read/write
                filtered = [
                    d for d in domain_docs if d.read_write_type == rw_type
                ]
                candidates = filtered or domain_docs[:5]

        if not candidates:
            return RouteResult(
                tier=RoutingTier.DECISION_TREE,
                confidence=0.0,
                reasoning=f"No match for intent={intent_lower} domain={domain}",
            )

        # Deduplicate
        seen = set()
        unique = []
        for c in candidates:
            if c.doc_id not in seen:
                seen.add(c.doc_id)
                unique.append(c)
        candidates = unique

        # Score and rank
        best = self._rank_candidates(candidates, query, intent_lower, domain)

        if best is None:
            return RouteResult(
                tier=RoutingTier.DECISION_TREE,
                confidence=0.0,
                reasoning="No candidate passed scoring",
            )

        # Check negative examples
        if self._matches_negative(best, query):
            return RouteResult(
                tier=RoutingTier.DECISION_TREE,
                confidence=0.3,
                reasoning=f"Matched negative example for {best.doc_id}",
                selected_tools=candidates[:5],
            )

        # Extract basic params
        params = {}
        if entity_id:
            params["id"] = entity_id

        confidence = 0.9 if len(candidates) == 1 else 0.8

        return RouteResult(
            tier=RoutingTier.DECISION_TREE,
            selected_api=best,
            selected_tools=candidates[:5],
            confidence=confidence,
            reasoning=f"Exact match: {best.doc_id} via intent={intent_lower} domain={domain}",
            extracted_params=params,
            tokens_used=0,
            cost_usd=0.0,
        )

    def _extract_action(self, query: str) -> Optional[str]:
        """Extract the primary action verb from the query."""
        q = query.lower()
        for action in _ACTION_TO_TOOL_PATTERN:
            if re.search(rf"\b{action}\b", q):
                return action
        return None

    def _rank_candidates(
        self,
        candidates: List[KBDocument],
        query: str,
        intent: str,
        domain: str,
    ) -> Optional[KBDocument]:
        """Rank candidates by relevance score."""
        if not candidates:
            return None

        scored = []
        query_lower = query.lower()
        query_words = set(re.findall(r"\w+", query_lower))

        for doc in candidates:
            score = 0.0

            # Keyword overlap
            doc_keywords = set(
                k.lower() for k in doc.keywords + doc.aliases
            )
            overlap = len(query_words & doc_keywords)
            score += overlap * 0.3

            # Intent tag match
            for tag in doc.intent_tags:
                if intent in tag.lower() or domain in tag.lower():
                    score += 0.4
                    break

            # Example query similarity (simple word overlap)
            for eq in doc.example_queries:
                eq_words = set(re.findall(r"\w+", eq.lower()))
                ex_overlap = len(query_words & eq_words) / max(
                    len(query_words), 1
                )
                score = max(score, score + ex_overlap * 0.3)

            scored.append((doc, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0] if scored else None

    def _matches_negative(self, doc: KBDocument, query: str) -> bool:
        """Check if query matches any negative routing examples."""
        query_lower = query.lower()
        for neg in doc.negative_examples:
            neg_query = neg.get("user_query", "").lower()
            if not neg_query:
                continue
            # Simple word overlap check
            neg_words = set(re.findall(r"\w+", neg_query))
            query_words = set(re.findall(r"\w+", query_lower))
            overlap = len(neg_words & query_words) / max(len(neg_words), 1)
            if overlap > 0.6:
                return True
        return False


# ---------------------------------------------------------------------------
# Tier 2: Tool-Use — domain-scoped native tool selection
# ---------------------------------------------------------------------------


class ToolUseRouter:
    """Tier 2: Build tool_use definitions from knowledge_base.

    Instead of sending all 5,620 tools, scope to the matched domain
    (typically 50-200 tools) and natively selects tool.
    """

    def __init__(self, indexer: KnowledgeIndexer):
        self._indexer = indexer

    def build_tool_definitions(
        self,
        domain: Optional[str] = None,
        rw_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Build Anthropic-format tool definitions from knowledge_base.

        Returns list of dicts compatible with Claude's tool_use format:
        [{"name": "...", "description": "...", "input_schema": {...}}]
        """
        tools = []

        for doc in self._indexer._documents.values():
            if doc.doc_type != "api":
                continue
            if domain and doc.domain != domain:
                continue
            if rw_type and doc.read_write_type != rw_type:
                continue

            # Build tool definition
            tool_def = {
                "name": self._safe_tool_name(doc.doc_id),
                "description": (
                    f"{doc.method} {doc.path} — {doc.summary or doc.doc_id}. "
                    f"Domain: {doc.domain}. "
                    f"Intent: {', '.join(doc.intent_tags[:3])}."
                ),
                "input_schema": self._build_schema(doc),
            }
            tools.append(tool_def)

            if len(tools) >= limit:
                break

        return tools

    def route(
        self,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str],
        tier1_candidates: List[KBDocument],
    ) -> RouteResult:
        """Prepare Tier 2 routing — builds tool definitions for LLM.

        The actual LLM call happens in the engine, not here.
        This method prepares the payload.
        """
        domain = _ENTITY_TO_DOMAIN.get(entity.lower(), entity.lower())
        rw_type = _INTENT_TO_RW.get(intent.lower(), None)

        # If Tier 1 gave us candidates, use their domain
        if tier1_candidates:
            domains = list(set(c.domain for c in tier1_candidates))
            if len(domains) == 1:
                domain = domains[0]

        tool_defs = self.build_tool_definitions(
            domain=domain, rw_type=rw_type, limit=50
        )

        if not tool_defs:
            # Broaden: try without rw_type filter
            tool_defs = self.build_tool_definitions(
                domain=domain, limit=50
            )

        if not tool_defs:
            # Broadest: all tools in domain
            tool_defs = self.build_tool_definitions(limit=30)

        # Collect few-shot examples from candidates
        few_shots = []
        for doc in tier1_candidates[:3]:
            for ex in doc.param_examples[:2]:
                few_shots.append(
                    {
                        "query": ex.get("query", ""),
                        "tool": doc.doc_id,
                        "params": ex.get("params", {}),
                    }
                )

        return RouteResult(
            tier=RoutingTier.TOOL_USE,
            selected_tools=tier1_candidates[:5],
            confidence=0.0,  # TBD after Claude responds
            reasoning=f"Scoped to {len(tool_defs)} tools in domain={domain}",
            tool_definitions=tool_defs,
            few_shot_examples=few_shots,
            # Token estimate: ~100 tokens per tool definition
            tokens_used=len(tool_defs) * 100,
        )

    @staticmethod
    def _safe_tool_name(doc_id: str) -> str:
        """Convert API doc_id to a valid tool name (alphanumeric + underscore)."""
        return re.sub(r"[^a-zA-Z0-9_]", "_", doc_id)[:64]

    @staticmethod
    def _build_schema(doc: KBDocument) -> dict:
        """Build a basic JSON Schema from the KBDocument.

        For production, this should read request_schema.yaml.
        """
        properties: Dict[str, dict] = {}
        required: List[str] = []

        # Extract param hints from examples
        if doc.param_examples:
            for ex in doc.param_examples[:3]:
                params = ex.get("params", {})
                for key, value in params.items():
                    if key not in properties:
                        ptype = "string"
                        if isinstance(value, int):
                            ptype = "integer"
                        elif isinstance(value, float):
                            ptype = "number"
                        elif isinstance(value, bool):
                            ptype = "boolean"
                        properties[key] = {"type": ptype}

        # Always allow a generic ID param
        if "id" not in properties:
            properties["id"] = {
                "type": "string",
                "description": "Entity ID (order ID, AWB, etc.)",
            }

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


# ---------------------------------------------------------------------------
# Main Router — orchestrates all 3 tiers
# ---------------------------------------------------------------------------


class IntelligentRouter:
    """3-tier routing engine.

    Usage:
        router = IntelligentRouter(indexer)
        router.build()  # Call after indexer.index_all()
        result = router.route(intent, entity, entity_id, query)
    """

    # Confidence thresholds
    TIER1_THRESHOLD = 0.75  # Above this → use Tier 1 result directly
    TIER2_THRESHOLD = 0.50  # Above this → use Tier 2 (Claude tool-use)
    # Below TIER2 → use Tier 3 (full reasoning)

    def __init__(self, indexer: KnowledgeIndexer):
        self._indexer = indexer
        self._tier1 = DecisionTreeRouter(indexer)
        self._tier2 = ToolUseRouter(indexer)

    def build(self) -> dict:
        """Build routing tables. Call after indexer.index_all().

        Returns stats about the routing table.
        """
        entry_count = self._tier1.build_routing_table()
        return {
            "tier1_entries": entry_count,
            "tier2_tool_count": self._indexer.document_count,
        }

    def route(
        self,
        intent: str,
        entity: str,
        entity_id: Optional[str],
        query: str,
        confidence_override: Optional[float] = None,
    ) -> RouteResult:
        """Route a query through the 3-tier system.

        Args:
            intent: Classified intent (lookup, explain, act, report, navigate, unknown)
            entity: Classified entity (order, shipment, payment, etc.)
            entity_id: Extracted entity ID if any
            query: Raw user query text
            confidence_override: Force a specific tier (for testing)

        Returns:
            RouteResult with the tier used and prepared data for that tier.
        """
        # Unknown intent → skip Tier 1, go to Tier 2 or 3
        if intent.lower() == "unknown":
            return self._route_unknown(query, entity, entity_id)

        # --- Tier 1: Decision Tree ---
        t1_result = self._tier1.route(intent, entity, entity_id, query)

        effective_confidence = (
            confidence_override
            if confidence_override is not None
            else t1_result.confidence
        )

        if effective_confidence >= self.TIER1_THRESHOLD:
            # High confidence → use decision tree result directly
            return t1_result

        # --- Tier 2: Tool-Use ---
        if effective_confidence >= self.TIER2_THRESHOLD:
            # Medium confidence → prepare domain-scoped tools for Claude
            return self._tier2.route(
                query=query,
                intent=intent,
                entity=entity,
                entity_id=entity_id,
                tier1_candidates=t1_result.selected_tools,
            )

        # --- Tier 3: Full Reasoning ---
        return self._route_full_reasoning(
            query, intent, entity, entity_id, t1_result
        )

    def _route_unknown(
        self, query: str, entity: str, entity_id: Optional[str]
    ) -> RouteResult:
        """Route unknown-intent queries — skip Tier 1."""
        # Try TF-IDF search as a softer match
        results = self._indexer.search(query, top_k=5)

        if results and results[0][1] > 0.3:
            # Decent TF-IDF match → Tier 2 with candidates
            candidates = [doc for doc, _ in results]
            return self._tier2.route(
                query=query,
                intent="unknown",
                entity=entity,
                entity_id=entity_id,
                tier1_candidates=candidates,
            )

        # No match → Tier 3
        return RouteResult(
            tier=RoutingTier.FULL_REASONING,
            confidence=0.0,
            reasoning="Unknown intent, no KB match — full reasoning needed",
            few_shot_examples=[],
        )

    def _route_full_reasoning(
        self,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str],
        tier1_result: RouteResult,
    ) -> RouteResult:
        """Tier 3: Prepare for full ReAct reasoning with few-shot examples."""
        # Gather few-shot examples from any Tier 1 candidates
        few_shots = []
        for doc in tier1_result.selected_tools[:3]:
            for ex in doc.param_examples[:2]:
                few_shots.append(
                    {
                        "query": ex.get("query", ""),
                        "tool": doc.doc_id,
                        "params": ex.get("params", {}),
                    }
                )

        # Also search TF-IDF for broader context
        tf_results = self._indexer.search(query, top_k=3)
        for doc, score in tf_results:
            if doc.doc_id not in {
                d.doc_id for d in tier1_result.selected_tools
            }:
                for ex in doc.param_examples[:1]:
                    few_shots.append(
                        {
                            "query": ex.get("query", ""),
                            "tool": doc.doc_id,
                            "params": ex.get("params", {}),
                        }
                    )

        return RouteResult(
            tier=RoutingTier.FULL_REASONING,
            selected_tools=tier1_result.selected_tools[:5],
            confidence=tier1_result.confidence,
            reasoning=(
                f"Low confidence ({tier1_result.confidence:.2f}) — "
                f"full reasoning with {len(few_shots)} few-shot examples"
            ),
            few_shot_examples=few_shots,
        )

    def get_stats(self) -> dict:
        """Return routing stats."""
        return {
            "indexer": self._indexer.get_stats(),
            "tier1_built": self._tier1._built,
            "tier1_intent_domain_keys": len(self._tier1._by_intent_domain),
            "tier1_tool_candidates": len(self._tier1._by_tool_candidate),
            "tier1_domain_groups": len(self._tier1._by_tool_group),
        }

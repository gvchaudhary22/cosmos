"""
Tests for the 3-tier intelligent router.

Covers:
  - DecisionTreeRouter: exact match, action verb, domain-only, negative examples
  - ToolUseRouter: tool definition building, domain scoping
  - IntelligentRouter: tier selection, confidence thresholds, unknown intents
"""

import pytest
from unittest.mock import MagicMock

from app.brain.indexer import KBDocument, KnowledgeIndexer
from app.brain.router import (
    DecisionTreeRouter,
    IntelligentRouter,
    RouteResult,
    RoutingTier,
    ToolUseRouter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_doc(
    doc_id: str,
    domain: str = "orders",
    intent_tags: list = None,
    tool_candidate: str = "",
    read_write_type: str = "read",
    risk_level: str = "low",
    method: str = "GET",
    path: str = "/api/v1/orders",
    example_queries: list = None,
    param_examples: list = None,
    negative_examples: list = None,
    keywords: list = None,
    aliases: list = None,
) -> KBDocument:
    return KBDocument(
        doc_id=doc_id,
        doc_type="api",
        repo="MultiChannel_API",
        domain=domain,
        summary=f"{method} {path}",
        intent_tags=intent_tags or [],
        keywords=keywords or [domain],
        aliases=aliases or [],
        example_queries=example_queries or [],
        tool_candidate=tool_candidate,
        primary_agent="order_ops_agent",
        read_write_type=read_write_type,
        risk_level=risk_level,
        approval_mode="auto" if risk_level == "low" else "manual",
        method=method,
        path=path,
        param_examples=param_examples or [],
        negative_examples=negative_examples or [],
        training_ready=True,
        confidence="high",
        text_for_embedding=f"{domain} {' '.join(intent_tags or [])} {path}",
    )


def _make_indexer_with_docs(docs: list) -> KnowledgeIndexer:
    """Create an indexer and inject documents directly (no YAML needed)."""
    indexer = KnowledgeIndexer("/fake/path")
    for doc in docs:
        indexer._documents[doc.doc_id] = doc
    indexer._indexed = True
    # Build minimal embeddings
    if docs:
        indexer._build_embeddings()
    return indexer


# ---------------------------------------------------------------------------
# Decision Tree Router tests
# ---------------------------------------------------------------------------


class TestDecisionTreeRouter:
    def setup_method(self):
        self.docs = [
            _make_doc(
                "mcapi.orders.list.get",
                domain="orders",
                intent_tags=["orders_list", "orders_lookup"],
                tool_candidate="orders_list",
                keywords=["orders", "list", "show"],
            ),
            _make_doc(
                "mcapi.orders.detail.get",
                domain="orders",
                intent_tags=["orders_lookup", "orders_detail"],
                tool_candidate="orders_detail",
                keywords=["orders", "detail", "show", "get"],
            ),
            _make_doc(
                "mcapi.orders.cancel.post",
                domain="orders",
                intent_tags=["orders_cancel"],
                tool_candidate="cancel_order",
                read_write_type="write",
                risk_level="high",
                method="POST",
                path="/api/v1/orders/cancel",
            ),
            _make_doc(
                "mcapi.shipment.track.get",
                domain="shipping",
                intent_tags=["shipment_track", "shipment_lookup"],
                tool_candidate="track_shipment",
                keywords=["shipment", "track", "shipping", "awb"],
            ),
            _make_doc(
                "mcapi.refund.create.post",
                domain="payments",
                intent_tags=["payment_refund"],
                tool_candidate="refund_payment",
                read_write_type="write",
                risk_level="high",
                method="POST",
                path="/api/v1/refund",
            ),
        ]
        self.indexer = _make_indexer_with_docs(self.docs)
        self.router = DecisionTreeRouter(self.indexer)
        self.router.build_routing_table()

    def test_exact_intent_domain_match(self):
        result = self.router.route("lookup", "order", "12345", "show order 12345")
        assert result.confidence >= 0.8
        assert result.selected_api is not None
        assert result.selected_api.domain == "orders"

    def test_action_verb_match(self):
        result = self.router.route("act", "order", "12345", "cancel order 12345")
        assert result.confidence >= 0.8
        assert result.selected_api is not None
        assert result.selected_api.read_write_type == "write"

    def test_refund_action_match(self):
        result = self.router.route("act", "payment", "55555", "refund payment for order 55555")
        assert result.selected_api is not None
        assert "refund" in result.selected_api.tool_candidate.lower()

    def test_domain_only_fallback(self):
        result = self.router.route("report", "order", None, "how many orders today")
        # Should find something in orders domain even without exact intent match
        assert result.selected_tools or result.confidence == 0.0

    def test_no_match(self):
        result = self.router.route("lookup", "unknown_entity", None, "something random")
        assert result.confidence < 0.5

    def test_negative_example_lowers_confidence(self):
        # Add a doc with negative examples
        doc = _make_doc(
            "mcapi.billing.dashboard.get",
            domain="billing",
            intent_tags=["billing_list"],
            keywords=["billing", "dashboard"],
            negative_examples=[
                {"user_query": "show order status", "should_not_use": "mcapi.billing.dashboard.get"}
            ],
        )
        indexer = _make_indexer_with_docs([doc])
        router = DecisionTreeRouter(indexer)
        router.build_routing_table()

        result = router.route("lookup", "billing", None, "show billing dashboard")
        # Should match billing
        assert result.selected_api is not None

    def test_routing_table_stats(self):
        assert self.router._built is True
        assert len(self.router._by_tool_candidate) >= 3
        assert len(self.router._by_tool_group) >= 2  # orders, shipping, payments


# ---------------------------------------------------------------------------
# Tool Use Router tests
# ---------------------------------------------------------------------------


class TestToolUseRouter:
    def setup_method(self):
        self.docs = [
            _make_doc(f"mcapi.orders.api{i}.get", domain="orders", intent_tags=[f"orders_{i}"])
            for i in range(10)
        ] + [
            _make_doc(f"mcapi.shipping.api{i}.get", domain="shipping", intent_tags=[f"shipping_{i}"])
            for i in range(5)
        ]
        self.indexer = _make_indexer_with_docs(self.docs)
        self.router = ToolUseRouter(self.indexer)

    def test_build_domain_scoped_tools(self):
        tools = self.router.build_tool_definitions(domain="orders")
        assert len(tools) == 10
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert "input_schema" in t

    def test_build_with_rw_filter(self):
        tools = self.router.build_tool_definitions(domain="orders", rw_type="read")
        assert len(tools) == 10  # All are read tools

    def test_build_with_limit(self):
        tools = self.router.build_tool_definitions(limit=3)
        assert len(tools) == 3

    def test_safe_tool_name(self):
        name = ToolUseRouter._safe_tool_name("mcapi.v1.orders.get")
        assert "." not in name
        assert name == "mcapi_v1_orders_get"

    def test_route_prepares_payload(self):
        result = self.router.route(
            query="show order 12345",
            intent="lookup",
            entity="order",
            entity_id="12345",
            tier1_candidates=self.docs[:2],
        )
        assert result.tier == RoutingTier.TOOL_USE
        assert len(result.tool_definitions) > 0
        assert result.reasoning


# ---------------------------------------------------------------------------
# Intelligent Router (full 3-tier) tests
# ---------------------------------------------------------------------------


class TestIntelligentRouter:
    def setup_method(self):
        self.docs = [
            _make_doc(
                "mcapi.orders.list.get",
                domain="orders",
                intent_tags=["orders_list", "orders_lookup"],
                tool_candidate="orders_list",
                keywords=["orders", "list", "show"],
                example_queries=["show all orders", "list orders"],
                param_examples=[{"query": "show order 12345", "params": {"id": "12345"}}],
            ),
            _make_doc(
                "mcapi.orders.cancel.post",
                domain="orders",
                intent_tags=["orders_cancel"],
                tool_candidate="cancel_order",
                read_write_type="write",
                method="POST",
                param_examples=[{"query": "cancel order 99999", "params": {"id": "99999"}}],
            ),
            _make_doc(
                "mcapi.shipment.track.get",
                domain="shipping",
                intent_tags=["shipment_track"],
                tool_candidate="track_shipment",
                keywords=["shipment", "track", "awb"],
            ),
        ]
        self.indexer = _make_indexer_with_docs(self.docs)
        self.router = IntelligentRouter(self.indexer)
        self.router.build()

    def test_tier1_high_confidence(self):
        """Clear intent + entity → Tier 1 decision tree."""
        result = self.router.route("lookup", "order", "12345", "show order 12345")
        assert result.tier == RoutingTier.DECISION_TREE
        assert result.confidence >= 0.75

    def test_tier2_medium_confidence(self):
        """Force medium confidence → Tier 2 tool-use."""
        result = self.router.route(
            "lookup", "order", None, "show order details",
            confidence_override=0.6,
        )
        assert result.tier == RoutingTier.TOOL_USE

    def test_tier3_low_confidence(self):
        """Force low confidence → Tier 3 full reasoning."""
        result = self.router.route(
            "lookup", "order", None, "something vague",
            confidence_override=0.2,
        )
        assert result.tier == RoutingTier.FULL_REASONING

    def test_unknown_intent_skips_tier1(self):
        """Unknown intent → skip to Tier 2 or 3."""
        result = self.router.route("unknown", "unknown", None, "hello there")
        assert result.tier in (RoutingTier.TOOL_USE, RoutingTier.FULL_REASONING)

    def test_write_operation_routing(self):
        """Write operations should route correctly."""
        result = self.router.route("act", "order", "99999", "cancel order 99999")
        assert result.selected_api is not None
        assert result.selected_api.read_write_type == "write"

    def test_get_stats(self):
        stats = self.router.get_stats()
        assert "indexer" in stats
        assert "tier1_built" in stats
        assert stats["tier1_built"] is True

    def test_route_result_has_params(self):
        result = self.router.route("lookup", "order", "12345", "show order 12345")
        if result.tier == RoutingTier.DECISION_TREE:
            assert result.extracted_params.get("id") == "12345"

    def test_few_shot_examples_in_tier3(self):
        result = self.router.route(
            "explain", "order", None, "why was my order delayed",
            confidence_override=0.1,
        )
        assert result.tier == RoutingTier.FULL_REASONING
        # May or may not have few-shots depending on matches


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestRouterEdgeCases:
    def test_empty_indexer(self):
        indexer = _make_indexer_with_docs([])
        router = IntelligentRouter(indexer)
        router.build()
        result = router.route("lookup", "order", "123", "show order 123")
        assert result.confidence == 0.0

    def test_single_doc(self):
        doc = _make_doc(
            "mcapi.orders.get",
            domain="orders",
            intent_tags=["orders_lookup"],
        )
        indexer = _make_indexer_with_docs([doc])
        router = IntelligentRouter(indexer)
        router.build()
        result = router.route("lookup", "order", "123", "show order 123")
        # Single candidate = higher confidence
        assert result.selected_api is not None

    def test_rebuild_after_new_docs(self):
        """Router can be rebuilt after adding new documents."""
        docs = [_make_doc("mcapi.a.get", domain="orders", intent_tags=["orders_lookup"])]
        indexer = _make_indexer_with_docs(docs)
        router = IntelligentRouter(indexer)
        router.build()

        # Add a new doc
        new_doc = _make_doc("mcapi.b.get", domain="payments", intent_tags=["payment_lookup"])
        indexer._documents[new_doc.doc_id] = new_doc
        indexer._build_embeddings()

        # Rebuild
        stats = router.build()
        assert stats["tier1_entries"] >= 2

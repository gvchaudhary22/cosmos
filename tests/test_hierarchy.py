"""
Tests for the HierarchicalIndex — 3-level domain/service/api hierarchy.

Covers:
  - Building hierarchy from KnowledgeIndexer documents
  - Keyword aggregation from leaves up to domain level
  - Route queries through domain -> service -> leaf
  - Inspection helpers: get_domains, get_services, get_stats
  - Edge cases: empty indexer, unknown domains, unassigned tool_candidate
"""

import os
import shutil
import tempfile

import pytest
import yaml

from cosmos.app.brain.indexer import KBDocument, KnowledgeIndexer
from cosmos.app.brain.hierarchy import HierarchicalIndex, HierarchyNode


# =====================================================================
# Helpers
# =====================================================================


def _make_api_dir(base_path: str, repo: str, api_id: str, **overrides):
    """Create a mock API directory with YAML files."""
    api_dir = os.path.join(
        base_path, repo, "pillar_3_api_mcp_tools", "apis", api_id
    )
    os.makedirs(api_dir, exist_ok=True)

    domain = overrides.get("domain", "orders")
    method = overrides.get("method", "GET")
    path = overrides.get("path", f"/api/v1/{domain}")
    tool_candidate = overrides.get("tool_candidate", f"{domain}_list")
    summary = overrides.get(
        "summary",
        f"{method} {path} endpoint for {domain}.",
    )
    keywords = overrides.get("keywords", [domain, "list"])
    aliases = overrides.get("aliases", [path])
    intent_primary = overrides.get("intent_primary", f"{domain}_list")
    read_write = overrides.get("read_write_type", "READ")

    overview = {
        "api": {"method": method, "path": path},
        "classification": {"domain": domain},
        "retrieval_hints": {
            "canonical_summary": summary,
            "keywords": keywords,
            "aliases": aliases,
        },
    }

    tool_agent_tags = {
        "tool_assignment": {
            "tool_candidate": tool_candidate,
            "tool_group": tool_candidate,
            "read_write_type": read_write,
            "risk_level": "low",
            "approval_mode": "auto",
        },
        "agent_assignment": {"owner": "test_agent"},
        "intent_tags": {
            "primary": intent_primary,
            "secondary": overrides.get("intent_secondary", []),
        },
        "negative_routing_examples": [],
    }

    examples = {
        "param_extraction_pairs": [
            {"query": f"show me {domain}", "params": {}},
        ]
    }

    index = {"training_ready": True, "confidence_by_section": {"overview": "high"}}

    for fname, data in [
        ("overview.yaml", overview),
        ("tool_agent_tags.yaml", tool_agent_tags),
        ("examples.yaml", examples),
        ("index.yaml", index),
    ]:
        with open(os.path.join(api_dir, fname), "w") as f:
            yaml.dump(data, f)


def _build_test_kb(tmp_path: str) -> KnowledgeIndexer:
    """Build a small test KB with 3 domains, multiple services."""
    # Orders domain: 3 APIs across 2 tool groups
    _make_api_dir(tmp_path, "TestRepo", "mcapi.orders.list",
                  domain="orders", tool_candidate="orders_list",
                  keywords=["orders", "list", "all"],
                  intent_primary="orders_list")
    _make_api_dir(tmp_path, "TestRepo", "mcapi.orders.detail",
                  domain="orders", tool_candidate="orders_detail",
                  keywords=["orders", "detail", "single"],
                  intent_primary="orders_detail")
    _make_api_dir(tmp_path, "TestRepo", "mcapi.orders.cancel",
                  domain="orders", tool_candidate="orders_cancel",
                  method="POST", read_write_type="WRITE",
                  keywords=["orders", "cancel", "refund"],
                  intent_primary="orders_cancel")

    # Shipping domain: 2 APIs
    _make_api_dir(tmp_path, "TestRepo", "mcapi.shipping.track",
                  domain="shipping", tool_candidate="shipping_track",
                  keywords=["shipping", "track", "status"],
                  intent_primary="shipping_track")
    _make_api_dir(tmp_path, "TestRepo", "mcapi.shipping.rates",
                  domain="shipping", tool_candidate="shipping_rates",
                  keywords=["shipping", "rates", "estimate"],
                  intent_primary="shipping_rates")

    # Payments domain: 1 API
    _make_api_dir(tmp_path, "TestRepo", "mcapi.payments.refund",
                  domain="payments", tool_candidate="payments_refund",
                  method="POST", read_write_type="WRITE",
                  keywords=["payments", "refund", "money"],
                  intent_primary="payments_refund")

    indexer = KnowledgeIndexer(tmp_path)
    indexer.index_all()
    return indexer


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def tmp_kb():
    """Provide a temp directory, cleaned up after test."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def indexer(tmp_kb):
    return _build_test_kb(tmp_kb)


@pytest.fixture
def hierarchy(indexer):
    h = HierarchicalIndex(indexer)
    h.build()
    return h


# =====================================================================
# Tests — HierarchyNode dataclass
# =====================================================================


class TestHierarchyNode:
    def test_default_construction(self):
        node = HierarchyNode(name="test", level=0)
        assert node.name == "test"
        assert node.level == 0
        assert node.children == {}
        assert node.docs == []
        assert node.keywords == set()
        assert node.doc_count == 0

    def test_fields_are_independent(self):
        """Ensure default_factory creates separate instances."""
        a = HierarchyNode(name="a", level=0)
        b = HierarchyNode(name="b", level=0)
        a.children["x"] = HierarchyNode(name="x", level=1)
        assert "x" not in b.children


# =====================================================================
# Tests — Build
# =====================================================================


class TestBuild:
    def test_build_returns_stats(self, indexer):
        h = HierarchicalIndex(indexer)
        stats = h.build()
        assert stats["built"] is True
        assert stats["total_docs"] == 6
        assert stats["domain_count"] == 3

    def test_build_creates_domains(self, hierarchy):
        domains = hierarchy.get_domains()
        domain_names = {d["domain"] for d in domains}
        assert domain_names == {"orders", "shipping", "payments"}

    def test_build_correct_doc_counts(self, hierarchy):
        domains = {d["domain"]: d for d in hierarchy.get_domains()}
        assert domains["orders"]["doc_count"] == 3
        assert domains["shipping"]["doc_count"] == 2
        assert domains["payments"]["doc_count"] == 1

    def test_build_service_counts(self, hierarchy):
        domains = {d["domain"]: d for d in hierarchy.get_domains()}
        # orders has 3 different tool_candidates
        assert domains["orders"]["service_count"] == 3
        assert domains["shipping"]["service_count"] == 2
        assert domains["payments"]["service_count"] == 1

    def test_is_built_flag(self, indexer):
        h = HierarchicalIndex(indexer)
        assert h.is_built is False
        h.build()
        assert h.is_built is True

    def test_keywords_aggregated_to_domain(self, hierarchy):
        domains = {d["domain"]: d for d in hierarchy.get_domains()}
        # Orders domain should have keywords from all 3 child services
        orders_node = hierarchy._root.children["orders"]
        assert "cancel" in orders_node.keywords
        assert "detail" in orders_node.keywords
        assert "orders" in orders_node.keywords

    def test_keywords_aggregated_to_service(self, hierarchy):
        orders_node = hierarchy._root.children["orders"]
        cancel_svc = orders_node.children["orders_cancel"]
        assert "cancel" in cancel_svc.keywords
        assert "refund" in cancel_svc.keywords

    def test_build_empty_indexer(self, tmp_kb):
        """Build with an empty indexer produces empty hierarchy."""
        indexer = KnowledgeIndexer(tmp_kb)
        indexer.index_all()
        h = HierarchicalIndex(indexer)
        stats = h.build()
        assert stats["built"] is True
        assert stats["total_docs"] == 0
        assert stats["domain_count"] == 0

    def test_rebuild_clears_previous(self, hierarchy, indexer):
        """Calling build() again resets the tree."""
        stats1 = hierarchy.get_stats()
        stats2 = hierarchy.build()
        assert stats1["total_docs"] == stats2["total_docs"]


# =====================================================================
# Tests — Route
# =====================================================================


class TestRoute:
    def test_route_by_domain_keyword(self, hierarchy):
        """Query mentioning 'orders' should return order docs."""
        results = hierarchy.route("show all orders")
        doc_ids = {d.doc_id for d in results}
        # Should include order APIs
        assert any("orders" in did for did in doc_ids)

    def test_route_with_intent(self, hierarchy):
        results = hierarchy.route("cancel", intent="cancel")
        doc_ids = {d.doc_id for d in results}
        assert "mcapi.orders.cancel" in doc_ids

    def test_route_with_entity(self, hierarchy):
        results = hierarchy.route("track", entity="shipping")
        doc_ids = {d.doc_id for d in results}
        assert "mcapi.shipping.track" in doc_ids

    def test_route_empty_query(self, hierarchy):
        results = hierarchy.route("")
        assert results == []

    def test_route_before_build(self, indexer):
        h = HierarchicalIndex(indexer)
        results = h.route("orders")
        assert results == []

    def test_route_returns_kbdocuments(self, hierarchy):
        results = hierarchy.route("shipping rates estimate")
        for doc in results:
            assert isinstance(doc, KBDocument)

    def test_route_top_domains_limits(self, hierarchy):
        """With top_domains=1, only best matching domain's docs returned."""
        results = hierarchy.route("payments refund money", top_domains=1)
        domains = {d.domain for d in results}
        # payments should be top match
        assert "payments" in domains

    def test_route_top_services_limits(self, hierarchy):
        """With top_services=1, only best service per domain."""
        results = hierarchy.route("orders cancel refund", top_domains=1, top_services=1)
        # Should narrow to just the cancel service
        tool_candidates = {d.tool_candidate for d in results}
        assert "orders_cancel" in tool_candidates

    def test_route_unknown_query_still_returns(self, hierarchy):
        """Even gibberish query returns something (top domains by default)."""
        results = hierarchy.route("xyzabc123")
        # With no keyword overlap, scores are all 0, but top_domains still selected
        # (they just have equal scores). Implementation takes first N.
        assert isinstance(results, list)


# =====================================================================
# Tests — Inspection Helpers
# =====================================================================


class TestInspection:
    def test_get_domains(self, hierarchy):
        domains = hierarchy.get_domains()
        assert len(domains) == 3
        for d in domains:
            assert "domain" in d
            assert "doc_count" in d
            assert "service_count" in d
            assert "keywords_sample" in d

    def test_get_services_existing_domain(self, hierarchy):
        services = hierarchy.get_services("orders")
        assert len(services) == 3
        svc_names = {s["service"] for s in services}
        assert svc_names == {"orders_list", "orders_detail", "orders_cancel"}

    def test_get_services_nonexistent_domain(self, hierarchy):
        services = hierarchy.get_services("nonexistent")
        assert services == []

    def test_get_stats(self, hierarchy):
        stats = hierarchy.get_stats()
        assert stats["built"] is True
        assert stats["total_docs"] == 6
        assert stats["domain_count"] == 3
        assert stats["service_count"] == 6  # 3 + 2 + 1
        assert stats["leaf_count"] == 6
        assert stats["avg_services_per_domain"] == 2.0
        assert stats["avg_apis_per_service"] == 1.0

    def test_get_stats_before_build(self, indexer):
        h = HierarchicalIndex(indexer)
        stats = h.get_stats()
        assert stats["built"] is False


# =====================================================================
# Tests — Edge Cases
# =====================================================================


class TestEdgeCases:
    def test_unassigned_tool_candidate(self, tmp_kb):
        """APIs with empty tool_candidate go to 'unassigned' service."""
        _make_api_dir(tmp_kb, "Repo", "mcapi.misc.endpoint",
                      domain="misc", tool_candidate="",
                      keywords=["misc"])
        indexer = KnowledgeIndexer(tmp_kb)
        indexer.index_all()
        h = HierarchicalIndex(indexer)
        h.build()
        services = h.get_services("misc")
        svc_names = {s["service"] for s in services}
        assert "unassigned" in svc_names

    def test_unknown_domain(self, tmp_kb):
        """APIs with no domain go to 'unknown'."""
        api_dir = os.path.join(
            tmp_kb, "Repo", "pillar_3_api_mcp_tools", "apis", "mcapi.nodomain"
        )
        os.makedirs(api_dir, exist_ok=True)
        # Write overview without domain
        overview = {
            "api": {"method": "GET", "path": "/test"},
            "classification": {},
            "retrieval_hints": {
                "canonical_summary": "some endpoint",
                "keywords": ["test"],
                "aliases": [],
            },
        }
        tags = {
            "tool_assignment": {"tool_candidate": "test_tool"},
            "agent_assignment": {"owner": "agent"},
            "intent_tags": {"primary": "test", "secondary": []},
        }
        for fname, data in [
            ("overview.yaml", overview),
            ("tool_agent_tags.yaml", tags),
            ("examples.yaml", {}),
            ("index.yaml", {}),
        ]:
            with open(os.path.join(api_dir, fname), "w") as f:
                yaml.dump(data, f)

        indexer = KnowledgeIndexer(tmp_kb)
        indexer.index_all()
        h = HierarchicalIndex(indexer)
        h.build()
        domain_names = {d["domain"] for d in h.get_domains()}
        assert "unknown" in domain_names

    def test_multiple_repos_same_domain(self, tmp_kb):
        """Docs from different repos in same domain are grouped together."""
        _make_api_dir(tmp_kb, "RepoA", "mcapi.orders.a",
                      domain="orders", tool_candidate="orders_list",
                      keywords=["orders"])
        _make_api_dir(tmp_kb, "RepoB", "mcapi.orders.b",
                      domain="orders", tool_candidate="orders_list",
                      keywords=["orders"])
        indexer = KnowledgeIndexer(tmp_kb)
        indexer.index_all()
        h = HierarchicalIndex(indexer)
        h.build()
        domains = {d["domain"]: d for d in h.get_domains()}
        assert domains["orders"]["doc_count"] == 2

    def test_tokenize_static(self):
        tokens = HierarchicalIndex._tokenize("Show me all ORDERS for user 42")
        assert "orders" in tokens
        assert "show" in tokens
        assert "user" in tokens  # 4 chars, passes length filter
        # Short words filtered out
        assert "me" not in tokens
        assert "42" not in tokens

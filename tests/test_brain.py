"""
Tests for the COSMOS RAG Brain module.

Covers:
  - KnowledgeIndexer: indexing, search, TF-IDF, cosine similarity
  - QueryGraph: full graph processing, routing, escalation
  - KBUpdatePipeline: change detection, webhooks, learning feedback
  - Integration: end-to-end create KB -> index -> query -> result
"""

import asyncio
import math
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.brain.indexer import KBDocument, KnowledgeIndexer
from app.brain.graph import GraphPhase, QueryGraph, QueryState
from app.brain.pipeline import IndexUpdate, KBUpdatePipeline
from app.brain.setup import create_brain


# =====================================================================
# Helpers — Create mock knowledge base directories with YAML files
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
        f"{method} {path} endpoint in {repo} for {domain} flows.",
    )
    keywords = overrides.get("keywords", [domain, "list"])
    aliases = overrides.get("aliases", [path])
    intent_primary = overrides.get("intent_primary", f"{domain}_list")
    read_write = overrides.get("read_write_type", "READ")
    risk = overrides.get("risk_level", "low")
    primary_agent = overrides.get("primary_agent", f"{domain}_agent")
    training_ready = overrides.get("training_ready", False)
    param_pairs = overrides.get(
        "param_extraction_pairs",
        [
            {
                "query": f"show {domain} for company 123",
                "params": {"company_id": "123"},
            }
        ],
    )
    negative_examples = overrides.get("negative_routing_examples", [])

    overview = {
        "api": {
            "id": api_id,
            "method": method,
            "path": path,
            "source_repo": repo,
        },
        "classification": {
            "domain": domain,
            "subdomain": "list",
            "intent_primary": intent_primary,
            "intent_secondary": [],
        },
        "retrieval_hints": {
            "canonical_summary": summary,
            "aliases": aliases,
            "keywords": keywords,
        },
    }
    with open(os.path.join(api_dir, "overview.yaml"), "w") as f:
        yaml.dump(overview, f)

    tags = {
        "tool_assignment": {
            "tool_candidate": tool_candidate,
            "read_write_type": read_write,
            "risk_level": risk,
            "approval_mode": "auto",
        },
        "agent_assignment": {"owner": primary_agent},
        "intent_tags": {
            "primary": intent_primary,
            "secondary": [],
        },
        "negative_routing_examples": negative_examples,
    }
    with open(os.path.join(api_dir, "tool_agent_tags.yaml"), "w") as f:
        yaml.dump(tags, f)

    examples = {
        "param_extraction_pairs": param_pairs,
    }
    with open(os.path.join(api_dir, "examples.yaml"), "w") as f:
        yaml.dump(examples, f)

    index = {
        "api_id": api_id,
        "training_ready": training_ready,
        "confidence_by_section": {"overview": "medium"},
    }
    with open(os.path.join(api_dir, "index.yaml"), "w") as f:
        yaml.dump(index, f)

    return api_dir


def _make_table_dir(base_path: str, repo: str, table_name: str, **overrides):
    """Create a mock table directory with YAML files."""
    table_dir = os.path.join(
        base_path, repo, "pillar_1_schema", "tables", table_name
    )
    os.makedirs(table_dir, exist_ok=True)

    domain = overrides.get("domain", "orders")
    description = overrides.get(
        "description", f"{table_name} table for {domain}"
    )
    columns = overrides.get(
        "columns",
        [
            {"name": "id", "type": "int"},
            {"name": "status", "type": "varchar"},
        ],
    )

    meta = {
        "table": table_name,
        "canonical_table": table_name,
        "domain": domain,
        "description": description,
    }
    with open(os.path.join(table_dir, "_meta.yaml"), "w") as f:
        yaml.dump(meta, f)

    cols = {"columns": columns}
    with open(os.path.join(table_dir, "columns.yaml"), "w") as f:
        yaml.dump(cols, f)

    return table_dir


@pytest.fixture
def mock_kb(tmp_path):
    """Create a mock knowledge base with several API and table entries."""
    kb = str(tmp_path / "kb")
    os.makedirs(kb)

    # APIs in MultiChannel_API
    _make_api_dir(
        kb,
        "MultiChannel_API",
        "mcapi.v1.orders.get",
        domain="orders",
        method="GET",
        path="/api/v1/orders",
        keywords=["orders", "list", "pending", "shipment"],
        aliases=["/api/v1/orders", "order list"],
        param_extraction_pairs=[
            {
                "query": "show pending orders for company 123",
                "params": {"company_id": "123", "status": "pending"},
            },
            {
                "query": "list all orders",
                "params": {},
            },
        ],
    )
    _make_api_dir(
        kb,
        "MultiChannel_API",
        "mcapi.v1.orders.cancel.post",
        domain="orders",
        method="POST",
        path="/api/v1/orders/{id}/cancel",
        tool_candidate="order_cancel",
        read_write_type="WRITE",
        risk_level="high",
        keywords=["orders", "cancel", "refund"],
        intent_primary="order_cancel",
        negative_routing_examples=[
            {
                "user_query": "show order status",
                "should_not_use": "mcapi.v1.orders.cancel.post",
                "use_instead": "mcapi.v1.orders.get",
            }
        ],
    )
    _make_api_dir(
        kb,
        "MultiChannel_API",
        "mcapi.v1.shipments.track.get",
        domain="shipments",
        method="GET",
        path="/api/v1/shipments/{id}/track",
        tool_candidate="shipment_track",
        keywords=["shipments", "tracking", "delivery", "awb"],
        aliases=["track shipment", "shipment tracking"],
    )
    _make_api_dir(
        kb,
        "MultiChannel_API",
        "mcapi.v1.billing.get",
        domain="billing",
        method="GET",
        path="/internal/report/billing",
        tool_candidate="billing_list",
        keywords=["billing", "invoice", "payment"],
    )

    # API in SR_Web
    _make_api_dir(
        kb,
        "SR_Web",
        "srweb.v1.account.details.get",
        domain="account",
        method="GET",
        path="/v1/account/details",
        keywords=["account", "details", "profile"],
    )

    # Tables
    _make_table_dir(
        kb,
        "MultiChannel_API",
        "orders",
        domain="orders",
        description="Main orders table",
        columns=[
            {"name": "id", "type": "bigint"},
            {"name": "company_id", "type": "int"},
            {"name": "status", "type": "varchar"},
            {"name": "channel_order_id", "type": "varchar"},
        ],
    )
    _make_table_dir(
        kb,
        "MultiChannel_API",
        "shipments",
        domain="shipments",
        description="Shipment records table",
        columns=[
            {"name": "id", "type": "bigint"},
            {"name": "order_id", "type": "bigint"},
            {"name": "awb_code", "type": "varchar"},
        ],
    )

    return kb


@pytest.fixture
def indexer(mock_kb):
    """Create and index a KnowledgeIndexer against the mock KB."""
    idx = KnowledgeIndexer(mock_kb)
    idx.index_all()
    return idx


# =====================================================================
# Indexer tests
# =====================================================================


class TestKnowledgeIndexer:
    def test_index_mock_kb(self, mock_kb):
        idx = KnowledgeIndexer(mock_kb)
        count = idx.index_all()
        assert count == 7  # 5 APIs + 2 tables
        assert idx.is_indexed is True
        assert idx.document_count == 7

    def test_search_returns_relevant_results(self, indexer):
        results = indexer.search("show pending orders")
        assert len(results) > 0
        doc, score = results[0]
        assert score > 0.0
        # The top result should be order-related
        assert "order" in doc.doc_id.lower() or doc.domain == "orders"

    def test_search_with_doc_type_filter(self, indexer):
        results = indexer.search("orders", filters={"doc_type": "api"})
        for doc, _ in results:
            assert doc.doc_type == "api"

    def test_search_with_domain_filter(self, indexer):
        results = indexer.search("list", filters={"domain": "billing"})
        for doc, _ in results:
            assert doc.domain == "billing"

    def test_search_with_read_write_filter(self, indexer):
        results = indexer.search(
            "cancel order", filters={"read_write_type": "write"}
        )
        for doc, _ in results:
            assert doc.read_write_type == "write"

    def test_search_with_repo_filter(self, indexer):
        results = indexer.search(
            "account details", filters={"repo": "SR_Web"}
        )
        for doc, _ in results:
            assert doc.repo == "SR_Web"

    def test_search_by_intent_lookup(self, indexer):
        results = indexer.search_by_intent("LOOKUP", "orders")
        assert len(results) > 0
        # Should filter to read-type docs
        for doc, _ in results:
            assert doc.read_write_type == "read"

    def test_search_by_intent_act(self, indexer):
        results = indexer.search_by_intent("ACT", "orders cancel")
        # Should filter to write-type docs
        for doc, _ in results:
            assert doc.read_write_type == "write"

    def test_tfidf_embedding_computed(self, indexer):
        doc = indexer.get_document("mcapi.v1.orders.get")
        assert doc is not None
        assert doc.embedding is not None
        assert len(doc.embedding) > 0
        # Embedding should be L2-normalized (norm ~= 1.0)
        norm = math.sqrt(sum(v * v for v in doc.embedding))
        assert abs(norm - 1.0) < 0.01

    def test_cosine_similarity_identical(self, indexer):
        # Identical vectors should have similarity 1.0
        vec = [0.5, 0.5, 0.5]
        sim = indexer._cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 0.001

    def test_cosine_similarity_orthogonal(self, indexer):
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        sim = indexer._cosine_similarity(vec_a, vec_b)
        assert abs(sim - 0.0) < 0.001

    def test_cosine_similarity_empty(self, indexer):
        assert indexer._cosine_similarity([], []) == 0.0
        assert indexer._cosine_similarity([1.0], []) == 0.0

    def test_get_document_by_id(self, indexer):
        doc = indexer.get_document("mcapi.v1.orders.get")
        assert doc is not None
        assert doc.doc_id == "mcapi.v1.orders.get"
        assert doc.doc_type == "api"
        assert doc.domain == "orders"
        assert doc.method == "GET"

    def test_get_document_not_found(self, indexer):
        doc = indexer.get_document("nonexistent.api")
        assert doc is None

    def test_get_document_table(self, indexer):
        doc = indexer.get_document("table.multichannel_api.orders")
        assert doc is not None
        assert doc.doc_type == "table"
        assert doc.domain == "orders"

    def test_stats_are_accurate(self, indexer):
        stats = indexer.get_stats()
        assert stats["indexed"] is True
        assert stats["total"] == 7
        assert stats["by_type"]["api"] == 5
        assert stats["by_type"]["table"] == 2
        assert "MultiChannel_API" in stats["by_repo"]
        assert "SR_Web" in stats["by_repo"]
        assert stats["vocab_size"] > 0

    def test_empty_knowledge_base(self, tmp_path):
        empty_kb = str(tmp_path / "empty_kb")
        os.makedirs(empty_kb)
        idx = KnowledgeIndexer(empty_kb)
        count = idx.index_all()
        assert count == 0
        assert idx.is_indexed is True
        assert idx.document_count == 0

    def test_nonexistent_path(self, tmp_path):
        idx = KnowledgeIndexer(str(tmp_path / "does_not_exist"))
        count = idx.index_all()
        assert count == 0
        assert idx.is_indexed is True

    def test_search_on_unindexed(self, tmp_path):
        idx = KnowledgeIndexer(str(tmp_path))
        results = idx.search("orders")
        assert results == []

    def test_search_empty_query(self, indexer):
        results = indexer.search("")
        # Empty query should return empty or low-relevance results
        assert isinstance(results, list)

    def test_search_top_k_limit(self, indexer):
        results = indexer.search("orders shipments billing", top_k=2)
        assert len(results) <= 2

    def test_negative_examples_indexed(self, indexer):
        doc = indexer.get_document("mcapi.v1.orders.cancel.post")
        assert doc is not None
        assert len(doc.negative_examples) > 0
        assert doc.negative_examples[0]["user_query"] == "show order status"

    def test_param_examples_indexed(self, indexer):
        doc = indexer.get_document("mcapi.v1.orders.get")
        assert doc is not None
        assert len(doc.param_examples) == 2
        assert doc.param_examples[0]["query"] == "show pending orders for company 123"

    def test_tokenizer(self, indexer):
        tokens = indexer._tokenize("Show Me Order #12345 for company_id")
        assert "show" in tokens
        assert "order" in tokens
        assert "12345" in tokens
        assert "company_id" in tokens
        # Short words (<=2 chars) should be excluded
        assert "me" not in tokens

    def test_api_fields_populated(self, indexer):
        doc = indexer.get_document("mcapi.v1.orders.cancel.post")
        assert doc is not None
        assert doc.method == "POST"
        assert doc.read_write_type == "write"
        assert doc.risk_level == "high"
        assert doc.tool_candidate == "order_cancel"
        assert "order_cancel" in doc.intent_tags


# =====================================================================
# QueryGraph tests
# =====================================================================


class TestQueryGraph:
    @pytest.fixture
    def graph(self, indexer):
        return QueryGraph(indexer)

    @pytest.fixture
    def graph_with_llm(self, indexer):
        mock_llm = AsyncMock()
        return QueryGraph(indexer, llm_client=mock_llm), mock_llm

    @pytest.mark.asyncio
    async def test_process_full_graph_no_llm(self, graph):
        state = await graph.process("show pending orders")
        assert state.phase in (GraphPhase.RESPOND, GraphPhase.ESCALATE)
        assert "embed" in state.phases_completed
        assert "retrieve" in state.phases_completed
        assert "select_tool" in state.phases_completed
        assert state.selected_tool is not None or state.phase == GraphPhase.ESCALATE

    @pytest.mark.asyncio
    async def test_tool_selection_no_llm_fallback(self, graph):
        state = await graph.process("show pending orders")
        # Without LLM, should pick top retrieval result
        if state.selected_tool:
            assert state.tool_confidence > 0.0

    @pytest.mark.asyncio
    async def test_tool_selection_with_llm(self, graph_with_llm):
        graph, mock_llm = graph_with_llm
        mock_llm.complete.return_value = '{"selected": 1, "confidence": 0.9, "reason": "orders match"}'
        state = await graph.process("show pending orders")
        assert state.selected_tool is not None
        assert state.tool_confidence == 0.9

    @pytest.mark.asyncio
    async def test_tool_selection_llm_fallback_on_error(self, graph_with_llm):
        graph, mock_llm = graph_with_llm
        mock_llm.complete.side_effect = Exception("LLM error")
        state = await graph.process("show pending orders")
        # Should fallback to top retrieval result
        assert state.selected_tool is not None

    @pytest.mark.asyncio
    async def test_param_extraction_basic(self, graph):
        state = await graph.process("show order 12345")
        if state.extracted_params:
            assert "id" in state.extracted_params
            assert state.extracted_params["id"] == "12345"

    @pytest.mark.asyncio
    async def test_param_extraction_with_llm(self, graph_with_llm):
        graph, mock_llm = graph_with_llm
        # First call: tool selection, second call: param extraction
        mock_llm.complete.side_effect = [
            '{"selected": 1, "confidence": 0.9, "reason": "match"}',
            '{"params": {"company_id": "456", "status": "pending"}}',
        ]
        state = await graph.process("show pending orders for company 456")
        assert state.extracted_params.get("company_id") == "456"

    @pytest.mark.asyncio
    async def test_validation_pass_get(self, graph):
        state = await graph.process("show orders")
        if state.selected_tool and state.selected_api:
            if state.selected_api.get("method") == "GET":
                assert state.validation_passed is True

    @pytest.mark.asyncio
    async def test_validation_fail_no_tool(self, indexer):
        # Create indexer with empty KB
        empty_indexer = KnowledgeIndexer("/nonexistent")
        empty_indexer._indexed = True
        graph = QueryGraph(empty_indexer)
        state = await graph.process("something random xyz 999")
        assert state.phase == GraphPhase.ESCALATE

    @pytest.mark.asyncio
    async def test_execution_mock_no_mcapi(self, graph):
        state = await graph.process("show pending orders")
        if state.execution_success:
            assert state.tool_result is not None
            assert state.tool_result.get("status") == "mock"

    @pytest.mark.asyncio
    async def test_escalation_low_confidence(self, indexer):
        # Create graph with empty indexer so nothing is found
        empty_indexer = KnowledgeIndexer("/nonexistent")
        empty_indexer._indexed = True
        graph = QueryGraph(empty_indexer)
        state = await graph.process("zzz completely unrelated query")
        assert state.phase == GraphPhase.ESCALATE
        assert "human agent" in state.response.lower()
        assert state.final_confidence <= 0.1

    @pytest.mark.asyncio
    async def test_graph_phases_tracked(self, graph):
        state = await graph.process("show billing reports")
        assert len(state.phases_completed) >= 3  # at least embed, retrieve, select_tool

    @pytest.mark.asyncio
    async def test_conditional_routing_selection_to_extract(self, graph):
        state = await graph.process("show pending orders")
        # If confidence >= 0.3, should go to extract_params
        if state.tool_confidence >= 0.3:
            assert "extract_params" in state.phases_completed

    @pytest.mark.asyncio
    async def test_conditional_routing_validation_to_execute(self, graph):
        state = await graph.process("show pending orders")
        if state.validation_passed:
            assert "execute" in state.phases_completed

    @pytest.mark.asyncio
    async def test_respond_with_llm(self, graph_with_llm):
        graph, mock_llm = graph_with_llm
        mock_llm.complete.side_effect = [
            '{"selected": 1, "confidence": 0.9, "reason": "match"}',
            '{"params": {}}',
            "Here are your pending orders: Order #1234 is pending shipment.",
        ]
        state = await graph.process("show pending orders")
        assert "pending orders" in state.response.lower() or state.response != ""

    @pytest.mark.asyncio
    async def test_execution_with_mock_mcapi(self, indexer):
        mock_mcapi = AsyncMock()
        mock_result = MagicMock()
        mock_result.data = {"orders": [{"id": 1, "status": "pending"}]}
        mock_result.status_code = 200
        mock_result.success = True
        mock_mcapi.get.return_value = mock_result
        mock_mcapi.post.return_value = mock_result

        graph = QueryGraph(indexer, mcapi_client=mock_mcapi)
        state = await graph.process("show pending orders")
        if state.execution_success and state.tool_result:
            assert "data" in state.tool_result

    @pytest.mark.asyncio
    async def test_query_state_defaults(self):
        state = QueryState(query="test")
        assert state.session_id == ""
        assert state.user_role == "agent"
        assert state.retrieved_docs == []
        assert state.phases_completed == []
        assert state.errors == []


# =====================================================================
# Pipeline tests
# =====================================================================


class TestKBUpdatePipeline:
    @pytest.fixture
    def pipeline(self, indexer, mock_kb):
        pipe = KBUpdatePipeline(indexer, mock_kb)
        pipe.snapshot_hashes()
        return pipe

    def test_scan_no_changes_after_snapshot(self, pipeline):
        changes = pipeline.scan_for_changes()
        assert len(changes) == 0

    def test_scan_detects_new_file(self, pipeline, mock_kb):
        # Add a new YAML file
        new_dir = os.path.join(
            mock_kb,
            "MultiChannel_API",
            "pillar_3_api_mcp_tools",
            "apis",
            "mcapi.v1.new_api.get",
        )
        os.makedirs(new_dir, exist_ok=True)
        with open(os.path.join(new_dir, "overview.yaml"), "w") as f:
            yaml.dump({"api": {"id": "mcapi.v1.new_api.get"}}, f)

        changes = pipeline.scan_for_changes()
        new_changes = [c for c in changes if c["change"] == "new"]
        assert len(new_changes) > 0

    def test_scan_detects_modified_file(self, pipeline, mock_kb):
        # Modify an existing YAML file
        overview_path = os.path.join(
            mock_kb,
            "MultiChannel_API",
            "pillar_3_api_mcp_tools",
            "apis",
            "mcapi.v1.orders.get",
            "overview.yaml",
        )
        with open(overview_path, "a") as f:
            f.write("\n# Modified\n")

        changes = pipeline.scan_for_changes()
        modified = [c for c in changes if c["change"] == "modified"]
        assert len(modified) > 0

    def test_scan_detects_deleted_file(self, pipeline, mock_kb):
        # Delete a YAML file
        overview_path = os.path.join(
            mock_kb,
            "MultiChannel_API",
            "pillar_3_api_mcp_tools",
            "apis",
            "mcapi.v1.billing.get",
            "overview.yaml",
        )
        os.remove(overview_path)

        changes = pipeline.scan_for_changes()
        deleted = [c for c in changes if c["change"] == "deleted"]
        assert len(deleted) > 0

    @pytest.mark.asyncio
    async def test_process_changes_updates_index(self, pipeline):
        changes = [
            {
                "path": "MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.new.get/overview.yaml",
                "change": "new",
            }
        ]
        updates = await pipeline.process_changes(changes)
        assert len(updates) > 0
        assert updates[0].doc_id == "mcapi.v1.new.get"
        assert updates[0].status == "indexed"

    @pytest.mark.asyncio
    async def test_process_deleted_change(self, pipeline, indexer):
        # Pre-check: doc exists
        assert indexer.get_document("mcapi.v1.orders.get") is not None

        changes = [
            {
                "path": "MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.orders.get/overview.yaml",
                "change": "deleted",
            }
        ]
        updates = await pipeline.process_changes(changes)
        assert len(updates) > 0
        # Doc should be removed
        assert indexer.get_document("mcapi.v1.orders.get") is None

    @pytest.mark.asyncio
    async def test_github_webhook_handler(self, pipeline):
        payload = {
            "event": "push",
            "repository": "MultiChannel_API",
            "changed_files": [
                "knowledge_base/shiprocket/MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.orders.get/overview.yaml"
            ],
            "commit_sha": "abc123",
        }
        updates = await pipeline.handle_github_webhook(payload)
        assert len(updates) > 0
        assert updates[0].source == "github_webhook"

    @pytest.mark.asyncio
    async def test_github_webhook_no_kb_files(self, pipeline):
        payload = {
            "event": "push",
            "changed_files": ["src/app.py", "README.md"],
        }
        updates = await pipeline.handle_github_webhook(payload)
        assert len(updates) == 0

    @pytest.mark.asyncio
    async def test_learning_feedback_handler(self, pipeline, indexer):
        doc = indexer.get_document("mcapi.v1.orders.get")
        orig_examples = len(doc.param_examples)

        feedback = {
            "doc_id": "mcapi.v1.orders.get",
            "correct_query": "show me pending orders for company 456",
            "correct_params": {"company_id": "456", "status": "pending"},
            "feedback_score": 5,
        }
        update = await pipeline.handle_learning_feedback(feedback)
        assert update is not None
        assert update.status == "indexed"
        assert update.update_type == "learning_feedback"

        # Check the doc got updated
        doc = indexer.get_document("mcapi.v1.orders.get")
        assert len(doc.param_examples) == orig_examples + 1

    @pytest.mark.asyncio
    async def test_learning_feedback_missing_doc(self, pipeline):
        feedback = {
            "doc_id": "nonexistent.api",
            "correct_query": "test",
            "correct_params": {},
        }
        update = await pipeline.handle_learning_feedback(feedback)
        assert update is not None
        assert update.status == "failed"
        assert "not found" in update.error

    @pytest.mark.asyncio
    async def test_learning_feedback_empty_doc_id(self, pipeline):
        feedback = {"doc_id": "", "correct_query": "test"}
        update = await pipeline.handle_learning_feedback(feedback)
        assert update is None

    @pytest.mark.asyncio
    async def test_full_reindex(self, pipeline):
        result = await pipeline.full_reindex()
        assert result["total"] > 0
        assert result["errors"] == 0
        assert "new" in result
        assert "updated" in result
        assert "removed" in result

    def test_update_history_tracking(self, pipeline):
        # Initially empty
        history = pipeline.get_update_history()
        assert isinstance(history, list)

    @pytest.mark.asyncio
    async def test_update_history_after_webhook(self, pipeline):
        payload = {
            "event": "push",
            "changed_files": [
                "knowledge_base/shiprocket/MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.orders.get/index.yaml"
            ],
        }
        await pipeline.handle_github_webhook(payload)
        history = pipeline.get_update_history()
        assert len(history) > 0
        assert history[0]["source"] == "github_webhook"

    def test_snapshot_and_change_detection(self, mock_kb):
        idx = KnowledgeIndexer(mock_kb)
        idx.index_all()
        pipe = KBUpdatePipeline(idx, mock_kb)
        pipe.snapshot_hashes()

        # No changes right after snapshot
        assert len(pipe.scan_for_changes()) == 0

        # Modify a file
        path = os.path.join(
            mock_kb,
            "MultiChannel_API",
            "pillar_3_api_mcp_tools",
            "apis",
            "mcapi.v1.orders.get",
            "overview.yaml",
        )
        with open(path, "a") as f:
            f.write("\n# change\n")

        changes = pipe.scan_for_changes()
        assert len(changes) > 0

    def test_stats_reporting(self, pipeline):
        stats = pipeline.get_stats()
        assert "last_reindex" in stats
        assert "total_updates" in stats
        assert "pending_updates" in stats
        assert "error_count" in stats
        assert "tracked_files" in stats
        assert stats["tracked_files"] > 0

    def test_register_callback(self, pipeline):
        called = []
        pipeline.register_callback(lambda updates: called.extend(updates))
        # Callbacks are tested indirectly through process_changes
        assert len(pipeline._callbacks) == 1

    @pytest.mark.asyncio
    async def test_callback_invoked_on_changes(self, pipeline):
        results = []
        pipeline.register_callback(lambda updates: results.extend(updates))

        changes = [
            {
                "path": "MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.new2.get/overview.yaml",
                "change": "new",
            }
        ]
        await pipeline.process_changes(changes)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_identify_doc_from_api_path(self, pipeline):
        doc_id = pipeline._identify_doc_from_path(
            "MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.orders.get/overview.yaml"
        )
        assert doc_id == "mcapi.v1.orders.get"

    @pytest.mark.asyncio
    async def test_identify_doc_from_table_path(self, pipeline):
        doc_id = pipeline._identify_doc_from_path(
            "MultiChannel_API/pillar_1_schema/tables/orders/columns.yaml"
        )
        assert doc_id == "table.multichannel_api.orders"


# =====================================================================
# Integration tests
# =====================================================================


class TestBrainIntegration:
    @pytest.mark.asyncio
    async def test_end_to_end_create_index_query(self, mock_kb):
        """End-to-end: create KB -> index -> query -> get result."""
        brain = create_brain(mock_kb)

        assert brain["document_count"] == 7
        assert brain["indexer"].is_indexed is True

        # Query
        state = await brain["graph"].process("show pending orders")
        assert state.phase in (GraphPhase.RESPOND, GraphPhase.ESCALATE)
        assert len(state.phases_completed) >= 3

    @pytest.mark.asyncio
    async def test_pipeline_update_then_query_reflects_new_data(self, mock_kb):
        """Pipeline update -> query reflects new data."""
        brain = create_brain(mock_kb)

        # Add a new API endpoint
        _make_api_dir(
            mock_kb,
            "MultiChannel_API",
            "mcapi.v1.returns.get",
            domain="returns",
            method="GET",
            path="/api/v1/returns",
            keywords=["returns", "refund", "reverse", "logistics"],
            aliases=["return list", "refund list"],
        )

        # Full reindex
        result = await brain["pipeline"].full_reindex()
        assert result["total"] == 8  # 7 + 1 new
        assert result["new"] >= 1

        # Search for the new doc
        results = brain["indexer"].search("returns refund")
        doc_ids = [doc.doc_id for doc, _ in results]
        assert "mcapi.v1.returns.get" in doc_ids

    @pytest.mark.asyncio
    async def test_create_brain_nonexistent_path(self, tmp_path):
        brain = create_brain(str(tmp_path / "nonexistent"))
        assert brain["document_count"] == 0
        assert brain["indexer"].is_indexed is True

    @pytest.mark.asyncio
    async def test_search_then_get_document(self, mock_kb):
        brain = create_brain(mock_kb)
        results = brain["indexer"].search("track shipment")
        assert len(results) > 0

        doc_id = results[0][0].doc_id
        doc = brain["indexer"].get_document(doc_id)
        assert doc is not None
        assert doc.doc_id == doc_id

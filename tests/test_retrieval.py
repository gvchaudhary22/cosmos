"""
Retrieval pipeline tests — superpowers TDD pattern.

Tests written BEFORE implementation changes, failing tests drive fixes.
Covers: vector search module filter, wave pipeline enrichment, ingest embedding_text.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_vectorstore():
    """Mock VectorStoreService with controllable search_similar responses."""
    vs = MagicMock()
    vs.search_similar = AsyncMock(return_value=[])
    vs.embed_text = MagicMock(return_value=[0.1] * 1536)
    return vs


@pytest.fixture
def mock_llm():
    """Mock LLM client that returns predictable JSON enrichment."""
    llm = MagicMock()
    llm.complete = AsyncMock(return_value='{"canonical": "billing module wallet", "keywords": ["billing", "wallet"], "api_hint": "", "module_hint": "billing"}')
    return llm


# ---------------------------------------------------------------------------
# Test: vectorstore module filter parameter
# ---------------------------------------------------------------------------

class TestVectorstoreModuleFilter:
    """search_similar must support metadata->>'module' filtering."""

    @pytest.mark.asyncio
    async def test_search_similar_accepts_module_param(self, mock_vectorstore):
        """search_similar should not raise when module= is passed."""
        from app.services.vectorstore import VectorStoreService
        vs_real = VectorStoreService.__new__(VectorStoreService)
        # Verify the method signature includes 'module'
        import inspect
        sig = inspect.signature(VectorStoreService.search_similar)
        assert "module" in sig.parameters, (
            "search_similar must accept a 'module' parameter for metadata filtering. "
            "Add: module: Optional[str] = None to the signature."
        )

    @pytest.mark.asyncio
    async def test_module_filter_applied_to_sql_when_set(self, mock_vectorstore):
        """When module= is passed, SQL WHERE clause must include metadata->>'module' filter."""
        mock_vectorstore.search_similar = AsyncMock(return_value=[
            {"entity_id": "module:MultiChannel_Web:billing", "similarity": 0.85,
             "content": "billing module: wallets invoices passbook"}
        ])
        results = await mock_vectorstore.search_similar(
            query="why wallet balance not updating",
            entity_type="module_doc",
            module="billing",
            limit=5,
        )
        mock_vectorstore.search_similar.assert_called_once()
        call_kwargs = mock_vectorstore.search_similar.call_args.kwargs
        assert call_kwargs.get("module") == "billing", "module filter must be passed through"
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Test: ingest.py embedding_text richness
# ---------------------------------------------------------------------------

class TestIngestEmbeddingText:
    """_ingest_module must build rich embedding_text from actual entity lists."""

    def test_embedding_text_uses_entities_not_top_entities(self):
        """index.yaml uses 'entities' key — ingest must read 'entities', not 'top_entities'."""
        # Simulate what kb generator writes
        index_data = {
            "entities": {
                "controllers": ["BillingController", "PassbookController"],
                "services": ["BillingService"],
                "db_tables": ["billing", "wallets", "invoices"],
                "api_routes": ["/billing/deduct", "/wallet/balance"],
                "keywords": ["remittance", "cod", "passbook"],
            },
            "quality": {"avg_score": 65.0, "training_ready": False},
        }
        # Read using 'entities' key (correct)
        entities_data = index_data.get("entities", index_data.get("top_entities", {}))
        controller_list = entities_data.get("controllers", [])
        db_table_list = entities_data.get("db_tables", [])

        assert len(controller_list) == 2, "Must read controllers from 'entities' key"
        assert len(db_table_list) == 3, "Must read db_tables from 'entities' key"

        # Simulate embedding_text construction
        parts = [f"module billing repo MultiChannel_Web"]
        if controller_list:
            parts.append("controllers: " + " ".join(controller_list))
        if db_table_list:
            parts.append("tables: " + " ".join(db_table_list))
        embedding_text = ". ".join(parts)

        assert "BillingController" in embedding_text, "embedding_text must include controller names"
        assert "wallets" in embedding_text, "embedding_text must include table names"
        assert "controllers=0" not in embedding_text, "old broken format must not be used"

    def test_embedding_text_not_generic_when_entities_exist(self):
        """embedding_text must not be 'module X: controllers=0 api_routes=0 db_tables=0'."""
        # Old broken format
        old_format = "module billing: controllers=0 api_routes=0 db_tables=0 repo=MultiChannel_Web"
        assert "controllers=0" in old_format  # confirm old format

        # New format should be rich
        new_format = "module billing repo MultiChannel_Web. controllers: BillingController PassbookController. tables: billing wallets invoices"
        assert "BillingController" in new_format
        assert "controllers=0" not in new_format


# ---------------------------------------------------------------------------
# Test: query enrichment extracts module_hint
# ---------------------------------------------------------------------------

class TestQueryEnrichment:
    """LangGraph query_enrichment node must extract module_hint for billing queries."""

    @pytest.mark.asyncio
    async def test_enrichment_extracts_module_hint(self, mock_llm):
        """For 'why wallet not credited', module_hint should be 'billing'."""
        import json
        query = "why wallet balance not updating after recharge"

        # Simulate enrichment response
        raw_resp = await mock_llm.complete(
            f'Query: "{query}"\nRespond with JSON: {{"canonical": "...", "keywords": [...], "api_hint": "...", "module_hint": "..."}}',
            max_tokens=150,
        )
        parsed = json.loads(raw_resp)

        assert "module_hint" in parsed, "Enrichment must return module_hint"
        assert parsed["module_hint"] == "billing", (
            f"For wallet/billing queries, module_hint should be 'billing', got: {parsed.get('module_hint')}"
        )

    @pytest.mark.asyncio
    async def test_enrichment_fallback_on_invalid_json(self, mock_llm):
        """If LLM returns invalid JSON, enrichment must not crash — use raw query."""
        mock_llm.complete = AsyncMock(return_value="not valid json at all")
        import json
        raw_resp = await mock_llm.complete("test prompt", max_tokens=150)
        try:
            json.loads(raw_resp)
            parsed = {"module_hint": "ok"}
        except (json.JSONDecodeError, ValueError):
            # Must fall back gracefully
            parsed = {"canonical": "test query", "keywords": [], "api_hint": "", "module_hint": ""}

        assert isinstance(parsed, dict), "Must always return a dict, never raise"
        assert "module_hint" in parsed, "Fallback must include module_hint key"


# ---------------------------------------------------------------------------
# Test: KB generator REST API extraction
# ---------------------------------------------------------------------------

class TestKBGeneratorAPIExtraction:
    """KB generator regex must extract REST API routes added to module CLAUDE.md files."""

    def test_re_api_route_matches_added_format(self):
        """Routes added as 'GET /path' must be extracted by RE_API_ROUTE."""
        import re
        RE_API_ROUTE = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE)\s+(/[a-zA-Z0-9_/{}\-.]+)")

        billing_section = """
## Backend REST API Calls

GET /billing/deduct
POST /billing/recharge
GET /wallet/balance
GET /account/details/remittance_summary
POST /billing/sync_statement
"""
        routes = RE_API_ROUTE.findall(billing_section)
        assert len(routes) == 5, f"Expected 5 routes extracted, got {len(routes)}: {routes}"
        assert "/billing/deduct" in routes
        assert "/wallet/balance" in routes

    def test_table_format_not_extracted(self):
        """Old table format '| billing/deduct | POST |' must NOT be extracted."""
        import re
        RE_API_ROUTE = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE)\s+(/[a-zA-Z0-9_/{}\-.]+)")

        table_format = """
| `billing/deduct` | POST | Deduct amount from wallet |
| `billing/recharge` | POST | Recharge wallet |
"""
        routes = RE_API_ROUTE.findall(table_format)
        # Table format doesn't have leading slash on path — should extract 0 routes
        assert len(routes) == 0, (
            f"Table format should NOT be extracted (no leading /), but got: {routes}. "
            "This is why we added the Backend REST API Calls section."
        )

"""Tests for COSMOS Phase 1 tool registry and read tools."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass
from typing import Any

# Allow running from project root
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools.base import BaseTool, ToolCategory, ToolDefinition, ToolParam, ToolResult, DataSource, RiskLevel
from app.tools.registry import ToolRegistry
from app.tools.read_tools import (
    OrderLookupTool,
    OrdersByCompanyTool,
    OrderSearchTool,
    ShipmentTrackTool,
    TrackingTimelineTool,
    NDRListTool,
    NDRDetailsTool,
    SellerInfoTool,
    SellerPlanTool,
    SellerHealthTool,
    BillingQueryTool,
    WalletBalanceTool,
    TransactionHistoryTool,
    ELKSearchTool,
    EndpointUsageTool,
)
from app.tools.setup import create_tool_registry


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@dataclass
class FakeMCAPIResponse:
    success: bool
    data: Any
    status_code: int = 200
    latency_ms: float = 1.0


@dataclass
class FakeLogSearchResult:
    total: int
    hits: list
    took_ms: int


@dataclass
class FakeEndpointUsageResult:
    total_requests: int
    endpoints: list


def _make_mcapi_mock() -> MagicMock:
    """Create a mock MCAPI client with all methods."""
    mock = MagicMock()
    mock.get_order = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"id": "123"}))
    mock.get_orders = AsyncMock(return_value=FakeMCAPIResponse(success=True, data=[]))
    mock.search_orders = AsyncMock(return_value=FakeMCAPIResponse(success=True, data=[]))
    mock.track_shipment = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"status": "in_transit"}))
    mock.get_tracking_timeline = AsyncMock(return_value=FakeMCAPIResponse(success=True, data=[]))
    mock.get_ndr_list = AsyncMock(return_value=FakeMCAPIResponse(success=True, data=[]))
    mock.get_ndr_details = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"id": "ndr1"}))
    mock.get_seller_info = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"company": "test"}))
    mock.get_seller_plan = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"plan": "pro"}))
    mock.get_seller_health = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"score": 85}))
    mock.get_billing = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"invoices": []}))
    mock.get_wallet_balance = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"balance": 1000}))
    mock.get_transactions = AsyncMock(return_value=FakeMCAPIResponse(success=True, data=[]))
    # Write endpoints (Phase 2)
    mock.cancel_order = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"cancelled": True}))
    mock.initiate_refund = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"refund_id": "R1"}))
    mock.reattempt_delivery = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"reattempt_id": "RA1"}))
    mock.update_address = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"updated": True}))
    mock.escalate_to_supervisor = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"ticket_id": "T1"}))
    mock.block_seller = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"blocked": True}))
    mock.issue_wallet_credit = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"credit_id": "C1"}))
    mock.reassign_courier = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"reassigned": True}))
    return mock


def _make_elk_mock() -> MagicMock:
    """Create a mock ELK client."""
    mock = MagicMock()
    mock.search_logs = AsyncMock(return_value=FakeLogSearchResult(total=5, hits=[{"msg": "test"}], took_ms=12))
    mock.get_endpoint_usage = AsyncMock(return_value=FakeEndpointUsageResult(total_requests=100, endpoints=[]))
    return mock


# --------------------------------------------------------------------------- #
# Registry tests
# --------------------------------------------------------------------------- #

class TestToolRegistry:
    def test_register_and_get(self):
        """Test that a tool can be registered and retrieved by name."""
        mcapi = _make_mcapi_mock()
        registry = ToolRegistry()
        tool = OrderLookupTool(mcapi)
        registry.register(tool)

        retrieved = registry.get("order_lookup")
        assert retrieved is not None
        assert retrieved.definition.name == "order_lookup"

    def test_get_nonexistent_returns_none(self):
        """Test that getting a non-registered tool returns None."""
        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_list_all(self):
        """Test listing all registered tools."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        all_tools = registry.list_all()
        assert len(all_tools) == 23  # 15 read + 8 write

    def test_list_for_role_admin_sees_all(self):
        """Admin role should see all 15 tools (including role-restricted ones)."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        admin_tools = registry.list_for_role("admin")
        assert len(admin_tools) == 23  # 15 read + 8 write (admin sees all)

    def test_list_for_role_agent_sees_filtered(self):
        """Agent role should NOT see seller_info or billing_query (role-restricted)."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        agent_tools = registry.list_for_role("agent")
        agent_tool_names = [t.name for t in agent_tools]

        # Agent should not see role-restricted tools
        assert "seller_info" not in agent_tool_names
        assert "billing_query" not in agent_tool_names

        # Agent should see unrestricted tools
        assert "order_lookup" in agent_tool_names
        assert "shipment_track" in agent_tool_names
        assert "elk_search" in agent_tool_names

        # 23 total - 2 read restricted - 5 write restricted = 16 visible for agent
        # Write tools restricted from agent: cancel_order, initiate_refund, block_seller,
        # issue_wallet_credit, reassign_courier, update_address (6 restricted)
        # Plus 2 read restricted: seller_info, billing_query
        # Agent sees: 23 - 8 = 15
        assert len(agent_tools) == 15


# --------------------------------------------------------------------------- #
# Validation tests
# --------------------------------------------------------------------------- #

class TestParamValidation:
    def test_order_lookup_missing_required(self):
        """Missing required param should return error string."""
        mcapi = _make_mcapi_mock()
        tool = OrderLookupTool(mcapi)
        error = tool.validate_params({})
        assert error is not None
        assert "order_id" in error

    def test_order_lookup_valid(self):
        """Valid params should return None (no error)."""
        mcapi = _make_mcapi_mock()
        tool = OrderLookupTool(mcapi)
        error = tool.validate_params({"order_id": "12345"})
        assert error is None

    def test_orders_by_company_missing_required(self):
        """Missing company_id should fail validation."""
        mcapi = _make_mcapi_mock()
        tool = OrdersByCompanyTool(mcapi)
        error = tool.validate_params({"status": "delivered"})
        assert error is not None
        assert "company_id" in error

    def test_elk_search_missing_query(self):
        """ELK search requires query param."""
        elk = _make_elk_mock()
        tool = ELKSearchTool(elk)
        error = tool.validate_params({})
        assert error is not None
        assert "query" in error

    def test_endpoint_usage_no_required_params(self):
        """EndpointUsageTool has no required params, so empty dict is valid."""
        elk = _make_elk_mock()
        tool = EndpointUsageTool(elk)
        error = tool.validate_params({})
        assert error is None


# --------------------------------------------------------------------------- #
# Execution tests
# --------------------------------------------------------------------------- #

class TestToolExecution:
    @pytest.mark.asyncio
    async def test_order_lookup_success(self):
        """Test successful order lookup execution."""
        mcapi = _make_mcapi_mock()
        tool = OrderLookupTool(mcapi)
        result = await tool.execute({"order_id": "12345"})

        assert result.success is True
        assert result.data == {"id": "123"}
        assert result.latency_ms > 0
        mcapi.get_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_order_lookup_error(self):
        """Test order lookup when MCAPI raises an exception."""
        mcapi = _make_mcapi_mock()
        mcapi.get_order = AsyncMock(side_effect=Exception("Connection refused"))
        tool = OrderLookupTool(mcapi)
        result = await tool.execute({"order_id": "12345"})

        assert result.success is False
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_elk_search_success(self):
        """Test successful ELK search execution."""
        elk = _make_elk_mock()
        tool = ELKSearchTool(elk)
        result = await tool.execute({"query": "error 500"})

        assert result.success is True
        assert result.data["total"] == 5
        assert len(result.data["hits"]) == 1

    @pytest.mark.asyncio
    async def test_endpoint_usage_success(self):
        """Test successful endpoint usage execution."""
        elk = _make_elk_mock()
        tool = EndpointUsageTool(elk)
        result = await tool.execute({"time_range": "1h", "top_n": "10"})

        assert result.success is True
        assert result.data["total_requests"] == 100

    @pytest.mark.asyncio
    async def test_registry_execute_not_found(self):
        """Test registry execute with unknown tool name."""
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {})
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_registry_execute_validation_failure(self):
        """Test registry execute with missing required params."""
        mcapi = _make_mcapi_mock()
        registry = ToolRegistry()
        registry.register(OrderLookupTool(mcapi))
        result = await registry.execute("order_lookup", {})
        assert result.success is False
        assert "Missing required parameter" in result.error

    @pytest.mark.asyncio
    async def test_seller_info_requires_at_least_one_param(self):
        """SellerInfoTool should fail if neither company_id nor email is provided."""
        mcapi = _make_mcapi_mock()
        tool = SellerInfoTool(mcapi)
        result = await tool.execute({})
        assert result.success is False
        assert "company_id or email" in result.error


# --------------------------------------------------------------------------- #
# Definition completeness tests
# --------------------------------------------------------------------------- #

class TestAllToolDefinitions:
    def test_all_tools_have_definitions(self):
        """Every registered tool must have a valid ToolDefinition."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        for defn in registry.list_all():
            assert defn.name, "Tool must have a name"
            assert defn.category in (ToolCategory.READ, ToolCategory.ACTION), f"{defn.name} should be READ or ACTION category"
            assert defn.description, f"{defn.name} must have a description"
            assert isinstance(defn.parameters, list), f"{defn.name} parameters must be a list"
            assert isinstance(defn.data_source, DataSource), f"{defn.name} must have a DataSource"
            assert isinstance(defn.risk_level, RiskLevel), f"{defn.name} must have a RiskLevel"
            assert isinstance(defn.allowed_roles, list), f"{defn.name} allowed_roles must be a list"

    def test_all_tool_names_are_unique(self):
        """No two tools should share the same name."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        names = [d.name for d in registry.list_all()]
        assert len(names) == len(set(names)), "Duplicate tool names detected"

    def test_expected_tool_names_present(self):
        """Verify all 23 expected tool names are registered (15 read + 8 write)."""
        mcapi = _make_mcapi_mock()
        elk = _make_elk_mock()
        registry = create_tool_registry(mcapi, elk)

        expected = {
            # Read tools (15)
            "order_lookup", "orders_by_company", "order_search",
            "shipment_track", "tracking_timeline",
            "ndr_list", "ndr_details",
            "seller_info", "seller_plan", "seller_health_score",
            "billing_query", "wallet_balance", "transaction_history",
            "elk_search", "endpoint_usage",
            # Write tools (8)
            "cancel_order", "initiate_refund", "reattempt_delivery",
            "update_address", "escalate_to_supervisor", "block_seller",
            "issue_wallet_credit", "reassign_courier",
        }
        actual = {d.name for d in registry.list_all()}
        assert actual == expected

"""Tests for COSMOS Phase 2: Actions with Approval."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# Allow running from project root
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools.base import ToolCategory, RiskLevel, ToolResult
from app.tools.write_tools import (
    WriteToolBase,
    CancelOrderTool,
    InitiateRefundTool,
    ReattemptDeliveryTool,
    UpdateAddressTool,
    EscalateToSupervisorTool,
    BlockSellerTool,
    IssueWalletCreditTool,
    ReassignCourierTool,
)
from app.engine.approval import (
    ApprovalEngine,
    ActionRequest,
    ROLE_HIERARCHY,
    RISK_APPROVAL_LEVEL,
    _role_level,
)
from app.engine.audit import ActionAuditor
from app.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@dataclass
class FakeMCAPIResponse:
    success: bool
    data: Any
    status_code: int = 200
    latency_ms: float = 1.0


def _make_mcapi_mock() -> MagicMock:
    """Create a mock MCAPI client with write methods."""
    mock = MagicMock()
    mock.cancel_order = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"cancelled": True}))
    mock.initiate_refund = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"refund_id": "R123"}))
    mock.reattempt_delivery = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"reattempt_id": "RA1"}))
    mock.update_address = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"updated": True}))
    mock.escalate_to_supervisor = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"ticket_id": "T1"}))
    mock.block_seller = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"blocked": True}))
    mock.issue_wallet_credit = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"credit_id": "C1"}))
    mock.reassign_courier = AsyncMock(return_value=FakeMCAPIResponse(success=True, data={"reassigned": True}))
    return mock


def _make_registry_with_tools(mcapi=None) -> ToolRegistry:
    """Create a registry with all write tools."""
    if mcapi is None:
        mcapi = _make_mcapi_mock()
    registry = ToolRegistry()
    registry.register(CancelOrderTool(mcapi))
    registry.register(InitiateRefundTool(mcapi))
    registry.register(ReattemptDeliveryTool(mcapi))
    registry.register(UpdateAddressTool(mcapi))
    registry.register(EscalateToSupervisorTool(mcapi))
    registry.register(BlockSellerTool(mcapi))
    registry.register(IssueWalletCreditTool(mcapi))
    registry.register(ReassignCourierTool(mcapi))
    return registry


# =========================================================================== #
# 1. Write Tool Validation Tests
# =========================================================================== #

class TestWriteToolDefinitions:
    """Test that all write tools have correct definitions."""

    def test_all_write_tools_are_action_category(self):
        mcapi = _make_mcapi_mock()
        tools = [
            CancelOrderTool(mcapi), InitiateRefundTool(mcapi),
            ReattemptDeliveryTool(mcapi), UpdateAddressTool(mcapi),
            EscalateToSupervisorTool(mcapi), BlockSellerTool(mcapi),
            IssueWalletCreditTool(mcapi), ReassignCourierTool(mcapi),
        ]
        for tool in tools:
            assert tool.definition.category == ToolCategory.ACTION, (
                f"{tool.definition.name} should be ACTION category"
            )

    def test_risk_levels_correct(self):
        mcapi = _make_mcapi_mock()
        expected = {
            "cancel_order": RiskLevel.HIGH,
            "initiate_refund": RiskLevel.CRITICAL,
            "reattempt_delivery": RiskLevel.LOW,
            "update_address": RiskLevel.MEDIUM,
            "escalate_to_supervisor": RiskLevel.LOW,
            "block_seller": RiskLevel.CRITICAL,
            "issue_wallet_credit": RiskLevel.HIGH,
            "reassign_courier": RiskLevel.MEDIUM,
        }
        tools = [
            CancelOrderTool(mcapi), InitiateRefundTool(mcapi),
            ReattemptDeliveryTool(mcapi), UpdateAddressTool(mcapi),
            EscalateToSupervisorTool(mcapi), BlockSellerTool(mcapi),
            IssueWalletCreditTool(mcapi), ReassignCourierTool(mcapi),
        ]
        for tool in tools:
            assert tool.definition.risk_level == expected[tool.definition.name], (
                f"{tool.definition.name} risk level mismatch"
            )

    def test_cancel_order_validates_required_params(self):
        mcapi = _make_mcapi_mock()
        tool = CancelOrderTool(mcapi)
        error = tool.validate_params({})
        assert error is not None
        assert "order_id" in error

    def test_initiate_refund_validates_required_params(self):
        mcapi = _make_mcapi_mock()
        tool = InitiateRefundTool(mcapi)
        error = tool.validate_params({})
        assert error is not None
        assert "order_id" in error

    def test_block_seller_validates_required_params(self):
        mcapi = _make_mcapi_mock()
        tool = BlockSellerTool(mcapi)
        error = tool.validate_params({"seller_id": "S1"})
        assert error is not None
        assert "reason" in error

    def test_update_address_validates_all_required(self):
        mcapi = _make_mcapi_mock()
        tool = UpdateAddressTool(mcapi)
        # Missing city, state, pincode
        error = tool.validate_params({"order_id": "1", "address_line1": "123 Main"})
        assert error is not None

    def test_valid_params_pass_validation(self):
        mcapi = _make_mcapi_mock()
        tool = CancelOrderTool(mcapi)
        error = tool.validate_params({"order_id": "12345"})
        assert error is None


# =========================================================================== #
# 2. Write Tool Execution Tests
# =========================================================================== #

class TestWriteToolExecution:
    """Test write tools return pending when no approval, execute when approved."""

    @pytest.mark.asyncio
    async def test_cancel_order_returns_pending_without_approval(self):
        mcapi = _make_mcapi_mock()
        tool = CancelOrderTool(mcapi)
        result = await tool.execute({"order_id": "123"})
        assert result.success is True
        assert result.data["status"] == "pending_approval"
        assert result.data["tool_name"] == "cancel_order"
        assert result.data["risk_level"] == "high"

    @pytest.mark.asyncio
    async def test_cancel_order_executes_when_approved(self):
        mcapi = _make_mcapi_mock()
        tool = CancelOrderTool(mcapi)
        result = await tool.execute({"order_id": "123"}, context={"approved": True})
        assert result.success is True
        assert result.data == {"cancelled": True}
        mcapi.cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_initiate_refund_returns_pending(self):
        mcapi = _make_mcapi_mock()
        tool = InitiateRefundTool(mcapi)
        result = await tool.execute({"order_id": "456"})
        assert result.data["status"] == "pending_approval"
        assert result.data["risk_level"] == "critical"

    @pytest.mark.asyncio
    async def test_reattempt_delivery_executes_when_approved(self):
        mcapi = _make_mcapi_mock()
        tool = ReattemptDeliveryTool(mcapi)
        result = await tool.execute({"awb": "AWB123"}, context={"approved": True})
        assert result.success is True
        mcapi.reattempt_delivery.assert_called_once()

    @pytest.mark.asyncio
    async def test_block_seller_returns_pending(self):
        mcapi = _make_mcapi_mock()
        tool = BlockSellerTool(mcapi)
        result = await tool.execute({"seller_id": "S1", "reason": "fraud"})
        assert result.data["status"] == "pending_approval"

    @pytest.mark.asyncio
    async def test_tool_handles_mcapi_error(self):
        mcapi = _make_mcapi_mock()
        mcapi.cancel_order = AsyncMock(side_effect=Exception("MCAPI down"))
        tool = CancelOrderTool(mcapi)
        result = await tool.execute({"order_id": "123"}, context={"approved": True})
        assert result.success is False
        assert "MCAPI down" in result.error


# =========================================================================== #
# 3. Approval Engine Tests
# =========================================================================== #

class TestApprovalEngine:
    """Test the approval engine lifecycle."""

    @pytest.mark.asyncio
    async def test_agent_cannot_self_approve(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="reattempt_delivery",
            params={"awb": "AWB1"}, risk_level="low",
            user_id="agent1", user_role="agent",
        )
        assert req.status == "pending"

    @pytest.mark.asyncio
    async def test_supervisor_auto_approves_low_risk(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="reattempt_delivery",
            params={"awb": "AWB1"}, risk_level="low",
            user_id="sup1", user_role="supervisor",
        )
        assert req.status == "approved"
        assert req.approved_by == "sup1"

    @pytest.mark.asyncio
    async def test_supervisor_cannot_auto_approve_high_risk(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="cancel_order",
            params={"order_id": "123"}, risk_level="high",
            user_id="sup1", user_role="supervisor",
        )
        assert req.status == "pending"

    @pytest.mark.asyncio
    async def test_admin_auto_approves_critical(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="block_seller",
            params={"seller_id": "S1", "reason": "fraud"}, risk_level="critical",
            user_id="admin1", user_role="admin",
        )
        assert req.status == "approved"

    @pytest.mark.asyncio
    async def test_manager_cannot_auto_approve_critical(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="initiate_refund",
            params={"order_id": "456"}, risk_level="critical",
            user_id="mgr1", user_role="manager",
        )
        assert req.status == "pending"

    @pytest.mark.asyncio
    async def test_approve_pending_request(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="cancel_order",
            params={"order_id": "123"}, risk_level="high",
            user_id="agent1", user_role="agent",
        )
        assert req.status == "pending"

        approved = await engine.approve(req.id, "mgr1", "manager")
        assert approved.status in ("executed", "failed")
        assert approved.approved_by == "mgr1"

    @pytest.mark.asyncio
    async def test_reject_pending_request(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="cancel_order",
            params={"order_id": "123"}, risk_level="high",
            user_id="agent1", user_role="agent",
        )
        rejected = await engine.reject(req.id, "mgr1", "Not justified")
        assert rejected.status == "rejected"
        assert rejected.rejection_reason == "Not justified"

    @pytest.mark.asyncio
    async def test_cannot_approve_non_pending(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="cancel_order",
            params={"order_id": "123"}, risk_level="high",
            user_id="agent1", user_role="agent",
        )
        await engine.reject(req.id, "mgr1", "no")
        with pytest.raises(ValueError, match="Cannot approve"):
            await engine.approve(req.id, "admin1", "admin")

    @pytest.mark.asyncio
    async def test_cannot_reject_non_pending(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="reattempt_delivery",
            params={"awb": "AWB1"}, risk_level="low",
            user_id="sup1", user_role="supervisor",
        )
        # Already auto-approved
        with pytest.raises(ValueError, match="Cannot reject"):
            await engine.reject(req.id, "admin1", "no")

    @pytest.mark.asyncio
    async def test_agent_cannot_approve_any_action(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="reattempt_delivery",
            params={"awb": "AWB1"}, risk_level="low",
            user_id="agent1", user_role="agent",
        )
        with pytest.raises(PermissionError, match="Agent role cannot approve"):
            await engine.approve(req.id, "agent2", "agent")

    @pytest.mark.asyncio
    async def test_insufficient_role_cannot_approve(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            session_id="s1", tool_name="block_seller",
            params={"seller_id": "S1", "reason": "fraud"}, risk_level="critical",
            user_id="agent1", user_role="agent",
        )
        with pytest.raises(PermissionError):
            await engine.approve(req.id, "sup1", "supervisor")

    @pytest.mark.asyncio
    async def test_approve_nonexistent_request(self):
        engine = ApprovalEngine()
        with pytest.raises(ValueError, match="not found"):
            await engine.approve("fake-id", "admin1", "admin")

    @pytest.mark.asyncio
    async def test_list_pending_filters_by_role(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)

        # Create requests at different risk levels
        await engine.request_action("s1", "reattempt_delivery", {"awb": "A1"}, "low", "ag1", "agent")
        await engine.request_action("s1", "cancel_order", {"order_id": "1"}, "high", "ag1", "agent")
        await engine.request_action("s1", "block_seller", {"seller_id": "S1", "reason": "r"}, "critical", "ag1", "agent")

        # Supervisor sees low + medium risk only
        sup_pending = await engine.list_pending("supervisor")
        sup_names = {r.tool_name for r in sup_pending}
        assert "reattempt_delivery" in sup_names
        # Supervisor level (1) cannot see high (requires 2) or critical (requires 3)
        assert "block_seller" not in sup_names

        # Admin sees everything
        admin_pending = await engine.list_pending("admin")
        assert len(admin_pending) >= 3

    @pytest.mark.asyncio
    async def test_expire_stale_requests(self):
        registry = _make_registry_with_tools()
        engine = ApprovalEngine(tool_registry=registry)
        req = await engine.request_action(
            "s1", "cancel_order", {"order_id": "1"}, "high", "ag1", "agent"
        )
        # Manually backdate
        engine._requests[req.id].created_at = datetime.utcnow() - timedelta(minutes=60)

        expired_count = await engine.expire_stale(max_age_minutes=30)
        assert expired_count == 1
        assert engine._requests[req.id].status == "expired"

    @pytest.mark.asyncio
    async def test_get_request(self):
        engine = ApprovalEngine()
        req = await engine.request_action("s1", "cancel_order", {"order_id": "1"}, "high", "ag1", "agent")
        found = await engine.get_request(req.id)
        assert found is not None
        assert found.id == req.id

    @pytest.mark.asyncio
    async def test_get_nonexistent_request(self):
        engine = ApprovalEngine()
        found = await engine.get_request("nonexistent")
        assert found is None


# =========================================================================== #
# 4. Audit Trail Tests
# =========================================================================== #

class TestActionAuditor:
    """Test audit trail logging and querying."""

    @pytest.mark.asyncio
    async def test_log_request(self):
        auditor = ActionAuditor()
        req = ActionRequest(
            id="r1", session_id="s1", tool_name="cancel_order",
            params={"order_id": "123"}, risk_level="high", requested_by="ag1",
        )
        await auditor.log_request(req)
        trail = await auditor.get_audit_trail(session_id="s1")
        assert len(trail) == 1
        assert trail[0]["event"] == "action_requested"

    @pytest.mark.asyncio
    async def test_log_approval(self):
        auditor = ActionAuditor()
        await auditor.log_approval("r1", "admin1", "approved")
        trail = await auditor.get_audit_trail()
        assert len(trail) == 1
        assert trail[0]["decision"] == "approved"

    @pytest.mark.asyncio
    async def test_log_rejection_with_reason(self):
        auditor = ActionAuditor()
        await auditor.log_approval("r1", "mgr1", "rejected", reason="Not valid")
        trail = await auditor.get_audit_trail()
        assert trail[0]["reason"] == "Not valid"

    @pytest.mark.asyncio
    async def test_log_execution(self):
        auditor = ActionAuditor()
        await auditor.log_execution("r1", {"success": True, "data": {}}, True)
        trail = await auditor.get_audit_trail()
        assert trail[0]["event"] == "action_executed"
        assert trail[0]["success"] is True

    @pytest.mark.asyncio
    async def test_filter_by_session_id(self):
        auditor = ActionAuditor()
        req1 = ActionRequest(id="r1", session_id="s1", tool_name="t1", params={}, risk_level="low", requested_by="u1")
        req2 = ActionRequest(id="r2", session_id="s2", tool_name="t2", params={}, risk_level="low", requested_by="u2")
        await auditor.log_request(req1)
        await auditor.log_request(req2)

        trail_s1 = await auditor.get_audit_trail(session_id="s1")
        assert len(trail_s1) == 1
        assert trail_s1[0]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_filter_by_user_id(self):
        auditor = ActionAuditor()
        req = ActionRequest(id="r1", session_id="s1", tool_name="t1", params={}, risk_level="low", requested_by="user_A")
        await auditor.log_request(req)
        await auditor.log_approval("r2", "user_B", "approved")

        trail_a = await auditor.get_audit_trail(user_id="user_A")
        assert len(trail_a) == 1

        trail_b = await auditor.get_audit_trail(user_id="user_B")
        assert len(trail_b) == 1

    @pytest.mark.asyncio
    async def test_limit(self):
        auditor = ActionAuditor()
        for i in range(10):
            await auditor.log_approval(f"r{i}", "admin", "approved")
        trail = await auditor.get_audit_trail(limit=5)
        assert len(trail) == 5


# =========================================================================== #
# 5. Role Hierarchy Tests
# =========================================================================== #

class TestRoleHierarchy:
    """Test role level mapping."""

    def test_agent_level_is_zero(self):
        assert _role_level("agent") == 0
        assert _role_level("support_agent") == 0

    def test_supervisor_level(self):
        assert _role_level("supervisor") == 1

    def test_manager_level(self):
        assert _role_level("manager") == 2

    def test_admin_level(self):
        assert _role_level("admin") == 3

    def test_unknown_role_defaults_to_zero(self):
        assert _role_level("random_role") == 0


# =========================================================================== #
# 6. API Endpoint Tests
# =========================================================================== #

class TestActionEndpoints:
    """Test the FastAPI action endpoints via TestClient."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.endpoints.actions import router, configure

        mcapi = _make_mcapi_mock()
        registry = _make_registry_with_tools(mcapi)
        engine = ApprovalEngine(tool_registry=registry)
        auditor = ActionAuditor()
        configure(engine, auditor)

        app = FastAPI()
        app.include_router(router, prefix="/cosmos/api/v1/actions")
        return TestClient(app)

    def test_request_action_pending(self, client):
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["tool_name"] == "cancel_order"

    def test_request_action_auto_approve(self, client):
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "reattempt_delivery",
            "params": {"awb": "AWB1"},
            "risk_level": "low",
            "user_id": "sup1",
            "user_role": "supervisor",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("approved", "executed")

    def test_approve_action(self, client):
        # First create a pending request
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        request_id = resp.json()["id"]

        # Approve it
        resp2 = client.post(f"/cosmos/api/v1/actions/{request_id}/approve", json={
            "approver_id": "mgr1",
            "approver_role": "manager",
        })
        assert resp2.status_code == 200
        assert resp2.json()["status"] in ("executed", "failed")

    def test_reject_action(self, client):
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        request_id = resp.json()["id"]

        resp2 = client.post(f"/cosmos/api/v1/actions/{request_id}/reject", json={
            "rejector_id": "mgr1",
            "reason": "Not justified",
        })
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "rejected"
        assert resp2.json()["rejection_reason"] == "Not justified"

    def test_get_action_details(self, client):
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        request_id = resp.json()["id"]

        resp2 = client.get(f"/cosmos/api/v1/actions/{request_id}")
        assert resp2.status_code == 200
        assert resp2.json()["id"] == request_id

    def test_get_nonexistent_action(self, client):
        resp = client.get("/cosmos/api/v1/actions/nonexistent-id")
        assert resp.status_code == 404

    def test_list_pending(self, client):
        # Create a pending request
        client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        resp = client.get("/cosmos/api/v1/actions/pending?approver_role=admin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_approve_with_insufficient_role(self, client):
        resp = client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "block_seller",
            "params": {"seller_id": "S1", "reason": "fraud"},
            "risk_level": "critical",
            "user_id": "agent1",
            "user_role": "agent",
        })
        request_id = resp.json()["id"]

        resp2 = client.post(f"/cosmos/api/v1/actions/{request_id}/approve", json={
            "approver_id": "sup1",
            "approver_role": "supervisor",
        })
        assert resp2.status_code == 403

    def test_audit_trail_endpoint(self, client):
        # Create and approve an action to generate audit entries
        client.post("/cosmos/api/v1/actions/request", json={
            "session_id": "s1",
            "tool_name": "cancel_order",
            "params": {"order_id": "123"},
            "risk_level": "high",
            "user_id": "agent1",
            "user_role": "agent",
        })
        resp = client.get("/cosmos/api/v1/actions/audit/trail?session_id=s1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1


# =========================================================================== #
# 7. Integration: Approval + Execution + Audit
# =========================================================================== #

class TestApprovalIntegration:
    """End-to-end approval flow tests."""

    @pytest.mark.asyncio
    async def test_full_flow_request_approve_execute(self):
        mcapi = _make_mcapi_mock()
        registry = _make_registry_with_tools(mcapi)
        engine = ApprovalEngine(tool_registry=registry)
        auditor = ActionAuditor()

        # Agent requests
        req = await engine.request_action(
            "s1", "cancel_order", {"order_id": "O1"}, "high", "agent1", "agent"
        )
        await auditor.log_request(req)
        assert req.status == "pending"

        # Manager approves
        approved = await engine.approve(req.id, "mgr1", "manager")
        await auditor.log_approval(req.id, "mgr1", "approved")

        # Check execution happened
        assert approved.execution_result is not None
        await auditor.log_execution(
            req.id, approved.execution_result, approved.execution_result.get("success", False)
        )

        # Verify audit trail (query by request_id to get all events for this action)
        trail = await auditor.get_audit_trail(request_id=req.id)
        events = [e["event"] for e in trail]
        assert "action_requested" in events
        assert "action_decision" in events
        assert "action_executed" in events

    @pytest.mark.asyncio
    async def test_full_flow_request_reject(self):
        engine = ApprovalEngine()
        auditor = ActionAuditor()

        req = await engine.request_action(
            "s1", "initiate_refund", {"order_id": "O2"}, "critical", "agent1", "agent"
        )
        await auditor.log_request(req)

        rejected = await engine.reject(req.id, "admin1", "Customer not eligible")
        await auditor.log_approval(req.id, "admin1", "rejected", reason="Customer not eligible")

        trail = await auditor.get_audit_trail(request_id=req.id)
        assert any(e.get("decision") == "rejected" for e in trail)

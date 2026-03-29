"""
Tests for KB-Aware Guardrails and DPO Training Pipeline.
"""

import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock

import yaml

from app.guardrails.base import GuardrailAction
from app.guardrails.kb_guardrails import (
    ApprovalModeGuardrail,
    BlastRadiusGuardrail,
    KBPIIFieldGuardrail,
    KBSafetyIndex,
    RoutingGuardrail,
    kb_safety_index,
)


# ---------------------------------------------------------------------------
# KBSafetyIndex
# ---------------------------------------------------------------------------

class TestKBSafetyIndex:
    def _create_kb(self, tmp_dir, api_id, index_data, guardrails_data=None):
        """Helper to create a KB structure in a temp dir."""
        repo_dir = os.path.join(tmp_dir, "TestRepo")
        apis_dir = os.path.join(repo_dir, "pillar_3_api_mcp_tools", "apis", api_id)
        os.makedirs(apis_dir, exist_ok=True)

        with open(os.path.join(apis_dir, "index.yaml"), "w") as f:
            yaml.dump(index_data, f)

        if guardrails_data:
            with open(os.path.join(apis_dir, "guardrails.yaml"), "w") as f:
                yaml.dump(guardrails_data, f)

        return tmp_dir

    def test_load_from_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(tmp, "mcapi.v1.orders.get", {
                "summary": {
                    "candidate_tool": "order_lookup",
                    "domain": "orders",
                    "method": "GET",
                    "path": "/api/v1/orders",
                },
                "safety": {
                    "read_write_type": "READ",
                    "idempotent": True,
                    "approval_mode": "auto",
                    "blast_radius": "low",
                    "pii_fields": ["customer_name", "customer_email"],
                },
            })

            idx = KBSafetyIndex()
            stats = idx.load_from_kb(tmp)
            assert stats["tools_indexed"] == 1
            assert stats["apis_loaded"] == 1

    def test_get_tool_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(tmp, "mcapi.v1.orders.cancel.post", {
                "summary": {"candidate_tool": "cancel_order", "domain": "orders"},
                "safety": {"blast_radius": "high", "approval_mode": "manual"},
            })

            idx = KBSafetyIndex()
            idx.load_from_kb(tmp)

            safety = idx.get_tool_safety("cancel_order")
            assert safety is not None
            assert safety["blast_radius"] == "high"
            assert safety["approval_mode"] == "manual"

    def test_get_pii_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(tmp, "mcapi.v1.ndr.get", {
                "summary": {"candidate_tool": "ndr_list", "domain": "ndr"},
                "safety": {"pii_fields": ["customer_name", "shipping_address"]},
            })

            idx = KBSafetyIndex()
            idx.load_from_kb(tmp)

            fields = idx.get_pii_fields("ndr_list")
            assert "customer_name" in fields
            assert "shipping_address" in fields

    def test_missing_candidate_tool_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(tmp, "mcapi.v1.unknown", {
                "summary": {"domain": "test"},  # No candidate_tool
                "safety": {},
            })

            idx = KBSafetyIndex()
            stats = idx.load_from_kb(tmp)
            assert stats["tools_indexed"] == 0

    def test_guardrails_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(
                tmp,
                "mcapi.v1.shipments.ndr.get",
                {
                    "summary": {"candidate_tool": "ndr_list", "domain": "ndr"},
                    "safety": {"blast_radius": "low"},
                },
                guardrails_data={
                    "guardrails": {
                        "routing_guardrails": ["Prefer for seller-facing NDR workflows."],
                        "data_safety": ["Mask PII in restricted contexts."],
                    }
                },
            )

            idx = KBSafetyIndex()
            idx.load_from_kb(tmp)

            safety = idx.get_tool_safety("ndr_list")
            assert safety["routing_guardrails"] == ["Prefer for seller-facing NDR workflows."]

    def test_empty_kb_path(self):
        idx = KBSafetyIndex()
        stats = idx.load_from_kb("/nonexistent/path")
        assert stats["tools_indexed"] == 0

    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._create_kb(tmp, "api1", {
                "summary": {"candidate_tool": "t1"}, "safety": {"blast_radius": "low"}
            })
            self._create_kb(tmp, "api2", {
                "summary": {"candidate_tool": "t2"}, "safety": {"blast_radius": "high"}
            })

            idx = KBSafetyIndex()
            idx.load_from_kb(tmp)
            stats = idx.get_stats()
            assert stats["total_tools"] == 2
            assert stats["blast_radius_distribution"]["low"] == 1
            assert stats["blast_radius_distribution"]["high"] == 1


# ---------------------------------------------------------------------------
# BlastRadiusGuardrail
# ---------------------------------------------------------------------------

class TestBlastRadiusGuardrail:
    @pytest.fixture(autouse=True)
    def _setup(self):
        # Inject test data into the singleton
        kb_safety_index._tools["safe_tool"] = {"blast_radius": "low"}
        kb_safety_index._tools["medium_tool"] = {"blast_radius": "medium"}
        kb_safety_index._tools["dangerous_tool"] = {"blast_radius": "high"}
        kb_safety_index._tools["critical_tool"] = {"blast_radius": "critical"}
        yield
        for k in ["safe_tool", "medium_tool", "dangerous_tool", "critical_tool"]:
            kb_safety_index._tools.pop(k, None)

    @pytest.mark.asyncio
    async def test_low_blast_allows(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "safe_tool"})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_medium_blast_warns(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "medium_tool"})
        assert result.action == GuardrailAction.WARN

    @pytest.mark.asyncio
    async def test_high_blast_blocks_without_approval(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "dangerous_tool"})
        assert result.action == GuardrailAction.BLOCK

    @pytest.mark.asyncio
    async def test_high_blast_allows_with_approval(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "dangerous_tool", "approved": True})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_critical_blocks_non_admin(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "critical_tool", "user_role": "support"})
        assert result.action == GuardrailAction.BLOCK

    @pytest.mark.asyncio
    async def test_critical_allows_admin(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "critical_tool", "user_role": "admin"})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_unknown_tool_allows(self):
        guard = BlastRadiusGuardrail()
        result = await guard.check({"tool_name": "nonexistent"})
        assert result.action == GuardrailAction.ALLOW


# ---------------------------------------------------------------------------
# ApprovalModeGuardrail
# ---------------------------------------------------------------------------

class TestApprovalModeGuardrail:
    @pytest.fixture(autouse=True)
    def _setup(self):
        kb_safety_index._tools["auto_tool"] = {"approval_mode": "auto"}
        kb_safety_index._tools["confirm_tool"] = {"approval_mode": "confirm"}
        kb_safety_index._tools["manual_tool"] = {"approval_mode": "manual"}
        yield
        for k in ["auto_tool", "confirm_tool", "manual_tool"]:
            kb_safety_index._tools.pop(k, None)

    @pytest.mark.asyncio
    async def test_auto_allows(self):
        guard = ApprovalModeGuardrail()
        result = await guard.check({"tool_name": "auto_tool"})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_confirm_blocks_without_approval(self):
        guard = ApprovalModeGuardrail()
        result = await guard.check({"tool_name": "confirm_tool"})
        assert result.action == GuardrailAction.BLOCK

    @pytest.mark.asyncio
    async def test_confirm_allows_with_approval(self):
        guard = ApprovalModeGuardrail()
        result = await guard.check({"tool_name": "confirm_tool", "approved": True})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_manual_blocks_without_approval(self):
        guard = ApprovalModeGuardrail()
        result = await guard.check({"tool_name": "manual_tool"})
        assert result.action == GuardrailAction.BLOCK


# ---------------------------------------------------------------------------
# KBPIIFieldGuardrail
# ---------------------------------------------------------------------------

class TestKBPIIFieldGuardrail:
    @pytest.fixture(autouse=True)
    def _setup(self):
        kb_safety_index._tools["ndr_list"] = {
            "pii_fields": ["customer_name", "customer_email"],
        }
        yield
        kb_safety_index._tools.pop("ndr_list", None)

    @pytest.mark.asyncio
    async def test_masks_pii_fields(self):
        guard = KBPIIFieldGuardrail()
        response = '{"customer_name": "John Doe", "status": "pending"}'
        result = await guard.check({
            "tool_name": "ndr_list",
            "user_role": "agent",
            "response": response,
        })
        assert result.action == GuardrailAction.MASK
        assert "[MASKED:customer_name]" in result.modified_data
        assert "pending" in result.modified_data

    @pytest.mark.asyncio
    async def test_admin_sees_full_data(self):
        guard = KBPIIFieldGuardrail()
        result = await guard.check({
            "tool_name": "ndr_list",
            "user_role": "admin",
            "response": '{"customer_name": "John"}',
        })
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_no_pii_fields_allows(self):
        guard = KBPIIFieldGuardrail()
        result = await guard.check({
            "tool_name": "unknown_tool",
            "user_role": "agent",
            "response": '{"data": "safe"}',
        })
        assert result.action == GuardrailAction.ALLOW


# ---------------------------------------------------------------------------
# RoutingGuardrail
# ---------------------------------------------------------------------------

class TestRoutingGuardrail:
    @pytest.fixture(autouse=True)
    def _setup(self):
        kb_safety_index._tools["ndr_list"] = {
            "domain": "ndr",
            "routing_guardrails": ["Prefer for seller-facing NDR workflows."],
        }
        yield
        kb_safety_index._tools.pop("ndr_list", None)

    @pytest.mark.asyncio
    async def test_matching_domain_allows(self):
        guard = RoutingGuardrail()
        result = await guard.check({"tool_name": "ndr_list", "intent": "ndr_lookup"})
        assert result.action == GuardrailAction.ALLOW

    @pytest.mark.asyncio
    async def test_mismatched_domain_warns(self):
        guard = RoutingGuardrail()
        result = await guard.check({"tool_name": "ndr_list", "intent": "billing_query"})
        assert result.action == GuardrailAction.WARN

    @pytest.mark.asyncio
    async def test_no_intent_allows(self):
        guard = RoutingGuardrail()
        result = await guard.check({"tool_name": "ndr_list", "intent": ""})
        assert result.action == GuardrailAction.ALLOW


# ---------------------------------------------------------------------------
# DPO Pipeline (unit tests without DB)
# ---------------------------------------------------------------------------

class TestDPOPipeline:
    def test_dpo_pair_dataclass(self):
        from app.learning.dpo_pipeline import DPOPair
        pair = DPOPair(
            prompt="Where is my order?",
            chosen="Your order is in transit.",
            rejected="I don't know.",
            intent="order_tracking",
            entity="order",
            chosen_confidence=0.95,
            rejected_confidence=0.2,
            chosen_feedback=5,
            rejected_feedback=1,
            chosen_model="claude-haiku",
            rejected_model="claude-haiku",
        )
        assert pair.chosen_confidence > pair.rejected_confidence
        assert pair.chosen_feedback > pair.rejected_feedback

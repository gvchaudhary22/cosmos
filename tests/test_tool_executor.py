"""
Tests for ToolExecutorService — DB-driven tool execution layer.

Covers:
  - Fallback registry (no DB): tool discovery + Anthropic-format conversion
  - Approval gate: HIGH-risk tools return pending_approval when not pre-approved
  - Approved execution: HIGH-risk tool executes when ctx.approved=True + mocked HTTP
  - Role filtering: wrong-role request blocked
  - Unknown tool: returns error result, not exception
  - call_with_tools() on LLMClient: tool_use blocks parsed, executor called
  - ReActEngine: act-intent query routes through tool_use loop
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tool_executor import (
    ToolExecutorService,
    SessionContext,
    ToolExecutionResult,
    _FALLBACK_INDEX,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_executor(db=None) -> ToolExecutorService:
    settings = MagicMock()
    settings.MCAPI_BASE_URL = "https://api.shiprocket.in"
    return ToolExecutorService(settings=settings, db=db)


# ---------------------------------------------------------------------------
# 1. Fallback registry — tool discovery
# ---------------------------------------------------------------------------

class TestFallbackRegistry:
    def test_orders_create_in_registry(self):
        assert "orders_create" in _FALLBACK_INDEX

    def test_orders_create_has_request_schema(self):
        tool = _FALLBACK_INDEX["orders_create"]
        schema = tool.request_schema
        assert schema["type"] == "object"
        assert "order_id" in schema["properties"]
        assert "order_items" in schema["required"]

    def test_to_anthropic_format(self):
        executor = make_executor()
        tool = _FALLBACK_INDEX["orders_create"]
        fmt = executor._to_anthropic_format(tool)
        assert fmt["name"] == "orders_create"
        assert "description" in fmt
        assert "input_schema" in fmt
        assert fmt["input_schema"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_get_tools_for_context_returns_matching(self):
        executor = make_executor()
        tools = await executor.get_tools_for_context(
            entity="orders", intent="act", user_role="operator"
        )
        names = [t["name"] for t in tools]
        assert "orders_create" in names
        assert "orders_cancel" in names

    @pytest.mark.asyncio
    async def test_get_tools_for_context_filters_by_role(self):
        executor = make_executor()
        # "viewer" should get orders_get (allowed) but not orders_create (operator/seller only)
        tools = await executor.get_tools_for_context(
            entity="orders", intent="lookup", user_role="viewer"
        )
        names = [t["name"] for t in tools]
        assert "orders_create" not in names

    @pytest.mark.asyncio
    async def test_get_tool_known(self):
        executor = make_executor()
        tool = await executor.get_tool("orders_create")
        assert tool is not None
        assert tool.risk_level == "high"

    @pytest.mark.asyncio
    async def test_get_tool_unknown_returns_none(self):
        executor = make_executor()
        tool = await executor.get_tool("does_not_exist")
        assert tool is None


# ---------------------------------------------------------------------------
# 2. Approval gate
# ---------------------------------------------------------------------------

class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_high_risk_tool_returns_pending_without_approval(self):
        executor = make_executor()
        ctx = SessionContext(seller_token="tok123", user_role="operator", approved=False)
        result = await executor.execute(
            "orders_create",
            {"order_id": "TEST-1", "order_date": "2026-04-04"},
            session_context=ctx,
        )
        assert result.status == "pending_approval"
        assert result.job_id is not None
        assert result.data["status"] == "pending_approval"
        assert "approval" in result.data["message"].lower()

    @pytest.mark.asyncio
    async def test_low_risk_tool_skips_approval_gate(self):
        executor = make_executor()
        ctx = SessionContext(seller_token="tok123", user_role="operator", approved=False)

        with patch.object(executor, "_call_http", new=AsyncMock(return_value={"order_id": 99})):
            result = await executor.execute("orders_get", {"order_id": 99}, ctx)

        assert result.status == "success"
        assert result.data == {"order_id": 99}

    @pytest.mark.asyncio
    async def test_high_risk_tool_executes_when_approved(self):
        executor = make_executor()
        ctx = SessionContext(seller_token="tok123", user_role="operator", approved=True)

        mock_response = {"order_id": "SR-9876", "status": "NEW"}
        with patch.object(executor, "_call_http", new=AsyncMock(return_value=mock_response)):
            result = await executor.execute("orders_create", {"order_id": "SR-1"}, ctx)

        assert result.status == "success"
        assert result.data["order_id"] == "SR-9876"


# ---------------------------------------------------------------------------
# 3. Role enforcement
# ---------------------------------------------------------------------------

class TestRoleEnforcement:
    @pytest.mark.asyncio
    async def test_disallowed_role_returns_error(self):
        executor = make_executor()
        ctx = SessionContext(user_role="viewer", approved=False)
        result = await executor.execute("orders_create", {}, ctx)
        assert result.status == "error"
        assert "not permitted" in result.error

    @pytest.mark.asyncio
    async def test_allowed_role_passes_gate(self):
        executor = make_executor()
        ctx = SessionContext(seller_token="tok", user_role="seller", approved=True)
        with patch.object(executor, "_call_http", new=AsyncMock(return_value={"ok": True})):
            result = await executor.execute("orders_create", {"order_id": "X"}, ctx)
        assert result.status == "success"


# ---------------------------------------------------------------------------
# 4. Unknown tool
# ---------------------------------------------------------------------------

class TestUnknownTool:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_not_exception(self):
        executor = make_executor()
        ctx = SessionContext(user_role="operator")
        result = await executor.execute("nonexistent_tool", {}, ctx)
        assert result.status == "error"
        assert "not found" in result.error


# ---------------------------------------------------------------------------
# 5. LLMClient.call_with_tools() — tool_use block parsing
# ---------------------------------------------------------------------------

class TestCallWithTools:
    @pytest.mark.asyncio
    async def test_call_with_tools_invokes_executor_on_tool_use(self):
        """Claude returns a tool_use block → executor is called → result fed back → final text."""
        from app.engine.llm_client import LLMClient, ToolUseResult

        # Build a mock Anthropic client
        mock_anthropic = AsyncMock()

        # Turn 1: Claude returns tool_use
        turn1_block_tool = MagicMock()
        turn1_block_tool.type = "tool_use"
        turn1_block_tool.id = "toolu_abc"
        turn1_block_tool.name = "orders_create"
        turn1_block_tool.input = {"order_id": "SR-1", "order_date": "2026-04-04"}

        turn1_response = MagicMock()
        turn1_response.content = [turn1_block_tool]
        turn1_response.stop_reason = "tool_use"
        turn1_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        # Turn 2: Claude synthesizes final answer
        turn2_block_text = MagicMock()
        turn2_block_text.type = "text"
        turn2_block_text.text = "Order SR-9876 created successfully."

        turn2_response = MagicMock()
        turn2_response.content = [turn2_block_text]
        turn2_response.stop_reason = "end_turn"
        turn2_response.usage = MagicMock(input_tokens=200, output_tokens=60)

        mock_anthropic.messages.create = AsyncMock(
            side_effect=[turn1_response, turn2_response]
        )

        # Build LLMClient with mocked internals
        client = MagicMock(spec=LLMClient)
        client._client = mock_anthropic
        client._router = MagicMock()
        client._router.route.return_value = MagicMock(
            model_id="claude-opus-4-6",
            max_tokens=4096,
            tier=MagicMock(value="opus"),
        )
        client._costs = MagicMock()
        client._costs.record = MagicMock()
        client._default_session_id = "test-session"
        client._resolve_intent = MagicMock(return_value=MagicMock())

        # Use the real call_with_tools method
        client.call_with_tools = LLMClient.call_with_tools.__get__(client)

        # Mock executor
        exec_result = MagicMock()
        exec_result.status = "success"
        exec_result.data = {"order_id": "SR-9876", "status": "NEW"}
        exec_result.pending_approval = None
        mock_executor = AsyncMock(return_value=exec_result)

        result = await client.call_with_tools(
            prompt="Create an order",
            tools=[{"name": "orders_create", "description": "...", "input_schema": {}}],
            tool_executor=mock_executor,
        )

        assert isinstance(result, ToolUseResult)
        assert "SR-9876" in result.final_text
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["name"] == "orders_create"
        assert result.pending_approval is None
        mock_executor.assert_awaited_once_with("orders_create", {"order_id": "SR-1", "order_date": "2026-04-04"})

    @pytest.mark.asyncio
    async def test_call_with_tools_surfaces_pending_approval(self):
        """When executor returns pending_approval, the loop stops and surfaces it."""
        from app.engine.llm_client import LLMClient, ToolUseResult

        mock_anthropic = AsyncMock()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_xyz"
        tool_block.name = "orders_create"
        tool_block.input = {"order_id": "SR-2"}

        turn1_response = MagicMock()
        turn1_response.content = [tool_block]
        turn1_response.stop_reason = "tool_use"
        turn1_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        turn2_text = MagicMock()
        turn2_text.type = "text"
        turn2_text.text = "Awaiting approval."

        turn2_response = MagicMock()
        turn2_response.content = [turn2_text]
        turn2_response.stop_reason = "end_turn"
        turn2_response.usage = MagicMock(input_tokens=100, output_tokens=30)

        mock_anthropic.messages.create = AsyncMock(
            side_effect=[turn1_response, turn2_response]
        )

        client = MagicMock(spec=LLMClient)
        client._client = mock_anthropic
        client._router = MagicMock()
        client._router.route.return_value = MagicMock(
            model_id="claude-opus-4-6", max_tokens=4096, tier=MagicMock(value="opus")
        )
        client._costs = MagicMock()
        client._costs.record = MagicMock()
        client._default_session_id = "test-session"
        client._resolve_intent = MagicMock(return_value=MagicMock())
        client.call_with_tools = LLMClient.call_with_tools.__get__(client)

        pending_data = {
            "status": "pending_approval",
            "tool_name": "orders_create",
            "message": "Requires operator approval.",
            "job_id": "job-abc",
        }
        exec_result = MagicMock()
        exec_result.status = "pending_approval"
        exec_result.data = pending_data
        exec_result.pending_approval = None  # ToolExecutorService sets this on result.data

        mock_executor = AsyncMock(return_value=exec_result)

        result = await client.call_with_tools(
            prompt="Create order",
            tools=[{"name": "orders_create", "description": "...", "input_schema": {}}],
            tool_executor=mock_executor,
        )

        # pending_approval should be surfaced on the result
        assert result.pending_approval is not None or "pending_approval" in (result.final_text or "")


# ---------------------------------------------------------------------------
# 6. ReActEngine — act-intent routes through tool_use loop
# ---------------------------------------------------------------------------

class TestReActToolUseRouting:
    @pytest.mark.asyncio
    async def test_act_intent_uses_tool_use_loop(self):
        """When intent=act and tool_executor present, _run_tool_use_loop is used."""
        from app.engine.react import ReActEngine, ReActResult

        # Mock classifier that returns act intent
        mock_classifier = MagicMock()
        mock_classification = MagicMock()
        mock_classification.intent = MagicMock(value="act")
        mock_classification.entity = MagicMock(value="orders")
        mock_classification.entity_id = None
        mock_classification.confidence = 0.9
        mock_classification.needs_ai = False
        mock_classifier.classify.return_value = mock_classification

        # Mock LLMClient with call_with_tools
        mock_llm = AsyncMock()
        mock_llm.call_with_tools = AsyncMock()

        from app.engine.llm_client import ToolUseResult
        mock_llm.call_with_tools.return_value = ToolUseResult(
            final_text="Order SR-9876 created.",
            tool_calls_made=[{"name": "orders_create", "input": {}}],
            pending_approval=None,
            turns=2,
        )

        # Mock tool executor
        mock_tool_executor = AsyncMock()
        mock_tool_executor.get_tools_for_context = AsyncMock(return_value=[
            {"name": "orders_create", "description": "...", "input_schema": {}}
        ])

        engine = ReActEngine(
            classifier=mock_classifier,
            tool_registry=MagicMock(),
            llm_client=mock_llm,
            guardrails=MagicMock(),
            tool_executor=mock_tool_executor,
        )

        result = await engine.process(
            "Create an order for me",
            session_context={"user_role": "operator", "seller_token": "tok"},
        )

        assert isinstance(result, ReActResult)
        assert "SR-9876" in result.response
        assert "orders_create" in result.tools_used
        mock_llm.call_with_tools.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_act_intent_skips_tool_use_loop(self):
        """When intent=lookup, standard rule-based loop is used."""
        from app.engine.react import ReActEngine

        mock_classifier = MagicMock()
        mock_classification = MagicMock()
        mock_classification.intent = MagicMock(value="lookup")
        mock_classification.entity = MagicMock(value="orders")
        mock_classification.entity_id = "99"
        mock_classification.confidence = 0.85
        mock_classification.needs_ai = False
        mock_classifier.classify.return_value = mock_classification

        mock_llm = AsyncMock()
        mock_llm.call_with_tools = AsyncMock()
        mock_llm.complete = AsyncMock(return_value='{"sufficient": true, "confidence": 0.9, "note": "ok"}')

        mock_registry = MagicMock()
        mock_registry.list_tools = MagicMock(return_value=[])
        mock_registry.get = MagicMock(return_value=None)

        mock_tool_executor = AsyncMock()

        engine = ReActEngine(
            classifier=mock_classifier,
            tool_registry=mock_registry,
            llm_client=mock_llm,
            guardrails=MagicMock(),
            tool_executor=mock_tool_executor,
        )

        await engine.process("Get order 99", session_context={})

        # call_with_tools should NOT have been called for a lookup intent
        mock_llm.call_with_tools.assert_not_awaited()

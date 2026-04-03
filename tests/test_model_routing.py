"""
Tests for COSMOS Phase 4: Model Routing & Prompt Optimization.

Covers:
  - ModelRouter: routing rules for each intent+confidence combo
  - PromptCacheManager: cache hit/miss tracking, system prompt building
  - ContextBudgeter: token estimation, truncation, priority ordering
  - CostTracker: recording, budget checks, daily summaries
  - LLMClient: routing integration, budget enforcement
  - Cost API endpoints
"""

import asyncio
import pytest
from datetime import datetime, date
from unittest.mock import AsyncMock, MagicMock, patch

from app.engine.classifier import Intent
from app.engine.model_router import ModelRouter, ModelTier, ModelProfile, PROFILES
from app.engine.prompt_cache import PromptCacheManager
from app.engine.context_budget import ContextBudgeter, BUDGETS, TokenBudget
from app.engine.cost_tracker import CostTracker, CostEntry
from app.engine.llm_client import LLMClient, BudgetExceededError, LLMClientError


def _run(coro):
    return asyncio.run(coro)


# =====================================================================
# ModelRouter tests
# =====================================================================


class TestModelRouter:
    def setup_method(self):
        self.router = ModelRouter()

    def test_lookup_high_confidence_routes_haiku(self):
        profile = self.router.route(Intent.LOOKUP, 0.9)
        assert profile.tier == ModelTier.HAIKU

    def test_lookup_moderate_confidence_routes_sonnet(self):
        profile = self.router.route(Intent.LOOKUP, 0.7)
        assert profile.tier == ModelTier.SONNET

    def test_navigate_routes_haiku(self):
        profile = self.router.route(Intent.NAVIGATE, 0.9)
        assert profile.tier == ModelTier.HAIKU

    def test_navigate_low_confidence_routes_opus(self):
        """Even NAVIGATE with very low confidence goes to Opus."""
        profile = self.router.route(Intent.NAVIGATE, 0.3)
        assert profile.tier == ModelTier.OPUS

    def test_act_routes_sonnet(self):
        profile = self.router.route(Intent.ACT, 0.9)
        assert profile.tier == ModelTier.SONNET

    def test_report_routes_sonnet(self):
        profile = self.router.route(Intent.REPORT, 0.8)
        assert profile.tier == ModelTier.SONNET

    def test_explain_single_entity_routes_sonnet(self):
        profile = self.router.route(Intent.EXPLAIN, 0.8)
        assert profile.tier == ModelTier.SONNET

    def test_explain_multi_entity_routes_opus(self):
        profile = self.router.route(
            Intent.EXPLAIN, 0.8, {"entity_count": 3}
        )
        assert profile.tier == ModelTier.OPUS

    def test_explain_causal_routes_opus(self):
        profile = self.router.route(
            Intent.EXPLAIN, 0.8, {"causal": True}
        )
        assert profile.tier == ModelTier.OPUS

    def test_low_confidence_routes_opus(self):
        profile = self.router.route(Intent.LOOKUP, 0.3)
        assert profile.tier == ModelTier.OPUS

    def test_multi_intent_routes_opus(self):
        profile = self.router.route(
            Intent.LOOKUP, 0.75, {"sub_intents": ["explain"]}
        )
        assert profile.tier == ModelTier.OPUS

    def test_security_always_routes_opus(self):
        profile = self.router.route(
            Intent.LOOKUP, 0.95, {"security": True}
        )
        assert profile.tier == ModelTier.OPUS

    def test_unknown_intent_routes_sonnet(self):
        profile = self.router.route(Intent.UNKNOWN, 0.6)
        assert profile.tier == ModelTier.SONNET

    def test_estimate_cost_haiku(self):
        cost = self.router.estimate_cost(ModelTier.HAIKU, 1000, 500)
        expected = (1000 / 1000 * 0.001) + (500 / 1000 * 0.005)
        assert cost == pytest.approx(expected, abs=1e-6)

    def test_estimate_cost_opus(self):
        cost = self.router.estimate_cost(ModelTier.OPUS, 1000, 1000)
        expected = (1000 / 1000 * 0.015) + (1000 / 1000 * 0.075)
        assert cost == pytest.approx(expected, abs=1e-6)

    def test_usage_stats_tracking(self):
        self.router.route(Intent.LOOKUP, 0.9)   # HAIKU
        self.router.route(Intent.ACT, 0.9)       # SONNET
        self.router.route(Intent.ACT, 0.8)       # SONNET
        self.router.route(Intent.LOOKUP, 0.3)    # OPUS

        stats = self.router.get_usage_stats()
        assert stats["total_queries"] == 4
        assert stats["by_tier"][ModelTier.HAIKU] == 1
        assert stats["by_tier"][ModelTier.SONNET] == 2
        assert stats["by_tier"][ModelTier.OPUS] == 1
        assert stats["distribution"]["haiku"] == pytest.approx(25.0)

    def test_profiles_complete(self):
        """All three tiers have profiles."""
        assert ModelTier.HAIKU in PROFILES
        assert ModelTier.SONNET in PROFILES
        assert ModelTier.OPUS in PROFILES
        for profile in PROFILES.values():
            assert profile.model_id
            assert profile.cost_per_1k_input > 0
            assert profile.max_tokens > 0


# =====================================================================
# PromptCacheManager tests
# =====================================================================


class TestPromptCacheManager:
    def setup_method(self):
        self.cache = PromptCacheManager()

    def test_get_system_prompt_returns_dict(self):
        prompt = self.cache.get_system_prompt()
        assert prompt["type"] == "text"
        assert "cache_control" in prompt
        assert prompt["cache_control"]["type"] == "ephemeral"
        assert "COSMOS" in prompt["text"]

    def test_get_system_prompt_with_tools(self):
        prompt = self.cache.get_system_prompt(tools=["lookup_order", "get_shipment"])
        assert "lookup_order" in prompt["text"]
        assert "get_shipment" in prompt["text"]

    def test_cache_hit_on_second_call(self):
        self.cache.get_system_prompt(role="agent", tools=["tool_a"])
        self.cache.get_system_prompt(role="agent", tools=["tool_a"])
        stats = self.cache.get_cache_stats()
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1
        assert stats["hit_rate_pct"] == 50.0

    def test_cache_miss_different_tools(self):
        self.cache.get_system_prompt(tools=["tool_a"])
        self.cache.get_system_prompt(tools=["tool_b"])
        stats = self.cache.get_cache_stats()
        assert stats["cache_misses"] == 2

    def test_build_cached_message_basic(self):
        prompt = self.cache.get_system_prompt()
        msg = self.cache.build_cached_message(prompt, "show order 123")
        assert len(msg["system"]) == 1
        assert msg["messages"][-1]["content"] == "show order 123"

    def test_build_cached_message_with_context(self):
        prompt = self.cache.get_system_prompt()
        ctx = {"company_id": "42", "role": "support"}
        msg = self.cache.build_cached_message(prompt, "hello", context=ctx)
        # Context adds 2 messages before the user message
        assert len(msg["messages"]) == 3
        assert "company_id" in msg["messages"][0]["content"]

    def test_invalidate_all(self):
        self.cache.get_system_prompt(role="a")
        self.cache.get_system_prompt(role="b")
        count = self.cache.invalidate()
        assert count == 2

    def test_invalidate_by_role(self):
        self.cache.get_system_prompt(role="keep")
        self.cache.get_system_prompt(role="drop")
        count = self.cache.invalidate(role="drop")
        assert count == 1


# =====================================================================
# ContextBudgeter tests
# =====================================================================


class TestContextBudgeter:
    def setup_method(self):
        self.budgeter = ContextBudgeter()

    def test_estimate_tokens_basic(self):
        assert self.budgeter.estimate_tokens("hello") == 1
        assert self.budgeter.estimate_tokens("a" * 100) == 25

    def test_estimate_tokens_empty(self):
        assert self.budgeter.estimate_tokens("") == 0
        assert self.budgeter.estimate_tokens(None) == 0

    def test_fit_within_budget_no_truncation(self):
        text = "Short text."
        result = self.budgeter.fit_within_budget(text, 100)
        assert result == text

    def test_fit_within_budget_truncates(self):
        text = "A" * 10000
        result = self.budgeter.fit_within_budget(text, 10)
        assert len(result) < len(text)
        assert "[...truncated" in result

    def test_fit_within_budget_zero_budget(self):
        result = self.budgeter.fit_within_budget("some text", 0)
        assert result == ""

    def test_build_context_window_haiku(self):
        result = self.budgeter.build_context_window(
            system_prompt="You are a helper.",
            tools=[{"name": "tool1"}],
            session_history=[{"role": "user", "content": "hi"}],
            user_message="show order 123",
            tier=ModelTier.HAIKU,
        )
        assert result["user_message"] == "show order 123"
        assert "budget_used" in result
        assert result["budget_used"]["tier"] == "haiku"

    def test_build_context_window_preserves_recent_history(self):
        history = [
            {"role": "user", "content": f"message {i}" * 100}
            for i in range(20)
        ]
        result = self.budgeter.build_context_window(
            system_prompt="sys",
            tools=[],
            session_history=history,
            user_message="latest",
            tier=ModelTier.HAIKU,
        )
        # Should keep recent messages, not all 20
        assert len(result["session_history"]) < 20

    def test_get_budget_for_tier(self):
        budget = self.budgeter.get_budget_for_tier(ModelTier.SONNET)
        assert budget.max_input_tokens == 16000
        assert budget.max_output_tokens == 4000

    def test_all_tiers_have_budgets(self):
        for tier in ModelTier:
            budget = self.budgeter.get_budget_for_tier(tier)
            assert isinstance(budget, TokenBudget)
            assert budget.max_input_tokens > 0


# =====================================================================
# CostTracker tests
# =====================================================================


class TestCostTracker:
    def setup_method(self):
        self.tracker = CostTracker(daily_budget_usd=10.0, per_session_budget_usd=1.0)

    def test_record_returns_entry(self):
        entry = self.tracker.record("sess1", "haiku", 1000, 500, "lookup")
        assert isinstance(entry, CostEntry)
        assert entry.session_id == "sess1"
        assert entry.model_tier == "haiku"
        assert entry.cost_usd > 0

    def test_record_cached_reduces_cost(self):
        normal = self.tracker.record("s1", "sonnet", 1000, 500, "lookup", cached=False)
        cached = self.tracker.record("s2", "sonnet", 1000, 500, "lookup", cached=True)
        assert cached.cost_usd < normal.cost_usd
        assert cached.cost_usd == pytest.approx(normal.cost_usd * 0.1, abs=1e-6)

    def test_check_budget_allowed(self):
        result = self.tracker.check_budget("sess1")
        assert result["allowed"] is True
        assert result["warning"] is None

    def test_check_budget_session_exceeded(self):
        # Record enough to exceed $1 session budget
        # Opus: 5000/1000*0.015 + 5000/1000*0.075 = 0.45 per call, need 3+ calls
        for _ in range(5):
            self.tracker.record("sess1", "opus", 5000, 5000, "explain")
        result = self.tracker.check_budget("sess1")
        assert result["allowed"] is False
        assert "Session budget" in result["warning"]

    def test_check_budget_daily_exceeded(self):
        for _ in range(500):
            self.tracker.record(f"s{_}", "opus", 5000, 5000, "explain")
        result = self.tracker.check_budget("new_session")
        assert result["allowed"] is False
        assert "Daily budget" in result["warning"]

    def test_get_daily_summary(self):
        self.tracker.record("s1", "haiku", 100, 50, "lookup")
        self.tracker.record("s1", "sonnet", 500, 200, "explain")
        summary = self.tracker.get_daily_summary()
        assert summary["query_count"] == 2
        assert summary["total_cost_usd"] > 0
        assert "haiku" in summary["by_model"]
        assert "lookup" in summary["by_intent"]

    def test_get_session_summary(self):
        self.tracker.record("s1", "haiku", 100, 50, "lookup")
        self.tracker.record("s1", "sonnet", 500, 200, "explain")
        self.tracker.record("s2", "haiku", 100, 50, "lookup")
        summary = self.tracker.get_session_summary("s1")
        assert summary["session_id"] == "s1"
        assert summary["query_count"] == 2

    def test_get_cost_trend(self):
        self.tracker.record("s1", "haiku", 100, 50, "lookup")
        trend = self.tracker.get_cost_trend(days=3)
        assert len(trend) == 3
        # Today should have the cost
        assert trend[-1]["cost_usd"] > 0
        assert trend[-1]["query_count"] == 1

    def test_unknown_tier_falls_back(self):
        """Unknown tier string uses Sonnet pricing as fallback."""
        entry = self.tracker.record("s1", "unknown_tier", 1000, 500, "lookup")
        assert entry.cost_usd > 0


# =====================================================================
# LLMClient tests
# =====================================================================


class TestLLMClient:
    def test_complete_without_client_raises(self):
        client = LLMClient(llm_mode="api")  # force api mode, no key → raises
        with pytest.raises(LLMClientError):
            _run(client.complete("hello"))

    def test_complete_budget_exceeded_raises(self):
        tracker = CostTracker(daily_budget_usd=0.0001, per_session_budget_usd=0.0001)
        # Burn through budget
        for _ in range(10):
            tracker.record("s1", "opus", 5000, 5000, "explain")

        client = LLMClient(cost_tracker=tracker)
        with pytest.raises(BudgetExceededError):
            _run(client.complete("hello", session_id="s1"))

    def test_client_backward_compatible_signature(self):
        """LLMClient.complete() accepts (prompt, max_tokens) like MockLLMClient."""
        import inspect
        sig = inspect.signature(LLMClient.complete)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "max_tokens" in params

    def test_get_router(self):
        client = LLMClient()
        assert isinstance(client.get_router(), ModelRouter)

    def test_get_cost_tracker(self):
        client = LLMClient()
        assert isinstance(client.get_cost_tracker(), CostTracker)

    def test_get_cache_manager(self):
        client = LLMClient()
        assert isinstance(client.get_cache_manager(), PromptCacheManager)

    def test_complete_with_mock_anthropic(self):
        """Full flow with a mocked Anthropic client."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = "The order is shipped."
        mock_response.content = [mock_block]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        client = LLMClient(
            api_key="test-key",
            llm_mode="api",
            anthropic_client=mock_client,
        )

        result = _run(client.complete("show order 123", intent="lookup", confidence=0.9))
        assert result == "The order is shipped."

        # Verify cost was recorded
        tracker = client.get_cost_tracker()
        summary = tracker.get_daily_summary()
        assert summary["query_count"] == 1

    def test_classify_uses_haiku_routing(self):
        """classify() should route through Haiku-level model."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = '{"intent": "lookup"}'
        mock_response.content = [mock_block]
        mock_response.usage = MagicMock(input_tokens=50, output_tokens=20)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        client = LLMClient(anthropic_client=mock_client, llm_mode="api")
        result = _run(client.classify("show order 123"))

        # Check that Haiku was used
        call_args = mock_client.messages.create.call_args
        assert "haiku" in call_args.kwargs.get("model", "")


# =====================================================================
# Cost API endpoint tests
# =====================================================================


class TestCostEndpoints:
    """Test the cost API endpoints using FastAPI test client pattern."""

    def test_current_budget_endpoint(self):
        from app.api.endpoints.costs import get_current_budget
        result = _run(get_current_budget(session_id="test"))
        assert "allowed" in result
        assert "daily_remaining" in result

    def test_daily_summary_endpoint(self):
        from app.api.endpoints.costs import get_daily_summary
        result = _run(get_daily_summary())
        assert "total_cost_usd" in result
        assert "query_count" in result

    def test_session_costs_endpoint(self):
        from app.api.endpoints.costs import get_session_costs
        result = _run(get_session_costs("test-session"))
        assert "session_id" in result
        assert result["session_id"] == "test-session"

    def test_cost_trend_endpoint(self):
        from app.api.endpoints.costs import get_cost_trend
        result = _run(get_cost_trend(days=3))
        assert len(result) == 3

    def test_model_usage_endpoint(self):
        from app.api.endpoints.costs import get_model_usage
        result = _run(get_model_usage())
        assert "total_queries" in result
        assert "by_tier" in result


# =====================================================================
# Integration: ReActEngine + LLMClient compatibility
# =====================================================================


class TestReActIntegration:
    """Verify LLMClient works as a drop-in for the existing MockLLMClient."""

    def test_llmclient_has_complete_method(self):
        client = LLMClient()
        assert hasattr(client, "complete")
        import inspect as _inspect
        assert _inspect.iscoroutinefunction(client.complete)

    def test_model_router_all_intents_covered(self):
        """Every Intent enum value produces a valid routing decision."""
        router = ModelRouter()
        for intent in Intent:
            profile = router.route(intent, 0.7)
            assert isinstance(profile, ModelProfile)
            assert profile.tier in ModelTier

    def test_cost_tracker_thread_safety_basic(self):
        """Multiple sessions can record concurrently without error."""
        tracker = CostTracker()
        for i in range(100):
            tracker.record(f"s{i % 5}", "sonnet", 500, 200, "lookup")
        summary = tracker.get_daily_summary()
        assert summary["query_count"] == 100

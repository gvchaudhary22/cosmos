"""
Tests for the COSMOS ReAct reasoning engine.

Covers:
  - IntentClassifier (rule-based tier)
  - Confidence scoring
  - ReActEngine single-loop and escalation flows
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from cosmos.app.engine.classifier import (
    ClassifyResult,
    Entity,
    Intent,
    IntentClassifier,
)
from cosmos.app.engine.confidence import score_confidence
from cosmos.app.engine.react import ReActEngine, ReActPhase, ReActResult


# =====================================================================
# Classifier tests
# =====================================================================


class TestClassifier:
    def setup_method(self):
        self.clf = IntentClassifier()

    def test_classify_lookup(self):
        result = self.clf.classify("show order 12345")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.ORDER
        assert result.entity_id == "12345"
        assert result.confidence == 1.0
        assert result.needs_ai is False

    def test_classify_explain(self):
        result = self.clf.classify("why is order delayed")
        assert result.intent == Intent.EXPLAIN
        assert result.entity == Entity.ORDER
        assert result.confidence > 0.0

    def test_classify_act(self):
        result = self.clf.classify("cancel order 12345")
        assert result.intent == Intent.ACT
        assert result.entity == Entity.ORDER
        assert result.entity_id == "12345"
        assert result.confidence == 1.0

    def test_classify_report(self):
        result = self.clf.classify("how many orders today")
        assert result.intent == Intent.REPORT
        assert result.entity == Entity.ORDER
        assert result.confidence > 0.0

    def test_classify_navigate(self):
        result = self.clf.classify("take me to returns page")
        assert result.intent == Intent.NAVIGATE
        assert result.entity == Entity.RETURN
        assert result.confidence == 1.0

    def test_classify_multi_intent(self):
        """A query that matches multiple intents."""
        result = self.clf.classify("show me why order 99999 was cancelled and how many refunds")
        # Should detect multiple intents
        assert result.intent in (Intent.LOOKUP, Intent.EXPLAIN, Intent.REPORT)
        assert len(result.sub_intents) >= 1
        # Confidence is lower for multi-intent
        assert result.confidence <= 0.75
        assert result.entity == Entity.ORDER
        assert result.entity_id == "99999"

    def test_classify_unknown(self):
        result = self.clf.classify("hello")
        assert result.intent == Intent.UNKNOWN
        assert result.needs_ai is True

    def test_classify_empty(self):
        result = self.clf.classify("")
        assert result.intent == Intent.UNKNOWN
        assert result.confidence == 0.0
        assert result.needs_ai is True

    def test_classify_entity_shipment(self):
        result = self.clf.classify("track shipment AWB12345678")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.SHIPMENT

    def test_classify_entity_ndr(self):
        result = self.clf.classify("show me ndr cases")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.NDR

    def test_classify_entity_payment(self):
        result = self.clf.classify("refund payment for order 55555")
        assert result.intent == Intent.ACT
        assert result.entity == Entity.PAYMENT
        assert result.entity_id == "55555"

    def test_classify_entity_wallet(self):
        result = self.clf.classify("show wallet balance")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.WALLET


# =====================================================================
# Confidence scoring tests
# =====================================================================


class TestConfidenceScoring:
    def test_perfect_score(self):
        result = score_confidence(1.0, 1.0, 1.0, 1.0)
        assert result == pytest.approx(1.0)

    def test_zero_score(self):
        result = score_confidence(0.0, 0.0, 0.0, 0.0)
        assert result == pytest.approx(0.0)

    def test_weighted_formula(self):
        result = score_confidence(0.5, 0.5, 0.5, 0.5)
        assert result == pytest.approx(0.5)

    def test_tool_heavy(self):
        # Tool success rate has highest weight (0.4)
        high_tool = score_confidence(1.0, 0.0, 0.0, 0.0)
        high_completeness = score_confidence(0.0, 1.0, 0.0, 0.0)
        assert high_tool > high_completeness

    def test_clamped_to_range(self):
        # Inputs already 0-1, so result should be too
        assert 0.0 <= score_confidence(0.1, 0.2, 0.3, 0.4) <= 1.0

    def test_specific_values(self):
        # 0.4*0.8 + 0.3*0.6 + 0.2*0.9 + 0.1*1.0 = 0.32 + 0.18 + 0.18 + 0.1 = 0.78
        result = score_confidence(0.8, 0.6, 0.9, 1.0)
        assert result == pytest.approx(0.78)


# =====================================================================
# ReAct engine tests
# =====================================================================


class MockToolRegistry:
    """Mock tool registry for testing."""

    def __init__(self, tools: dict = None):
        self._tools = tools or {}

    def get(self, name: str):
        return self._tools.get(name)

    def list_tools(self):
        return list(self._tools.keys())


class MockLLMClient:
    """Mock LLM client."""

    def __init__(self, response: str = "Mock LLM response"):
        self._response = response

    async def complete(self, prompt: str, max_tokens: int = 500) -> str:
        return self._response


class MockGuardrails:
    """Mock guardrails (no-op)."""

    pass


def _run(coro):
    """Helper to run async tests."""
    return asyncio.run(coro)


class TestReActEngine:
    def _make_engine(self, tools=None, llm_response="Mock response"):
        classifier = IntentClassifier()
        registry = MockToolRegistry(tools or {})
        llm = MockLLMClient(llm_response)
        guardrails = MockGuardrails()
        return ReActEngine(classifier, registry, llm, guardrails)

    def test_react_single_loop(self):
        """Single loop: tool succeeds, confidence is high, engine responds."""

        async def mock_lookup_order(entity_id=None, entity=None):
            return {"order_id": entity_id, "status": "shipped", "eta": "2026-03-30"}

        engine = self._make_engine(
            tools={"lookup_order": mock_lookup_order},
            llm_response="Your order 12345 has been shipped and will arrive by March 30.",
        )

        result: ReActResult = _run(engine.process("show order 12345"))

        assert result.confidence >= 0.5
        assert result.escalated is False
        assert result.total_loops == 1
        assert "lookup_order" in result.tools_used
        assert len(result.steps) >= 4  # reason, act, observe, evaluate (+ reflect)

        # Verify phase order
        phases = [s.phase for s in result.steps]
        assert phases[0] == ReActPhase.REASON
        assert phases[1] == ReActPhase.ACT
        assert phases[2] == ReActPhase.OBSERVE
        assert phases[3] == ReActPhase.EVALUATE

    def test_react_escalation(self):
        """Tools fail, confidence stays low, engine escalates."""

        async def failing_tool(entity_id=None, entity=None):
            raise RuntimeError("Service unavailable")

        engine = self._make_engine(
            tools={"lookup_order": failing_tool},
        )

        result: ReActResult = _run(engine.process("show order 99999"))

        assert result.confidence < 0.3
        assert result.escalated is True
        assert result.escalation_reason is not None
        assert "escalat" in result.response.lower()

    def test_react_no_tools(self):
        """Query with unknown intent, no matching tools — falls back to LLM."""
        engine = self._make_engine(
            tools={},
            llm_response="Hello! How can I help you today?",
        )

        result: ReActResult = _run(engine.process("hello there"))

        assert result.escalated is False
        assert result.total_loops == 1
        assert len(result.tools_used) == 0

    def test_react_multiple_tools(self):
        """Multiple tools executed in parallel."""

        async def mock_lookup_order(entity_id=None, entity=None):
            return {"order_id": entity_id, "status": "delivered"}

        async def mock_search_order(entity_id=None, entity=None):
            return {"results": [{"order_id": entity_id}]}

        engine = self._make_engine(
            tools={
                "lookup_order": mock_lookup_order,
                "search_order": mock_search_order,
            },
            llm_response="Order found and delivered.",
        )

        result: ReActResult = _run(engine.process("show order 12345"))
        assert result.confidence >= 0.5
        assert "lookup_order" in result.tools_used

    def test_react_latency_tracked(self):
        """Total latency is tracked."""
        engine = self._make_engine(tools={}, llm_response="Hi")
        result = _run(engine.process("hello"))
        assert result.total_latency_ms > 0

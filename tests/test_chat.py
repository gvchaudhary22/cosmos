"""
Tests for the COSMOS chat API endpoints.

Verifies that POST /chat and POST /chat/stream use a real ReActEngine
(injected via app.state) rather than returning hardcoded stubs.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from fastapi import FastAPI

from cosmos.app.api.endpoints.chat import router as chat_router
from cosmos.app.engine.classifier import IntentClassifier
from cosmos.app.engine.react import ReActEngine, ReActPhase, ReActResult, ReActStep, ToolResult


# =====================================================================
# Helpers
# =====================================================================


class MockToolRegistry:
    """Minimal tool registry for testing."""

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
    pass


def _build_test_app(react_engine=None, tournament_engine=None):
    """Build a minimal FastAPI app with chat router and injected engines."""
    app = FastAPI()
    app.include_router(chat_router, prefix="/chat")
    app.state.react_engine = react_engine
    app.state.tournament_engine = tournament_engine
    return app


def _make_engine(tools=None, llm_response="Mock response"):
    """Create a ReActEngine with mocked dependencies."""
    classifier = IntentClassifier()
    registry = MockToolRegistry(tools or {})
    llm = MockLLMClient(llm_response)
    guardrails = MockGuardrails()
    return ReActEngine(classifier, registry, llm, guardrails)


# =====================================================================
# POST /chat tests
# =====================================================================


class TestChatEndpoint:
    """Tests for POST /chat — non-streaming."""

    def test_chat_returns_real_engine_response(self):
        """Engine processes query and returns a real response, not a stub."""

        async def mock_lookup_order(entity_id=None, entity=None):
            return {"order_id": entity_id, "status": "shipped", "eta": "2026-03-30"}

        engine = _make_engine(
            tools={"lookup_order": mock_lookup_order},
            llm_response="Your order 12345 has been shipped.",
        )
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        data = resp.json()

        # Must NOT be the old stub
        assert "stub" not in data["content"].lower()
        assert "not yet implemented" not in data["content"].lower()

        # Should have real engine output
        assert data["confidence"] is not None
        assert data["confidence"] >= 0.0
        assert data["escalated"] is not None
        assert isinstance(data["tools_used"], list)
        assert data["total_loops"] >= 1

    def test_chat_no_engine_returns_error_message(self):
        """When react_engine is None, return an informative message."""
        app = _build_test_app(react_engine=None)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert "not initialized" in data["content"].lower()
        assert data["confidence"] == 0.0

    def test_chat_escalation_on_tool_failure(self):
        """When tools fail, engine escalates and sets escalated=True."""

        async def failing_tool(entity_id=None, entity=None):
            raise RuntimeError("Service unavailable")

        engine = _make_engine(tools={"lookup_order": failing_tool})
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "show order 99999",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["escalated"] is True
        assert data["confidence"] < 0.3

    def test_chat_no_tools_uses_llm_fallback(self):
        """When no tools match, engine falls back to LLM direct answer."""
        engine = _make_engine(tools={}, llm_response="Hello! How can I help?")
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "hello there",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["escalated"] is False
        assert len(data["tools_used"]) == 0

    def test_chat_includes_tools_used(self):
        """Response includes the names of tools that were invoked."""

        async def mock_lookup(entity_id=None, entity=None):
            return {"status": "delivered"}

        engine = _make_engine(
            tools={"lookup_order": mock_lookup},
            llm_response="Delivered.",
        )
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        data = resp.json()
        assert "lookup_order" in data["tools_used"]

    def test_chat_session_id_preserved(self):
        """When a session_id is provided, it is echoed back."""
        engine = _make_engine(tools={}, llm_response="Hi")
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        sid = "12345678-1234-5678-1234-567812345678"
        resp = client.post("/chat", json={
            "message": "hello",
            "user_id": "test-user",
            "session_id": sid,
        })

        data = resp.json()
        assert data["session_id"] == sid

    def test_chat_session_id_generated_when_missing(self):
        """When no session_id is provided, one is generated."""
        engine = _make_engine(tools={}, llm_response="Hi")
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "hello",
            "user_id": "test-user",
        })

        data = resp.json()
        assert data["session_id"] is not None
        assert len(data["session_id"]) > 0

    def test_chat_validation_empty_message(self):
        """Empty message should fail validation."""
        engine = _make_engine(tools={}, llm_response="Hi")
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "",
            "user_id": "test-user",
        })

        assert resp.status_code == 422


# =====================================================================
# POST /chat/stream tests
# =====================================================================


class TestChatStreamEndpoint:
    """Tests for POST /chat/stream — SSE streaming."""

    def _parse_sse(self, raw_text: str) -> list:
        """Parse raw SSE text into a list of (event, data) tuples."""
        events = []
        for block in raw_text.strip().split("\n\n"):
            event_type = None
            data_str = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_str = line[6:]
            if event_type and data_str:
                events.append((event_type, json.loads(data_str)))
        return events

    def test_stream_emits_sse_events_with_engine(self):
        """Stream endpoint emits phase, thinking, chunk, done events."""

        async def mock_lookup(entity_id=None, entity=None):
            return {"order_id": entity_id, "status": "shipped"}

        engine = _make_engine(
            tools={"lookup_order": mock_lookup},
            llm_response="Shipped.",
        )
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat/stream", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        events = self._parse_sse(resp.text)
        event_types = [e[0] for e in events]

        assert "phase" in event_types
        assert "done" in event_types
        assert "chunk" in event_types

        # Done event should have real confidence
        done_events = [e[1] for e in events if e[0] == "done"]
        assert len(done_events) == 1
        done_data = done_events[0]
        assert "confidence" in done_data
        assert "tools_used" in done_data
        assert done_data["total_loops"] >= 1

    def test_stream_emits_tool_events(self):
        """Stream emits tool execution events when tools are called."""

        async def mock_lookup(entity_id=None, entity=None):
            return {"status": "delivered"}

        engine = _make_engine(
            tools={"lookup_order": mock_lookup},
            llm_response="Delivered.",
        )
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat/stream", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        events = self._parse_sse(resp.text)
        tool_events = [e for e in events if e[0] == "tool"]
        assert len(tool_events) > 0
        assert tool_events[0][1]["tool"] == "lookup_order"
        assert tool_events[0][1]["status"] == "success"

    def test_stream_no_engine_returns_stub(self):
        """When no engine, stream returns stub message."""
        app = _build_test_app(react_engine=None)
        client = TestClient(app)

        resp = client.post("/chat/stream", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        assert resp.status_code == 200
        events = self._parse_sse(resp.text)
        chunk_events = [e for e in events if e[0] == "chunk"]
        assert any("not initialized" in e[1]["text"].lower() for e in chunk_events)

        done_events = [e for e in events if e[0] == "done"]
        assert done_events[0][1]["confidence"] == 0.0

    def test_stream_error_handling(self):
        """If engine raises, an error SSE event is emitted."""
        engine = MagicMock()
        engine.process = AsyncMock(side_effect=RuntimeError("Engine exploded"))
        app = _build_test_app(react_engine=engine)
        client = TestClient(app)

        resp = client.post("/chat/stream", json={
            "message": "show order 12345",
            "user_id": "test-user",
        })

        events = self._parse_sse(resp.text)
        error_events = [e for e in events if e[0] == "error"]
        assert len(error_events) > 0
        assert "exploded" in error_events[0][1]["message"].lower()


# =====================================================================
# Tournament mode tests
# =====================================================================


class TestTournamentMode:
    """Tests for tournament_mode=true on POST /chat."""

    def test_tournament_mode_uses_tournament_engine(self):
        """When tournament_mode=True, the TournamentEngine is invoked."""
        from cosmos.app.brain.tournament import (
            StrategyName,
            StrategyResult,
            TournamentEngine,
            TournamentMode,
            TournamentResult,
        )

        # Create a mock tournament engine
        mock_tournament = MagicMock(spec=TournamentEngine)
        winner = StrategyResult(
            strategy=StrategyName.DECISION_TREE,
            answer="Order 12345 is shipped.",
            confidence=0.95,
        )
        mock_tournament.run = AsyncMock(return_value=TournamentResult(
            query="show order 12345",
            intent="lookup",
            entity="order",
            winner=winner,
            all_results=[winner],
            mode=TournamentMode.TOURNAMENT,
            total_latency_ms=50.0,
            total_cost_usd=0.0,
        ))

        react_engine = _make_engine(tools={}, llm_response="Hi")
        app = _build_test_app(react_engine=react_engine, tournament_engine=mock_tournament)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "show order 12345",
            "user_id": "test-user",
            "tournament_mode": True,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Order 12345 is shipped."
        assert data["confidence"] == 0.95
        assert "decision_tree" in data["tools_used"]
        mock_tournament.run.assert_called_once()

    def test_tournament_mode_falls_back_to_react_if_no_tournament(self):
        """When tournament_mode=True but no TournamentEngine, use ReActEngine."""
        engine = _make_engine(tools={}, llm_response="Hello!")
        app = _build_test_app(react_engine=engine, tournament_engine=None)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "hello",
            "user_id": "test-user",
            "tournament_mode": True,
        })

        assert resp.status_code == 200
        data = resp.json()
        # Should get a real response from the react engine, not an error
        assert "not initialized" not in data["content"].lower()

    def test_tournament_mode_no_winner(self):
        """When tournament produces no winner, return informative message."""
        from cosmos.app.brain.tournament import (
            TournamentEngine,
            TournamentMode,
            TournamentResult,
        )

        mock_tournament = MagicMock(spec=TournamentEngine)
        mock_tournament.run = AsyncMock(return_value=TournamentResult(
            query="gibberish",
            intent="unknown",
            entity="unknown",
            winner=None,
            all_results=[],
            mode=TournamentMode.TOURNAMENT,
        ))

        react_engine = _make_engine(tools={}, llm_response="Hi")
        app = _build_test_app(react_engine=react_engine, tournament_engine=mock_tournament)
        client = TestClient(app)

        resp = client.post("/chat", json={
            "message": "gibberish",
            "user_id": "test-user",
            "tournament_mode": True,
        })

        data = resp.json()
        assert data["confidence"] == 0.0
        assert "no winning strategy" in data["content"].lower()

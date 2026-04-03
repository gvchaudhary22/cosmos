"""
Tests for the COSMOS bridge API endpoints.

Covers:
  POST /process  — Full MARS->COSMOS flow
  GET  /stats    — Bridge statistics
  GET  /health   — MARS connectivity status
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints.bridge import router as bridge_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockReActResult:
    def __init__(
        self,
        response="Mock bridge response",
        confidence=0.85,
        tools_used=None,
        escalated=False,
    ):
        self.response = response
        self.confidence = confidence
        self.tools_used = tools_used or []
        self.escalated = escalated


class MockReActEngine:
    def __init__(self, result: MockReActResult = None):
        self._result = result or MockReActResult()

    async def process(self, message: str, context: dict) -> MockReActResult:
        return self._result


def _build_app(engine=None):
    app = FastAPI()
    app.include_router(bridge_router, prefix="/bridge")
    app.state.react_engine = engine
    return app


# ---------------------------------------------------------------------------
# POST /bridge/process
# ---------------------------------------------------------------------------


class TestBridgeProcess:
    def test_returns_200_with_valid_request(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={
                "message": "What is the status of order ORD-123?",
                "company_id": "shiprocket",
                "user_id": "user-001",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "Mock bridge response"
        assert body["confidence"] == 0.85
        assert body["escalated"] is False

    def test_auto_generates_session_id_when_not_provided(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "hello", "company_id": "c1", "user_id": "u1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "session_id" in body
        assert len(body["session_id"]) > 0

    def test_preserves_provided_session_id(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)
        sid = str(uuid4())

        resp = client.post(
            "/bridge/process",
            json={"session_id": sid, "message": "track my shipment", "company_id": "c1", "user_id": "u1"},
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == sid

    def test_returns_escalated_when_engine_not_initialized(self):
        app = _build_app(engine=None)
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "anything", "company_id": "c1", "user_id": "u1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["escalated"] is True
        assert body["confidence"] == 0.0
        assert "not initialized" in body["content"].lower()

    def test_includes_tools_used_from_engine(self):
        result = MockReActResult(tools_used=["order_lookup", "tracking_api"])
        app = _build_app(MockReActEngine(result))
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "track order", "company_id": "c1", "user_id": "u1"},
        )
        body = resp.json()
        assert "order_lookup" in body["tools_used"]
        assert "tracking_api" in body["tools_used"]

    def test_escalated_response_has_escalation_reason(self):
        result = MockReActResult(escalated=True, confidence=0.3)
        app = _build_app(MockReActEngine(result))
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "complicated query", "company_id": "c1", "user_id": "u1"},
        )
        body = resp.json()
        assert body["escalated"] is True
        assert body["escalation_reason"] == "Low confidence"

    def test_non_escalated_response_has_no_escalation_reason(self):
        result = MockReActResult(escalated=False, confidence=0.9)
        app = _build_app(MockReActEngine(result))
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "simple question", "company_id": "c1", "user_id": "u1"},
        )
        body = resp.json()
        assert body["escalated"] is False
        assert body["escalation_reason"] is None

    def test_passes_intent_hint_and_entity_hint_to_engine(self):
        captured_context: dict = {}

        class CapturingEngine:
            async def process(self, message: str, context: dict) -> MockReActResult:
                captured_context.update(context)
                return MockReActResult()

        app = _build_app(CapturingEngine())
        client = TestClient(app)

        client.post(
            "/bridge/process",
            json={
                "message": "query",
                "company_id": "c1",
                "user_id": "u1",
                "intent_hint": "order_status",
                "entity_hint": "ORD-999",
            },
        )
        assert captured_context.get("intent_hint") == "order_status"
        assert captured_context.get("entity_hint") == "ORD-999"

    def test_passes_metadata_to_engine(self):
        captured_context: dict = {}

        class CapturingEngine:
            async def process(self, message: str, context: dict) -> MockReActResult:
                captured_context.update(context)
                return MockReActResult()

        app = _build_app(CapturingEngine())
        client = TestClient(app)

        client.post(
            "/bridge/process",
            json={
                "message": "query",
                "company_id": "c1",
                "user_id": "u1",
                "metadata": {"source": "whatsapp", "priority": "high"},
            },
        )
        assert captured_context.get("source") == "whatsapp"
        assert captured_context.get("priority") == "high"

    def test_engine_exception_returns_escalated_response(self):
        class ErrorEngine:
            async def process(self, message: str, context: dict) -> MockReActResult:
                raise RuntimeError("Engine crashed unexpectedly")

        app = _build_app(ErrorEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "cause error", "company_id": "c1", "user_id": "u1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["escalated"] is True
        assert "Engine crashed" in body["escalation_reason"]

    def test_response_has_latency_ms_field(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "quick query", "company_id": "c1", "user_id": "u1"},
        )
        body = resp.json()
        assert "total_latency_ms" in body
        assert body["total_latency_ms"] >= 0

    def test_defaults_channel_to_mars(self):
        captured_context: dict = {}

        class CapturingEngine:
            async def process(self, message: str, context: dict) -> MockReActResult:
                captured_context.update(context)
                return MockReActResult()

        app = _build_app(CapturingEngine())
        client = TestClient(app)
        client.post(
            "/bridge/process",
            json={"message": "hi", "company_id": "c1", "user_id": "u1"},
        )
        assert captured_context.get("channel") == "mars"

    def test_message_too_short_returns_422(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "", "company_id": "c1", "user_id": "u1"},
        )
        assert resp.status_code == 422

    def test_missing_company_id_returns_422(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        resp = client.post(
            "/bridge/process",
            json={"message": "hello", "user_id": "u1"},
        )
        assert resp.status_code == 422

    def test_message_id_is_unique_per_request(self):
        app = _build_app(MockReActEngine())
        client = TestClient(app)

        payload = {"message": "same message", "company_id": "c1", "user_id": "u1"}
        r1 = client.post("/bridge/process", json=payload).json()
        r2 = client.post("/bridge/process", json=payload).json()
        assert r1["message_id"] != r2["message_id"]


# ---------------------------------------------------------------------------
# GET /bridge/stats
# ---------------------------------------------------------------------------


class TestBridgeStats:
    def test_returns_200_even_when_mars_unavailable(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient") as MockClient, \
             patch("app.middleware.mars_bridge.MarsBridge") as MockBridge:
            MockBridge.return_value.get_stats.return_value = {
                "mars_handled": 10,
                "cosmos_handled": 5,
                "mars_unavailable": 2,
            }
            MockClient.return_value.close = AsyncMock()

            resp = client.get("/bridge/stats")
            assert resp.status_code == 200

    def test_returns_error_status_when_exception(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient", side_effect=Exception("import error")):
            resp = client.get("/bridge/stats")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "error"
            assert "stats" in body
            assert body["stats"]["mars_handled"] == 0

    def test_stats_keys_present_on_success(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient") as MockClient, \
             patch("app.middleware.mars_bridge.MarsBridge") as MockBridge:
            MockBridge.return_value.get_stats.return_value = {
                "mars_handled": 100,
                "cosmos_handled": 50,
                "mars_unavailable": 5,
            }
            MockClient.return_value.close = AsyncMock()

            resp = client.get("/bridge/stats")
            body = resp.json()
            assert "stats" in body


# ---------------------------------------------------------------------------
# GET /bridge/health
# ---------------------------------------------------------------------------


class TestBridgeHealth:
    def test_connected_when_mars_healthy(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient") as MockClient:
            instance = MockClient.return_value
            instance.health_check = AsyncMock(return_value=True)
            instance.close = AsyncMock()

            resp = client.get("/bridge/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "connected"

    def test_disconnected_when_mars_unhealthy(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient") as MockClient:
            instance = MockClient.return_value
            instance.health_check = AsyncMock(return_value=False)
            instance.close = AsyncMock()

            resp = client.get("/bridge/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "disconnected"

    def test_error_status_when_exception(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient", side_effect=Exception("connection refused")):
            resp = client.get("/bridge/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "error"
            assert "error" in body

    def test_returns_mars_url_in_response(self):
        app = _build_app()
        client = TestClient(app)

        with patch("app.clients.mars.MarsClient") as MockClient:
            instance = MockClient.return_value
            instance.health_check = AsyncMock(return_value=True)
            instance.close = AsyncMock()

            resp = client.get("/bridge/health")
            body = resp.json()
            assert "mars_url" in body
            assert "bridge_enabled" in body

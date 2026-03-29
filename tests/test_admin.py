"""
Tests for COSMOS admin API endpoints.

Covers:
  GET  /analytics                    — Dashboard analytics
  GET  /analytics/intents/{intent}   — Per-intent deep dive
  GET  /analytics/costs              — Cost breakdown
  GET  /analytics/traffic            — Hourly traffic
  GET  /audit-log                    — Audit log with filters
  GET  /distillation/stats           — Distillation collection stats
  POST /distillation/export          — Export training data
"""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cosmos.app.api.endpoints.admin import router as admin_router


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

DASHBOARD_STUB = {
    "period_days": 7,
    "total_queries": 1500,
    "queries_per_day": [200, 210, 215, 195, 230, 225, 225],
    "avg_confidence": 0.82,
    "confidence_distribution": {"high": 70, "medium": 20, "low": 10},
    "intent_breakdown": {"order_status": 40, "tracking": 35, "support": 25},
    "entity_breakdown": {},
    "avg_latency_ms": 320.5,
    "p95_latency_ms": 890.0,
    "escalation_rate": 0.08,
    "total_cost_usd": 12.45,
    "avg_cost_per_query": 0.0083,
    "model_usage_breakdown": {"claude-sonnet-4-6": 80, "haiku": 20},
}

INTENT_STUB = {
    "intent": "order_status",
    "period_days": 7,
    "total_queries": 600,
    "avg_confidence": 0.88,
    "avg_latency_ms": 280.0,
    "escalation_count": 30,
    "escalation_rate": 0.05,
    "total_cost_usd": 4.98,
}

COST_STUB = {
    "period_days": 30,
    "total_cost_usd": 55.20,
    "total_queries": 6500,
    "avg_cost_per_query": 0.0085,
    "by_model": {"claude-sonnet-4-6": 42.00, "haiku": 13.20},
    "by_intent": {"order_status": 21.00, "tracking": 18.00},
    "by_day": [],
}

DISTILLATION_STUB = {
    "total_records": 2450,
    "avg_confidence": 0.87,
    "feedback_distribution": {"5": 800, "4": 1200, "3": 450},
    "total_cost_usd": 0.95,
    "exportable_records": 1500,
}


def _app_with_mocks(analytics_engine=None, collector=None):
    """Build a FastAPI app with admin router and mocked dependencies."""
    from cosmos.app.db.session import get_db
    from cosmos.app.learning.analytics import AnalyticsEngine
    from cosmos.app.learning.collector import DistillationCollector

    db = AsyncMock()
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db

    if analytics_engine is not None:
        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=analytics_engine):
            pass

    return app, db


# ---------------------------------------------------------------------------
# GET /admin/analytics
# ---------------------------------------------------------------------------


class TestGetAnalytics:
    def test_returns_200_with_dashboard_structure(self):
        mock_engine = AsyncMock()
        mock_engine.get_dashboard = AsyncMock(return_value=DASHBOARD_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            client = TestClient(app)
            resp = client.get("/admin/analytics")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_queries"] == 1500
        assert body["period_days"] == 7
        assert "avg_confidence" in body
        assert "escalation_rate" in body

    def test_accepts_custom_days_param(self):
        mock_engine = AsyncMock()
        mock_engine.get_dashboard = AsyncMock(return_value={**DASHBOARD_STUB, "period_days": 30})

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            client = TestClient(app)
            resp = client.get("/admin/analytics?days=30")

        assert resp.status_code == 200
        assert resp.json()["period_days"] == 30

    def test_returns_model_usage_breakdown(self):
        mock_engine = AsyncMock()
        mock_engine.get_dashboard = AsyncMock(return_value=DASHBOARD_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            client = TestClient(app)
            resp = client.get("/admin/analytics")

        body = resp.json()
        assert "model_usage_breakdown" in body
        assert "intent_breakdown" in body


# ---------------------------------------------------------------------------
# GET /admin/analytics/intents/{intent}
# ---------------------------------------------------------------------------


class TestGetIntentAnalytics:
    def _make_app(self, stub=None):
        mock_engine = AsyncMock()
        mock_engine.get_intent_analytics = AsyncMock(return_value=stub or INTENT_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        return app, mock_engine

    def test_returns_intent_analytics(self):
        app, engine = self._make_app()
        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=engine):
            resp = TestClient(app).get("/admin/analytics/intents/order_status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "order_status"
        assert body["total_queries"] == 600

    def test_returns_escalation_stats(self):
        app, engine = self._make_app()
        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=engine):
            resp = TestClient(app).get("/admin/analytics/intents/tracking")

        body = resp.json()
        assert "escalation_rate" in body
        assert "escalation_count" in body

    def test_accepts_days_query_param(self):
        stub = {**INTENT_STUB, "period_days": 14}
        app, engine = self._make_app(stub)
        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=engine):
            resp = TestClient(app).get("/admin/analytics/intents/order_status?days=14")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /admin/analytics/costs
# ---------------------------------------------------------------------------


class TestGetCostReport:
    def test_returns_cost_report(self):
        mock_engine = AsyncMock()
        mock_engine.get_cost_report = AsyncMock(return_value=COST_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            resp = TestClient(app).get("/admin/analytics/costs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_cost_usd"] == 55.20
        assert body["period_days"] == 30
        assert "by_model" in body
        assert "by_intent" in body

    def test_accepts_custom_days(self):
        mock_engine = AsyncMock()
        mock_engine.get_cost_report = AsyncMock(return_value={**COST_STUB, "period_days": 7})

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            resp = TestClient(app).get("/admin/analytics/costs?days=7")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /admin/analytics/traffic
# ---------------------------------------------------------------------------


class TestGetHourlyTraffic:
    def test_returns_hourly_traffic_list(self):
        mock_engine = AsyncMock()
        mock_engine.get_hourly_traffic = AsyncMock(
            return_value=[{"hour": h, "count": h * 10} for h in range(24)]
        )

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            resp = TestClient(app).get("/admin/analytics/traffic")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 24
        assert body[0]["hour"] == 0

    def test_returns_empty_list_when_no_data(self):
        mock_engine = AsyncMock()
        mock_engine.get_hourly_traffic = AsyncMock(return_value=[])

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.AnalyticsEngine", return_value=mock_engine):
            resp = TestClient(app).get("/admin/analytics/traffic")

        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /admin/audit-log
# ---------------------------------------------------------------------------


class TestGetAuditLog:
    def _make_audit_row(self, action="query", user_id="u1"):
        row = MagicMock()
        row.id = "audit-001"
        row.action = action
        row.user_id = user_id
        row.resource_type = "session"
        row.resource_id = "sess-123"
        row.details = {"ip": "127.0.0.1"}
        row.created_at = datetime.now(timezone.utc)
        return row

    def _make_db_with_audit(self, rows, total=None):
        db = AsyncMock()
        call_count = 0

        async def execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.scalars.return_value.all.return_value = rows
            result.scalar.return_value = total if total is not None else len(rows)
            return result

        db.execute = execute
        return db

    def test_returns_audit_entries(self):
        from cosmos.app.db.session import get_db

        rows = [self._make_audit_row("query"), self._make_audit_row("export")]
        db = self._make_db_with_audit(rows, total=2)

        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        resp = TestClient(app).get("/admin/audit-log")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["entries"]) == 2

    def test_entries_have_required_fields(self):
        from cosmos.app.db.session import get_db

        rows = [self._make_audit_row("login", "user-xyz")]
        db = self._make_db_with_audit(rows)

        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        resp = TestClient(app).get("/admin/audit-log")
        entry = resp.json()["entries"][0]
        assert entry["action"] == "login"
        assert entry["user_id"] == "user-xyz"
        assert "created_at" in entry
        assert "details" in entry

    def test_returns_empty_entries_when_none(self):
        from cosmos.app.db.session import get_db

        db = self._make_db_with_audit([], total=0)
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        resp = TestClient(app).get("/admin/audit-log")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_accepts_action_filter_param(self):
        from cosmos.app.db.session import get_db

        db = self._make_db_with_audit([], total=0)
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        resp = TestClient(app).get("/admin/audit-log?action=export")
        assert resp.status_code == 200

    def test_accepts_user_id_filter_param(self):
        from cosmos.app.db.session import get_db

        db = self._make_db_with_audit([], total=0)
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        resp = TestClient(app).get("/admin/audit-log?user_id=specific-user")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /admin/distillation/stats
# ---------------------------------------------------------------------------


class TestGetDistillationStats:
    def test_returns_distillation_stats(self):
        mock_collector = AsyncMock()
        mock_collector.get_stats = AsyncMock(return_value=DISTILLATION_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.DistillationCollector", return_value=mock_collector):
            resp = TestClient(app).get("/admin/distillation/stats")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_records"] == 2450
        assert body["exportable_records"] == 1500
        assert "avg_confidence" in body

    def test_returns_feedback_distribution(self):
        mock_collector = AsyncMock()
        mock_collector.get_stats = AsyncMock(return_value=DISTILLATION_STUB)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.DistillationCollector", return_value=mock_collector):
            resp = TestClient(app).get("/admin/distillation/stats")

        body = resp.json()
        assert "feedback_distribution" in body


# ---------------------------------------------------------------------------
# POST /admin/distillation/export
# ---------------------------------------------------------------------------


class TestExportTrainingData:
    def test_exports_jsonl_data(self):
        export_data = "\n".join([
            json.dumps({"input": "track order", "output": "Your order is on the way.", "score": 5}),
            json.dumps({"input": "cancel order", "output": "Order cancelled successfully.", "score": 4}),
        ])

        mock_collector = AsyncMock()
        mock_collector.export_training_data = AsyncMock(return_value=export_data)

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.DistillationCollector", return_value=mock_collector):
            resp = TestClient(app).post(
                "/admin/distillation/export",
                json={"min_confidence": 0.8, "min_feedback": 4, "format": "jsonl"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["record_count"] == 2
        assert "track order" in body["data"]

    def test_returns_zero_count_when_no_data(self):
        mock_collector = AsyncMock()
        mock_collector.export_training_data = AsyncMock(return_value="")

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.DistillationCollector", return_value=mock_collector):
            resp = TestClient(app).post(
                "/admin/distillation/export",
                json={"min_confidence": 0.95, "min_feedback": 5, "format": "jsonl"},
            )

        assert resp.status_code == 200
        assert resp.json()["record_count"] == 0

    def test_uses_default_values_when_not_specified(self):
        captured: dict = {}
        mock_collector = AsyncMock()

        async def export(**kwargs):
            captured.update(kwargs)
            return ""

        mock_collector.export_training_data = export

        from cosmos.app.db.session import get_db

        db = AsyncMock()
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: db

        with patch("cosmos.app.api.endpoints.admin.DistillationCollector", return_value=mock_collector):
            TestClient(app).post("/admin/distillation/export", json={})

        # defaults: min_confidence=0.7, min_feedback=4, format="jsonl"
        assert captured.get("min_confidence") == 0.7
        assert captured.get("min_feedback") == 4
        assert captured.get("format") == "jsonl"

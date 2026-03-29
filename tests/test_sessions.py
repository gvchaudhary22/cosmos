"""
Tests for COSMOS session management endpoints.

Covers:
  GET    /sessions              — List with optional user_id filter
  GET    /sessions/{id}         — Get specific session
  DELETE /sessions/{id}         — Soft-close session
  GET    /sessions/{id}/messages — Get session messages
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cosmos.app.api.endpoints.sessions import router as sessions_router


# ---------------------------------------------------------------------------
# Helpers — lightweight ORM row fakes
# ---------------------------------------------------------------------------


def _session_row(
    id=None,
    user_id="user-001",
    channel="whatsapp",
    status="active",
    metadata_=None,
    created_at=None,
    updated_at=None,
):
    row = MagicMock()
    row.id = id or uuid4()
    row.user_id = user_id
    row.channel = channel
    row.status = status
    row.metadata_ = metadata_ or {}
    row.created_at = created_at or datetime.now(timezone.utc)
    row.updated_at = updated_at or datetime.now(timezone.utc)
    return row


def _message_row(session_id, role="user", content="Hello"):
    row = MagicMock()
    row.id = uuid4()
    row.session_id = session_id
    row.role = role
    row.content = content
    row.token_count = 12
    row.model = "claude-sonnet-4-6"
    row.created_at = datetime.now(timezone.utc)
    return row


def _make_db(sessions=None, messages=None, session_count=None):
    """Build a mock AsyncSession that returns given rows."""
    db = AsyncMock()
    sessions = sessions or []
    messages = messages or []

    async def execute(stmt):
        result = MagicMock()
        # scalar() for COUNT queries
        result.scalar.return_value = session_count if session_count is not None else len(sessions)
        result.scalar_one_or_none.return_value = sessions[0] if sessions else None
        result.scalars.return_value.all.return_value = sessions
        return result

    db.execute = execute
    db.commit = AsyncMock()
    return db


def _build_app(db=None):
    app = FastAPI()
    app.include_router(sessions_router, prefix="/sessions")

    if db is not None:
        from app.db import session as db_session_module
        app.dependency_overrides[db_session_module.get_db] = lambda: db

    return app


def _app_with_db(db):
    """Build app with DB dependency override."""
    from cosmos.app.db.session import get_db

    app = FastAPI()
    app.include_router(sessions_router, prefix="/sessions")
    app.dependency_overrides[get_db] = lambda: db
    return app


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_returns_empty_list_when_no_sessions(self):
        db = _make_db(sessions=[], session_count=0)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get("/sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["sessions"] == []

    def test_returns_sessions_list(self):
        sessions = [_session_row(user_id="u1"), _session_row(user_id="u1")]
        db = _make_db(sessions=sessions, session_count=2)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get("/sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["sessions"]) == 2

    def test_session_response_has_required_fields(self):
        sid = uuid4()
        sessions = [_session_row(id=sid, user_id="user-99", channel="web", status="active")]
        db = _make_db(sessions=sessions, session_count=1)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get("/sessions")
        body = resp.json()
        sess = body["sessions"][0]
        assert "id" in sess
        assert "user_id" in sess
        assert sess["user_id"] == "user-99"
        assert sess["channel"] == "web"
        assert sess["status"] == "active"

    def test_accepts_user_id_filter_param(self):
        db = _make_db(sessions=[], session_count=0)
        app = _app_with_db(db)
        client = TestClient(app)

        # Should not error — query string is passed to DB filter
        resp = client.get("/sessions?user_id=specific-user")
        assert resp.status_code == 200

    def test_accepts_limit_and_offset_params(self):
        db = _make_db(sessions=[], session_count=0)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get("/sessions?limit=5&offset=10")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /sessions/{id}
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_returns_session_when_found(self):
        sid = uuid4()
        sessions = [_session_row(id=sid, user_id="u-found", channel="web")]
        db = _make_db(sessions=sessions)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "u-found"
        assert body["channel"] == "web"

    def test_returns_404_when_not_found(self):
        db = _make_db(sessions=[])  # scalar_one_or_none returns None
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{uuid4()}")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_invalid_uuid_returns_422(self):
        db = _make_db(sessions=[])
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get("/sessions/not-a-uuid")
        assert resp.status_code == 422

    def test_returns_metadata_field(self):
        sid = uuid4()
        sessions = [_session_row(id=sid, metadata_={"source": "mobile", "lang": "en"})]
        db = _make_db(sessions=sessions)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}")
        body = resp.json()
        assert "metadata" in body


# ---------------------------------------------------------------------------
# DELETE /sessions/{id}
# ---------------------------------------------------------------------------


class TestDeleteSession:
    def test_soft_closes_session(self):
        sid = uuid4()
        sessions = [_session_row(id=sid, status="active")]
        db = _make_db(sessions=sessions)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.delete(f"/sessions/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "closed"
        assert body["session_id"] == str(sid)

    def test_returns_404_when_session_not_found(self):
        db = _make_db(sessions=[])
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.delete(f"/sessions/{uuid4()}")
        assert resp.status_code == 404

    def test_calls_db_commit_on_success(self):
        sid = uuid4()
        sessions = [_session_row(id=sid)]
        db = _make_db(sessions=sessions)
        app = _app_with_db(db)
        client = TestClient(app)

        client.delete(f"/sessions/{sid}")
        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /sessions/{id}/messages
# ---------------------------------------------------------------------------


class TestGetSessionMessages:
    def _db_with_session_and_messages(self, sid, messages):
        db = AsyncMock()
        call_count = 0

        async def execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()

            if call_count == 1:
                # First call: session existence check
                result.scalar_one_or_none.return_value = sid
            elif call_count == 2:
                # Second call: COUNT
                result.scalar.return_value = len(messages)
            else:
                # Third call: actual messages
                result.scalars.return_value.all.return_value = messages

            return result

        db.execute = execute
        return db

    def test_returns_messages_for_session(self):
        sid = uuid4()
        msgs = [
            _message_row(sid, role="user", content="Hello"),
            _message_row(sid, role="assistant", content="Hi there!"),
        ]
        db = self._db_with_session_and_messages(sid, msgs)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["messages"]) == 2

    def test_message_has_role_and_content(self):
        sid = uuid4()
        msgs = [_message_row(sid, role="user", content="Track my order")]
        db = self._db_with_session_and_messages(sid, msgs)
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}/messages")
        msg = resp.json()["messages"][0]
        assert msg["content"] == "Track my order"
        assert msg["role"] == "user"

    def test_returns_404_when_session_not_found(self):
        db = AsyncMock()

        async def execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        db.execute = execute
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{uuid4()}/messages")
        assert resp.status_code == 404

    def test_returns_empty_messages_for_session_with_no_messages(self):
        sid = uuid4()
        db = self._db_with_session_and_messages(sid, [])
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["messages"] == []

    def test_accepts_limit_and_offset(self):
        sid = uuid4()
        db = self._db_with_session_and_messages(sid, [])
        app = _app_with_db(db)
        client = TestClient(app)

        resp = client.get(f"/sessions/{sid}/messages?limit=10&offset=5")
        assert resp.status_code == 200

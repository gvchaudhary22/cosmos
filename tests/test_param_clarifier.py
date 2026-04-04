"""Unit tests for ParamClarificationEngine (Issue #18).

The engine is driven by vector search results (knowledge_chunks), NOT by
intent name string matching. This handles arbitrary user phrasing.
"""

import pytest
from pathlib import Path

from app.brain.param_clarifier import (
    ParamClarificationEngine,
    ClarificationRequest,
    _SoftRequired,
    _APIEntry,
)

API_ID = "mcapi.v1.admin.shipments.get"
SIMILARITY_HIGH = 0.85
SIMILARITY_LOW = 0.40


def _engine_with_index(entries: dict) -> ParamClarificationEngine:
    engine = ParamClarificationEngine(kb_root="/fake/kb")
    engine._index = entries
    return engine


def _shipments_entry() -> _APIEntry:
    return _APIEntry(
        api_entity_id=API_ID,
        soft_required=[
            _SoftRequired(
                param="client_id",
                alias="company_id",
                ask_if_missing="Which company's shipments? Provide company ID. Example: 25149",
                skip_if_present=["awb", "sr_order_id"],
            )
        ],
    )


def _chunk(entity_id: str, similarity: float = SIMILARITY_HIGH) -> dict:
    return {"entity_id": entity_id, "similarity": similarity, "content": "some content"}


# ---------------------------------------------------------------------------
# Core: missing client_id → clarification question returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clarifier_returns_question_when_company_missing():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="show me shipments for today",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.pending_param == "client_id"
    assert req.api_entity_id == API_ID
    assert "25149" in req.question or "company" in req.question.lower()


# ---------------------------------------------------------------------------
# company_id provided as execute() param → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_when_company_id_provided():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="show me shipments for today",
        company_id="25149",
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Company ID embedded in query text → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_when_company_in_query():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    for query in [
        "show shipments for company 25149",
        "seller 12345 shipments today",
        "company_id=99001 status=6",
    ]:
        req = await engine.check(
            knowledge_chunks=[_chunk(API_ID)],
            query=query,
            company_id=None,
            session_context={},
        )
        assert req is None, f"Expected no clarification for: {query!r}"


# ---------------------------------------------------------------------------
# AWB in query → skip_if_present → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_when_awb_present_in_query():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="search AWB SR123456789 in admin",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# sr_order_id in session_context → skip_if_present → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_when_sr_order_id_in_context():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="show me this order",
        company_id=None,
        session_context={"sr_order_id": "987654"},
    )
    assert req is None


# ---------------------------------------------------------------------------
# company_id in session_context → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_when_company_id_in_session_context():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="show me shipments for today",
        company_id=None,
        session_context={"company_id": "25149"},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Low similarity chunk → no clarification (weak match)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_for_low_similarity_chunk():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID, similarity=SIMILARITY_LOW)],
        query="show me shipments for today",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Chunk entity_id not in index → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_for_unregistered_api():
    engine = _engine_with_index({})  # empty index
    req = await engine.check(
        knowledge_chunks=[_chunk(API_ID)],
        query="show me shipments for today",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Empty knowledge_chunks → no clarification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_for_empty_chunks():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[],
        query="show me shipments for today",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Chunk without entity_id → skipped gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_clarification_for_chunk_without_entity_id():
    engine = _engine_with_index({API_ID: _shipments_entry()})
    req = await engine.check(
        knowledge_chunks=[{"similarity": 0.9, "content": "some content"}],
        query="show me shipments for today",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Multiple chunks — first high-similarity match with soft_required wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_matching_chunk_wins():
    other_api = "mcapi.v1.admin.orders.get"
    engine = _engine_with_index({API_ID: _shipments_entry()})
    # First chunk = orders (not in index), second chunk = shipments (in index)
    req = await engine.check(
        knowledge_chunks=[
            _chunk(other_api, similarity=0.92),   # not in index
            _chunk(API_ID, similarity=0.88),       # in index, missing company_id
        ],
        query="show me data for today",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.api_entity_id == API_ID


# ---------------------------------------------------------------------------
# Index build: nonexistent kb_root → empty index, no crash
# ---------------------------------------------------------------------------

def test_build_index_with_nonexistent_kb_root():
    engine = ParamClarificationEngine(kb_root="/nonexistent/path")
    index = engine._build_index()
    assert index == {}

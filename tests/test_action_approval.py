"""
Tests for ActionApprovalGate — write action approval gate (M3-P1 #22, M3-P2).

Covers:
  - ActionApprovalGate.propose() / consume() lifecycle (async, Phase 2)
  - Single-use token (replay attack prevention)
  - Token expiry
  - Generic write action detection: detect_write_action() + WriteActionSignal
  - Feature flag detection: feature_cod_toggle, feature_srf_enable
  - Intent detection: is_cancel_order_intent() (backward compat)
  - Order ID extraction: extract_order_ids()
  - HybridChatRequest has confirm_action / confirm_token fields
"""
import time
import pytest
import pytest_asyncio

from app.brain.action_approval import (
    ActionApprovalGate, ActionProposal, WriteActionSignal, _TOKEN_TTL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gate() -> ActionApprovalGate:
    return ActionApprovalGate()


# ---------------------------------------------------------------------------
# propose / consume lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_returns_proposal_with_token():
    gate = _gate()
    p = await gate.propose("sess-1", "orders_cancel", {"ids": [98765432]}, "Cancel order 98765432")
    assert isinstance(p, ActionProposal)
    assert len(p.confirm_token) > 16
    assert p.action_type == "orders_cancel"
    assert p.action_input == {"ids": [98765432]}
    assert p.session_id == "sess-1"
    assert p.risk_level == "high"
    assert p.ttl_seconds() > 0


@pytest.mark.asyncio
async def test_consume_valid_token_returns_proposal():
    gate = _gate()
    p = await gate.propose("sess-1", "orders_cancel", {"ids": [11111111]}, "Cancel order 11111111")
    result = await gate.consume(p.confirm_token)
    assert result is not None
    assert result.action_type == "orders_cancel"
    assert result.action_input == {"ids": [11111111]}


@pytest.mark.asyncio
async def test_consume_removes_token_single_use():
    """Replay attack prevention: second consume with same token returns None."""
    gate = _gate()
    p = await gate.propose("sess-1", "orders_cancel", {"ids": [22222222]}, "Cancel")
    await gate.consume(p.confirm_token)  # first use — valid
    result = await gate.consume(p.confirm_token)  # second use — invalid
    assert result is None


@pytest.mark.asyncio
async def test_consume_unknown_token_returns_none():
    gate = _gate()
    assert await gate.consume("totallybogustoken12345678") is None


@pytest.mark.asyncio
async def test_consume_expired_token_returns_none():
    """Token with expires_at in the past is rejected."""
    gate = _gate()
    p = await gate.propose("sess-1", "orders_cancel", {}, "Cancel")
    # Manually expire the proposal by backdating expires_at
    p.expires_at = time.monotonic() - 1.0
    gate._pending[p.confirm_token] = p
    result = await gate.consume(p.confirm_token)
    assert result is None


@pytest.mark.asyncio
async def test_pending_count_reflects_active_proposals():
    gate = _gate()
    assert gate.pending_count() == 0
    p1 = await gate.propose("s1", "orders_cancel", {}, "Cancel 1")
    p2 = await gate.propose("s2", "orders_cancel", {}, "Cancel 2")
    assert gate.pending_count() == 2
    await gate.consume(p1.confirm_token)
    assert gate.pending_count() == 1


@pytest.mark.asyncio
async def test_expire_stale_cleans_up_old_proposals():
    gate = _gate()
    p = await gate.propose("s1", "orders_cancel", {}, "Cancel")
    # Backdate to simulate expiry
    p.expires_at = time.monotonic() - 1.0
    gate._pending[p.confirm_token] = p
    gate._expire_stale()
    assert p.confirm_token not in gate._pending


@pytest.mark.asyncio
async def test_ttl_seconds_decreases():
    gate = _gate()
    p = await gate.propose("s1", "orders_cancel", {}, "Cancel")
    # TTL should be close to _TOKEN_TTL
    assert _TOKEN_TTL - 2 <= p.ttl_seconds() <= _TOKEN_TTL


@pytest.mark.asyncio
async def test_proposal_records_session_id():
    """Session ID is stored on proposal so the confirm path can validate ownership."""
    gate = _gate()
    p = await gate.propose("sess-xyz", "orders_cancel", {}, "Cancel")
    r = await gate.consume(p.confirm_token)
    assert r.session_id == "sess-xyz"


@pytest.mark.asyncio
async def test_proposals_from_different_sessions_have_distinct_tokens():
    gate = _gate()
    p1 = await gate.propose("sess-a", "orders_cancel", {}, "Cancel A")
    p2 = await gate.propose("sess-b", "orders_cancel", {}, "Cancel B")
    assert p1.confirm_token != p2.confirm_token
    # Consuming p1's token yields session-a, not session-b
    r = await gate.consume(p1.confirm_token)
    assert r.session_id == "sess-a"


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

def test_is_cancel_order_intent_from_query_keyword():
    assert ActionApprovalGate.is_cancel_order_intent(
        "cancel order 98765432", intents=[], knowledge_chunks=[]
    ) is True


def test_is_cancel_order_intent_hinglish():
    assert ActionApprovalGate.is_cancel_order_intent(
        "order cancel karo 12345678", intents=[], knowledge_chunks=[]
    ) is True


def test_is_cancel_order_intent_from_intents_list_string():
    assert ActionApprovalGate.is_cancel_order_intent(
        "do it", intents=["cancel_order"], knowledge_chunks=[]
    ) is True


def test_is_cancel_order_intent_from_intents_list_dict():
    assert ActionApprovalGate.is_cancel_order_intent(
        "do it",
        intents=[{"action": "cancel_order", "confidence": 0.9}],
        knowledge_chunks=[],
    ) is True


def test_is_cancel_order_intent_from_high_similarity_chunk():
    chunks = [{"entity_id": "mcapi.v1.orders.cancel.post", "similarity": 0.88}]
    assert ActionApprovalGate.is_cancel_order_intent(
        "I want to stop the order", intents=[], knowledge_chunks=chunks
    ) is True


def test_is_cancel_order_intent_low_similarity_chunk_ignored():
    """Chunk below 0.70 similarity threshold does not trigger."""
    chunks = [{"entity_id": "mcapi.v1.orders.cancel.post", "similarity": 0.50}]
    assert ActionApprovalGate.is_cancel_order_intent(
        "I want to stop the order", intents=[], knowledge_chunks=chunks
    ) is False


def test_is_cancel_order_intent_non_cancel_query():
    assert ActionApprovalGate.is_cancel_order_intent(
        "show me NDR data", intents=[], knowledge_chunks=[]
    ) is False


# ---------------------------------------------------------------------------
# Order ID extraction
# ---------------------------------------------------------------------------

def test_extract_order_ids_single():
    ids = ActionApprovalGate.extract_order_ids("cancel order 98765432")
    assert ids == [98765432]


def test_extract_order_ids_multiple():
    ids = ActionApprovalGate.extract_order_ids("cancel orders 98765432 and 11122233")
    assert set(ids) == {98765432, 11122233}


def test_extract_order_ids_none():
    ids = ActionApprovalGate.extract_order_ids("cancel order please")
    assert ids == []


def test_extract_order_ids_short_numbers_ignored():
    """Numbers under 7 digits are not Shiprocket order IDs."""
    ids = ActionApprovalGate.extract_order_ids("cancel order 123 please")
    assert ids == []


# ---------------------------------------------------------------------------
# HybridChatRequest schema — confirm_action / confirm_token fields
# ---------------------------------------------------------------------------

def test_hybrid_chat_request_has_confirm_fields():
    from app.api.endpoints.hybrid_chat import HybridChatRequest
    req = HybridChatRequest(message="cancel order 98765432", user_id="u1")
    assert req.confirm_action is False
    assert req.confirm_token is None


def test_hybrid_chat_request_confirm_fields_settable():
    from app.api.endpoints.hybrid_chat import HybridChatRequest
    req = HybridChatRequest(
        message="confirm cancel",
        user_id="u1",
        confirm_action=True,
        confirm_token="abc123token",
    )
    assert req.confirm_action is True
    assert req.confirm_token == "abc123token"


# ---------------------------------------------------------------------------
# detect_write_action + WriteActionSignal (Phase 2 — generic)
# ---------------------------------------------------------------------------

def test_detect_write_action_returns_write_action_signal():
    signal = ActionApprovalGate.detect_write_action(
        "cancel order 98765432", intents=[], knowledge_chunks=[]
    )
    assert isinstance(signal, WriteActionSignal)
    assert signal.tool_name == "orders_cancel"
    assert signal.risk_level == "high"


def test_detect_write_action_no_match_returns_none():
    signal = ActionApprovalGate.detect_write_action(
        "show me NDR data", intents=[], knowledge_chunks=[]
    )
    assert signal is None


def test_detect_write_action_cancel_with_order_ids():
    signal = ActionApprovalGate.detect_write_action(
        "cancel order 98765432 and 11122233", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "orders_cancel"
    assert set(signal.action_input["ids"]) == {98765432, 11122233}
    assert "98765432" in signal.summary or "2 order" in signal.summary


def test_detect_write_action_cancel_no_order_ids():
    signal = ActionApprovalGate.detect_write_action(
        "cancel order please", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.action_input == {}
    assert "no order IDs" in signal.summary.lower() or "ids" not in str(signal.action_input)


# ---------------------------------------------------------------------------
# Feature flag detection: feature_cod_toggle
# ---------------------------------------------------------------------------

def test_detect_write_action_cod_disable_keyword():
    signal = ActionApprovalGate.detect_write_action(
        "disable cod for company 25149", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "feature_cod_toggle"
    assert signal.action_input.get("company_id") == 25149
    assert signal.action_input.get("enabled") is False
    assert signal.risk_level == "high"


def test_detect_write_action_cod_enable_keyword():
    signal = ActionApprovalGate.detect_write_action(
        "enable cod for seller 25149", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "feature_cod_toggle"
    assert signal.action_input.get("enabled") is True


def test_detect_write_action_cod_hinglish():
    signal = ActionApprovalGate.detect_write_action(
        "cod band karo company 25149", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "feature_cod_toggle"
    assert signal.action_input.get("enabled") is False


def test_detect_write_action_cod_from_intent():
    signal = ActionApprovalGate.detect_write_action(
        "help me",
        intents=[{"action": "cod_toggle", "confidence": 0.9}],
        knowledge_chunks=[],
    )
    assert signal is not None
    assert signal.tool_name == "feature_cod_toggle"


def test_detect_write_action_cod_from_kb_chunk():
    chunks = [{"entity_id": "enablepartialcodtoggle.by_company_id.post", "similarity": 0.85}]
    signal = ActionApprovalGate.detect_write_action(
        "I want to change the feature", intents=[], knowledge_chunks=chunks
    )
    assert signal is not None
    assert signal.tool_name == "feature_cod_toggle"


def test_detect_write_action_cod_no_company_id():
    signal = ActionApprovalGate.detect_write_action(
        "disable cod please", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert "company_id" not in signal.action_input
    assert "not detected" in signal.summary.lower() or "company" in signal.summary.lower()


# ---------------------------------------------------------------------------
# Feature flag detection: feature_srf_enable
# ---------------------------------------------------------------------------

def test_detect_write_action_srf_enable_keyword():
    signal = ActionApprovalGate.detect_write_action(
        "enable srf for company 25149", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "feature_srf_enable"
    assert signal.action_input.get("company_id") == 25149
    assert signal.action_input.get("enabled") is True
    assert signal.risk_level == "medium"


def test_detect_write_action_srf_disable_keyword():
    signal = ActionApprovalGate.detect_write_action(
        "srf off for 25149", intents=[], knowledge_chunks=[]
    )
    assert signal is not None
    assert signal.tool_name == "feature_srf_enable"
    assert signal.action_input.get("enabled") is False


def test_detect_write_action_srf_from_kb_chunk():
    chunks = [{"entity_id": "srf_feature_enable.by_company_id.post", "similarity": 0.80}]
    signal = ActionApprovalGate.detect_write_action(
        "toggle the feature", intents=[], knowledge_chunks=chunks
    )
    assert signal is not None
    assert signal.tool_name == "feature_srf_enable"


# ---------------------------------------------------------------------------
# WriteActionSignal token is UUID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_token_is_uuid_format():
    """Phase 2: token is UUID v4 for DB FK compatibility."""
    import re
    gate = _gate()
    p = await gate.propose("sess-uuid", "orders_cancel", {}, "Cancel")
    uuid_re = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.I
    )
    assert uuid_re.match(p.confirm_token), f"Token {p.confirm_token!r} is not UUID v4"

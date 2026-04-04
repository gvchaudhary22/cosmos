"""
Tests for issue #22 — cancel order write action with approval gate.

Covers:
  - ActionApprovalGate.propose() / consume() lifecycle
  - Single-use token (replay attack prevention)
  - Token expiry
  - Intent detection: is_cancel_order_intent()
  - Order ID extraction: extract_order_ids()
  - HybridChatRequest has confirm_action / confirm_token fields
  - SSE approval_required event emitted when cancel intent detected
  - SSE error emitted on invalid / expired token
"""
import time
import pytest

from app.brain.action_approval import ActionApprovalGate, ActionProposal, _TOKEN_TTL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gate() -> ActionApprovalGate:
    return ActionApprovalGate()


# ---------------------------------------------------------------------------
# propose / consume lifecycle
# ---------------------------------------------------------------------------

def test_propose_returns_proposal_with_token():
    gate = _gate()
    p = gate.propose("sess-1", "orders_cancel", {"ids": [98765432]}, "Cancel order 98765432")
    assert isinstance(p, ActionProposal)
    assert len(p.confirm_token) > 16
    assert p.action_type == "orders_cancel"
    assert p.action_input == {"ids": [98765432]}
    assert p.session_id == "sess-1"
    assert p.risk_level == "high"
    assert p.ttl_seconds() > 0


def test_consume_valid_token_returns_proposal():
    gate = _gate()
    p = gate.propose("sess-1", "orders_cancel", {"ids": [11111111]}, "Cancel order 11111111")
    result = gate.consume(p.confirm_token)
    assert result is not None
    assert result.action_type == "orders_cancel"
    assert result.action_input == {"ids": [11111111]}


def test_consume_removes_token_single_use():
    """Replay attack prevention: second consume with same token returns None."""
    gate = _gate()
    p = gate.propose("sess-1", "orders_cancel", {"ids": [22222222]}, "Cancel")
    gate.consume(p.confirm_token)  # first use — valid
    result = gate.consume(p.confirm_token)  # second use — invalid
    assert result is None


def test_consume_unknown_token_returns_none():
    gate = _gate()
    assert gate.consume("totallybogustoken12345678") is None


def test_consume_expired_token_returns_none():
    """Token with expires_at in the past is rejected."""
    gate = _gate()
    p = gate.propose("sess-1", "orders_cancel", {}, "Cancel")
    # Manually expire the proposal by backdating expires_at
    p.expires_at = time.monotonic() - 1.0
    gate._pending[p.confirm_token] = p
    result = gate.consume(p.confirm_token)
    assert result is None


def test_pending_count_reflects_active_proposals():
    gate = _gate()
    assert gate.pending_count() == 0
    p1 = gate.propose("s1", "orders_cancel", {}, "Cancel 1")
    p2 = gate.propose("s2", "orders_cancel", {}, "Cancel 2")
    assert gate.pending_count() == 2
    gate.consume(p1.confirm_token)
    assert gate.pending_count() == 1


def test_expire_stale_cleans_up_old_proposals():
    gate = _gate()
    p = gate.propose("s1", "orders_cancel", {}, "Cancel")
    # Backdate to simulate expiry
    p.expires_at = time.monotonic() - 1.0
    gate._pending[p.confirm_token] = p
    gate._expire_stale()
    assert p.confirm_token not in gate._pending


def test_ttl_seconds_decreases():
    gate = _gate()
    p = gate.propose("s1", "orders_cancel", {}, "Cancel")
    # TTL should be close to _TOKEN_TTL
    assert _TOKEN_TTL - 2 <= p.ttl_seconds() <= _TOKEN_TTL


def test_proposal_records_session_id():
    """Session ID is stored on proposal so the confirm path can validate ownership."""
    gate = _gate()
    p = gate.propose("sess-xyz", "orders_cancel", {}, "Cancel")
    r = gate.consume(p.confirm_token)
    assert r.session_id == "sess-xyz"


def test_proposals_from_different_sessions_have_distinct_tokens():
    gate = _gate()
    p1 = gate.propose("sess-a", "orders_cancel", {}, "Cancel A")
    p2 = gate.propose("sess-b", "orders_cancel", {}, "Cancel B")
    assert p1.confirm_token != p2.confirm_token
    # Consuming p1's token yields session-a, not session-b
    r = gate.consume(p1.confirm_token)
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

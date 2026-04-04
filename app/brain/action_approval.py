"""
ActionApprovalGate — orchestrates write action approval in the streaming chat.

Flow:
  1. COSMOS detects a write action intent (e.g., cancel order).
  2. propose() creates an ActionProposal with a single-use confirm_token.
  3. SSE stream yields approval_required event with confirm_token + action summary.
  4. LIME shows a confirmation dialog to the operator.
  5. Operator clicks Confirm → next request arrives with:
         confirm_action=True, confirm_token=<token>
  6. consume() validates token (single-use, 5-min TTL).
  7. ToolExecutorService re-executes with ctx.approved=True.
  8. SSE stream yields action_executed event with result.

Token strategy:
  - 32-char URL-safe random bytes (secrets.token_urlsafe(24))
  - Stored in-memory keyed by token string
  - TTL: 5 minutes from proposal time
  - Single-use: deleted on first consume()
  - Replay protection: pop() removes the token atomically

Gate is a singleton attached to app.state.action_approval_gate at startup.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Proposal TTL in seconds (5 minutes)
_TOKEN_TTL: int = 300

# Keywords that signal a cancel-order write action
_CANCEL_KEYWORDS: frozenset = frozenset([
    "cancel order", "order cancel", "cancel this order",
    "cancel karo", "order cancel karo", "cancel my order",
    "orders cancel", "cancel the order",
])

# Order ID regex — 7-12 digit numeric IDs used by Shiprocket
_ORDER_ID_RE = re.compile(r'\b(\d{7,12})\b')


@dataclass
class ActionProposal:
    """A pending write action awaiting operator confirmation."""
    confirm_token: str
    session_id: str
    action_type: str            # tool_executor tool name, e.g. "orders_cancel"
    action_input: Dict[str, Any]
    summary: str                # Human-readable description shown to operator
    risk_level: str = "high"
    expires_at: float = field(default_factory=lambda: time.monotonic() + _TOKEN_TTL)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def ttl_seconds(self) -> int:
        """Remaining TTL in whole seconds (0 if expired)."""
        return max(0, int(self.expires_at - time.monotonic()))


class ActionApprovalGate:
    """
    In-memory approval gate for write actions.

    propose() → ActionProposal with confirm_token
    consume(token) → ActionProposal or None (invalid/expired)

    Tokens are single-use and expire after _TOKEN_TTL seconds.
    _expire_stale() runs on every propose/consume to prevent memory growth.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, ActionProposal] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def propose(
        self,
        session_id: str,
        action_type: str,
        action_input: Dict[str, Any],
        summary: str,
        risk_level: str = "high",
    ) -> ActionProposal:
        """
        Create a pending approval proposal.
        Returns ActionProposal with a single-use confirm_token.
        """
        self._expire_stale()
        token = secrets.token_urlsafe(24)
        proposal = ActionProposal(
            confirm_token=token,
            session_id=session_id,
            action_type=action_type,
            action_input=action_input,
            summary=summary,
            risk_level=risk_level,
        )
        self._pending[token] = proposal
        logger.info(
            "action_approval.proposed",
            action=action_type,
            session=session_id,
            token_prefix=token[:8],
        )
        return proposal

    def consume(self, token: str) -> Optional[ActionProposal]:
        """
        Validate and consume a confirm_token (single-use).
        Returns ActionProposal if valid, None if invalid or expired.
        Deletes token on success to prevent replay attacks.
        """
        self._expire_stale()
        proposal = self._pending.pop(token, None)
        if proposal is None:
            logger.warning(
                "action_approval.invalid_token",
                token_prefix=token[:8] if len(token) >= 8 else "?",
            )
            return None
        if time.monotonic() > proposal.expires_at:
            logger.warning(
                "action_approval.expired_token",
                action=proposal.action_type,
                session=proposal.session_id,
            )
            return None
        logger.info(
            "action_approval.consumed",
            action=proposal.action_type,
            session=proposal.session_id,
        )
        return proposal

    def pending_count(self) -> int:
        """Number of unexpired pending proposals (for monitoring)."""
        self._expire_stale()
        return len(self._pending)

    # ------------------------------------------------------------------
    # Intent detection helpers (static — no state needed)
    # ------------------------------------------------------------------

    @staticmethod
    def is_cancel_order_intent(
        query: str,
        intents: List,
        knowledge_chunks: List[Dict],
    ) -> bool:
        """
        Return True if query+context signals a cancel-order write action.

        Checks (in order):
        1. Exact cancel keywords in query text
        2. Intent list contains cancel action signal
        3. High-confidence vector chunk matches orders cancel entity_id
        """
        q_lower = query.lower()

        # 1. Keyword match in query
        if any(kw in q_lower for kw in _CANCEL_KEYWORDS):
            return True

        # 2. Intent list signals cancel
        for intent in intents:
            if isinstance(intent, str) and "cancel" in intent.lower():
                return True
            if isinstance(intent, dict):
                action = str(intent.get("action", "") or intent.get("intent", ""))
                if "cancel" in action.lower() and "order" in action.lower():
                    return True

        # 3. Vector search matched cancel-order KB entity (similarity ≥ 0.70)
        for chunk in knowledge_chunks:
            eid = chunk.get("entity_id", "")
            if "cancel" in eid.lower() and "order" in eid.lower():
                sim = float(chunk.get("similarity", 0.0))
                if sim >= 0.70:
                    return True

        return False

    @staticmethod
    def extract_order_ids(query: str) -> List[int]:
        """Extract Shiprocket order IDs (7-12 digits) from free-form query text."""
        return [int(m) for m in _ORDER_ID_RE.findall(query)]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _expire_stale(self) -> None:
        now = time.monotonic()
        stale = [t for t, p in self._pending.items() if now > p.expires_at]
        for t in stale:
            del self._pending[t]

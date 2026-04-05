"""
ActionApprovalGate — orchestrates write action approval in the streaming chat.

Phase 2 refactor: generic write action detection (KB-driven via entity_id patterns),
DB-backed token persistence, and support for feature flag actions.

Flow:
  1. COSMOS detects a write action intent via detect_write_action().
  2. propose() creates an ActionProposal with single-use confirm_token (UUID).
  3. SSE stream yields approval_required event with confirm_token + action summary.
  4. LIME shows a confirmation dialog to the operator.
  5. Operator clicks Confirm → next request arrives with:
         confirm_action=True, confirm_token=<token>
  6. consume() validates token (single-use, 5-min TTL, session ownership).
  7. ToolExecutorService re-executes with ctx.approved=True.
  8. SSE stream yields action_executed event with result.

Token strategy (Phase 2):
  - UUID v4 string (DB-compatible as primary key)
  - In-memory dict (L1 cache) + icrm_action_approvals table (persistent)
  - TTL: 5 minutes; single-use; session-scoped
  - DB persistence is best-effort — in-memory fallback always active

Registry:
  - _WRITE_ACTION_REGISTRY defines all known write action types
  - Adding a new write action = add entry to registry (no streaming code change)
  - Detects via: KB entity_id patterns → intent list → query keywords (priority order)
"""
from __future__ import annotations

import re
import uuid as _uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.db.repositories import ApprovalRepository

logger = structlog.get_logger()

# Proposal TTL in seconds (5 minutes)
_TOKEN_TTL: int = 300

# Company/seller ID regex (shared with param_clarifier)
_COMPANY_ID_RE = re.compile(
    r'(?:company|seller|client|cid|client_id|company_id)'
    r'[\s_]*(?:id)?[\s:=#]*(\d{3,10})',
    re.I,
)

# Order ID regex — 7-12 digit numeric IDs used by Shiprocket
_ORDER_ID_RE = re.compile(r'\b(\d{7,12})\b')

# "enable"/"disable" detection regex
_ENABLE_RE = re.compile(r'\b(enable|on|activate|start|allow|turn on)\b', re.I)
_DISABLE_RE = re.compile(r'\b(disable|off|deactivate|stop|block|turn off|band karo)\b', re.I)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

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


@dataclass
class WriteActionSignal:
    """
    Returned by detect_write_action() when a write action is identified.
    Carries everything needed to call gate.propose().
    """
    tool_name: str               # "orders_cancel", "feature_cod_toggle", "feature_srf_enable"
    action_input: Dict[str, Any] # extracted params ready for tool_executor
    summary: str                 # human-readable for approval card
    risk_level: str              # "high" | "medium"
    entity_id: str               # KB entity_id that triggered this (or action_type if keyword)


@dataclass
class _WriteActionConfig:
    """Internal: defines how to detect and extract params for one write action type."""
    tool_name: str
    entity_id_patterns: List[str]   # substrings to match against chunk.entity_id (lowercase)
    keywords: frozenset             # substrings to match against query.lower()
    intent_patterns: frozenset      # substrings to match in intent strings
    risk_level: str = "high"
    feature_label: str = ""         # human-readable feature name for approval card


# ---------------------------------------------------------------------------
# Write action registry — add new write actions here, no streaming code changes
# ---------------------------------------------------------------------------

_WRITE_ACTION_REGISTRY: Dict[str, _WriteActionConfig] = {
    "orders_cancel": _WriteActionConfig(
        tool_name="orders_cancel",
        entity_id_patterns=["orders.cancel", "orders/cancel"],
        keywords=frozenset([
            "cancel order", "order cancel", "cancel this order",
            "cancel karo", "order cancel karo", "cancel my order",
            "orders cancel", "cancel the order",
        ]),
        intent_patterns=frozenset(["cancel_order", "cancel order", "order_cancel"]),
        risk_level="high",
        feature_label="Cancel Order",
    ),
    "feature_cod_toggle": _WriteActionConfig(
        tool_name="feature_cod_toggle",
        entity_id_patterns=["enablepartialcodtoggle", "codtoggle", "cod_toggle"],
        keywords=frozenset([
            "disable cod", "enable cod", "toggle cod",
            "cod enable", "cod disable", "cod band karo", "cod on karo",
            "partial cod", "cod toggle", "cod feature",
        ]),
        intent_patterns=frozenset(["cod_toggle", "feature_cod", "enable_cod", "disable_cod"]),
        risk_level="high",
        feature_label="COD (Cash on Delivery)",
    ),
    "feature_srf_enable": _WriteActionConfig(
        tool_name="feature_srf_enable",
        entity_id_patterns=["srf_feature_enable", "srf-feature-enable", "srfenable"],
        keywords=frozenset([
            "enable srf", "disable srf", "srf feature",
            "srf enable", "srf disable", "srf on", "srf off",
        ]),
        intent_patterns=frozenset(["feature_srf", "srf_feature", "enable_srf", "disable_srf"]),
        risk_level="medium",
        feature_label="SRF Feature",
    ),
}

# ---------------------------------------------------------------------------
# ActionApprovalGate
# ---------------------------------------------------------------------------

class ActionApprovalGate:
    """
    Approval gate for write actions.

    Phase 2: async propose/consume with optional DB persistence.
    In-memory dict is L1 cache; icrm_action_approvals table is persistent store.
    DB persistence is best-effort — FK/connection failures degrade gracefully.

    Usage:
        gate = ActionApprovalGate(approval_repo=repo)  # or no repo for in-memory only

        signal = ActionApprovalGate.detect_write_action(query, intents, chunks)
        if signal:
            proposal = await gate.propose(session_id, signal.tool_name, ...)
            yield approval_required SSE

        # On confirm:
        proposal = await gate.consume(confirm_token)
        if proposal and proposal.session_id == session_id:
            execute tool with ctx.approved=True
    """

    def __init__(self, approval_repo: Optional["ApprovalRepository"] = None) -> None:
        self._pending: Dict[str, ActionProposal] = {}
        self._repo = approval_repo

    # ------------------------------------------------------------------
    # Public API (async for DB persistence)
    # ------------------------------------------------------------------

    async def propose(
        self,
        session_id: str,
        action_type: str,
        action_input: Dict[str, Any],
        summary: str,
        risk_level: str = "high",
    ) -> ActionProposal:
        """
        Create a pending approval proposal.
        Stores in-memory (always) and in DB (if repo available, best-effort).
        Returns ActionProposal with UUID confirm_token.
        """
        self._expire_stale()
        token = str(_uuid.uuid4())
        proposal = ActionProposal(
            confirm_token=token,
            session_id=session_id,
            action_type=action_type,
            action_input=action_input,
            summary=summary,
            risk_level=risk_level,
        )
        self._pending[token] = proposal

        # Best-effort DB persistence
        if self._repo is not None:
            try:
                await self._repo.create({
                    "id": token,
                    "session_id": session_id,
                    "action_type": action_type,
                    "risk_level": risk_level,
                    "approval_mode": "manual",
                    "metadata": {
                        "action_input": action_input,
                        "summary": summary,
                        "expires_at": proposal.expires_at,
                    },
                })
            except Exception as exc:
                logger.warning(
                    "action_approval.db_persist_failed",
                    action=action_type,
                    error=str(exc),
                )

        logger.info(
            "action_approval.proposed",
            action=action_type,
            session=session_id,
            token_prefix=token[:8],
        )
        return proposal

    async def consume(self, token: str) -> Optional[ActionProposal]:
        """
        Validate and consume a confirm_token (single-use).
        Returns ActionProposal if valid, None if invalid or expired.
        Checks in-memory first, then DB (for post-restart recovery).
        """
        self._expire_stale()

        # L1: in-memory (fast path — covers 99% of cases)
        proposal = self._pending.pop(token, None)
        if proposal is not None:
            if time.monotonic() > proposal.expires_at:
                logger.warning(
                    "action_approval.expired_token",
                    action=proposal.action_type,
                    session=proposal.session_id,
                )
                return None
            # Mark consumed in DB — MUST succeed or token stays replayable after restart.
            # Log warning on failure so operators can detect DB persistence issues.
            _db_consumed = False
            if self._repo is not None:
                try:
                    await self._repo.update_status(token, "approved")
                    _db_consumed = True
                except Exception as _dbe:
                    logger.warning(
                        "action_approval.db_consumed_failed",
                        action=proposal.action_type,
                        token_prefix=token[:8],
                        error=str(_dbe),
                    )
            logger.info(
                "action_approval.consumed",
                action=proposal.action_type,
                session=proposal.session_id,
                db_consumed=_db_consumed,
            )
            return proposal

        # L2: DB fallback (post-restart recovery)
        # Only reached when token not in memory — i.e. after server restart.
        # update_status is called BEFORE returning the proposal so that a
        # concurrent L2 lookup (multi-instance or rapid retry) sees "approved"
        # and returns None rather than re-consuming the token.
        if self._repo is not None:
            try:
                record = await self._repo.get_by_id(token)
                if record and record.get("approved") is None:
                    meta = record.get("metadata") or {}
                    if isinstance(meta, str):
                        import json
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}
                    expires_at = meta.get("expires_at", 0)
                    if time.monotonic() > expires_at:
                        logger.warning("action_approval.db_token_expired", token_prefix=token[:8])
                        return None
                    proposal = ActionProposal(
                        confirm_token=token,
                        session_id=record.get("session_id") or "",
                        action_type=record.get("action_type") or "",
                        action_input=meta.get("action_input") or {},
                        summary=meta.get("summary") or "",
                        risk_level=record.get("risk_level") or "high",
                        expires_at=expires_at,
                    )
                    await self._repo.update_status(token, "approved")
                    logger.info(
                        "action_approval.consumed_from_db",
                        action=proposal.action_type,
                        token_prefix=token[:8],
                    )
                    return proposal
            except Exception as exc:
                logger.warning("action_approval.db_consume_failed", error=str(exc))

        logger.warning(
            "action_approval.invalid_token",
            token_prefix=token[:8] if len(token) >= 8 else "?",
        )
        return None

    def pending_count(self) -> int:
        """Number of unexpired pending proposals in memory (for monitoring)."""
        self._expire_stale()
        return len(self._pending)

    # ------------------------------------------------------------------
    # Generic write action detection (Phase 2 — replaces is_cancel_order_intent)
    # ------------------------------------------------------------------

    @staticmethod
    def detect_write_action(
        query: str,
        intents: List,
        knowledge_chunks: List[Dict],
    ) -> Optional[WriteActionSignal]:
        """
        Detect any known write action from query + context.

        Detection priority (first match wins per action type, highest-confidence first):
        1. KB chunk entity_id pattern match (similarity ≥ 0.75) — most reliable
        2. Intent list contains known write action string
        3. Query keyword match

        Returns WriteActionSignal or None if no write action detected.
        """
        q_lower = query.lower()

        for action_type, cfg in _WRITE_ACTION_REGISTRY.items():
            # 1. KB entity_id match (highest confidence — from vector search)
            for chunk in knowledge_chunks:
                eid = chunk.get("entity_id", "").lower()
                sim = float(chunk.get("similarity", 0.0))
                if sim >= 0.75 and any(p in eid for p in cfg.entity_id_patterns):
                    return _build_signal(action_type, cfg, query)

            # 2. Intent list match
            for intent in intents:
                if isinstance(intent, str):
                    intent_str = intent.lower()
                elif isinstance(intent, dict):
                    intent_str = str(
                        intent.get("action", "") or intent.get("intent", "")
                    ).lower()
                else:
                    continue
                if any(p in intent_str for p in cfg.intent_patterns):
                    return _build_signal(action_type, cfg, query)

            # 3. Keyword match in query text
            if any(kw in q_lower for kw in cfg.keywords):
                return _build_signal(action_type, cfg, query)

        return None

    # ------------------------------------------------------------------
    # Backward-compat helpers (Phase 1 — kept for existing tests)
    # ------------------------------------------------------------------

    @staticmethod
    def is_cancel_order_intent(
        query: str,
        intents: List,
        knowledge_chunks: List[Dict],
    ) -> bool:
        """Phase 1 compat: returns True if cancel order write action detected."""
        signal = ActionApprovalGate.detect_write_action(query, intents, knowledge_chunks)
        return signal is not None and signal.tool_name == "orders_cancel"

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


# ---------------------------------------------------------------------------
# Signal builder helpers
# ---------------------------------------------------------------------------

def _build_signal(action_type: str, cfg: _WriteActionConfig, query: str) -> WriteActionSignal:
    """Extract action_input and build summary for the detected write action."""
    if action_type == "orders_cancel":
        order_ids = [int(m) for m in _ORDER_ID_RE.findall(query)]
        action_input: Dict[str, Any] = {"ids": order_ids} if order_ids else {}
        summary = (
            f"Cancel {len(order_ids)} order(s): {order_ids}"
            if order_ids else
            "Cancel order (no order IDs detected — please confirm or provide IDs)"
        )
    elif action_type in ("feature_cod_toggle", "feature_srf_enable"):
        company_match = _COMPANY_ID_RE.search(query)
        company_id = company_match.group(1) if company_match else None
        enabled = _detect_enabled_flag(query)
        action_input = {}
        if company_id:
            action_input["company_id"] = int(company_id)
        if enabled is not None:
            action_input["enabled"] = enabled
        action_word = "Enable" if enabled else "Disable" if enabled is False else "Toggle"
        label = cfg.feature_label or cfg.tool_name
        summary = (
            f"{action_word} {label} for company {company_id}"
            if company_id else
            f"{action_word} {label} (company ID not detected — please confirm)"
        )
    else:
        action_input = {}
        summary = f"Execute: {cfg.feature_label or cfg.tool_name}"

    return WriteActionSignal(
        tool_name=cfg.tool_name,
        action_input=action_input,
        summary=summary,
        risk_level=cfg.risk_level,
        entity_id=action_type,
    )


def _detect_enabled_flag(query: str) -> Optional[bool]:
    """Return True=enable, False=disable, None=ambiguous from query text."""
    has_disable = bool(_DISABLE_RE.search(query))
    has_enable = bool(_ENABLE_RE.search(query))
    if has_disable and not has_enable:
        return False
    if has_enable and not has_disable:
        return True
    return None

"""
Session State Manager — MARS STATE pattern adapted for COSMOS sessions.

Provides:
  - SessionState: compact cross-turn memory for a conversation
  - SessionStateManager: create, update, validate, summarise sessions

Reference: mars/docs/token-optimization.md (Layer: STATE.md as persistent memory)
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class SessionState:
    session_id: str
    user_id: str
    company_id: str
    created_at: float  # epoch
    last_active: float  # epoch

    # Conversation state
    message_count: int = 0
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0

    # Context accumulation
    entities_discussed: Dict[str, List[str]] = field(default_factory=dict)  # entity_type -> [entity_ids]
    intents_used: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    actions_pending: List[str] = field(default_factory=list)  # pending approval IDs

    # Decision log (MARS STATE pattern)
    decisions: List[dict] = field(default_factory=list)  # [{timestamp, decision, reason}]

    # Error tracking for escalation detection
    failed_tool_calls: int = 0
    low_confidence_count: int = 0

    # Checksum for validation
    checksum: str = ""


class SessionStateManager:
    """Manages session state with MARS checksums and decision logging."""

    def __init__(self):
        self._states: Dict[str, SessionState] = {}

    def create_session(self, session_id: str, user_id: str, company_id: str) -> SessionState:
        """Create new session state."""
        now = time.time()
        state = SessionState(
            session_id=session_id,
            user_id=user_id,
            company_id=company_id,
            created_at=now,
            last_active=now,
        )
        state.checksum = self.compute_checksum(state)
        self._states[session_id] = state
        return state

    def get_state(self, session_id: str) -> Optional[SessionState]:
        """Get current session state."""
        return self._states.get(session_id)

    def update_after_query(
        self,
        session_id: str,
        intent: str,
        entity: str,
        entity_id: str,
        tools: List[str],
        tokens: int,
        cost: float,
    ) -> SessionState:
        """Update state after processing a query."""
        state = self._states.get(session_id)
        if state is None:
            raise ValueError(f"Session '{session_id}' not found")

        state.last_active = time.time()
        state.message_count += 1
        state.total_tokens_used += tokens
        state.total_cost_usd += cost

        # Track intent
        if intent and intent not in state.intents_used:
            state.intents_used.append(intent)

        # Track entity
        if entity and entity_id:
            if entity not in state.entities_discussed:
                state.entities_discussed[entity] = []
            if entity_id not in state.entities_discussed[entity]:
                state.entities_discussed[entity].append(entity_id)

        # Track tools
        for tool in tools:
            if tool not in state.tools_used:
                state.tools_used.append(tool)

        state.checksum = self.compute_checksum(state)
        return state

    def log_decision(self, session_id: str, decision: str, reason: str) -> None:
        """Log a decision (MARS pattern)."""
        state = self._states.get(session_id)
        if state is None:
            raise ValueError(f"Session '{session_id}' not found")

        state.decisions.append({
            "timestamp": datetime.now(tz=None).isoformat(),
            "decision": decision,
            "reason": reason,
        })
        state.checksum = self.compute_checksum(state)

    def record_failure(self, session_id: str, is_tool_failure: bool = False,
                       is_low_confidence: bool = False) -> None:
        """Record a failure for escalation detection."""
        state = self._states.get(session_id)
        if state is None:
            raise ValueError(f"Session '{session_id}' not found")

        if is_tool_failure:
            state.failed_tool_calls += 1
        if is_low_confidence:
            state.low_confidence_count += 1
        state.checksum = self.compute_checksum(state)

    def compute_checksum(self, state: SessionState) -> str:
        """SHA-256 checksum of state for validation."""
        # Build a deterministic representation excluding the checksum field itself
        data = {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "company_id": state.company_id,
            "message_count": state.message_count,
            "total_tokens_used": state.total_tokens_used,
            "total_cost_usd": state.total_cost_usd,
            "entities_discussed": state.entities_discussed,
            "intents_used": state.intents_used,
            "tools_used": state.tools_used,
            "actions_pending": state.actions_pending,
            "decisions": state.decisions,
            "failed_tool_calls": state.failed_tool_calls,
            "low_confidence_count": state.low_confidence_count,
        }
        raw = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def validate_checksum(self, session_id: str) -> bool:
        """Verify state hasn't been tampered with."""
        state = self._states.get(session_id)
        if state is None:
            return False
        expected = self.compute_checksum(state)
        return state.checksum == expected

    def get_context_summary(self, session_id: str) -> str:
        """Build a compact context summary for the next LLM call.

        Instead of full history, return: entities discussed, recent decisions,
        pending actions. This is the STATE.md approach — compact cross-turn memory.
        """
        state = self._states.get(session_id)
        if state is None:
            return ""

        parts: List[str] = []

        # Entities discussed
        if state.entities_discussed:
            entity_parts = []
            for etype, eids in state.entities_discussed.items():
                entity_parts.append(f"{etype}: {', '.join(eids[-5:])}")  # last 5
            parts.append(f"Entities: {'; '.join(entity_parts)}")

        # Intents used
        if state.intents_used:
            parts.append(f"Intents: {', '.join(state.intents_used[-5:])}")

        # Recent decisions (last 3)
        if state.decisions:
            recent = state.decisions[-3:]
            dec_parts = [f"{d['decision']} ({d['reason']})" for d in recent]
            parts.append(f"Recent decisions: {'; '.join(dec_parts)}")

        # Pending actions
        if state.actions_pending:
            parts.append(f"Pending approvals: {', '.join(state.actions_pending)}")

        # Stats
        parts.append(
            f"Messages: {state.message_count}, "
            f"Tokens: {state.total_tokens_used}, "
            f"Cost: ${state.total_cost_usd:.4f}"
        )

        return " | ".join(parts)

    def should_escalate(self, session_id: str) -> dict:
        """Check if session should be escalated based on patterns.

        Returns:
            {
                "escalate": bool,
                "reasons": List[str]
            }
        """
        state = self._states.get(session_id)
        if state is None:
            return {"escalate": False, "reasons": ["Session not found"]}

        reasons: List[str] = []

        # Too many failed tool calls
        if state.failed_tool_calls >= 3:
            reasons.append(
                f"Too many failed tool calls ({state.failed_tool_calls})"
            )

        # Too many low-confidence responses
        if state.low_confidence_count >= 3:
            reasons.append(
                f"Too many low-confidence responses ({state.low_confidence_count})"
            )

        # Budget exceeded (session-level: $1 per session as default threshold)
        if state.total_cost_usd > 1.0:
            reasons.append(
                f"Session cost exceeded threshold (${state.total_cost_usd:.4f})"
            )

        # Repeated same intent (stuck in loop) — same intent used > 3 times
        if state.intents_used:
            from collections import Counter
            counts = Counter(state.intents_used)
            # Note: intents_used tracks unique intents, so we check message_count vs unique
            # If message_count > 5 and only 1 unique intent, likely stuck
            if state.message_count > 5 and len(set(state.intents_used)) == 1:
                reasons.append(
                    f"Stuck in loop: same intent '{state.intents_used[0]}' "
                    f"for {state.message_count} messages"
                )

        return {
            "escalate": len(reasons) > 0,
            "reasons": reasons,
        }

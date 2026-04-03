"""
Session State Manager — MARS STATE pattern adapted for COSMOS sessions.

Provides:
  - SessionState: compact cross-turn memory for a conversation
  - SessionStateManager: create, update, validate, summarise sessions

Phase 6b — Multi-turn context compression:
  When message_count >= COMPRESS_THRESHOLD (default 10), the manager
  summarises older turns into a single compact memory block stored in
  SessionState.context_summary.  On subsequent turns this summary is
  prepended instead of replaying full history, keeping token count flat
  regardless of conversation length.

Reference: mars/docs/token-optimization.md (Layer: STATE.md as persistent memory)
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Compression triggers when the session reaches this many turns
COMPRESS_THRESHOLD = 10
# Keep the last N turns verbatim after compression (recent context wins)
KEEP_RECENT_TURNS = 4
# Rough token budget for the compressed summary block
SUMMARY_MAX_TOKENS = 400


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

    # Phase 6b: per-turn history (compact) + compressed memory block
    # Each turn: {"turn": int, "query": str, "intent": str, "entity": str,
    #              "entity_id": str, "tools": list, "tokens": int}
    turn_history: List[Dict[str, Any]] = field(default_factory=list)
    # LLM-generated summary of turns [0 .. N-KEEP_RECENT_TURNS-1]
    context_summary: str = ""
    # True once at least one compression pass has been done
    history_compressed: bool = False

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

        # Phase 6b: record turn in history
        state.turn_history.append({
            "turn": state.message_count,
            "query": "",  # caller may enrich via enrich_last_turn()
            "intent": intent or "",
            "entity": entity or "",
            "entity_id": entity_id or "",
            "tools": list(tools),
            "tokens": tokens,
        })

        state.checksum = self.compute_checksum(state)
        return state

    def enrich_last_turn(self, session_id: str, query: str) -> None:
        """Backfill the query text for the most recently added turn."""
        state = self._states.get(session_id)
        if state and state.turn_history:
            state.turn_history[-1]["query"] = query[:200]  # cap
            state.checksum = self.compute_checksum(state)

    def should_compress(self, session_id: str) -> bool:
        """Return True when the session has enough turns to benefit from compression."""
        state = self._states.get(session_id)
        if state is None:
            return False
        return state.message_count >= COMPRESS_THRESHOLD

    async def compress_history(
        self,
        session_id: str,
        llm_client=None,  # optional LLMClient for LLM-based summary
    ) -> str:
        """
        Phase 6b: Compress older turns into a compact summary string.

        Algorithm:
          1. Take turns [0 .. N-KEEP_RECENT_TURNS-1] from turn_history.
          2. If llm_client is provided: ask LLM to write a 2-3 sentence
             summary of what was discussed (entities, tools, decisions).
          3. If no llm_client: build a deterministic keyword summary.
          4. Store result in state.context_summary.
          5. Trim turn_history to the last KEEP_RECENT_TURNS entries.
          6. Set state.history_compressed = True.

        Returns the generated summary string.
        """
        state = self._states.get(session_id)
        if state is None:
            return ""

        turns_to_compress = state.turn_history[:-KEEP_RECENT_TURNS] if len(state.turn_history) > KEEP_RECENT_TURNS else []
        if not turns_to_compress:
            return state.context_summary  # nothing to compress yet

        summary = ""

        if llm_client is not None:
            # LLM-based summary — most accurate
            turns_text = "\n".join(
                f"Turn {t['turn']}: user asked about {t.get('intent','?')} "
                f"entity={t.get('entity','?')}:{t.get('entity_id','?')} "
                f"tools={t.get('tools',[])} query={t.get('query','')[:100]}"
                for t in turns_to_compress
            )
            prompt = (
                f"Summarize the following conversation turns in 2-3 sentences, "
                f"focusing on entities discussed, decisions made, and open questions. "
                f"Be concise — this summary will prepend future context windows.\n\n"
                f"{turns_text}"
            )
            try:
                if hasattr(llm_client, "complete"):
                    summary = await llm_client.complete(
                        prompt, max_tokens=SUMMARY_MAX_TOKENS,
                        intent="summarize", confidence=0.9,
                    )
                    if isinstance(summary, dict):
                        summary = summary.get("text", "")
            except Exception:
                summary = ""  # fall through to keyword summary

        if not summary:
            # Deterministic keyword summary (zero-latency fallback)
            all_intents = list({t["intent"] for t in turns_to_compress if t.get("intent")})
            all_entities: Dict[str, List[str]] = {}
            for t in turns_to_compress:
                e, eid = t.get("entity", ""), t.get("entity_id", "")
                if e and eid:
                    all_entities.setdefault(e, []).append(eid)
            all_tools = list({tl for t in turns_to_compress for tl in t.get("tools", [])})
            entity_str = "; ".join(f"{k}: {v[-3:]}" for k, v in all_entities.items())
            summary = (
                f"[Compressed {len(turns_to_compress)} turns] "
                f"Discussed: intents={all_intents[:5]}, entities={entity_str}, "
                f"tools={all_tools[:5]}"
            )

        # Update state: store summary, trim history, mark compressed
        # Prepend any existing summary (layered compression for very long sessions)
        if state.context_summary:
            summary = state.context_summary + " | " + summary

        state.context_summary = summary[:1000]  # hard cap
        state.turn_history = state.turn_history[-KEEP_RECENT_TURNS:]
        state.history_compressed = True
        state.checksum = self.compute_checksum(state)

        return state.context_summary

    def get_compressed_context_prefix(self, session_id: str) -> str:
        """Return the compressed summary + recent turns as a prefix for LLM context.

        Used by QueryOrchestrator to prepend cross-turn memory to the prompt
        without including full turn-by-turn history.
        """
        state = self._states.get(session_id)
        if state is None:
            return ""

        parts: List[str] = []
        if state.context_summary:
            parts.append(f"[Session Memory] {state.context_summary}")

        # Append recent turns (last KEEP_RECENT_TURNS)
        for t in state.turn_history[-KEEP_RECENT_TURNS:]:
            q = t.get("query", "")
            if q:
                parts.append(f"Turn {t['turn']}: {q[:100]}")

        return "\n".join(parts)

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

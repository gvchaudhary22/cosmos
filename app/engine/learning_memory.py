"""
Learning Memory — 3-tier memory system for continuous improvement.

Tier 1: Working Memory (per conversation)
  - Current entities being discussed
  - Operator's stated goal
  - Steps already taken
  - Already handled by session_state.py

Tier 2: Episodic Memory (per operator)
  - Preferred export formats
  - Frequently asked query types
  - Past interaction summaries
  - Seller portfolio knowledge

Tier 3: Institutional Memory (global)
  - Resolved case patterns
  - Carrier behavior patterns by season/region
  - Common issue → resolution mappings
  - SLA reality vs promise data

Usage:
    memory = LearningMemory()
    await memory.record_interaction(operator_id, query, response, tools_used, feedback)
    preferences = await memory.get_operator_preferences(operator_id)
    patterns = await memory.get_relevant_patterns(domain, query)
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


class LearningMemory:
    """3-tier memory system: working (session), episodic (operator), institutional (global)."""

    # --- Tier 2: Episodic Memory (per operator) ---

    async def record_interaction(
        self,
        operator_id: str,
        query: str,
        response: str,
        tools_used: List[str],
        domain: str = "",
        feedback: Optional[str] = None,
        success: bool = True,
    ):
        """Record an interaction to build operator memory over time."""
        if not operator_id:
            return

        try:
            # Track query type preference
            query_type = self._classify_query_type(query)
            await self._upsert_episodic(operator_id, "query_type", query_type, {
                "last_query": query[:200],
                "domain": domain,
            })

            # Track tool usage preference
            for tool in tools_used:
                await self._upsert_episodic(operator_id, "tool_usage", tool, {
                    "domain": domain,
                })

            # Track domain preference
            if domain:
                await self._upsert_episodic(operator_id, "domain_preference", domain, {})

            # If feedback provided, record it
            if feedback:
                await self._store_feedback(operator_id, query, response, feedback, success)

        except Exception as e:
            logger.debug("learning_memory.record_failed", error=str(e))

    async def get_operator_preferences(self, operator_id: str) -> Dict[str, Any]:
        """Get learned preferences for an operator."""
        if not operator_id:
            return {}

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""SELECT memory_type, memory_key, memory_value, occurrence_count, confidence
                            FROM cosmos_operator_memory
                            WHERE operator_id = :oid
                            ORDER BY occurrence_count DESC
                            LIMIT 20"""),
                    {"oid": operator_id},
                )

                preferences = {
                    "frequent_query_types": [],
                    "preferred_tools": [],
                    "domain_focus": [],
                }

                for row in result.fetchall():
                    value = json.loads(row.memory_value) if row.memory_value else {}
                    entry = {
                        "key": row.memory_key,
                        "count": row.occurrence_count,
                        "confidence": row.confidence,
                        **value,
                    }

                    if row.memory_type == "query_type":
                        preferences["frequent_query_types"].append(entry)
                    elif row.memory_type == "tool_usage":
                        preferences["preferred_tools"].append(entry)
                    elif row.memory_type == "domain_preference":
                        preferences["domain_focus"].append(entry)

                return preferences

        except Exception as e:
            logger.debug("learning_memory.get_preferences_failed", error=str(e))
            return {}

    async def get_context_for_prompt(self, operator_id: str) -> str:
        """Generate operator context string for inclusion in system prompt."""
        prefs = await self.get_operator_preferences(operator_id)
        if not prefs or not any(prefs.values()):
            return ""

        lines = ["Operator preferences (learned from past interactions):"]

        if prefs["frequent_query_types"]:
            types = [p["key"] for p in prefs["frequent_query_types"][:3]]
            lines.append(f"- Common query types: {', '.join(types)}")

        if prefs["preferred_tools"]:
            tools = [p["key"] for p in prefs["preferred_tools"][:5]]
            lines.append(f"- Frequently uses: {', '.join(tools)}")

        if prefs["domain_focus"]:
            domains = [p["key"] for p in prefs["domain_focus"][:3]]
            lines.append(f"- Domain focus: {', '.join(domains)}")

        return "\n".join(lines)

    # --- Tier 3: Institutional Memory (global) ---

    async def record_pattern(
        self,
        pattern_type: str,
        pattern_key: str,
        pattern_value: Dict[str, Any],
        conversation_id: str = "",
    ):
        """Record a learned pattern in institutional memory."""
        try:
            async with AsyncSessionLocal() as session:
                existing = await session.execute(
                    text("""SELECT id, occurrence_count, source_conversations
                            FROM cosmos_institutional_memory
                            WHERE pattern_type = :pt AND pattern_key = :pk"""),
                    {"pt": pattern_type, "pk": pattern_key},
                )
                row = existing.fetchone()

                if row:
                    # Update existing pattern
                    conversations = json.loads(row.source_conversations) if row.source_conversations else []
                    if conversation_id and conversation_id not in conversations:
                        conversations.append(conversation_id)
                    new_count = row.occurrence_count + 1
                    confidence = min(1.0, 0.3 + new_count * 0.1)

                    await session.execute(
                        text("""UPDATE cosmos_institutional_memory
                                SET occurrence_count = :cnt, confidence = :conf,
                                    source_conversations = :convs,
                                    pattern_value = :val, updated_at = NOW()
                                WHERE id = :id"""),
                        {
                            "id": row.id,
                            "cnt": new_count,
                            "conf": confidence,
                            "convs": json.dumps(conversations[-10:]),
                            "val": json.dumps(pattern_value, default=str),
                        },
                    )
                else:
                    # Insert new pattern
                    await session.execute(
                        text("""INSERT INTO cosmos_institutional_memory
                                (id, pattern_type, pattern_key, pattern_value, confidence,
                                 occurrence_count, source_conversations)
                                VALUES (:id, :pt, :pk, :val, 0.3, 1, :convs)"""),
                        {
                            "id": str(uuid.uuid4()),
                            "pt": pattern_type,
                            "pk": pattern_key,
                            "val": json.dumps(pattern_value, default=str),
                            "convs": json.dumps([conversation_id] if conversation_id else []),
                        },
                    )

                await session.commit()

        except Exception as e:
            logger.debug("learning_memory.record_pattern_failed", error=str(e))

    async def get_relevant_patterns(self, domain: str = "", query: str = "", limit: int = 5) -> List[Dict]:
        """Get institutional patterns relevant to a query."""
        try:
            async with AsyncSessionLocal() as session:
                # Get high-confidence patterns, optionally filtered by domain
                if domain:
                    result = await session.execute(
                        text("""SELECT pattern_type, pattern_key, pattern_value,
                                       confidence, occurrence_count
                                FROM cosmos_institutional_memory
                                WHERE confidence >= 0.5
                                  AND (pattern_key LIKE :domain_pattern
                                       OR JSON_EXTRACT(pattern_value, '$.domain') = :domain)
                                ORDER BY confidence DESC, occurrence_count DESC
                                LIMIT :lim"""),
                        {"domain_pattern": f"%{domain}%", "domain": domain, "lim": limit},
                    )
                else:
                    result = await session.execute(
                        text("""SELECT pattern_type, pattern_key, pattern_value,
                                       confidence, occurrence_count
                                FROM cosmos_institutional_memory
                                WHERE confidence >= 0.5
                                ORDER BY confidence DESC, occurrence_count DESC
                                LIMIT :lim"""),
                        {"lim": limit},
                    )

                patterns = []
                for row in result.fetchall():
                    patterns.append({
                        "type": row.pattern_type,
                        "key": row.pattern_key,
                        "value": json.loads(row.pattern_value) if row.pattern_value else {},
                        "confidence": row.confidence,
                        "occurrences": row.occurrence_count,
                    })
                return patterns

        except Exception as e:
            logger.debug("learning_memory.get_patterns_failed", error=str(e))
            return []

    # --- Helpers ---

    async def _upsert_episodic(
        self, operator_id: str, memory_type: str, memory_key: str, value: Dict
    ):
        """Insert or update an episodic memory entry."""
        try:
            async with AsyncSessionLocal() as session:
                existing = await session.execute(
                    text("""SELECT id, occurrence_count FROM cosmos_operator_memory
                            WHERE operator_id = :oid AND memory_type = :mt AND memory_key = :mk"""),
                    {"oid": operator_id, "mt": memory_type, "mk": memory_key},
                )
                row = existing.fetchone()

                if row:
                    new_count = row.occurrence_count + 1
                    confidence = min(1.0, 0.3 + new_count * 0.05)
                    await session.execute(
                        text("""UPDATE cosmos_operator_memory
                                SET occurrence_count = :cnt, confidence = :conf,
                                    memory_value = :val, last_seen_at = NOW(), updated_at = NOW()
                                WHERE id = :id"""),
                        {"id": row.id, "cnt": new_count, "conf": confidence,
                         "val": json.dumps(value, default=str)},
                    )
                else:
                    await session.execute(
                        text("""INSERT INTO cosmos_operator_memory
                                (id, operator_id, memory_type, memory_key, memory_value, confidence)
                                VALUES (:id, :oid, :mt, :mk, :val, 0.3)"""),
                        {"id": str(uuid.uuid4()), "oid": operator_id, "mt": memory_type,
                         "mk": memory_key, "val": json.dumps(value, default=str)},
                    )

                await session.commit()
        except Exception as e:
            logger.debug("learning_memory.upsert_episodic_failed", error=str(e))

    async def _store_feedback(
        self, operator_id: str, query: str, response: str, feedback: str, success: bool
    ):
        """Store explicit operator feedback for learning."""
        await self.record_pattern(
            pattern_type="operator_feedback",
            pattern_key=f"{operator_id}:{int(time.time())}",
            pattern_value={
                "query": query[:500],
                "response": response[:500],
                "feedback": feedback,
                "success": success,
                "operator_id": operator_id,
            },
        )

    # --- Tier 1: Working Memory — session-scoped entity tracking ---

    async def record_entity_resolution(
        self,
        operator_id: str,
        session_id: str,
        entity_id: str,
        entity_type: str,
        resolved_data: Dict[str, Any],
    ):
        """Track entities resolved in this session to avoid re-fetching.

        Stored as episodic memory with memory_type='session_entity' and
        key '{session_id}:{entity_id}'. Used by get_session_state() to
        build a per-session entity cache that RIPER Research phase can
        skip re-retrieving.
        """
        if not operator_id or not session_id or not entity_id:
            return
        await self._upsert_episodic(
            operator_id=operator_id,
            memory_type="session_entity",
            memory_key=f"{session_id}:{entity_id}",
            value={
                "entity_type": entity_type,
                "session_id": session_id,
                "resolved_at": int(time.time()),
                **{k: v for k, v in resolved_data.items() if k not in ("session_id",)},
            },
        )

    async def get_session_state(
        self, operator_id: str, session_id: str
    ) -> Dict[str, Any]:
        """Return entities resolved this session + recent query fingerprints.

        Mirrors the STATE.md cross-session entity tracking from COSMOS:
          - 'resolved_entities': {entity_id: {type, data}} for this session
          - 'session_id': the session being queried

        RIPER Research phase reads this to skip re-retrieval when the same
        entity (e.g. order_id, AWB) appears across turns in the same session.
        """
        if not operator_id or not session_id:
            return {"resolved_entities": {}, "session_id": session_id}

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""SELECT memory_key, memory_value, updated_at
                            FROM cosmos_operator_memory
                            WHERE operator_id = :oid
                              AND memory_type = 'session_entity'
                              AND memory_key LIKE :prefix
                            ORDER BY updated_at DESC
                            LIMIT 50"""),
                    {"oid": operator_id, "prefix": f"{session_id}:%"},
                )

                resolved: Dict[str, Any] = {}
                for row in result.fetchall():
                    # key is "{session_id}:{entity_id}" — strip session prefix
                    entity_id = row.memory_key.split(":", 1)[1] if ":" in row.memory_key else row.memory_key
                    value = json.loads(row.memory_value) if row.memory_value else {}
                    resolved[entity_id] = value

                return {"resolved_entities": resolved, "session_id": session_id}

        except Exception as e:
            logger.debug("learning_memory.get_session_state_failed", error=str(e))
            return {"resolved_entities": {}, "session_id": session_id}

    def _classify_query_type(self, query: str) -> str:
        """Simple query type classification for memory tracking."""
        q = query.lower()
        if any(w in q for w in ["status", "track", "where", "check"]):
            return "lookup"
        if any(w in q for w in ["cancel", "update", "change", "modify"]):
            return "action"
        if any(w in q for w in ["why", "reason", "stuck", "delayed", "failed"]):
            return "troubleshoot"
        if any(w in q for w in ["how", "what", "explain", "tell me about"]):
            return "explain"
        if any(w in q for w in ["report", "export", "download", "list all"]):
            return "report"
        return "general"

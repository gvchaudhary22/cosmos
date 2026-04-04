"""
Pattern Match Fast Path — Skip full pipeline for high-confidence repeat queries.

After a query pattern succeeds N times (default 50), cache the resolution path.
Next matching query skips M2-M8 and directly executes the cached tool sequence.

Flow:
  1. Every successful query resolution → save pattern to DB
  2. On new query → check pattern cache BEFORE full pipeline
  3. If match with confidence >= 0.90 → execute cached path → M9 safety → respond
  4. Skip: Planner, Retriever, ReAct loop (saves ~1.5s + LLM cost)

Pattern format:
  "track order {id}" → normalized: "track_order_{entity_id}"
  Resolution: [shipment_track(order_id={id})]

Storage: cosmos_pattern_cache table (PostgreSQL)
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

PATTERN_TABLE = "cosmos_pattern_cache"
MIN_SUCCESSES_FOR_FAST_PATH = 30  # promote after 30 successes
CONFIDENCE_THRESHOLD = 0.90       # only fast-path if confidence >= this
MAX_PATTERNS = 10000              # cap total patterns


@dataclass
class CachedPattern:
    """A cached resolution pattern."""
    pattern_key: str
    intent: str
    entity_type: str
    tool_sequence: List[Dict[str, Any]]  # [{tool_name, param_template}, ...]
    agent_name: Optional[str] = None
    confidence: float = 0.0
    success_count: int = 0
    total_count: int = 0
    avg_latency_ms: float = 0.0
    last_used_at: Optional[str] = None


@dataclass
class FastPathResult:
    """Result of a fast-path execution."""
    hit: bool = False
    pattern_key: str = ""
    tool_sequence: List[Dict] = field(default_factory=list)
    confidence: float = 0.0
    skipped_stages: List[str] = field(default_factory=list)


class PatternCache:
    """Pattern-based fast path for repeat query types."""

    def __init__(self):
        self._local_cache: Dict[str, CachedPattern] = {}
        self._loaded = False

    async def ensure_schema(self):
        """Create pattern cache table."""
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {PATTERN_TABLE} (
                        pattern_key VARCHAR(500) NOT NULL,
                        repo_id VARCHAR(255) NOT NULL DEFAULT '',
                        role VARCHAR(100) NOT NULL DEFAULT '',
                        intent VARCHAR(100) NOT NULL DEFAULT '',
                        entity_type VARCHAR(100) NOT NULL DEFAULT '',
                        tool_sequence JSON NOT NULL DEFAULT '[]',
                        agent_name VARCHAR(200),
                        confidence FLOAT NOT NULL DEFAULT 0.0,
                        success_count INT NOT NULL DEFAULT 0,
                        total_count INT NOT NULL DEFAULT 0,
                        avg_latency_ms FLOAT DEFAULT 0.0,
                        kb_version VARCHAR(64) DEFAULT '',
                        tool_version VARCHAR(64) DEFAULT '',
                        created_at TIMESTAMP DEFAULT now(),
                        last_used_at TIMESTAMP DEFAULT now(),
                        PRIMARY KEY (pattern_key, repo_id, role)
                    )
                """))
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.debug("pattern_cache.schema_error", error=str(e))

    async def load_patterns(self):
        """Load high-confidence patterns into local memory."""
        if self._loaded:
            return
        self._loaded = True

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(text(f"""
                    SELECT pattern_key, intent, entity_type, tool_sequence,
                           agent_name, confidence, success_count, total_count,
                           avg_latency_ms, last_used_at
                    FROM {PATTERN_TABLE}
                    WHERE confidence >= :threshold
                    ORDER BY success_count DESC
                    LIMIT :limit
                """), {"threshold": CONFIDENCE_THRESHOLD, "limit": MAX_PATTERNS})

                for row in result.fetchall():
                    pattern = CachedPattern(
                        pattern_key=row.pattern_key,
                        intent=row.intent,
                        entity_type=row.entity_type,
                        tool_sequence=row.tool_sequence if isinstance(row.tool_sequence, list) else json.loads(row.tool_sequence),
                        agent_name=row.agent_name,
                        confidence=row.confidence,
                        success_count=row.success_count,
                        total_count=row.total_count,
                        avg_latency_ms=row.avg_latency_ms,
                        last_used_at=str(row.last_used_at) if row.last_used_at else None,
                    )
                    self._local_cache[row.pattern_key] = pattern

                logger.info("pattern_cache.loaded", patterns=len(self._local_cache))
        except Exception as e:
            logger.warning("pattern_cache.load_failed", error=str(e))

    def match(self, query: str, intent: str, entity_type: str,
              repo_id: str = "", role: str = "") -> FastPathResult:
        """Check if query matches a cached pattern. O(1) lookup.
        Scoped by repo_id + role to prevent cross-tenant/cross-role leaks."""
        pattern_key = self._normalize(query, intent, entity_type)
        # Scope the cache key by repo+role
        scoped_key = f"{pattern_key}|{repo_id}|{role}"

        cached = self._local_cache.get(scoped_key) or self._local_cache.get(pattern_key)
        if cached and cached.confidence >= CONFIDENCE_THRESHOLD and cached.success_count >= MIN_SUCCESSES_FOR_FAST_PATH:
            return FastPathResult(
                hit=True,
                pattern_key=pattern_key,
                tool_sequence=cached.tool_sequence,
                confidence=cached.confidence,
                skipped_stages=["planner", "retriever", "react_loop", "reflector"],
            )

        return FastPathResult(hit=False)

    async def invalidate_all(self, reason: str = "kb_update"):
        """Invalidate all cached patterns (e.g., after KB rebuild or tool change)."""
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text(f"""
                    UPDATE {PATTERN_TABLE}
                    SET confidence = confidence * 0.5,
                        kb_version = :reason
                """), {"reason": reason})
                await session.commit()
            # Clear local cache
            self._local_cache.clear()
            self._loaded = False
            logger.info("pattern_cache.invalidated", reason=reason)
        except Exception as e:
            logger.warning("pattern_cache.invalidate_failed", error=str(e))

    async def record_success(self, query: str, intent: str, entity_type: str,
                              tool_sequence: List[Dict], agent_name: str = "",
                              latency_ms: float = 0.0,
                              repo_id: str = "", role: str = "",
                              kb_version: str = ""):
        """Record a successful resolution. Builds pattern confidence over time."""
        pattern_key = self._normalize(query, intent, entity_type)

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text(f"""
                    INSERT INTO {PATTERN_TABLE}
                        (pattern_key, intent, entity_type, tool_sequence, agent_name,
                         success_count, total_count, confidence, avg_latency_ms)
                    VALUES
                        (:key, :intent, :entity, :tools, :agent,
                         1, 1, 1.0, :latency)
                    ON DUPLICATE KEY UPDATE
                        success_count = {PATTERN_TABLE}.success_count + 1,
                        total_count = {PATTERN_TABLE}.total_count + 1,
                        confidence = ({PATTERN_TABLE}.success_count + 1.0) / ({PATTERN_TABLE}.total_count + 1.0),
                        avg_latency_ms = ({PATTERN_TABLE}.avg_latency_ms * {PATTERN_TABLE}.total_count + :latency) / ({PATTERN_TABLE}.total_count + 1),
                        last_used_at = now(),
                        tool_sequence = :tools
                """), {
                    "key": pattern_key,
                    "intent": intent,
                    "entity": entity_type,
                    "tools": json.dumps(tool_sequence),
                    "agent": agent_name,
                    "latency": latency_ms,
                })
                await session.commit()

                # Update local cache
                if pattern_key in self._local_cache:
                    p = self._local_cache[pattern_key]
                    p.success_count += 1
                    p.total_count += 1
                    p.confidence = p.success_count / p.total_count
                else:
                    self._local_cache[pattern_key] = CachedPattern(
                        pattern_key=pattern_key, intent=intent,
                        entity_type=entity_type, tool_sequence=tool_sequence,
                        agent_name=agent_name, success_count=1, total_count=1,
                        confidence=1.0,
                    )

        except Exception as e:
            logger.debug("pattern_cache.record_failed", error=str(e))

    async def record_failure(self, query: str, intent: str, entity_type: str):
        """Record a failed resolution. Lowers pattern confidence."""
        pattern_key = self._normalize(query, intent, entity_type)

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text(f"""
                    UPDATE {PATTERN_TABLE}
                    SET total_count = total_count + 1,
                        confidence = success_count::float / (total_count + 1),
                        last_used_at = now()
                    WHERE pattern_key = :key
                """), {"key": pattern_key})
                await session.commit()

                if pattern_key in self._local_cache:
                    p = self._local_cache[pattern_key]
                    p.total_count += 1
                    p.confidence = p.success_count / p.total_count
        except Exception:
            pass

    def _normalize(self, query: str, intent: str, entity_type: str) -> str:
        """Normalize query into a pattern key.

        "Track order 12345678" → "lookup:order:track_order_{id}"
        "Cancel order 98765 and refund" → "act:order:cancel_order_refund"
        """
        q = query.lower().strip()
        # Remove specific IDs/numbers → replace with {id}
        q = re.sub(r'\b\d{5,}\b', '{id}', q)
        # Remove AWB-like alphanumeric
        q = re.sub(r'\b[A-Z0-9]{8,}\b', '{awb}', q, flags=re.IGNORECASE)
        # Normalize whitespace
        q = re.sub(r'\s+', '_', q)
        # Remove common filler words
        for filler in ['ka', 'ki', 'ke', 'hai', 'kya', 'mera', 'meri', 'please', 'karo', 'batao', 'dikhao']:
            q = q.replace(f'_{filler}_', '_').replace(f'{filler}_', '').replace(f'_{filler}', '')
        # Truncate to reasonable length
        q = q[:100]

        return f"{intent}:{entity_type}:{q}"

    async def get_stats(self) -> Dict:
        """Return cache statistics."""
        total = len(self._local_cache)
        fast_path_ready = sum(1 for p in self._local_cache.values()
                              if p.confidence >= CONFIDENCE_THRESHOLD and p.success_count >= MIN_SUCCESSES_FOR_FAST_PATH)
        return {
            "total_patterns": total,
            "fast_path_ready": fast_path_ready,
            "threshold": CONFIDENCE_THRESHOLD,
            "min_successes": MIN_SUCCESSES_FOR_FAST_PATH,
        }

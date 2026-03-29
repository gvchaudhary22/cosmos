"""Redis cache for COSMOS session state, rate limiting, and tool result caching.

Provides async Redis operations with graceful degradation — if Redis is
unavailable, methods return None / 0 instead of raising.
"""

import json
from typing import Optional

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()


class RedisCache:
    """Redis cache for session state, rate limiting, and temporary data."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._redis: Optional[redis.Redis] = None
        self._url = redis_url

    async def connect(self) -> None:
        """Establish Redis connection."""
        self._redis = redis.from_url(self._url, decode_responses=True)
        try:
            await self._redis.ping()
            logger.info("redis.connected", url=self._url)
        except Exception as exc:
            logger.warning("redis.connect_failed", error=str(exc))
            self._redis = None

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    @property
    def is_connected(self) -> bool:
        return self._redis is not None

    # ------------------------------------------------------------------ #
    # Session state cache
    # ------------------------------------------------------------------ #

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Get cached session state."""
        if not self._redis:
            return None
        try:
            data = await self._redis.get(f"session:{session_id}")
            return json.loads(data) if data else None
        except Exception as exc:
            logger.warning("redis.get_session_failed", error=str(exc))
            return None

    async def set_session(self, session_id: str, state: dict, ttl: int = 3600) -> None:
        """Cache session state with TTL (default 1 hour)."""
        if not self._redis:
            return
        try:
            await self._redis.setex(
                f"session:{session_id}",
                ttl,
                json.dumps(state, default=str),
            )
        except Exception as exc:
            logger.warning("redis.set_session_failed", error=str(exc))

    async def delete_session(self, session_id: str) -> None:
        """Remove session from cache."""
        if not self._redis:
            return
        try:
            await self._redis.delete(f"session:{session_id}")
        except Exception as exc:
            logger.warning("redis.delete_session_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #

    async def increment_rate(self, key: str, window: int = 60) -> int:
        """Increment rate counter with sliding window.

        Returns the current count after increment.
        """
        if not self._redis:
            return 0
        try:
            redis_key = f"rate:{key}"
            pipe = self._redis.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window)
            results = await pipe.execute()
            return results[0]  # The INCR result
        except Exception as exc:
            logger.warning("redis.increment_rate_failed", error=str(exc))
            return 0

    async def get_rate(self, key: str) -> int:
        """Get current rate count."""
        if not self._redis:
            return 0
        try:
            val = await self._redis.get(f"rate:{key}")
            return int(val) if val else 0
        except Exception as exc:
            logger.warning("redis.get_rate_failed", error=str(exc))
            return 0

    # ------------------------------------------------------------------ #
    # Tool result cache
    # ------------------------------------------------------------------ #

    async def cache_tool_result(
        self, tool_name: str, params_hash: str, result: dict, ttl: int = 300
    ) -> None:
        """Cache tool results to avoid duplicate MCAPI calls."""
        if not self._redis:
            return
        try:
            key = f"tool:{tool_name}:{params_hash}"
            await self._redis.setex(key, ttl, json.dumps(result, default=str))
        except Exception as exc:
            logger.warning("redis.cache_tool_result_failed", error=str(exc))

    async def get_cached_tool_result(
        self, tool_name: str, params_hash: str
    ) -> Optional[dict]:
        """Get cached tool result."""
        if not self._redis:
            return None
        try:
            key = f"tool:{tool_name}:{params_hash}"
            data = await self._redis.get(key)
            return json.loads(data) if data else None
        except Exception as exc:
            logger.warning("redis.get_cached_tool_result_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------ #
    # Generic get/set
    # ------------------------------------------------------------------ #

    async def get(self, key: str) -> Optional[str]:
        """Generic get."""
        if not self._redis:
            return None
        try:
            return await self._redis.get(key)
        except Exception:
            return None

    async def set(self, key: str, value: str, ttl: int = None) -> None:
        """Generic set with optional TTL."""
        if not self._redis:
            return
        try:
            if ttl:
                await self._redis.setex(key, ttl, value)
            else:
                await self._redis.set(key, value)
        except Exception:
            pass

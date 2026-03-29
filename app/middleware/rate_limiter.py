"""HTTP-level rate limiting middleware using sliding window."""

import time
from collections import defaultdict
from typing import Dict, List, Tuple
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# Role-based rate limits (requests per window)
ROLE_LIMITS = {
    "anonymous": 30,
    "agent": 60,
    "seller": 60,
    "support_agent": 120,
    "support_admin": 120,
    "supervisor": 120,
    "admin": 300,
}

# Chat endpoints get stricter limits
CHAT_LIMITS = {
    "anonymous": 10,
    "agent": 20,
    "seller": 20,
    "support_agent": 40,
    "support_admin": 40,
    "supervisor": 40,
    "admin": 100,
}

# Paths exempt from rate limiting
EXEMPT_PREFIXES = ("/cosmos/health", "/cosmos/metrics")


class HTTPRateLimiter(BaseHTTPMiddleware):
    """IP and user-based rate limiting.

    Limits:
    - Anonymous: 30 req/min per IP
    - Authenticated agent: 60 req/min per user
    - Authenticated supervisor+: 120 req/min per user
    - Admin: 300 req/min per user
    - /chat endpoints: separate stricter limit (20 req/min for agents)

    Exempt: /health/*, /metrics
    """

    def __init__(self, app, default_limit: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self._default_limit = default_limit
        self._window = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip exempt paths
        path = request.url.path
        if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES) or path in (
            "/cosmos/metrics",
        ):
            return await call_next(request)

        # Determine identity and limit
        key, limit = self._get_rate_key(request)

        # Sliding window check
        now = time.time()
        self._cleanup(key, now)

        if len(self._requests[key]) >= limit:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "retry_after": self._window},
                headers={"Retry-After": str(self._window)},
            )

        self._requests[key].append(now)
        response = await call_next(request)

        # Add rate limit headers
        remaining = limit - len(self._requests[key])
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(int(now + self._window))

        return response

    def _get_rate_key(self, request: Request) -> Tuple[str, int]:
        """Extract rate limit key and limit from request.

        Tries to extract user info from X-User-Id and X-User-Role headers
        (set by upstream auth). Falls back to IP-based limiting.
        """
        user_id = request.headers.get("X-User-Id")
        role = request.headers.get("X-User-Role", "anonymous")

        is_chat = "/chat" in request.url.path

        if user_id:
            key = f"user:{user_id}"
            if is_chat:
                limit = CHAT_LIMITS.get(role, CHAT_LIMITS["agent"])
            else:
                limit = ROLE_LIMITS.get(role, self._default_limit)
        else:
            # Anonymous — use client IP
            client_host = request.client.host if request.client else "unknown"
            key = f"ip:{client_host}"
            if is_chat:
                limit = CHAT_LIMITS["anonymous"]
            else:
                limit = ROLE_LIMITS["anonymous"]

        return key, limit

    def _cleanup(self, key: str, now: float) -> None:
        """Remove requests outside the window."""
        cutoff = now - self._window
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]

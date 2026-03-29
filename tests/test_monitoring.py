"""Tests for Prometheus metrics, MetricsMiddleware, and HTTPRateLimiter."""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock

from app.monitoring.metrics import (
    Counter,
    Gauge,
    Histogram,
    METRICS,
    collect_all,
    init_metrics,
)


# ------------------------------------------------------------------ #
# Counter Tests
# ------------------------------------------------------------------ #


class TestCounter:
    def test_inc_default(self):
        c = Counter("test_counter", "A test counter")
        c.inc()
        assert c.get() == 1.0

    def test_inc_custom_value(self):
        c = Counter("test_counter", "A test counter")
        c.inc(5.0)
        assert c.get() == 5.0

    def test_inc_with_labels(self):
        c = Counter("http_total", "HTTP requests", ["method", "status"])
        c.inc(method="GET", status="200")
        c.inc(method="GET", status="200")
        c.inc(method="POST", status="201")
        assert c.get(method="GET", status="200") == 2.0
        assert c.get(method="POST", status="201") == 1.0
        assert c.get(method="DELETE", status="404") == 0.0

    def test_get_missing_label_returns_zero(self):
        c = Counter("test_counter", "A test counter", ["method"])
        assert c.get(method="PUT") == 0.0

    def test_collect_format_no_labels(self):
        c = Counter("simple_total", "Simple counter")
        c.inc(3.0)
        output = c.collect()
        assert "# HELP simple_total Simple counter" in output
        assert "# TYPE simple_total counter" in output
        assert "simple_total 3.0" in output

    def test_collect_format_with_labels(self):
        c = Counter("http_total", "HTTP requests", ["method"])
        c.inc(method="GET")
        output = c.collect()
        assert '# TYPE http_total counter' in output
        assert 'http_total{method="GET"} 1.0' in output

    def test_collect_empty(self):
        c = Counter("empty_total", "Empty counter")
        output = c.collect()
        assert "# HELP empty_total" in output
        assert "# TYPE empty_total counter" in output
        # No value lines
        lines = output.strip().split("\n")
        assert len(lines) == 2


# ------------------------------------------------------------------ #
# Histogram Tests
# ------------------------------------------------------------------ #


class TestHistogram:
    def test_observe(self):
        h = Histogram("latency", "Request latency")
        h.observe(0.05)
        h.observe(0.15)
        obs = h.get_observations()
        assert len(obs) == 2
        assert 0.05 in obs
        assert 0.15 in obs

    def test_observe_with_labels(self):
        h = Histogram("latency", "Request latency", ["method"])
        h.observe(0.1, method="GET")
        h.observe(0.2, method="POST")
        assert len(h.get_observations(method="GET")) == 1
        assert len(h.get_observations(method="POST")) == 1

    def test_collect_bucket_counts(self):
        h = Histogram("latency", "Latency", buckets=[0.1, 0.5, 1.0])
        h.observe(0.05)
        h.observe(0.3)
        h.observe(0.8)
        h.observe(2.0)
        output = h.collect()
        # 0.05 <= 0.1, so bucket 0.1 count = 1
        assert 'latency_bucket{le="0.1"} 1' in output
        # 0.05, 0.3 <= 0.5, so bucket 0.5 count = 2
        assert 'latency_bucket{le="0.5"} 2' in output
        # 0.05, 0.3, 0.8 <= 1.0, so bucket 1.0 count = 3
        assert 'latency_bucket{le="1.0"} 3' in output
        # +Inf bucket = 4
        assert 'latency_bucket{le="+Inf"} 4' in output
        # Sum and count
        assert "latency_count 4" in output

    def test_collect_format_header(self):
        h = Histogram("test_hist", "Test histogram")
        output = h.collect()
        assert "# HELP test_hist Test histogram" in output
        assert "# TYPE test_hist histogram" in output

    def test_collect_empty(self):
        h = Histogram("empty_hist", "Empty histogram")
        output = h.collect()
        lines = output.strip().split("\n")
        assert len(lines) == 2  # Only HELP and TYPE

    def test_custom_buckets(self):
        h = Histogram("loops", "Loops", buckets=[1, 2, 3])
        h.observe(1)
        h.observe(2)
        h.observe(3)
        output = h.collect()
        assert 'le="1"' in output
        assert 'le="2"' in output
        assert 'le="3"' in output


# ------------------------------------------------------------------ #
# Gauge Tests
# ------------------------------------------------------------------ #


class TestGauge:
    def test_set(self):
        g = Gauge("active_sessions", "Active sessions")
        g.set(5.0)
        assert g.get() == 5.0

    def test_inc(self):
        g = Gauge("active_sessions", "Active sessions")
        g.set(3.0)
        g.inc()
        assert g.get() == 4.0

    def test_inc_custom(self):
        g = Gauge("active_sessions", "Active sessions")
        g.inc(10.0)
        assert g.get() == 10.0

    def test_dec(self):
        g = Gauge("active_sessions", "Active sessions")
        g.set(5.0)
        g.dec()
        assert g.get() == 4.0

    def test_dec_custom(self):
        g = Gauge("active_sessions", "Active sessions")
        g.set(10.0)
        g.dec(3.0)
        assert g.get() == 7.0

    def test_collect_format(self):
        g = Gauge("active", "Active count")
        g.set(42.0)
        output = g.collect()
        assert "# HELP active Active count" in output
        assert "# TYPE active gauge" in output
        assert "active 42.0" in output

    def test_collect_with_labels(self):
        g = Gauge("connections", "Connections", ["pool"])
        g.set(10.0, pool="primary")
        g.set(5.0, pool="replica")
        output = g.collect()
        assert 'connections{pool="primary"} 10.0' in output
        assert 'connections{pool="replica"} 5.0' in output

    def test_collect_empty(self):
        g = Gauge("empty_gauge", "Empty gauge")
        output = g.collect()
        lines = output.strip().split("\n")
        assert len(lines) == 2


# ------------------------------------------------------------------ #
# init_metrics / collect_all
# ------------------------------------------------------------------ #


class TestMetricsInit:
    def test_init_metrics_populates_registry(self):
        init_metrics()
        assert "cosmos_requests_total" in METRICS
        assert "cosmos_request_duration_seconds" in METRICS
        assert "cosmos_react_queries_total" in METRICS
        assert "cosmos_llm_calls_total" in METRICS
        assert "cosmos_active_sessions" in METRICS
        assert "cosmos_mars_requests_total" in METRICS

    def test_collect_all_returns_string(self):
        init_metrics()
        output = collect_all()
        assert isinstance(output, str)
        assert "cosmos_requests_total" in output

    def test_collect_all_includes_all_metrics(self):
        init_metrics()
        output = collect_all()
        for key in METRICS:
            assert key in output


# ------------------------------------------------------------------ #
# MetricsMiddleware Tests
# ------------------------------------------------------------------ #


class TestMetricsMiddleware:
    @pytest.mark.asyncio
    async def test_records_request_counter(self):
        """Middleware should increment cosmos_requests_total."""
        init_metrics()
        from app.middleware.metrics import MetricsMiddleware

        # Build a mock ASGI app and request
        mock_app = AsyncMock()
        middleware = MetricsMiddleware(mock_app)

        request = MagicMock()
        request.method = "GET"
        request.url.path = "/cosmos/api/v1/tools"

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        response = await middleware.dispatch(request, mock_call_next)
        assert response.status_code == 200

        count = METRICS["cosmos_requests_total"].get(
            method="GET", endpoint="/cosmos/api/v1/tools", status="200"
        )
        assert count >= 1.0

    @pytest.mark.asyncio
    async def test_records_request_duration(self):
        """Middleware should observe request duration."""
        init_metrics()
        from app.middleware.metrics import MetricsMiddleware

        mock_app = AsyncMock()
        middleware = MetricsMiddleware(mock_app)

        request = MagicMock()
        request.method = "POST"
        request.url.path = "/cosmos/api/v1/chat"

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        await middleware.dispatch(request, mock_call_next)

        obs = METRICS["cosmos_request_duration_seconds"].get_observations(
            method="POST", endpoint="/cosmos/api/v1/chat"
        )
        assert len(obs) >= 1
        assert all(v >= 0 for v in obs)

    @pytest.mark.asyncio
    async def test_skips_metrics_endpoint(self):
        """Middleware should not record metrics for /cosmos/metrics itself."""
        init_metrics()
        from app.middleware.metrics import MetricsMiddleware

        mock_app = AsyncMock()
        middleware = MetricsMiddleware(mock_app)

        request = MagicMock()
        request.method = "GET"
        request.url.path = "/cosmos/metrics"

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        await middleware.dispatch(request, mock_call_next)

        count = METRICS["cosmos_requests_total"].get(
            method="GET", endpoint="/cosmos/metrics", status="200"
        )
        assert count == 0.0


# ------------------------------------------------------------------ #
# HTTPRateLimiter Tests
# ------------------------------------------------------------------ #


class TestHTTPRateLimiter:
    def _make_request(self, path="/cosmos/api/v1/tools", host="1.2.3.4",
                      user_id=None, user_role=None):
        request = MagicMock()
        request.url.path = path
        request.method = "GET"
        request.client = MagicMock()
        request.client.host = host
        headers = {}
        if user_id:
            headers["X-User-Id"] = str(user_id)
        if user_role:
            headers["X-User-Role"] = user_role
        request.headers = headers
        return request

    @pytest.mark.asyncio
    async def test_allows_under_limit(self):
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=5, window_seconds=60)

        request = self._make_request()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        # Temporarily set anonymous limit low for testing
        orig = ROLE_LIMITS["anonymous"]
        ROLE_LIMITS["anonymous"] = 3
        try:
            limiter = HTTPRateLimiter(mock_app, default_limit=3, window_seconds=60)

            request = self._make_request()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}

            async def mock_call_next(req):
                return mock_response

            # Use up the limit
            for _ in range(3):
                await limiter.dispatch(request, mock_call_next)

            # 4th request should be blocked
            response = await limiter.dispatch(request, mock_call_next)
            assert response.status_code == 429
        finally:
            ROLE_LIMITS["anonymous"] = orig

    @pytest.mark.asyncio
    async def test_429_response_format(self):
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        orig = ROLE_LIMITS["anonymous"]
        ROLE_LIMITS["anonymous"] = 1
        try:
            limiter = HTTPRateLimiter(mock_app, default_limit=1, window_seconds=60)

            request = self._make_request()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}

            async def mock_call_next(req):
                return mock_response

            await limiter.dispatch(request, mock_call_next)
            response = await limiter.dispatch(request, mock_call_next)

            assert response.status_code == 429
            body = response.body.decode()
            assert "Rate limit exceeded" in body
            assert "retry_after" in body
        finally:
            ROLE_LIMITS["anonymous"] = orig

    @pytest.mark.asyncio
    async def test_rate_limit_headers(self):
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=10, window_seconds=60)

        request = self._make_request()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    @pytest.mark.asyncio
    async def test_exempt_health_path(self):
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        orig = ROLE_LIMITS["anonymous"]
        ROLE_LIMITS["anonymous"] = 1
        limiter = HTTPRateLimiter(mock_app, default_limit=1, window_seconds=60)

        request = self._make_request(path="/cosmos/health")
        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        # Should never be blocked (health is exempt)
        try:
            for _ in range(5):
                response = await limiter.dispatch(request, mock_call_next)
                assert response.status_code == 200
        finally:
            ROLE_LIMITS["anonymous"] = orig

    @pytest.mark.asyncio
    async def test_exempt_metrics_path(self):
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        orig = ROLE_LIMITS["anonymous"]
        ROLE_LIMITS["anonymous"] = 1
        limiter = HTTPRateLimiter(mock_app, default_limit=1, window_seconds=60)

        request = self._make_request(path="/cosmos/metrics")
        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        try:
            for _ in range(5):
                response = await limiter.dispatch(request, mock_call_next)
                assert response.status_code == 200
        finally:
            ROLE_LIMITS["anonymous"] = orig

    @pytest.mark.asyncio
    async def test_anonymous_limit(self):
        """Anonymous users should be limited to 30 req/min."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=60, window_seconds=60)

        request = self._make_request()  # No user_id = anonymous
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        # First request should set limit header to 30
        response = await limiter.dispatch(request, mock_call_next)
        assert response.headers["X-RateLimit-Limit"] == "30"

    @pytest.mark.asyncio
    async def test_agent_limit(self):
        """Authenticated agents should get 60 req/min."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=60, window_seconds=60)

        request = self._make_request(user_id=42, user_role="agent")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert response.headers["X-RateLimit-Limit"] == "60"

    @pytest.mark.asyncio
    async def test_supervisor_limit(self):
        """Supervisors should get 120 req/min."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=60, window_seconds=60)

        request = self._make_request(user_id=99, user_role="supervisor")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert response.headers["X-RateLimit-Limit"] == "120"

    @pytest.mark.asyncio
    async def test_admin_limit(self):
        """Admins should get 300 req/min."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=60, window_seconds=60)

        request = self._make_request(user_id=1, user_role="admin")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert response.headers["X-RateLimit-Limit"] == "300"

    @pytest.mark.asyncio
    async def test_chat_endpoint_stricter_limit(self):
        """Chat endpoints should have stricter limits for agents."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=60, window_seconds=60)

        request = self._make_request(
            path="/cosmos/api/v1/chat", user_id=42, user_role="agent"
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        response = await limiter.dispatch(request, mock_call_next)
        assert response.headers["X-RateLimit-Limit"] == "20"

    @pytest.mark.asyncio
    async def test_sliding_window_cleanup(self):
        """Old requests outside the window should be cleaned up."""
        from app.middleware.rate_limiter import HTTPRateLimiter

        mock_app = AsyncMock()
        limiter = HTTPRateLimiter(mock_app, default_limit=2, window_seconds=60)

        # Manually inject old timestamps
        key = "ip:1.2.3.4"
        now = time.time()
        limiter._requests[key] = [now - 120, now - 90]  # Both outside window

        request = self._make_request()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        # Should succeed because old entries are cleaned
        response = await limiter.dispatch(request, mock_call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_different_ips_independent(self):
        """Different IPs should have independent rate limits."""
        from app.middleware.rate_limiter import HTTPRateLimiter, ROLE_LIMITS

        mock_app = AsyncMock()
        orig = ROLE_LIMITS["anonymous"]
        ROLE_LIMITS["anonymous"] = 1
        try:
            limiter = HTTPRateLimiter(mock_app, default_limit=1, window_seconds=60)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}

            async def mock_call_next(req):
                return mock_response

            req1 = self._make_request(host="10.0.0.1")
            req2 = self._make_request(host="10.0.0.2")

            resp1 = await limiter.dispatch(req1, mock_call_next)
            resp2 = await limiter.dispatch(req2, mock_call_next)

            assert resp1.status_code == 200
            assert resp2.status_code == 200
        finally:
            ROLE_LIMITS["anonymous"] = orig


# ------------------------------------------------------------------ #
# Prometheus Text Format Parseable
# ------------------------------------------------------------------ #


class TestPrometheusFormat:
    def test_full_output_parseable(self):
        """The full collect_all() output should be valid Prometheus text format."""
        init_metrics()
        # Generate some data
        METRICS["cosmos_requests_total"].inc(method="GET", endpoint="/test", status="200")
        METRICS["cosmos_request_duration_seconds"].observe(0.05, method="GET", endpoint="/test")
        METRICS["cosmos_active_sessions"].set(3.0)

        output = collect_all()
        lines = output.strip().split("\n")

        for line in lines:
            if not line:
                continue
            # Each line should be a comment (#) or a metric line
            assert line.startswith("#") or line.startswith("cosmos_"), (
                f"Unexpected line format: {line!r}"
            )

    def test_counter_prometheus_format(self):
        c = Counter("test_prom", "Test", ["env"])
        c.inc(env="prod")
        output = c.collect()
        # Should have HELP, TYPE, and value line
        assert output.count("# HELP") == 1
        assert output.count("# TYPE") == 1
        assert "counter" in output
        assert 'env="prod"' in output

    def test_histogram_sum_and_count(self):
        h = Histogram("req_dur", "Duration", buckets=[0.5, 1.0])
        h.observe(0.3)
        h.observe(0.7)
        output = h.collect()
        assert "req_dur_sum" in output
        assert "req_dur_count 2" in output

"""Tests for MCAPIClient."""

import pytest
import httpx
from app.clients.mcapi import MCAPIClient, MCAPIError, MCAPIResponse


def _mock_transport(status_code: int = 200, json_body: dict = None):
    """Return an httpx.MockTransport that always returns the given status/body."""
    import json as _json

    body = _json.dumps(json_body or {}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def _make_client(transport: httpx.MockTransport, base_url: str = "https://mcapi.test") -> MCAPIClient:
    """Create an MCAPIClient wired to a mock transport."""
    client = MCAPIClient(base_url=base_url)
    client._client = httpx.AsyncClient(transport=transport, base_url=base_url)
    return client


# ------------------------------------------------------------------ #
# URL construction
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_order_url():
    """get_order should hit /v1/orders/{order_id}."""
    captured_url = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"id": "ORD-123"})

    client = _make_client(httpx.MockTransport(handler))
    resp = await client.get_order("ORD-123")

    assert resp.success is True
    assert resp.status_code == 200
    assert "/v1/orders/ORD-123" in captured_url
    await client.close()


@pytest.mark.asyncio
async def test_track_shipment_url():
    """track_shipment should hit /v1/tracking/{awb}."""
    captured_url = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"awb": "AWB999"})

    client = _make_client(httpx.MockTransport(handler))
    resp = await client.track_shipment("AWB999")

    assert resp.success is True
    assert "/v1/tracking/AWB999" in captured_url
    await client.close()


# ------------------------------------------------------------------ #
# Query params
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_orders_with_filters():
    """get_orders should build correct query parameters."""
    captured_url = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"orders": []})

    client = _make_client(httpx.MockTransport(handler))
    await client.get_orders(
        company_id=42,
        status=["shipped", "delivered"],
        date_from="2025-01-01",
        date_to="2025-01-31",
        limit=10,
    )

    assert "company_id=42" in captured_url
    assert "status=shipped%2Cdelivered" in captured_url or "status=shipped,delivered" in captured_url
    assert "date_from=2025-01-01" in captured_url
    assert "date_to=2025-01-31" in captured_url
    assert "limit=10" in captured_url
    await client.close()


# ------------------------------------------------------------------ #
# Error handling
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_error_404():
    """404 should raise MCAPIError with status_code=404."""
    client = _make_client(_mock_transport(status_code=404))
    with pytest.raises(MCAPIError) as exc_info:
        await client.get_order("MISSING")
    assert exc_info.value.status_code == 404
    await client.close()


@pytest.mark.asyncio
async def test_error_429():
    """429 should raise MCAPIError for rate limiting."""
    client = _make_client(_mock_transport(status_code=429))
    with pytest.raises(MCAPIError) as exc_info:
        await client.get_order("RATE")
    assert exc_info.value.status_code == 429
    assert "Rate limit" in exc_info.value.message
    await client.close()


@pytest.mark.asyncio
async def test_error_500():
    """5xx should raise MCAPIError for server errors."""
    client = _make_client(_mock_transport(status_code=502))
    with pytest.raises(MCAPIError) as exc_info:
        await client.get_order("ERR")
    assert exc_info.value.status_code == 502
    await client.close()


# ------------------------------------------------------------------ #
# Latency tracking
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_response_includes_latency():
    """Every response should include latency_ms >= 0."""
    client = _make_client(_mock_transport(200, {"ok": True}))
    resp = await client.get_order("X")
    assert resp.latency_ms >= 0
    await client.close()

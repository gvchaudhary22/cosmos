"""Async HTTP client for Shiprocket's MCAPI (internal REST API)."""

import time
import httpx
import structlog
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class MCAPIResponse:
    success: bool
    data: Any
    status_code: int
    latency_ms: float


class MCAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MCAPIClient:
    """Async client for Shiprocket MCAPI with rate limiting and error handling."""

    def __init__(self, base_url: str, timeout: float = 10.0, rate_limit: int = 100):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit = rate_limit
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def close(self):
        if self._client:
            await self._client.aclose()

    def _ensure_client(self):
        if self._client is None:
            raise MCAPIError("Client not started. Call start() first.", status_code=0)

    async def get(
        self, path: str, params: Dict = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Execute GET request with logging and error handling."""
        self._ensure_client()

        log = logger.bind(method="GET", path=path, params=params)
        log.info("mcapi_request_start")

        start = time.monotonic()
        try:
            response = await self._client.get(path, params=params, headers=headers)
            latency_ms = round((time.monotonic() - start) * 1000, 2)

            log.info(
                "mcapi_request_complete",
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

            if response.status_code == 401:
                raise MCAPIError("Unauthorized", status_code=401)
            if response.status_code == 403:
                raise MCAPIError("Forbidden", status_code=403)
            if response.status_code == 404:
                raise MCAPIError(f"Not found: {path}", status_code=404)
            if response.status_code == 429:
                raise MCAPIError("Rate limit exceeded", status_code=429)
            if response.status_code >= 500:
                raise MCAPIError(
                    f"Server error: {response.status_code}",
                    status_code=response.status_code,
                )

            data = response.json() if response.content else None
            return MCAPIResponse(
                success=True,
                data=data,
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        except httpx.HTTPError as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.error("mcapi_request_error", error=str(exc), latency_ms=latency_ms)
            raise MCAPIError(f"HTTP error: {exc}", status_code=0) from exc

    async def post(
        self, path: str, json: Dict = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Execute POST request with logging and error handling."""
        self._ensure_client()

        log = logger.bind(method="POST", path=path)
        log.info("mcapi_request_start")

        start = time.monotonic()
        try:
            response = await self._client.post(path, json=json, headers=headers)
            latency_ms = round((time.monotonic() - start) * 1000, 2)

            log.info(
                "mcapi_request_complete",
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

            if response.status_code == 401:
                raise MCAPIError("Unauthorized", status_code=401)
            if response.status_code == 403:
                raise MCAPIError("Forbidden", status_code=403)
            if response.status_code == 404:
                raise MCAPIError(f"Not found: {path}", status_code=404)
            if response.status_code == 429:
                raise MCAPIError("Rate limit exceeded", status_code=429)
            if response.status_code >= 500:
                raise MCAPIError(
                    f"Server error: {response.status_code}",
                    status_code=response.status_code,
                )

            data = response.json() if response.content else None
            return MCAPIResponse(
                success=True,
                data=data,
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        except httpx.HTTPError as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.error("mcapi_request_error", error=str(exc), latency_ms=latency_ms)
            raise MCAPIError(f"HTTP error: {exc}", status_code=0) from exc

    async def put(
        self, path: str, json: Dict = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Execute PUT request with logging and error handling."""
        self._ensure_client()

        log = logger.bind(method="PUT", path=path)
        log.info("mcapi_request_start")

        start = time.monotonic()
        try:
            response = await self._client.put(path, json=json, headers=headers)
            latency_ms = round((time.monotonic() - start) * 1000, 2)

            log.info(
                "mcapi_request_complete",
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

            if response.status_code == 401:
                raise MCAPIError("Unauthorized", status_code=401)
            if response.status_code == 403:
                raise MCAPIError("Forbidden", status_code=403)
            if response.status_code == 404:
                raise MCAPIError(f"Not found: {path}", status_code=404)
            if response.status_code == 429:
                raise MCAPIError("Rate limit exceeded", status_code=429)
            if response.status_code >= 500:
                raise MCAPIError(
                    f"Server error: {response.status_code}",
                    status_code=response.status_code,
                )

            data = response.json() if response.content else None
            return MCAPIResponse(
                success=True,
                data=data,
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        except httpx.HTTPError as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.error("mcapi_request_error", error=str(exc), latency_ms=latency_ms)
            raise MCAPIError(f"HTTP error: {exc}", status_code=0) from exc

    # ------------------------------------------------------------------ #
    # Pre-built methods for ICRM endpoints
    # ------------------------------------------------------------------ #

    async def get_order(
        self, order_id: str, headers: Dict = None
    ) -> MCAPIResponse:
        """Fetch a single order by ID."""
        return await self.get(f"/v1/orders/{order_id}", headers=headers)

    async def get_orders(
        self,
        company_id: int,
        status: List[str] = None,
        date_from: str = None,
        date_to: str = None,
        limit: int = 50,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """Fetch orders for a company with optional filters."""
        params: Dict[str, Any] = {"company_id": company_id, "limit": limit}
        if status:
            params["status"] = ",".join(status)
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return await self.get("/v1/orders", params=params, headers=headers)

    async def search_orders(
        self,
        query: str,
        filters: Dict = None,
        limit: int = 50,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """Search orders by query string with optional filters."""
        params: Dict[str, Any] = {"q": query, "limit": limit}
        if filters:
            params.update(filters)
        return await self.get("/v1/orders/search", params=params, headers=headers)

    async def track_shipment(
        self, awb: str, headers: Dict = None
    ) -> MCAPIResponse:
        """Track a shipment by AWB number."""
        return await self.get(f"/v1/tracking/{awb}", headers=headers)

    async def get_tracking_timeline(
        self, awb: str, headers: Dict = None
    ) -> MCAPIResponse:
        """Get full tracking timeline for a shipment."""
        return await self.get(f"/v1/tracking/{awb}/timeline", headers=headers)

    async def get_ndr_list(
        self,
        company_id: int,
        status: str = None,
        date_from: str = None,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """List NDRs for a company."""
        params: Dict[str, Any] = {"company_id": company_id}
        if status:
            params["status"] = status
        if date_from:
            params["date_from"] = date_from
        return await self.get("/v1/ndr", params=params, headers=headers)

    async def get_ndr_details(
        self, ndr_id: str, headers: Dict = None
    ) -> MCAPIResponse:
        """Get details for a specific NDR."""
        return await self.get(f"/v1/ndr/{ndr_id}", headers=headers)

    async def get_seller_info(
        self,
        company_id: int = None,
        email: str = None,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """Look up seller info by company ID or email."""
        params: Dict[str, Any] = {}
        if company_id is not None:
            params["company_id"] = company_id
        if email:
            params["email"] = email
        return await self.get("/v1/sellers", params=params, headers=headers)

    async def get_seller_plan(
        self, company_id: int, headers: Dict = None
    ) -> MCAPIResponse:
        """Get the subscription plan for a seller."""
        return await self.get(
            f"/v1/sellers/{company_id}/plan", headers=headers
        )

    async def get_seller_health(
        self, company_id: int, headers: Dict = None
    ) -> MCAPIResponse:
        """Get seller health / performance metrics."""
        return await self.get(
            f"/v1/sellers/{company_id}/health", headers=headers
        )

    async def get_billing(
        self,
        company_id: int,
        query_type: str = "invoices",
        date_range: str = None,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """Get billing information (invoices, statements, etc.)."""
        params: Dict[str, Any] = {
            "company_id": company_id,
            "type": query_type,
        }
        if date_range:
            params["date_range"] = date_range
        return await self.get("/v1/billing", params=params, headers=headers)

    async def get_wallet_balance(
        self, company_id: int, headers: Dict = None
    ) -> MCAPIResponse:
        """Get wallet balance for a company."""
        return await self.get(
            f"/v1/billing/{company_id}/wallet", headers=headers
        )

    async def get_transactions(
        self,
        company_id: int,
        limit: int = 50,
        offset: int = 0,
        headers: Dict = None,
    ) -> MCAPIResponse:
        """Get wallet transactions for a company."""
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        return await self.get(
            f"/v1/billing/{company_id}/transactions",
            params=params,
            headers=headers,
        )

    # ------------------------------------------------------------------ #
    # Write / Action endpoints (Phase 2)
    # ------------------------------------------------------------------ #

    async def cancel_order(
        self, order_id: str, reason: str = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Cancel an order."""
        payload: Dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        return await self.post(f"/v1/orders/{order_id}/cancel", json=payload, headers=headers)

    async def initiate_refund(
        self, order_id: str, amount: float = None, reason: str = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Initiate a refund for an order."""
        payload: Dict[str, Any] = {}
        if amount is not None:
            payload["amount"] = amount
        if reason:
            payload["reason"] = reason
        return await self.post(f"/v1/orders/{order_id}/refund", json=payload, headers=headers)

    async def reattempt_delivery(
        self, awb: str, preferred_date: str = None, instructions: str = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Reattempt delivery for an NDR shipment."""
        payload: Dict[str, Any] = {}
        if preferred_date:
            payload["preferred_date"] = preferred_date
        if instructions:
            payload["instructions"] = instructions
        return await self.post(f"/v1/shipments/{awb}/reattempt", json=payload, headers=headers)

    async def update_address(
        self, order_id: str, address: Dict, headers: Dict = None
    ) -> MCAPIResponse:
        """Update delivery address for an order."""
        return await self.put(f"/v1/orders/{order_id}/address", json=address, headers=headers)

    async def escalate_to_supervisor(
        self, subject: str, description: str, priority: str = "medium",
        related_ids: Dict = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Create an escalation ticket."""
        payload: Dict[str, Any] = {
            "subject": subject,
            "description": description,
            "priority": priority,
        }
        if related_ids:
            payload["related_ids"] = related_ids
        return await self.post("/v1/escalations", json=payload, headers=headers)

    async def block_seller(
        self, seller_id: str, reason: str, headers: Dict = None
    ) -> MCAPIResponse:
        """Block a seller account."""
        return await self.post(
            f"/v1/sellers/{seller_id}/block",
            json={"reason": reason},
            headers=headers,
        )

    async def issue_wallet_credit(
        self, seller_id: str, amount: float, reason: str = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Credit a seller's wallet."""
        payload: Dict[str, Any] = {"amount": amount}
        if reason:
            payload["reason"] = reason
        return await self.post(f"/v1/wallets/{seller_id}/credit", json=payload, headers=headers)

    async def reassign_courier(
        self, awb: str, courier_id: str, reason: str = None, headers: Dict = None
    ) -> MCAPIResponse:
        """Reassign a shipment to a different courier."""
        payload: Dict[str, Any] = {"courier_id": courier_id}
        if reason:
            payload["reason"] = reason
        return await self.post(f"/v1/shipments/{awb}/reassign", json=payload, headers=headers)

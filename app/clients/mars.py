"""Async HTTP client for MARS Go backend (port 8080).

MARS handles 80% of ICRM queries with rules (zero AI cost).
COSMOS is called only for the 20% needing AI reasoning.

Flow: ICRM -> MARS (rule check) -> if needs_ai -> COSMOS -> response
"""

import time
import httpx
import structlog
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = structlog.get_logger()


@dataclass
class MarsResponse:
    success: bool
    data: Any
    error: Optional[str] = None
    status_code: int = 200


class MarsError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MarsClient:
    """Async HTTP client for MARS Go backend.

    MARS handles 80% of ICRM queries with rules (zero AI cost).
    COSMOS is called only for the 20% needing AI reasoning.

    Flow: ICRM -> MARS (rule check) -> if needs_ai -> COSMOS -> response
    """

    def __init__(self, base_url: str = "http://localhost:8080", timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Dict = None) -> MarsResponse:
        """Execute GET request with logging and error handling."""
        client = await self._get_client()
        log = logger.bind(method="GET", path=path)
        log.info("mars_request_start")

        start = time.monotonic()
        try:
            response = await client.get(path, params=params)
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.info("mars_request_complete", status_code=response.status_code, latency_ms=latency_ms)

            if response.status_code >= 500:
                return MarsResponse(success=False, data=None, error=f"Server error: {response.status_code}", status_code=response.status_code)

            data = response.json() if response.content else None
            return MarsResponse(success=True, data=data, status_code=response.status_code)

        except httpx.HTTPError as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.error("mars_request_error", error=str(exc), latency_ms=latency_ms)
            return MarsResponse(success=False, data=None, error=f"HTTP error: {exc}", status_code=0)

    async def _post(self, path: str, json: Dict = None) -> MarsResponse:
        """Execute POST request with logging and error handling."""
        client = await self._get_client()
        log = logger.bind(method="POST", path=path)
        log.info("mars_request_start")

        start = time.monotonic()
        try:
            response = await client.post(path, json=json)
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.info("mars_request_complete", status_code=response.status_code, latency_ms=latency_ms)

            if response.status_code >= 500:
                return MarsResponse(success=False, data=None, error=f"Server error: {response.status_code}", status_code=response.status_code)

            data = response.json() if response.content else None
            return MarsResponse(success=True, data=data, status_code=response.status_code)

        except httpx.HTTPError as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            log.error("mars_request_error", error=str(exc), latency_ms=latency_ms)
            return MarsResponse(success=False, data=None, error=f"HTTP error: {exc}", status_code=0)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if MARS is reachable."""
        try:
            resp = await self._get("/health")
            return resp.success and resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Safety Gate
    # ------------------------------------------------------------------

    async def check_prompt_safety(self, text: str) -> dict:
        """POST /api/v1/safety/check -- Run MARS prompt safety check before AI processing.

        Returns: {"safe": bool, "score": float, "flags": [...]}
        """
        resp = await self._post("/api/v1/safety/check", json={"text": text})
        if resp.success and resp.data:
            return resp.data
        return {"safe": True, "score": 0.0, "flags": [], "error": resp.error}

    # ------------------------------------------------------------------
    # Intent Pre-Classification
    # ------------------------------------------------------------------

    async def classify_intent(self, text: str, company_id: str) -> dict:
        """POST /api/v1/chat/classify -- Ask MARS to classify intent with rules first.

        MARS uses its rule-based classifier (zero AI cost).
        Returns: {"intent": str, "entity": str, "entity_id": str,
                  "confidence": float, "needs_cosmos": bool}

        If needs_cosmos=false, MARS handled it with rules. COSMOS should not process.
        If needs_cosmos=true, COSMOS takes over for AI reasoning.
        """
        resp = await self._post(
            "/api/v1/chat/classify",
            json={"text": text, "company_id": company_id},
        )
        if resp.success and resp.data:
            return resp.data
        # Default: assume COSMOS needs to handle it
        return {
            "intent": "unknown",
            "entity": "unknown",
            "entity_id": None,
            "confidence": 0.0,
            "needs_cosmos": True,
            "error": resp.error,
        }

    # ------------------------------------------------------------------
    # State Persistence
    # ------------------------------------------------------------------

    async def save_state(self, session_id: str, state: dict) -> dict:
        """POST /api/v1/state -- Save COSMOS session state to MARS for cross-session recovery."""
        resp = await self._post(
            "/api/v1/state",
            json={"session_id": session_id, "state": state},
        )
        if resp.success and resp.data:
            return resp.data
        return {"saved": False, "error": resp.error}

    async def resume_state(self, session_id: str) -> Optional[dict]:
        """POST /api/v1/state/resume -- Resume state from MARS."""
        resp = await self._post(
            "/api/v1/state/resume",
            json={"session_id": session_id},
        )
        if resp.success and resp.data:
            return resp.data
        return None

    # ------------------------------------------------------------------
    # Ticket Integration
    # ------------------------------------------------------------------

    async def create_escalation_ticket(
        self, session_id: str, reason: str, context: dict
    ) -> dict:
        """POST /api/v1/tickets -- Create escalation ticket in MARS when COSMOS can't handle query."""
        resp = await self._post(
            "/api/v1/tickets",
            json={
                "session_id": session_id,
                "reason": reason,
                "context": context,
            },
        )
        if resp.success and resp.data:
            return resp.data
        return {"created": False, "error": resp.error}

    # ------------------------------------------------------------------
    # Learning Sync
    # ------------------------------------------------------------------

    async def sync_learning(self, records: List[dict]) -> dict:
        """POST /api/v1/learning/records -- Sync distillation records to MARS for cross-platform learning."""
        resp = await self._post(
            "/api/v1/learning/records",
            json={"records": records},
        )
        if resp.success and resp.data:
            return resp.data
        return {"synced": 0, "error": resp.error}

    # ------------------------------------------------------------------
    # Wave Engine
    # ------------------------------------------------------------------

    async def dispatch_wave(self, tasks: List[dict]) -> dict:
        """POST /api/v1/waves -- Dispatch parallel tasks via MARS wave engine."""
        resp = await self._post(
            "/api/v1/waves",
            json={"tasks": tasks},
        )
        if resp.success and resp.data:
            return resp.data
        return {"dispatched": False, "error": resp.error}

    # ------------------------------------------------------------------
    # Token Economics
    # ------------------------------------------------------------------

    async def get_budget_status(self, company_id: str) -> dict:
        """GET /api/v1/admin/budget/{company_id} -- Check token budget from MARS."""
        resp = await self._get(f"/api/v1/admin/budget/{company_id}")
        if resp.success and resp.data:
            return resp.data
        return {"budget_remaining": None, "error": resp.error}

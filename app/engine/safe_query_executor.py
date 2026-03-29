"""
Safe DB Tool — Tier 3 fallback that requests parameterized queries from MARS.

COSMOS does NOT execute SQL directly. Instead:
1. CodebaseIntelligence suggests a template name (e.g., "order_by_id")
2. This tool sends the template + parameters to MARS via gRPC
3. MARS executes on prod slave with all safety rules (EXPLAIN, timeout, etc.)
4. MARS returns structured results back to COSMOS

Templates are pre-defined and whitelisted — no LLM-generated SQL ever hits the DB.

Safety enforced by MARS (Go side):
  - Read replica only
  - Parameterized templates only (no dynamic SQL)
  - Mandatory company_id scoping
  - No SELECT *
  - Hard row cap (100)
  - 1-second KILL timeout
  - EXPLAIN before execute
  - Max 2-table join
  - Whitelisted tables only
  - Query fingerprint logging
  - Denylist for repeated bad query shapes
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class SafeDBResult:
    """Result from a MARS safe DB query."""
    success: bool = False
    data: List[Dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    template_used: str = ""
    execution_time_ms: float = 0.0
    error: Optional[str] = None
    safety_checks: List[Dict[str, Any]] = field(default_factory=list)


class SafeDBTool:
    """
    COSMOS-side client for Tier 3 DB fallback.
    Sends parameterized template requests to MARS via gRPC.
    MARS handles all DB execution with safety rules.
    """

    def __init__(self, cosmos_grpc_client=None, mars_http_url: str = ""):
        """
        Args:
            cosmos_grpc_client: gRPC client to MARS (if available)
            mars_http_url: Fallback HTTP URL to MARS safe-query endpoint
        """
        self.grpc_client = cosmos_grpc_client
        self.mars_http_url = mars_http_url.rstrip("/")

    async def execute_template(
        self,
        template_name: str,
        company_id: str,
        entity_id: Optional[str] = None,
        is_icrm_user: bool = False,
        extra_params: Optional[Dict[str, str]] = None,
    ) -> SafeDBResult:
        """
        Request MARS to execute a whitelisted query template.

        Args:
            template_name: Pre-defined template (e.g., "order_by_id", "recent_orders")
            company_id: Mandatory tenant scoping
            entity_id: Optional entity (order_id, awb, etc.)
            is_icrm_user: If True, MARS allows cross-company queries
            extra_params: Additional template parameters
        """
        t0 = time.monotonic()
        result = SafeDBResult(template_used=template_name)

        params = {
            "template": template_name,
            "company_id": company_id,
            "is_icrm": is_icrm_user,
        }
        if entity_id:
            params["entity_id"] = entity_id
        if extra_params:
            params.update(extra_params)

        try:
            # Try gRPC first (preferred)
            if self.grpc_client:
                data = await self._execute_via_grpc(params)
            elif self.mars_http_url:
                data = await self._execute_via_http(params)
            else:
                result.error = "No MARS connection configured for safe DB queries"
                return result

            result.success = True
            result.data = data.get("rows", [])
            result.row_count = len(result.data)
            result.safety_checks = data.get("safety_checks", [])
            result.execution_time_ms = (time.monotonic() - t0) * 1000

            logger.info(
                "safe_db_tool.success",
                template=template_name,
                rows=result.row_count,
                ms=round(result.execution_time_ms, 1),
            )

        except Exception as e:
            result.error = str(e)
            result.execution_time_ms = (time.monotonic() - t0) * 1000
            logger.warning(
                "safe_db_tool.failed",
                template=template_name,
                error=str(e),
            )

        return result

    async def _execute_via_grpc(self, params: Dict) -> Dict:
        """Execute via MARS gRPC SafeQuery service."""
        # Uses the existing COSMOS gRPC client to call MARS
        # The proto would have a SafeQueryService with ExecuteTemplate RPC
        # For now, fall back to HTTP if gRPC service not yet defined
        if hasattr(self.grpc_client, "safe_query"):
            # Future: gRPC call
            pass
        # Fallback to HTTP
        return await self._execute_via_http(params)

    async def _execute_via_http(self, params: Dict) -> Dict:
        """Execute via MARS HTTP safe-query endpoint."""
        import httpx

        url = f"{self.mars_http_url}/api/v1/safe-query/execute"

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=params)

            if resp.status_code == 200:
                return resp.json().get("data", {})
            elif resp.status_code == 400:
                body = resp.json()
                raise ValueError(f"Safety check failed: {body.get('message', 'unknown')}")
            elif resp.status_code == 403:
                raise PermissionError("Access denied: tenant isolation violation")
            elif resp.status_code == 408:
                raise TimeoutError("Query killed: exceeded 1-second timeout")
            else:
                raise RuntimeError(f"MARS safe-query returned {resp.status_code}: {resp.text[:200]}")

    @staticmethod
    def available_templates() -> List[Dict[str, str]]:
        """List available query templates (for documentation/debugging)."""
        from app.engine.codebase_intelligence import _DB_TEMPLATES
        return [
            {
                "name": name,
                "table": tmpl["table"],
                "triggers": tmpl["triggers"],
            }
            for name, tmpl in _DB_TEMPLATES.items()
        ]

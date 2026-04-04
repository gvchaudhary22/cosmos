"""
DB-driven tool executor for COSMOS.

Tool definitions live in the cosmos_tools MySQL table, seeded by the training
pipeline from knowledge_base/.../pillar_11_tools/*.yaml files.

At query time:
  1. get_tools_for_context() fetches relevant rows → Anthropic-format tool defs
  2. Claude selects which tool to call (via tool_use)
  3. execute() runs the HTTP call using stored endpoint metadata
  4. Approval gate blocks HIGH/CRITICAL risk tools until operator confirms

This replaces the hardcoded Python tool classes in app/tools/ for act-intent queries.
The existing Python tools remain as a fallback for backward compatibility.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
import structlog

from app.config import Settings

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class ToolRow:
    """Mirrors a row in the cosmos_tools table."""
    id: str
    name: str
    description: str
    entity: str
    intent: str
    http_method: str
    endpoint_path: str
    base_url_key: str
    auth_type: str
    request_schema: Dict
    risk_level: str
    approval_mode: str
    allowed_roles: List[str]
    display_name: str = ""
    response_schema: Optional[Dict] = None
    kb_doc_id: Optional[str] = None
    action_contract: Optional[str] = None
    trust_score: float = 0.9


@dataclass
class ToolExecutionResult:
    status: str                         # "success" | "pending_approval" | "error"
    tool_name: str
    tool_input: Dict
    data: Optional[Any] = None
    error: Optional[str] = None
    job_id: Optional[str] = None        # set when status == "pending_approval"
    latency_ms: float = 0.0


@dataclass
class SessionContext:
    seller_token: Optional[str] = None
    company_token: Optional[str] = None
    icrm_token: Optional[str] = None    # ICRM admin token for /v1/admin/* endpoints
    user_role: str = "operator"
    approved: bool = False              # True after operator confirms a high-risk tool
    approved_job_id: Optional[str] = None


# ---------------------------------------------------------------------------
# In-memory fallback registry (matches cosmos_tools seed rows in migration SQL)
# Used when DB is not available (dev/test) or for bootstrapping.
# ---------------------------------------------------------------------------

_FALLBACK_TOOLS: List[ToolRow] = [
    ToolRow(
        id="orders_create",
        name="orders_create",
        display_name="Create Order",
        description=(
            "Create a new shipping order in Shiprocket. "
            "Validates address, calculates freight, assigns courier, triggers RTO prediction."
        ),
        entity="orders",
        intent="act",
        http_method="POST",
        endpoint_path="/api/v1/app/orders/create",
        base_url_key="MCAPI_BASE_URL",
        auth_type="seller_token",
        request_schema={
            "type": "object",
            "required": [
                "order_id", "order_date", "billing_customer_name",
                "billing_address", "billing_city", "billing_pincode",
                "billing_state", "billing_country", "billing_phone",
                "order_items", "payment_method", "sub_total",
                "length", "breadth", "height", "weight",
            ],
            "properties": {
                "order_id":              {"type": "string",  "description": "Seller reference order ID"},
                "order_date":            {"type": "string",  "description": "Order date YYYY-MM-DD"},
                "billing_customer_name": {"type": "string",  "description": "Customer full name"},
                "billing_address":       {"type": "string",  "description": "Full billing address line"},
                "billing_city":          {"type": "string"},
                "billing_pincode":       {"type": "string",  "description": "6-digit pincode"},
                "billing_state":         {"type": "string"},
                "billing_country":       {"type": "string",  "description": "Default: India"},
                "billing_phone":         {"type": "string",  "description": "10-digit mobile"},
                "shipping_is_billing":   {"type": "boolean", "description": "Use billing address for shipping"},
                "order_items": {
                    "type": "array",
                    "description": "List of items in the order",
                    "items": {
                        "type": "object",
                        "required": ["name", "sku", "units", "selling_price"],
                        "properties": {
                            "name":          {"type": "string"},
                            "sku":           {"type": "string"},
                            "units":         {"type": "integer", "minimum": 1},
                            "selling_price": {"type": "number"},
                        },
                    },
                },
                "payment_method": {"type": "string", "enum": ["prepaid", "cod"]},
                "sub_total":      {"type": "number", "description": "Order value in INR"},
                "length":         {"type": "number", "description": "Package length cm"},
                "breadth":        {"type": "number", "description": "Package breadth cm"},
                "height":         {"type": "number", "description": "Package height cm"},
                "weight":         {"type": "number", "description": "Package weight kg"},
                "channel_id":     {"type": "integer", "description": "Optional channel ID"},
            },
        },
        risk_level="high",
        approval_mode="manual",
        allowed_roles=["operator", "seller"],
        kb_doc_id="pillar_11_tools/orders_create",
        action_contract="pillar_6_action_contracts/domains/orders/create_order",
    ),
    ToolRow(
        id="orders_get",
        name="orders_get",
        display_name="Get Order",
        description="Fetch a single order by order_id from Shiprocket.",
        entity="orders",
        intent="lookup",
        http_method="GET",
        endpoint_path="/api/v1/orders/show",
        base_url_key="MCAPI_BASE_URL",
        auth_type="seller_token",
        request_schema={
            "type": "object",
            "required": ["order_id"],
            "properties": {
                "order_id": {"type": "integer", "description": "Shiprocket order ID"},
            },
        },
        risk_level="low",
        approval_mode="auto",
        allowed_roles=["operator", "seller", "viewer"],
    ),
    ToolRow(
        id="orders_cancel",
        name="orders_cancel",
        display_name="Cancel Order",
        description="Cancel one or more Shiprocket orders before AWB generation.",
        entity="orders",
        intent="act",
        http_method="POST",
        endpoint_path="/api/v1/orders/cancel",
        base_url_key="MCAPI_BASE_URL",
        auth_type="seller_token",
        request_schema={
            "type": "object",
            "required": ["ids"],
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of order IDs to cancel",
                },
            },
        },
        risk_level="high",
        approval_mode="manual",
        allowed_roles=["operator", "seller"],
    ),
    ToolRow(
        id="track_shipment",
        name="track_shipment",
        display_name="Track Shipment",
        description="Get real-time tracking status and events for a shipment by AWB number.",
        entity="shipment",
        intent="lookup",
        http_method="GET",
        endpoint_path="/api/v1/courier/track/awb",
        base_url_key="MCAPI_BASE_URL",
        auth_type="seller_token",
        request_schema={
            "type": "object",
            "required": ["awb"],
            "properties": {
                "awb": {"type": "string", "description": "AWB / tracking number"},
            },
        },
        risk_level="low",
        approval_mode="auto",
        allowed_roles=["operator", "seller", "viewer"],
    ),
]

_FALLBACK_INDEX: Dict[str, ToolRow] = {t.id: t for t in _FALLBACK_TOOLS}


# ---------------------------------------------------------------------------
# ToolExecutorService
# ---------------------------------------------------------------------------

class ToolExecutorService:
    """
    DB-driven tool executor.

    Loads tool definitions from cosmos_tools table (or fallback in-memory registry).
    Executes live MCAPI calls based on stored HTTP endpoint metadata.
    Enforces approval gate for HIGH/CRITICAL risk tools.
    """

    def __init__(self, settings: Settings, db=None):
        self._settings = settings
        self._db = db       # SQLAlchemy async session factory — None in fallback mode
        self._http = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def get_tools_for_context(
        self,
        entity: str,
        intent: str,
        user_role: str = "operator",
        limit: int = 10,
    ) -> List[Dict]:
        """
        Return Anthropic-format tool definitions matching entity+intent.
        These are passed directly to Claude in the tools= parameter.
        """
        rows = await self._fetch_tools(entity=entity, intent=intent, role=user_role)
        return [self._to_anthropic_format(row) for row in rows[:limit]]

    async def get_tool(self, tool_name: str) -> Optional[ToolRow]:
        """Fetch a single tool by ID."""
        if self._db is None:
            return _FALLBACK_INDEX.get(tool_name)
        # DB path: select from cosmos_tools where id = tool_name and enabled = 1
        try:
            from sqlalchemy import text
            async with self._db() as session:
                result = await session.execute(
                    text("SELECT * FROM cosmos_tools WHERE id = :id AND enabled = 1"),
                    {"id": tool_name},
                )
                row = result.mappings().first()
                return self._row_to_tool(dict(row)) if row else None
        except Exception as exc:
            logger.warning("tool_executor.db_get_failed", tool=tool_name, error=str(exc))
            return _FALLBACK_INDEX.get(tool_name)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        tool_name: str,
        tool_input: Dict,
        session_context: Optional[SessionContext] = None,
    ) -> ToolExecutionResult:
        """
        Execute a tool by name.

        For HIGH/CRITICAL risk tools: returns pending_approval unless
        session_context.approved is True.

        Flow:
          1. Load tool metadata from DB
          2. Check role permission
          3. Apply approval gate for risky tools
          4. Build + fire HTTP request
          5. Return result for Claude to synthesize
        """
        import time
        t0 = time.time()
        ctx = session_context or SessionContext()

        tool = await self.get_tool(tool_name)
        if tool is None:
            return ToolExecutionResult(
                status="error",
                tool_name=tool_name,
                tool_input=tool_input,
                error=f"Tool '{tool_name}' not found in cosmos_tools registry",
            )

        # Role check
        if tool.allowed_roles and ctx.user_role not in tool.allowed_roles:
            return ToolExecutionResult(
                status="error",
                tool_name=tool_name,
                tool_input=tool_input,
                error=f"Role '{ctx.user_role}' is not permitted to execute '{tool_name}'",
            )

        # Approval gate
        needs_approval = tool.risk_level in ("high", "critical") and tool.approval_mode != "auto"
        if needs_approval and not ctx.approved:
            job_id = uuid.uuid4().hex
            logger.info(
                "tool_executor.pending_approval",
                tool=tool_name,
                risk=tool.risk_level,
                job_id=job_id,
            )
            return ToolExecutionResult(
                status="pending_approval",
                tool_name=tool_name,
                tool_input=tool_input,
                job_id=job_id,
                data={
                    "status": "pending_approval",
                    "tool_name": tool_name,
                    "tool_display_name": tool.display_name or tool_name,
                    "risk_level": tool.risk_level,
                    "tool_input": tool_input,
                    "job_id": job_id,
                    "message": (
                        f"Action '{tool.display_name or tool_name}' requires operator approval "
                        f"before it can be executed (risk level: {tool.risk_level}). "
                        f"Please confirm to proceed."
                    ),
                },
            )

        # Execute HTTP call
        try:
            result_data = await self._call_http(tool, tool_input, ctx)
            latency = (time.time() - t0) * 1000
            logger.info(
                "tool_executor.executed",
                tool=tool_name,
                latency_ms=round(latency, 1),
            )
            return ToolExecutionResult(
                status="success",
                tool_name=tool_name,
                tool_input=tool_input,
                data=result_data,
                latency_ms=latency,
            )
        except Exception as exc:
            latency = (time.time() - t0) * 1000
            logger.error("tool_executor.http_error", tool=tool_name, error=str(exc))
            return ToolExecutionResult(
                status="error",
                tool_name=tool_name,
                tool_input=tool_input,
                error=str(exc),
                latency_ms=latency,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_tools(
        self, entity: str, intent: str, role: str
    ) -> List[ToolRow]:
        """Fetch matching tools. Falls back to in-memory registry when DB is unavailable."""
        if self._db is None:
            return self._fallback_fetch(entity, intent, role)

        try:
            from sqlalchemy import text
            async with self._db() as session:
                result = await session.execute(
                    text("""
                        SELECT * FROM cosmos_tools
                        WHERE enabled = 1
                          AND (entity = :entity OR intent = :intent)
                        ORDER BY trust_score DESC
                        LIMIT 20
                    """),
                    {"entity": entity, "intent": intent},
                )
                rows = result.mappings().all()
                tools = [self._row_to_tool(dict(r)) for r in rows]
                # Filter by role
                return [t for t in tools if not t.allowed_roles or role in t.allowed_roles]
        except Exception as exc:
            logger.warning("tool_executor.db_fetch_failed", error=str(exc))
            return self._fallback_fetch(entity, intent, role)

    def _fallback_fetch(self, entity: str, intent: str, role: str) -> List[ToolRow]:
        results = []
        for tool in _FALLBACK_TOOLS:
            if tool.entity != entity and tool.intent != intent:
                continue
            if tool.allowed_roles and role not in tool.allowed_roles:
                continue
            results.append(tool)
        return results

    async def _call_http(
        self, tool: ToolRow, params: Dict, ctx: SessionContext
    ) -> Any:
        """Build and fire the HTTP request based on tool metadata."""
        base_url = getattr(self._settings, tool.base_url_key, None)
        if not base_url:
            # Try attribute lookup with lowercase
            base_url = getattr(self._settings, tool.base_url_key.lower(), None)
        if not base_url:
            raise ValueError(
                f"Config key '{tool.base_url_key}' not found in settings. "
                f"Add it to app/config.py."
            )

        url = base_url.rstrip("/") + tool.endpoint_path
        headers = self._build_auth_headers(tool.auth_type, ctx)

        method = tool.http_method.upper()
        if method == "GET":
            response = await self._http.get(url, params=params, headers=headers)
        elif method == "POST":
            response = await self._http.post(url, json=params, headers=headers)
        elif method == "PUT":
            response = await self._http.put(url, json=params, headers=headers)
        elif method == "DELETE":
            response = await self._http.delete(url, params=params, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        return response.json()

    def _build_auth_headers(self, auth_type: str, ctx: SessionContext) -> Dict:
        if auth_type == "icrm_token" and ctx.icrm_token:
            return {"Authorization": f"Bearer {ctx.icrm_token}"}
        if auth_type == "seller_token" and ctx.seller_token:
            return {"Authorization": f"Bearer {ctx.seller_token}"}
        if auth_type == "company_token" and ctx.company_token:
            return {"Authorization": f"Bearer {ctx.company_token}"}
        # Fallback: if auth_type=icrm_token but no icrm_token, try seller_token
        if auth_type == "icrm_token" and ctx.seller_token:
            return {"Authorization": f"Bearer {ctx.seller_token}"}
        return {}

    @staticmethod
    def _to_anthropic_format(tool: ToolRow) -> Dict:
        """Convert a ToolRow to the dict format Claude's API expects in tools=[]."""
        return {
            "name": tool.id,
            "description": tool.description,
            "input_schema": tool.request_schema,
        }

    @staticmethod
    def _row_to_tool(row: Dict) -> ToolRow:
        import json
        def _json(v):
            if isinstance(v, (dict, list)):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return {}
            return {}

        return ToolRow(
            id=row["id"],
            name=row["name"],
            display_name=row.get("display_name", ""),
            description=row["description"],
            entity=row.get("entity", ""),
            intent=row.get("intent", ""),
            http_method=row.get("http_method", "GET"),
            endpoint_path=row.get("endpoint_path", ""),
            base_url_key=row.get("base_url_key", "MCAPI_BASE_URL"),
            auth_type=row.get("auth_type", "seller_token"),
            request_schema=_json(row.get("request_schema", {})),
            response_schema=_json(row.get("response_schema")),
            risk_level=row.get("risk_level", "low"),
            approval_mode=row.get("approval_mode", "auto"),
            allowed_roles=_json(row.get("allowed_roles", [])),
            trust_score=float(row.get("trust_score", 0.9)),
            kb_doc_id=row.get("kb_doc_id"),
            action_contract=row.get("action_contract"),
        )

"""All 15 read-only tools for COSMOS Phase 1."""

import time
from typing import Any, Dict, Optional

from app.tools.base import (
    BaseTool,
    DataSource,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
    ToolParam,
    ToolResult,
)


# --------------------------------------------------------------------------- #
# 1. OrderLookupTool
# --------------------------------------------------------------------------- #

class OrderLookupTool(BaseTool):
    """Look up a single order by ID, AWB, or channel order ID."""

    definition = ToolDefinition(
        name="order_lookup",
        category=ToolCategory.READ,
        description="Look up a single order by ID, AWB number, or channel order ID",
        parameters=[
            ToolParam("order_id", "str", True, "Order ID, AWB number, or channel order ID"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_order(params["order_id"], headers=headers)
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 2. OrdersByCompanyTool
# --------------------------------------------------------------------------- #

class OrdersByCompanyTool(BaseTool):
    """Fetch orders for a company with optional filters."""

    definition = ToolDefinition(
        name="orders_by_company",
        category=ToolCategory.READ,
        description="Fetch orders for a company with optional status and date filters",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
            ToolParam("status", "str", False, "Comma-separated order statuses to filter by"),
            ToolParam("date_from", "str", False, "Start date (YYYY-MM-DD)"),
            ToolParam("date_to", "str", False, "End date (YYYY-MM-DD)"),
            ToolParam("limit", "int", False, "Max results to return", default=50),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            status_list = params.get("status", "").split(",") if params.get("status") else None
            response = await self.mcapi.get_orders(
                company_id=int(params["company_id"]),
                status=status_list,
                date_from=params.get("date_from"),
                date_to=params.get("date_to"),
                limit=int(params.get("limit", 50)),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 3. OrderSearchTool
# --------------------------------------------------------------------------- #

class OrderSearchTool(BaseTool):
    """Search orders by query string with optional filters."""

    definition = ToolDefinition(
        name="order_search",
        category=ToolCategory.READ,
        description="Search orders by query string with optional filters",
        parameters=[
            ToolParam("query", "str", True, "Search query (order ID, customer name, SKU, etc.)"),
            ToolParam("filters", "str", False, "Additional filters as key=value pairs, comma-separated"),
            ToolParam("limit", "int", False, "Max results to return", default=50),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            filters = None
            if params.get("filters"):
                filters = {}
                for pair in params["filters"].split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        filters[k.strip()] = v.strip()
            response = await self.mcapi.search_orders(
                query=params["query"],
                filters=filters,
                limit=int(params.get("limit", 50)),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 4. ShipmentTrackTool
# --------------------------------------------------------------------------- #

class ShipmentTrackTool(BaseTool):
    """Track a shipment by AWB number."""

    definition = ToolDefinition(
        name="shipment_track",
        category=ToolCategory.READ,
        description="Track a shipment by AWB number to get current status and location",
        parameters=[
            ToolParam("awb", "str", True, "AWB (Air Waybill) tracking number"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.track_shipment(params["awb"], headers=headers)
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 5. TrackingTimelineTool
# --------------------------------------------------------------------------- #

class TrackingTimelineTool(BaseTool):
    """Get full tracking timeline for a shipment."""

    definition = ToolDefinition(
        name="tracking_timeline",
        category=ToolCategory.READ,
        description="Get the full tracking timeline with all status updates for a shipment",
        parameters=[
            ToolParam("awb", "str", True, "AWB (Air Waybill) tracking number"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_tracking_timeline(params["awb"], headers=headers)
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 6. NDRListTool
# --------------------------------------------------------------------------- #

class NDRListTool(BaseTool):
    """List NDRs (Non-Delivery Reports) for a company."""

    definition = ToolDefinition(
        name="ndr_list",
        category=ToolCategory.READ,
        description="List NDRs for a company with optional status and date filters",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
            ToolParam("status", "str", False, "NDR status filter (e.g. open, closed, action_required)"),
            ToolParam("date_from", "str", False, "Start date (YYYY-MM-DD)"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_ndr_list(
                company_id=int(params["company_id"]),
                status=params.get("status"),
                date_from=params.get("date_from"),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 7. NDRDetailsTool
# --------------------------------------------------------------------------- #

class NDRDetailsTool(BaseTool):
    """Get details for a specific NDR."""

    definition = ToolDefinition(
        name="ndr_details",
        category=ToolCategory.READ,
        description="Get detailed information for a specific NDR by its ID",
        parameters=[
            ToolParam("ndr_id", "str", True, "NDR ID"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_ndr_details(params["ndr_id"], headers=headers)
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 8. SellerInfoTool
# --------------------------------------------------------------------------- #

class SellerInfoTool(BaseTool):
    """Look up seller information by company ID or email."""

    definition = ToolDefinition(
        name="seller_info",
        category=ToolCategory.READ,
        description="Look up seller information by company ID or email address",
        parameters=[
            ToolParam("company_id", "int", False, "Company ID"),
            ToolParam("email", "str", False, "Seller email address"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "kam_support_admin", "sales"],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            if not params.get("company_id") and not params.get("email"):
                return ToolResult(
                    success=False,
                    error="At least one of company_id or email is required",
                    latency_ms=(time.time() - start) * 1000,
                )
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_seller_info(
                company_id=int(params["company_id"]) if params.get("company_id") else None,
                email=params.get("email"),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 9. SellerPlanTool
# --------------------------------------------------------------------------- #

class SellerPlanTool(BaseTool):
    """Get the subscription plan for a seller."""

    definition = ToolDefinition(
        name="seller_plan",
        category=ToolCategory.READ,
        description="Get the current subscription plan details for a seller",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_seller_plan(
                company_id=int(params["company_id"]),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 10. SellerHealthTool
# --------------------------------------------------------------------------- #

class SellerHealthTool(BaseTool):
    """Get seller health / performance score."""

    definition = ToolDefinition(
        name="seller_health_score",
        category=ToolCategory.READ,
        description="Get seller health and performance metrics for a company",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_seller_health(
                company_id=int(params["company_id"]),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 11. BillingQueryTool
# --------------------------------------------------------------------------- #

class BillingQueryTool(BaseTool):
    """Query billing information for a company."""

    definition = ToolDefinition(
        name="billing_query",
        category=ToolCategory.READ,
        description="Query billing information (invoices, statements, etc.) for a company",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
            ToolParam("query_type", "str", False, "Type of billing query: invoices, statements, summary", default="invoices"),
            ToolParam("date_range", "str", False, "Date range filter (e.g. 30d, 90d)"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "accounts", "billing", "support_admin"],
        risk_level=RiskLevel.MEDIUM,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_billing(
                company_id=int(params["company_id"]),
                query_type=params.get("query_type", "invoices"),
                date_range=params.get("date_range"),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 12. WalletBalanceTool
# --------------------------------------------------------------------------- #

class WalletBalanceTool(BaseTool):
    """Get wallet balance for a company."""

    definition = ToolDefinition(
        name="wallet_balance",
        category=ToolCategory.READ,
        description="Get the current wallet balance for a company",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_wallet_balance(
                company_id=int(params["company_id"]),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 13. TransactionHistoryTool
# --------------------------------------------------------------------------- #

class TransactionHistoryTool(BaseTool):
    """Get wallet transaction history for a company."""

    definition = ToolDefinition(
        name="transaction_history",
        category=ToolCategory.READ,
        description="Get wallet transaction history for a company with pagination",
        parameters=[
            ToolParam("company_id", "int", True, "Company ID"),
            ToolParam("limit", "int", False, "Max transactions to return", default=50),
            ToolParam("offset", "int", False, "Pagination offset", default=0),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            headers = context.get("headers") if context else None
            response = await self.mcapi.get_transactions(
                company_id=int(params["company_id"]),
                limit=int(params.get("limit", 50)),
                offset=int(params.get("offset", 0)),
                headers=headers,
            )
            return ToolResult(
                success=response.success,
                data=response.data,
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 14. ELKSearchTool
# --------------------------------------------------------------------------- #

class ELKSearchTool(BaseTool):
    """Search application logs via Elasticsearch."""

    definition = ToolDefinition(
        name="elk_search",
        category=ToolCategory.READ,
        description="Search application logs in Elasticsearch (full-text, trace ID, or error diagnosis)",
        parameters=[
            ToolParam("query", "str", True, "Search query or trace ID"),
            ToolParam("index_pattern", "str", False, "Elasticsearch index pattern", default="star-api-*"),
            ToolParam("time_range", "str", False, "Time range (e.g. 30m, 24h, 7d)", default="24h"),
            ToolParam("mode", "str", False, "Search mode: search, trace, or diagnose", default="search"),
        ],
        data_source=DataSource.ELK,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, elk_client):
        self.elk = elk_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            result = await self.elk.search_logs(
                query=params["query"],
                index_pattern=params.get("index_pattern", "star-api-*"),
                time_range=params.get("time_range", "24h"),
                mode=params.get("mode", "search"),
            )
            return ToolResult(
                success=True,
                data={"total": result.total, "hits": result.hits, "took_ms": result.took_ms},
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)


# --------------------------------------------------------------------------- #
# 15. EndpointUsageTool
# --------------------------------------------------------------------------- #

class EndpointUsageTool(BaseTool):
    """Get API endpoint usage analytics from Elasticsearch."""

    definition = ToolDefinition(
        name="endpoint_usage",
        category=ToolCategory.READ,
        description="Get API endpoint usage statistics (request counts, latency, error rates) from access logs",
        parameters=[
            ToolParam("time_range", "str", False, "Time range (e.g. 1h, 24h, 7d)", default="24h"),
            ToolParam("path_filter", "str", False, "Wildcard filter for endpoint paths (e.g. /v1/orders/*)"),
            ToolParam("top_n", "int", False, "Number of top endpoints to return", default=20),
        ],
        data_source=DataSource.ELK,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, elk_client):
        self.elk = elk_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        try:
            result = await self.elk.get_endpoint_usage(
                time_range=params.get("time_range", "24h"),
                path_filter=params.get("path_filter"),
                top_n=int(params.get("top_n", 20)),
            )
            return ToolResult(
                success=True,
                data={
                    "total_requests": result.total_requests,
                    "endpoints": result.endpoints,
                },
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)

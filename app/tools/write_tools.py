"""All 8 write/action tools for COSMOS Phase 2.

Each tool requires approval before execution. The tool's execute() returns
a pending-approval dict when called without prior approval. Actual MCAPI
calls happen only via the ApprovalEngine after the request is approved.
"""

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
# Base class for write tools
# --------------------------------------------------------------------------- #

class WriteToolBase(BaseTool):
    """Base for write tools — requires approval before execution."""

    requires_approval: bool = True
    risk_level: str = "low"  # overridden by subclasses

    def _pending_result(self, params: Dict[str, Any]) -> ToolResult:
        """Return a pending-approval result instead of executing."""
        return ToolResult(
            success=True,
            data={
                "status": "pending_approval",
                "tool_name": self.definition.name,
                "params": params,
                "risk_level": self.definition.risk_level.value,
                "message": (
                    f"Action '{self.definition.name}' requires approval "
                    f"(risk: {self.definition.risk_level.value}). "
                    "Awaiting authorisation before execution."
                ),
            },
        )


# --------------------------------------------------------------------------- #
# 1. CancelOrderTool
# --------------------------------------------------------------------------- #

class CancelOrderTool(WriteToolBase):
    """Cancel an order via MCAPI."""

    requires_approval = True
    risk_level = "high"

    definition = ToolDefinition(
        name="cancel_order",
        category=ToolCategory.ACTION,
        description="Cancel an order by order ID",
        parameters=[
            ToolParam("order_id", "str", True, "Order ID to cancel"),
            ToolParam("reason", "str", False, "Cancellation reason"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "supervisor", "manager"],
        risk_level=RiskLevel.HIGH,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        # If context signals pre-approved, execute directly
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                response = await self.mcapi.cancel_order(
                    order_id=params["order_id"],
                    reason=params.get("reason"),
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 2. InitiateRefundTool
# --------------------------------------------------------------------------- #

class InitiateRefundTool(WriteToolBase):
    """Initiate a refund for an order."""

    requires_approval = True
    risk_level = "critical"

    definition = ToolDefinition(
        name="initiate_refund",
        category=ToolCategory.ACTION,
        description="Initiate a refund for an order",
        parameters=[
            ToolParam("order_id", "str", True, "Order ID to refund"),
            ToolParam("amount", "float", False, "Refund amount (omit for full refund)"),
            ToolParam("reason", "str", False, "Refund reason"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "manager"],
        risk_level=RiskLevel.CRITICAL,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                amount = float(params["amount"]) if params.get("amount") else None
                response = await self.mcapi.initiate_refund(
                    order_id=params["order_id"],
                    amount=amount,
                    reason=params.get("reason"),
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 3. ReattemptDeliveryTool
# --------------------------------------------------------------------------- #

class ReattemptDeliveryTool(WriteToolBase):
    """Reattempt delivery for an NDR shipment."""

    requires_approval = True
    risk_level = "low"

    definition = ToolDefinition(
        name="reattempt_delivery",
        category=ToolCategory.ACTION,
        description="Reattempt delivery for a failed/NDR shipment",
        parameters=[
            ToolParam("awb", "str", True, "AWB tracking number"),
            ToolParam("preferred_date", "str", False, "Preferred reattempt date (YYYY-MM-DD)"),
            ToolParam("instructions", "str", False, "Delivery instructions"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                response = await self.mcapi.reattempt_delivery(
                    awb=params["awb"],
                    preferred_date=params.get("preferred_date"),
                    instructions=params.get("instructions"),
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 4. UpdateAddressTool
# --------------------------------------------------------------------------- #

class UpdateAddressTool(WriteToolBase):
    """Update delivery address for an order."""

    requires_approval = True
    risk_level = "medium"

    definition = ToolDefinition(
        name="update_address",
        category=ToolCategory.ACTION,
        description="Update the delivery address for an order",
        parameters=[
            ToolParam("order_id", "str", True, "Order ID"),
            ToolParam("address_line1", "str", True, "Address line 1"),
            ToolParam("city", "str", True, "City"),
            ToolParam("state", "str", True, "State"),
            ToolParam("pincode", "str", True, "Pincode"),
            ToolParam("address_line2", "str", False, "Address line 2"),
            ToolParam("phone", "str", False, "Contact phone number"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "supervisor", "manager"],
        risk_level=RiskLevel.MEDIUM,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                address = {
                    "address_line1": params["address_line1"],
                    "city": params["city"],
                    "state": params["state"],
                    "pincode": params["pincode"],
                }
                if params.get("address_line2"):
                    address["address_line2"] = params["address_line2"]
                if params.get("phone"):
                    address["phone"] = params["phone"]
                response = await self.mcapi.update_address(
                    order_id=params["order_id"],
                    address=address,
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 5. EscalateToSupervisorTool
# --------------------------------------------------------------------------- #

class EscalateToSupervisorTool(WriteToolBase):
    """Create an escalation ticket for a supervisor."""

    requires_approval = True
    risk_level = "low"

    definition = ToolDefinition(
        name="escalate_to_supervisor",
        category=ToolCategory.ACTION,
        description="Create an escalation ticket for supervisor review",
        parameters=[
            ToolParam("subject", "str", True, "Escalation subject"),
            ToolParam("description", "str", True, "Description of the issue"),
            ToolParam("priority", "str", False, "Priority: low, medium, high", default="medium"),
            ToolParam("order_id", "str", False, "Related order ID"),
            ToolParam("awb", "str", False, "Related AWB number"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=[],
        risk_level=RiskLevel.LOW,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                related_ids = {}
                if params.get("order_id"):
                    related_ids["order_id"] = params["order_id"]
                if params.get("awb"):
                    related_ids["awb"] = params["awb"]
                response = await self.mcapi.escalate_to_supervisor(
                    subject=params["subject"],
                    description=params["description"],
                    priority=params.get("priority", "medium"),
                    related_ids=related_ids or None,
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 6. BlockSellerTool
# --------------------------------------------------------------------------- #

class BlockSellerTool(WriteToolBase):
    """Block a seller account."""

    requires_approval = True
    risk_level = "critical"

    definition = ToolDefinition(
        name="block_seller",
        category=ToolCategory.ACTION,
        description="Block a seller account (requires admin approval)",
        parameters=[
            ToolParam("seller_id", "str", True, "Seller / company ID to block"),
            ToolParam("reason", "str", True, "Reason for blocking the seller"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin"],
        risk_level=RiskLevel.CRITICAL,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                response = await self.mcapi.block_seller(
                    seller_id=params["seller_id"],
                    reason=params["reason"],
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 7. IssueWalletCreditTool
# --------------------------------------------------------------------------- #

class IssueWalletCreditTool(WriteToolBase):
    """Credit a seller's wallet."""

    requires_approval = True
    risk_level = "high"

    definition = ToolDefinition(
        name="issue_wallet_credit",
        category=ToolCategory.ACTION,
        description="Issue a credit to a seller's wallet",
        parameters=[
            ToolParam("seller_id", "str", True, "Seller / company ID"),
            ToolParam("amount", "float", True, "Amount to credit"),
            ToolParam("reason", "str", False, "Reason for the credit"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "manager"],
        risk_level=RiskLevel.HIGH,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                response = await self.mcapi.issue_wallet_credit(
                    seller_id=params["seller_id"],
                    amount=float(params["amount"]),
                    reason=params.get("reason"),
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)


# --------------------------------------------------------------------------- #
# 8. ReassignCourierTool
# --------------------------------------------------------------------------- #

class ReassignCourierTool(WriteToolBase):
    """Reassign a shipment to a different courier."""

    requires_approval = True
    risk_level = "medium"

    definition = ToolDefinition(
        name="reassign_courier",
        category=ToolCategory.ACTION,
        description="Reassign a shipment to a different courier partner",
        parameters=[
            ToolParam("awb", "str", True, "AWB tracking number"),
            ToolParam("courier_id", "str", True, "New courier partner ID"),
            ToolParam("reason", "str", False, "Reason for reassignment"),
        ],
        data_source=DataSource.MCAPI,
        allowed_roles=["admin", "support_admin", "supervisor", "manager"],
        risk_level=RiskLevel.MEDIUM,
    )

    def __init__(self, mcapi_client):
        self.mcapi = mcapi_client

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        start = time.time()
        if context and context.get("approved"):
            try:
                headers = context.get("headers") if context else None
                response = await self.mcapi.reassign_courier(
                    awb=params["awb"],
                    courier_id=params["courier_id"],
                    reason=params.get("reason"),
                    headers=headers,
                )
                return ToolResult(
                    success=response.success,
                    data=response.data,
                    latency_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return ToolResult(success=False, error=str(e), latency_ms=(time.time() - start) * 1000)
        return self._pending_result(params)

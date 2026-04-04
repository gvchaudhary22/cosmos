"""Factory function to create and register all COSMOS tools (Phase 1 + Phase 2)."""

from app.tools.registry import ToolRegistry
from app.tools.read_tools import (
    OrderLookupTool,
    OrdersByCompanyTool,
    OrderSearchTool,
    ShipmentTrackTool,
    TrackingTimelineTool,
    NDRListTool,
    NDRDetailsTool,
    SellerInfoTool,
    SellerPlanTool,
    SellerHealthTool,
    BillingQueryTool,
    WalletBalanceTool,
    TransactionHistoryTool,
    ELKSearchTool,
    EndpointUsageTool,
)
from app.tools.write_tools import (
    CancelOrderTool,
    CreateOrderTool,
    InitiateRefundTool,
    ReattemptDeliveryTool,
    UpdateAddressTool,
    EscalateToSupervisorTool,
    BlockSellerTool,
    IssueWalletCreditTool,
    ReassignCourierTool,
)
from app.clients.mcapi import MCAPIClient
from app.clients.elk import ELKClient


def create_tool_registry(mcapi: MCAPIClient, elk: ELKClient) -> ToolRegistry:
    """Create a ToolRegistry with all 15 read tools + 8 write tools registered."""
    registry = ToolRegistry()

    # ---- Phase 1: Read tools (1-15) ---- #

    # Order tools (1-3)
    registry.register(OrderLookupTool(mcapi))
    registry.register(OrdersByCompanyTool(mcapi))
    registry.register(OrderSearchTool(mcapi))

    # Shipping / tracking tools (4-5)
    registry.register(ShipmentTrackTool(mcapi))
    registry.register(TrackingTimelineTool(mcapi))

    # NDR tools (6-7)
    registry.register(NDRListTool(mcapi))
    registry.register(NDRDetailsTool(mcapi))

    # Seller tools (8-10)
    registry.register(SellerInfoTool(mcapi))
    registry.register(SellerPlanTool(mcapi))
    registry.register(SellerHealthTool(mcapi))

    # Billing / wallet tools (11-13)
    registry.register(BillingQueryTool(mcapi))
    registry.register(WalletBalanceTool(mcapi))
    registry.register(TransactionHistoryTool(mcapi))

    # ELK / observability tools (14-15)
    registry.register(ELKSearchTool(elk))
    registry.register(EndpointUsageTool(elk))

    # ---- Phase 2: Write / Action tools (16-24) ---- #

    registry.register(CancelOrderTool(mcapi))
    registry.register(CreateOrderTool(mcapi))
    registry.register(InitiateRefundTool(mcapi))
    registry.register(ReattemptDeliveryTool(mcapi))
    registry.register(UpdateAddressTool(mcapi))
    registry.register(EscalateToSupervisorTool(mcapi))
    registry.register(BlockSellerTool(mcapi))
    registry.register(IssueWalletCreditTool(mcapi))
    registry.register(ReassignCourierTool(mcapi))

    return registry

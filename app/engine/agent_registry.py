"""
Predefined Agent Registry — 18 persistent agents with scoped tools + knowledge.

Architecture:
  1. AgentRegistry holds all predefined agents (loaded at startup)
  2. AgentPlanner selects agent(s) for a query based on intent + entity
  3. Multi-agent queries → Planner creates execution chain with handoff
  4. AgentForge still creates custom agents when no predefined matches

Agent tiers:
  Tier 1 (6 core): OrderOps, ShipmentOps, CourierOps, SettingsAdmin, BillingWallet, NDRResolver
  Tier 2 (6 specialized): ReturnExchange, ChannelSync, CatalogProducts, WeightDispute, InternationalShip, ReportAnalytics
  Tier 3 (6 operational): EscalationManager, PickupWarehouse, CustomerComm, FraudDetection, SystemDebugger, AuthLogin
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import structlog

logger = structlog.get_logger()


class AgentTier(str, Enum):
    CORE = "core"           # Tier 1: highest volume
    SPECIALIZED = "specialized"  # Tier 2: domain expertise
    OPERATIONAL = "operational"  # Tier 3: low volume, high complexity


@dataclass
class AgentDefinition:
    """Predefined agent configuration."""
    name: str
    display_name: str
    tier: AgentTier
    domain: str
    description: str

    # Tool scoping
    tools_allowed: List[str]     # tools this agent CAN use
    tools_denied: List[str] = field(default_factory=list)  # explicit deny

    # Knowledge scoping — used by ScopedRetrieval to filter vector search
    # These are DOMAIN keywords matched against entity_id and content, not entity_type values
    # e.g., "orders" matches table:orders:*, api:*:orders.*, module:*:orders:*
    knowledge_scope: List[str] = field(default_factory=list)

    # Behavior
    instructions: str = ""       # natural language system prompt
    anti_patterns: List[str] = field(default_factory=list)  # what NOT to do
    escalation_rules: str = "after 3 failed attempts → escalate to supervisor"
    max_loops: int = 3

    # Few-shot examples
    examples: List[Dict[str, str]] = field(default_factory=list)

    # Handoff rules
    can_handoff_to: List[str] = field(default_factory=list)  # agent names this can hand off to
    accepts_handoff_from: List[str] = field(default_factory=list)

    # Metrics
    queries_handled: int = 0
    success_rate: float = 0.0


# ===================================================================
# TIER 1: Core Agents (6)
# ===================================================================

TIER1_AGENTS = [
    AgentDefinition(
        name="order_ops",
        display_name="Order Operations Agent",
        tier=AgentTier.CORE,
        domain="orders",
        description="Handles all order-related queries: status checks, cancellations, address updates, order search",
        tools_allowed=["order_lookup", "orders_by_company", "order_search", "cancel_order", "update_address"],
        knowledge_scope=["orders", "core_orders"],
        instructions=(
            "You are the Order Operations agent. Always verify the order exists before any action. "
            "Check cancellation eligibility — if order is already SHIPPED (status 6+), inform it cannot be cancelled. "
            "For address updates, verify the shipment hasn't been picked up yet. "
            "If the user also needs a refund, HANDOFF to billing_wallet agent with full context."
        ),
        anti_patterns=["Never process refunds", "Never modify pricing", "Never delete order records"],
        can_handoff_to=["billing_wallet", "shipment_ops", "ndr_resolver"],
        accepts_handoff_from=["channel_sync", "return_exchange"],
        examples=[
            {"query": "Order 12345 ka status batao", "action": "order_lookup(id=12345)"},
            {"query": "Cancel order 98765", "action": "order_lookup(id=98765) → cancel_order(id=98765)"},
        ],
    ),
    AgentDefinition(
        name="shipment_ops",
        display_name="Shipment Operations Agent",
        tier=AgentTier.CORE,
        domain="shipments",
        description="AWB tracking, courier reassignment, pickup issues, delivery timeline",
        tools_allowed=["shipment_track", "tracking_timeline", "reassign_courier"],
        knowledge_scope=["shipments", "couriers_awb"],
        instructions=(
            "You are the Shipment Operations agent. Track AWB status, show delivery timeline, "
            "and handle courier reassignment requests. Know the courier SLAs: Delhivery 3-5 days, "
            "BlueDart 2-4 days, Ecom 4-7 days. If NDR/RTO, handoff to ndr_resolver."
        ),
        can_handoff_to=["ndr_resolver", "courier_ops", "order_ops"],
        accepts_handoff_from=["order_ops", "pickup_warehouse"],
        examples=[
            {"query": "AWB 9876543210 kahan tak pahuncha?", "action": "shipment_track(awb=9876543210)"},
        ],
    ),
    AgentDefinition(
        name="courier_ops",
        display_name="Courier Operations Agent",
        tier=AgentTier.CORE,
        domain="courier",
        description="Courier onboarding, serviceability, rate cards, ownkey setup, performance comparison",
        tools_allowed=["shipment_track", "seller_health_score", "elk_search"],
        knowledge_scope=["couriers", "courier"],
        instructions="Handle courier-related queries: serviceability check, rate comparison, ownkey setup, performance metrics.",
        can_handoff_to=["shipment_ops", "billing_wallet"],
        accepts_handoff_from=["shipment_ops"],
    ),
    AgentDefinition(
        name="settings_admin",
        display_name="Settings Administration Agent",
        tier=AgentTier.CORE,
        domain="settings",
        description="Company settings, plan changes, feature flags, KYC, profile updates",
        tools_allowed=["seller_info", "seller_plan", "seller_health_score"],
        knowledge_scope=["settings", "company_settings"],
        instructions="Handle settings queries: plan info, KYC status, feature flag checks, company profile.",
        can_handoff_to=["billing_wallet", "auth_login"],
        accepts_handoff_from=["auth_login"],
    ),
    AgentDefinition(
        name="billing_wallet",
        display_name="Billing & Wallet Agent",
        tier=AgentTier.CORE,
        domain="billing",
        description="Weight disputes, COD remittance, wallet recharge, refunds, transaction history",
        tools_allowed=["billing_query", "wallet_balance", "transaction_history", "issue_wallet_credit", "initiate_refund"],
        knowledge_scope=["billing", "billing_wallet"],
        instructions=(
            "Handle all billing queries: freight charges, weight discrepancies, COD remittance, "
            "wallet balance, refund processing. For weight disputes, always check applied vs declared weight."
        ),
        can_handoff_to=["weight_dispute", "order_ops"],
        accepts_handoff_from=["order_ops", "shipment_ops", "weight_dispute"],
        examples=[
            {"query": "Wallet balance check karo", "action": "wallet_balance(company_id=...)"},
            {"query": "Refund process karo order 12345", "action": "initiate_refund(order_id=12345)"},
        ],
    ),
    AgentDefinition(
        name="ndr_resolver",
        display_name="NDR Resolution Agent",
        tier=AgentTier.CORE,
        domain="ndr",
        description="Non-delivery report handling, reattempt, address correction, RTO decision",
        tools_allowed=["ndr_list", "ndr_details", "reattempt_delivery", "update_address"],
        knowledge_scope=["ndr", "ndr_data"],
        instructions=(
            "Handle NDR queries. Check attempt count (max 3). Analyze NDR reason. "
            "If 'Customer Unavailable' → suggest alternate phone/address. "
            "If 'Wrong Address' → update_address then reattempt. "
            "After 3 failed attempts, RTO initiates automatically."
        ),
        can_handoff_to=["shipment_ops", "order_ops"],
        accepts_handoff_from=["shipment_ops", "order_ops"],
        examples=[
            {"query": "AWB 5544332211 pe NDR aaya hai", "action": "ndr_details(awb=5544332211) → reattempt_delivery(...)"},
        ],
    ),
]

# ===================================================================
# TIER 2: Specialized Agents (6)
# ===================================================================

TIER2_AGENTS = [
    AgentDefinition(
        name="return_exchange",
        display_name="Return & Exchange Agent",
        tier=AgentTier.SPECIALIZED,
        domain="returns",
        description="Return pickup, exchange orders, reverse logistics, QC",
        tools_allowed=["order_lookup", "shipment_track", "initiate_refund"],
        knowledge_scope=["returns"],
        instructions="Handle return/exchange requests. Verify return eligibility window (7 days from delivery).",
        can_handoff_to=["order_ops", "billing_wallet"],
    ),
    AgentDefinition(
        name="channel_sync",
        display_name="Channel Sync Agent",
        tier=AgentTier.SPECIALIZED,
        domain="webhook",
        description="Shopify/Amazon/WooCommerce sync issues, webhook failures, order import",
        tools_allowed=["elk_search", "order_search", "endpoint_usage"],
        knowledge_scope=["webhook", "channels"],
        instructions="Debug channel sync issues. Check webhook logs, channel auth status, sync timestamps.",
        can_handoff_to=["order_ops", "system_debugger"],
    ),
    AgentDefinition(
        name="catalog_products",
        display_name="Catalog & Products Agent",
        tier=AgentTier.SPECIALIZED,
        domain="catalog",
        description="SKU mapping, product weight mismatch, inventory sync",
        tools_allowed=["order_lookup", "seller_info"],
        knowledge_scope=["catalog", "products"],
        instructions="Handle product/catalog queries: SKU lookup, weight freeze, inventory sync issues.",
    ),
    AgentDefinition(
        name="weight_dispute",
        display_name="Weight Dispute Agent",
        tier=AgentTier.SPECIALIZED,
        domain="discrepancy",
        description="Freeze/unfreeze weights, escalate to courier, POD image check",
        tools_allowed=["billing_query", "shipment_track", "order_lookup"],
        knowledge_scope=["discrepancy", "weight_discrepancy"],
        instructions="Handle weight discrepancy cases. Compare applied weight vs declared. Check courier's weight scan image.",
        can_handoff_to=["billing_wallet"],
        accepts_handoff_from=["billing_wallet"],
    ),
    AgentDefinition(
        name="international_ship",
        display_name="International Shipping Agent",
        tier=AgentTier.SPECIALIZED,
        domain="international",
        description="Customs clearance, HS codes, CargoX quotes, international couriers",
        tools_allowed=["shipment_track", "billing_query", "order_lookup"],
        knowledge_scope=["international"],
        instructions="Handle international shipping queries. Know customs rules, HS code requirements, duty calculations.",
    ),
    AgentDefinition(
        name="report_analytics",
        display_name="Report & Analytics Agent",
        tier=AgentTier.SPECIALIZED,
        domain="analytics",
        description="Report generation, download requests, trend analysis",
        tools_allowed=["billing_query", "elk_search", "endpoint_usage"],
        knowledge_scope=["analytics", "reports"],
        instructions="Handle report requests. Trigger report jobs and track their status.",
    ),
]

# ===================================================================
# TIER 3: Operational Agents (6)
# ===================================================================

TIER3_AGENTS = [
    AgentDefinition(
        name="escalation_manager",
        display_name="Escalation Manager Agent",
        tier=AgentTier.OPERATIONAL,
        domain="escalation",
        description="SLA breaches, priority routing, callback scheduling",
        tools_allowed=["escalate_to_supervisor", "ndr_details", "order_lookup"],
        knowledge_scope=["escalation"],
        instructions="Handle escalation cases. Check SLA status, priority level, and route to supervisor.",
    ),
    AgentDefinition(
        name="pickup_warehouse",
        display_name="Pickup & Warehouse Agent",
        tier=AgentTier.OPERATIONAL,
        domain="warehouse",
        description="Pickup scheduling, manifest generation, warehouse operations",
        tools_allowed=["shipment_track", "seller_info"],
        knowledge_scope=["warehouse", "pickup_manifests"],
        instructions="Handle pickup/warehouse queries. Check manifest status, pickup scheduling.",
    ),
    AgentDefinition(
        name="customer_comm",
        display_name="Customer Communication Agent",
        tier=AgentTier.OPERATIONAL,
        domain="notifications",
        description="WhatsApp delivery notifications, tracking page issues, SMS",
        tools_allowed=["order_lookup", "shipment_track"],
        knowledge_scope=["notifications"],
        instructions="Handle customer communication queries. Check notification status, tracking page.",
    ),
    AgentDefinition(
        name="fraud_detection",
        display_name="Fraud Detection Agent",
        tier=AgentTier.OPERATIONAL,
        domain="security",
        description="Suspicious COD orders, fake NDR patterns, bulk abuse",
        tools_allowed=["order_search", "billing_query", "elk_search", "block_seller"],
        knowledge_scope=["security"],
        instructions="Analyze suspicious activity patterns. Check COD order clusters, NDR frequency anomalies.",
    ),
    AgentDefinition(
        name="system_debugger",
        display_name="System Debugger Agent",
        tier=AgentTier.OPERATIONAL,
        domain="internal",
        description="API errors, deployment issues, log analysis",
        tools_allowed=["elk_search", "endpoint_usage"],
        knowledge_scope=["observability"],
        instructions="Debug system issues. Search ELK logs, analyze endpoint usage patterns, identify errors.",
    ),
    AgentDefinition(
        name="auth_login",
        display_name="Auth & Login Agent",
        tier=AgentTier.OPERATIONAL,
        domain="auth",
        description="Login issues, token expiry, SSO failures, password reset",
        tools_allowed=["seller_info", "elk_search"],
        knowledge_scope=["auth", "users"],
        instructions="Handle authentication queries. Check login status, token expiry, SSO configuration.",
    ),
]


class AgentRegistry:
    """Central registry for all predefined agents."""

    def __init__(self):
        self._agents: Dict[str, AgentDefinition] = {}
        self._domain_index: Dict[str, List[str]] = {}  # domain → [agent_names]
        self._register_all()

    def _register_all(self):
        for agent in TIER1_AGENTS + TIER2_AGENTS + TIER3_AGENTS:
            self._agents[agent.name] = agent
            self._domain_index.setdefault(agent.domain, []).append(agent.name)
        logger.info("agent_registry.loaded", agents=len(self._agents),
                     tier1=len(TIER1_AGENTS), tier2=len(TIER2_AGENTS), tier3=len(TIER3_AGENTS))

    def get(self, name: str) -> Optional[AgentDefinition]:
        return self._agents.get(name)

    def get_by_domain(self, domain: str) -> Optional[AgentDefinition]:
        """Get the best agent for a domain (prefer Tier 1 > 2 > 3)."""
        names = self._domain_index.get(domain, [])
        for name in names:
            return self._agents[name]
        return None

    def get_for_intent(self, intent: str, entity_type: str) -> Optional[AgentDefinition]:
        """Select agent based on intent + entity type."""
        # Map entity type to domain
        entity_domain_map = {
            "order": "orders", "orders": "orders",
            "shipment": "shipments", "shipments": "shipments", "awb": "shipments",
            "ndr": "ndr", "non-delivery": "ndr",
            "billing": "billing", "wallet": "billing", "refund": "billing",
            "courier": "courier",
            "seller": "settings", "settings": "settings", "plan": "settings",
            "return": "returns", "exchange": "returns",
            "channel": "webhook", "sync": "webhook", "shopify": "webhook",
            "product": "catalog", "sku": "catalog",
            "weight": "discrepancy",
            "report": "analytics",
            "escalation": "escalation", "escalate": "escalation",
            "pickup": "warehouse", "manifest": "warehouse",
            "login": "auth", "password": "auth",
        }

        domain = entity_domain_map.get(entity_type.lower())
        if domain:
            return self.get_by_domain(domain)
        return None

    def list_all(self) -> List[AgentDefinition]:
        return list(self._agents.values())

    def list_by_tier(self, tier: AgentTier) -> List[AgentDefinition]:
        return [a for a in self._agents.values() if a.tier == tier]

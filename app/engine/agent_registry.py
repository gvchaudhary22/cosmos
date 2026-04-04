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
    KNOWLEDGE = "knowledge"     # Tier 4: pillar-first retrieval specialists (COSMOS transfer)


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
        tools_allowed=["order_lookup", "orders_by_company", "order_search", "create_order", "cancel_order", "update_address"],
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


# ===================================================================
# TIER 4: Knowledge Retrieval Specialists (5) — COSMOS pattern
# Pillar-first agents: route by what TYPE of knowledge is needed,
# not just which business domain. These run before domain agents
# when the query is about KB structure rather than live operations.
# ===================================================================

KNOWLEDGE_AGENTS = [
    AgentDefinition(
        name="schema_retriever",
        display_name="Schema Retrieval Agent",
        tier=AgentTier.KNOWLEDGE,
        domain="schema",
        description=(
            "Answers questions about database schema: tables, columns, data types, "
            "status values, enums, foreign keys. Uses P1 (Schema) pillar first, "
            "then follows graph edges to P3 (APIs) for related endpoints."
        ),
        tools_allowed=["kb_lookup", "graph_traverse", "vector_search"],
        knowledge_scope=["P1", "schema", "tables", "columns", "status_values", "enums"],
        instructions=(
            "You are the Schema Retrieval agent. Retrieve from P1 (Schema) pillar first. "
            "For table questions: return column names, types, and status code meanings. "
            "For status value questions: return all enum values with descriptions. "
            "Cross-reference to P3 (APIs) via graph edges to show which endpoints use the table. "
            "Never fabricate column names or status codes — only return what is in KB."
        ),
        anti_patterns=[
            "Never guess column names",
            "Never invent status codes not in KB",
            "Never merge schema info from different tables",
        ],
        can_handoff_to=["api_retriever", "workflow_diagnoser"],
        accepts_handoff_from=["api_retriever", "workflow_diagnoser"],
        examples=[
            {"query": "what columns does orders table have?", "action": "kb_lookup(pillar=P1, entity=orders_table)"},
            {"query": "what are the status values for shipments?", "action": "kb_lookup(pillar=P1, entity=shipment_status_enum)"},
        ],
    ),
    AgentDefinition(
        name="api_retriever",
        display_name="API Retrieval Agent",
        tier=AgentTier.KNOWLEDGE,
        domain="api_docs",
        description=(
            "Answers questions about API endpoints: method, URL, parameters, response format, "
            "authentication. Uses P3 (APIs & Tools) pillar first, then PPR traversal for "
            "related endpoints. Covers all 5,617 endpoints across 8 Shiprocket repos."
        ),
        tools_allowed=["kb_lookup", "graph_traverse", "vector_search", "ppr_traverse"],
        knowledge_scope=["P3", "apis", "endpoints", "parameters", "response", "tools"],
        instructions=(
            "You are the API Retrieval agent. Retrieve from P3 (APIs & Tools) pillar first. "
            "For endpoint questions: return HTTP method, URL path, required/optional params, "
            "response schema, and authentication requirements. "
            "Use PPR traversal to find related endpoints (e.g., create → update → cancel flow). "
            "Cross-reference P1 (Schema) for field types when needed."
        ),
        anti_patterns=[
            "Never invent API endpoints not in KB",
            "Never guess authentication requirements",
            "Never conflate endpoints from different repos",
        ],
        can_handoff_to=["schema_retriever", "action_executor"],
        accepts_handoff_from=["schema_retriever", "action_executor"],
        examples=[
            {"query": "how to call the create order API?", "action": "kb_lookup(pillar=P3, entity=orders_create_endpoint)"},
            {"query": "what endpoint cancels an order?", "action": "vector_search(pillar=P3, query='cancel order endpoint')"},
        ],
    ),
    AgentDefinition(
        name="action_executor",
        display_name="Action Executor Agent",
        tier=AgentTier.KNOWLEDGE,
        domain="action_contracts",
        description=(
            "Answers questions about what actions to take: step-by-step execution, "
            "preconditions, side effects, rollback procedures. Uses P6 (Action Contracts) "
            "pillar first. Covers cancel order, assign courier, update address, etc."
        ),
        tools_allowed=["kb_lookup", "graph_traverse", "vector_search"],
        knowledge_scope=["P6", "action_contracts", "preconditions", "execution", "rollback", "side_effects"],
        instructions=(
            "You are the Action Executor agent. Retrieve from P6 (Action Contracts) pillar first. "
            "For action questions: return preconditions (what must be true before), "
            "step-by-step execution graph, side effects (what changes after), "
            "and rollback procedure (how to undo). "
            "Always check preconditions before presenting execution steps. "
            "If action has approval_mode, state it clearly."
        ),
        anti_patterns=[
            "Never skip precondition checks",
            "Never omit side effects from action responses",
            "Never present rollback as optional",
        ],
        can_handoff_to=["workflow_diagnoser", "api_retriever"],
        accepts_handoff_from=["workflow_diagnoser", "api_retriever"],
        examples=[
            {"query": "how do I cancel an order?", "action": "kb_lookup(pillar=P6, entity=cancel_order_contract)"},
            {"query": "steps to assign a courier", "action": "kb_lookup(pillar=P6, entity=assign_courier_contract)"},
        ],
    ),
    AgentDefinition(
        name="workflow_diagnoser",
        display_name="Workflow Diagnoser Agent",
        tier=AgentTier.KNOWLEDGE,
        domain="workflow_runbooks",
        description=(
            "Diagnoses why something happened and what state a workflow is in. "
            "Uses P7 (Workflow Runbooks) pillar: state machines, decision matrices, "
            "operator playbooks. Handles NDR, RTO, COD remittance, pickup failure workflows."
        ),
        tools_allowed=["kb_lookup", "graph_traverse", "ppr_traverse", "vector_search"],
        knowledge_scope=["P7", "workflows", "state_machines", "ndr", "rto", "cod", "pickup", "runbooks"],
        instructions=(
            "You are the Workflow Diagnoser agent. Retrieve from P7 (Workflow Runbooks) pillar first. "
            "For 'why did X happen' questions: trace the state machine transitions. "
            "For 'stuck at Y' questions: check the decision matrix for that state. "
            "For 'what should operator do' questions: use the operator_playbook. "
            "Always include: current state → valid transitions → invalid transitions → escalation path."
        ),
        anti_patterns=[
            "Never guess state machine transitions",
            "Never omit invalid transitions from diagnosis",
            "Never diagnose without checking P7 runbook first",
        ],
        can_handoff_to=["action_executor", "schema_retriever"],
        accepts_handoff_from=["action_executor", "schema_retriever"],
        examples=[
            {"query": "why did my order go RTO?", "action": "kb_lookup(pillar=P7, entity=rto_workflow_state_machine)"},
            {"query": "NDR pe kya karna chahiye?", "action": "kb_lookup(pillar=P7, entity=ndr_operator_playbook)"},
        ],
    ),
    AgentDefinition(
        name="page_navigator",
        display_name="Page Navigator Agent",
        tier=AgentTier.KNOWLEDGE,
        domain="pages_ui",
        description=(
            "Answers 'where is this field/feature in the UI?' questions. "
            "Uses P4 (Pages & Fields) pillar first: page names, field locations, "
            "field → API → table traces. Covers 24 ICRM pages."
        ),
        tools_allowed=["kb_lookup", "vector_search"],
        knowledge_scope=["P4", "pages", "fields", "ui", "screens", "navigation"],
        instructions=(
            "You are the Page Navigator agent. Retrieve from P4 (Pages & Fields) pillar first. "
            "For 'where is X' questions: return page name, section, and field label. "
            "For 'how to find Y' questions: return navigation path (Menu → Section → Page). "
            "Always include the field → API → table.column trace when available."
        ),
        anti_patterns=[
            "Never guess UI paths without P4 data",
            "Never conflate ICRM admin pages with SR_Web seller pages",
        ],
        can_handoff_to=["api_retriever", "schema_retriever"],
        accepts_handoff_from=["api_retriever"],
        examples=[
            {"query": "where is the COD remittance button?", "action": "kb_lookup(pillar=P4, entity=cod_remittance_page)"},
            {"query": "which screen shows NDR list?", "action": "vector_search(pillar=P4, query='NDR list page')"},
        ],
    ),
]


class AgentRegistry:
    """Central registry for all predefined agents."""

    def __init__(self):
        self._agents: Dict[str, AgentDefinition] = {}
        self._domain_index: Dict[str, List[str]] = {}  # domain → [agent_names]
        self._register_all()

    def _register_all(self):
        for agent in TIER1_AGENTS + TIER2_AGENTS + TIER3_AGENTS + KNOWLEDGE_AGENTS:
            self._agents[agent.name] = agent
            self._domain_index.setdefault(agent.domain, []).append(agent.name)
        logger.info("agent_registry.loaded", agents=len(self._agents),
                     tier1=len(TIER1_AGENTS), tier2=len(TIER2_AGENTS),
                     tier3=len(TIER3_AGENTS), knowledge=len(KNOWLEDGE_AGENTS))

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

    def get_knowledge_agent(self, query: str, pillar_hint: Optional[str] = None) -> Optional[AgentDefinition]:
        """
        Select the best knowledge retrieval specialist for a query.

        Pillar-first routing (COSMOS transfer):
          P1 / schema queries  → schema_retriever
          P3 / api queries     → api_retriever
          P4 / page queries    → page_navigator
          P6 / action queries  → action_executor
          P7 / workflow queries → workflow_diagnoser

        Falls back to keyword matching on query text when pillar_hint is absent.
        """
        # Direct pillar hint takes priority
        pillar_to_agent = {
            "P1": "schema_retriever",
            "P3": "api_retriever",
            "P4": "page_navigator",
            "P6": "action_executor",
            "P7": "workflow_diagnoser",
        }
        if pillar_hint and pillar_hint.upper() in pillar_to_agent:
            return self._agents.get(pillar_to_agent[pillar_hint.upper()])

        # Keyword-based fallback routing
        q = query.lower()
        if any(kw in q for kw in ["what table", "what column", "which column", "status value",
                                    "enum", "schema", "data type", "foreign key"]):
            return self._agents.get("schema_retriever")
        if any(kw in q for kw in ["what endpoint", "which api", "how to call", "api for",
                                    "post /", "get /", "put /", "delete /", "request param"]):
            return self._agents.get("api_retriever")
        if any(kw in q for kw in ["how do i cancel", "how to assign", "steps to", "how to update",
                                    "cancel order", "assign courier", "what should i do"]):
            return self._agents.get("action_executor")
        if any(kw in q for kw in ["why did", "why is", "stuck at", "ndr", "rto", "what happened",
                                    "diagnosis", "diagnose", "went to rto", "not delivered"]):
            return self._agents.get("workflow_diagnoser")
        if any(kw in q for kw in ["where is", "which screen", "which page", "how to find",
                                    "where can i", "which section", "ui", "dashboard"]):
            return self._agents.get("page_navigator")
        return None

    def list_all(self) -> List[AgentDefinition]:
        return list(self._agents.values())

    def list_by_tier(self, tier: AgentTier) -> List[AgentDefinition]:
        return [a for a in self._agents.values() if a.tier == tier]

"""
Skill Registry — Reusable instruction bundles that agents can share.

A Skill = Instructions + Tool References + Triggers + Knowledge Scope

Skills separate "what to do" from "who does it":
  - OrderLookup skill can be shared by OrderOps, NDRResolver, BillingWallet
  - Cancellation skill has specific step-by-step instructions + tools

7 Action Types per skill:
  1. api_call     — Call a registered tool (API endpoint)
  2. prompt       — Call LLM with structured prompt template
  3. handoff      — Transfer to another agent with context
  4. notification — Send email/SMS/WhatsApp
  5. workflow     — Chain multiple actions in sequence
  6. respond      — Send formatted response to user
  7. internal     — Update ticket, set priority, log note
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class ActionType(str, Enum):
    API_CALL = "api_call"
    PROMPT = "prompt"
    HANDOFF = "handoff"
    NOTIFICATION = "notification"
    WORKFLOW = "workflow"
    RESPOND = "respond"
    INTERNAL = "internal"


@dataclass
class SkillAction:
    """One action within a skill."""
    name: str
    action_type: ActionType
    tool_name: Optional[str] = None      # for API_CALL
    prompt_template: Optional[str] = None  # for PROMPT
    handoff_to: Optional[str] = None      # for HANDOFF
    params_template: Dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class SkillDefinition:
    """A reusable skill that agents can use."""
    name: str
    display_name: str
    description: str
    instructions: str                      # natural language instructions
    actions: List[SkillAction]             # ordered list of actions
    triggers: List[str] = field(default_factory=list)  # keywords that activate this skill
    knowledge_tags: List[str] = field(default_factory=list)  # KB entity_types to scope retrieval
    domain: str = ""
    required_tools: List[str] = field(default_factory=list)  # tools this skill needs


# ===================================================================
# PREDEFINED SKILLS
# ===================================================================

SKILLS = [
    # --- Order skills ---
    SkillDefinition(
        name="order_lookup",
        display_name="Order Lookup",
        description="Look up order status, details, and history",
        domain="orders",
        instructions="Always verify order exists. Return: order ID, status (with meaning), customer name, channel, created date.",
        triggers=["order status", "order details", "find order", "check order", "order kahan hai"],
        required_tools=["order_lookup"],
        actions=[
            SkillAction(name="fetch_order", action_type=ActionType.API_CALL, tool_name="order_lookup"),
            SkillAction(name="format_response", action_type=ActionType.RESPOND, description="Format order details for user"),
        ],
    ),
    SkillDefinition(
        name="cancellation",
        display_name="Order Cancellation",
        description="Check eligibility and cancel an order",
        domain="orders",
        instructions=(
            "Step 1: Fetch order details. Step 2: Check if status < SHIPPED (6). "
            "Step 3: If eligible, execute cancel. Step 4: Confirm with order ID. "
            "If NOT eligible (already shipped), inform user and suggest alternatives."
        ),
        triggers=["cancel order", "order cancel", "cancel karo", "cancellation"],
        required_tools=["order_lookup", "cancel_order"],
        actions=[
            SkillAction(name="fetch_order", action_type=ActionType.API_CALL, tool_name="order_lookup"),
            SkillAction(name="check_eligibility", action_type=ActionType.INTERNAL, description="Check status < 6"),
            SkillAction(name="cancel", action_type=ActionType.API_CALL, tool_name="cancel_order"),
            SkillAction(name="confirm", action_type=ActionType.RESPOND, description="Confirm cancellation with order ID"),
        ],
    ),
    SkillDefinition(
        name="address_update",
        display_name="Address Update",
        description="Update shipping address before pickup",
        domain="orders",
        instructions="Verify shipment not picked up yet. Update address. Confirm change.",
        triggers=["update address", "change address", "address galat hai", "wrong address"],
        required_tools=["order_lookup", "update_address"],
        actions=[
            SkillAction(name="fetch_order", action_type=ActionType.API_CALL, tool_name="order_lookup"),
            SkillAction(name="update", action_type=ActionType.API_CALL, tool_name="update_address"),
        ],
    ),

    # --- Shipment skills ---
    SkillDefinition(
        name="awb_tracking",
        display_name="AWB Tracking",
        description="Track shipment by AWB number",
        domain="shipments",
        instructions="Fetch tracking info. Show: current status, location, courier, EDD.",
        triggers=["track awb", "awb status", "kahan pahuncha", "tracking", "delivery status"],
        required_tools=["shipment_track"],
        actions=[
            SkillAction(name="track", action_type=ActionType.API_CALL, tool_name="shipment_track"),
            SkillAction(name="respond", action_type=ActionType.RESPOND),
        ],
    ),
    SkillDefinition(
        name="courier_reassignment",
        display_name="Courier Reassignment",
        description="Reassign shipment to a different courier",
        domain="shipments",
        instructions="Check current courier. Verify reassignment is possible. Execute reassignment.",
        triggers=["reassign courier", "change courier", "different courier"],
        required_tools=["shipment_track", "reassign_courier"],
        actions=[
            SkillAction(name="check_current", action_type=ActionType.API_CALL, tool_name="shipment_track"),
            SkillAction(name="reassign", action_type=ActionType.API_CALL, tool_name="reassign_courier"),
        ],
    ),

    # --- NDR skills ---
    SkillDefinition(
        name="ndr_resolution",
        display_name="NDR Resolution",
        description="Handle non-delivery report: analyze, reattempt, or RTO",
        domain="ndr",
        instructions=(
            "Step 1: Get NDR details + attempt count. "
            "Step 2: Analyze reason (Customer Unavailable / Wrong Address / Refused). "
            "Step 3: If attempt < 3 and fixable → update address/phone → reattempt. "
            "Step 4: If attempt >= 3 → inform RTO will initiate."
        ),
        triggers=["ndr", "non-delivery", "delivery failed", "reattempt", "NDR aaya hai"],
        required_tools=["ndr_details", "reattempt_delivery", "update_address"],
        actions=[
            SkillAction(name="get_ndr", action_type=ActionType.API_CALL, tool_name="ndr_details"),
            SkillAction(name="analyze", action_type=ActionType.INTERNAL, description="Analyze NDR reason + attempt count"),
            SkillAction(name="fix_address", action_type=ActionType.API_CALL, tool_name="update_address"),
            SkillAction(name="reattempt", action_type=ActionType.API_CALL, tool_name="reattempt_delivery"),
        ],
    ),

    # --- Billing skills ---
    SkillDefinition(
        name="refund_processing",
        display_name="Refund Processing",
        description="Process refund for an order",
        domain="billing",
        instructions="Verify order is cancelled/returned. Calculate refund amount. Initiate refund. Confirm.",
        triggers=["refund", "money back", "paisa wapas", "process refund"],
        required_tools=["order_lookup", "initiate_refund"],
        actions=[
            SkillAction(name="verify_order", action_type=ActionType.API_CALL, tool_name="order_lookup"),
            SkillAction(name="refund", action_type=ActionType.API_CALL, tool_name="initiate_refund"),
            SkillAction(name="confirm", action_type=ActionType.RESPOND),
        ],
    ),
    SkillDefinition(
        name="wallet_check",
        display_name="Wallet Balance Check",
        description="Check wallet balance and recent transactions",
        domain="billing",
        instructions="Fetch wallet balance and last 5 transactions.",
        triggers=["wallet balance", "wallet check", "paisa kitna hai"],
        required_tools=["wallet_balance", "transaction_history"],
        actions=[
            SkillAction(name="balance", action_type=ActionType.API_CALL, tool_name="wallet_balance"),
            SkillAction(name="history", action_type=ActionType.API_CALL, tool_name="transaction_history"),
        ],
    ),

    # --- Cross-agent skills ---
    SkillDefinition(
        name="cancel_and_refund",
        display_name="Cancel + Refund Workflow",
        description="Cancel order then process refund (multi-agent)",
        domain="orders",
        instructions="Cancel the order first. If cancel succeeds, handoff to billing for refund.",
        triggers=["cancel and refund", "cancel order refund", "cancel karo aur refund"],
        required_tools=["order_lookup", "cancel_order"],
        actions=[
            SkillAction(name="fetch", action_type=ActionType.API_CALL, tool_name="order_lookup"),
            SkillAction(name="cancel", action_type=ActionType.API_CALL, tool_name="cancel_order"),
            SkillAction(name="handoff_refund", action_type=ActionType.HANDOFF, handoff_to="billing_wallet",
                        description="Pass cancel result to billing for refund processing"),
        ],
    ),
    SkillDefinition(
        name="escalate_to_human",
        display_name="Escalate to Supervisor",
        description="Route to human supervisor when AI cannot resolve",
        domain="escalation",
        instructions="Summarize the issue, what was tried, and why escalation is needed.",
        triggers=["escalate", "supervisor", "human agent", "manager se baat"],
        required_tools=["escalate_to_supervisor"],
        actions=[
            SkillAction(name="summarize", action_type=ActionType.PROMPT,
                        prompt_template="Summarize the conversation: issue, what was tried, why escalating."),
            SkillAction(name="escalate", action_type=ActionType.API_CALL, tool_name="escalate_to_supervisor"),
            SkillAction(name="notify", action_type=ActionType.NOTIFICATION,
                        description="Notify supervisor via internal chat"),
        ],
    ),
]


class SkillRegistry:
    """Central registry for all skills."""

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        self._trigger_index: Dict[str, str] = {}  # trigger_keyword → skill_name
        self._register_all()

    def _register_all(self):
        for skill in SKILLS:
            self._skills[skill.name] = skill
            for trigger in skill.triggers:
                for word in trigger.lower().split():
                    if len(word) > 3:
                        self._trigger_index[word] = skill.name
        logger.info("skill_registry.loaded", skills=len(self._skills))

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def match_query(self, query: str) -> List[SkillDefinition]:
        """Find skills matching a query by trigger keywords."""
        query_words = set(query.lower().split())
        matched = {}
        for word in query_words:
            skill_name = self._trigger_index.get(word)
            if skill_name and skill_name not in matched:
                matched[skill_name] = self._skills[skill_name]
        return list(matched.values())

    def get_for_agent(self, agent_tools: List[str]) -> List[SkillDefinition]:
        """Get skills that an agent can execute (based on its tool list)."""
        result = []
        agent_tool_set = set(agent_tools)
        for skill in self._skills.values():
            if set(skill.required_tools).issubset(agent_tool_set):
                result.append(skill)
        return result

    def list_all(self) -> List[SkillDefinition]:
        return list(self._skills.values())

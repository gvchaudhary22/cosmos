"""
Agent Planner (M2) — Creates execution plans for multi-intent queries.

For simple queries: selects 1 agent, direct execution.
For complex queries: creates a DAG of agent executions with dependencies + handoffs.

Example:
  Query: "Cancel order 12345 and process refund"
  Planner output:
    Step 1: order_ops → cancel_order (sequential)
    Step 2: billing_wallet → initiate_refund (depends on step 1, gets cancel result)

  Query: "Track AWB 9876 and also check seller plan"
  Planner output:
    Step 1: shipment_ops → shipment_track (parallel)
    Step 2: settings_admin → seller_plan (parallel, independent)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum

import structlog

from app.engine.agent_registry import AgentDefinition, AgentRegistry

logger = structlog.get_logger()


class ExecutionMode(str, Enum):
    SEQUENTIAL = "sequential"  # step N depends on step N-1
    PARALLEL = "parallel"      # steps are independent


@dataclass
class PlanStep:
    """One step in an execution plan."""
    step_id: int
    agent_name: str
    intent: str
    entity_type: str
    entity_id: Optional[str] = None
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    depends_on: Optional[int] = None  # step_id this depends on
    context_from: Optional[int] = None  # step_id to get context from
    tool_hints: List[str] = field(default_factory=list)  # suggested tools


@dataclass
class ExecutionPlan:
    """Multi-agent execution plan."""
    query: str
    steps: List[PlanStep] = field(default_factory=list)
    is_multi_agent: bool = False
    estimated_latency_ms: float = 0.0
    planning_rationale: str = ""


@dataclass
class HandoffContext:
    """Context passed between agents during handoff."""
    from_agent: str
    to_agent: str
    query: str
    partial_result: Dict[str, Any] = field(default_factory=dict)
    entities_resolved: Dict[str, Any] = field(default_factory=dict)
    tools_used: List[str] = field(default_factory=list)
    reason: str = ""


class AgentPlanner:
    """Creates execution plans for queries, supporting multi-agent handoff."""

    # Intent pairs that require sequential execution (second depends on first)
    SEQUENTIAL_PAIRS = {
        ("cancel", "refund"): ("order_ops", "billing_wallet"),
        ("cancel", "reattempt"): ("order_ops", "ndr_resolver"),
        ("track", "refund"): ("shipment_ops", "billing_wallet"),
        ("return", "refund"): ("return_exchange", "billing_wallet"),
        ("ndr", "reattempt"): ("ndr_resolver", "shipment_ops"),
        ("escalate", "cancel"): ("order_ops", "escalation_manager"),
    }

    # Intent keywords → domain mapping
    INTENT_DOMAIN_MAP = {
        "cancel": "orders", "status": "orders", "order": "orders", "modify": "orders",
        "track": "shipments", "awb": "shipments", "deliver": "shipments", "ship": "shipments",
        "courier": "courier", "serviceability": "courier", "rate": "courier",
        "ndr": "ndr", "reattempt": "ndr", "non-delivery": "ndr", "rto": "ndr",
        "refund": "billing", "billing": "billing", "wallet": "billing", "charge": "billing",
        "weight": "discrepancy", "discrepancy": "discrepancy",
        "return": "returns", "exchange": "returns",
        "sync": "webhook", "channel": "webhook", "shopify": "webhook", "amazon": "webhook",
        "plan": "settings", "setting": "settings", "kyc": "settings",
        "report": "analytics", "download": "analytics",
        "escalate": "escalation", "supervisor": "escalation",
        "pickup": "warehouse", "manifest": "warehouse",
        "login": "auth", "password": "auth",
        "product": "catalog", "sku": "catalog",
    }

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def plan(self, query: str, intents: List[str], entity_type: str,
             entity_id: Optional[str] = None) -> ExecutionPlan:
        """Create an execution plan for a query.

        Args:
            query: The user's query
            intents: List of detected intents (e.g., ["cancel", "refund"])
            entity_type: Primary entity (e.g., "order")
            entity_id: If extracted (e.g., "12345")
        """
        plan = ExecutionPlan(query=query)

        if len(intents) <= 1:
            # Single intent → single agent
            intent = intents[0] if intents else "lookup"
            agent = self._find_agent(intent, entity_type)
            if agent:
                plan.steps.append(PlanStep(
                    step_id=1,
                    agent_name=agent.name,
                    intent=intent,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    mode=ExecutionMode.SEQUENTIAL,
                    tool_hints=agent.tools_allowed[:3],
                ))
                plan.planning_rationale = f"Single intent '{intent}' → {agent.name}"
            return plan

        # Multi-intent → check for known sequential pairs
        plan.is_multi_agent = True
        used_intents = set()

        # Check sequential dependencies
        for i, intent_a in enumerate(intents):
            for intent_b in intents[i+1:]:
                pair_key = (self._normalize_intent(intent_a), self._normalize_intent(intent_b))
                reverse_key = (pair_key[1], pair_key[0])

                if pair_key in self.SEQUENTIAL_PAIRS:
                    agent_a_name, agent_b_name = self.SEQUENTIAL_PAIRS[pair_key]
                    agent_a = self.registry.get(agent_a_name)
                    agent_b = self.registry.get(agent_b_name)

                    step1 = PlanStep(
                        step_id=len(plan.steps) + 1,
                        agent_name=agent_a_name,
                        intent=intent_a,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        mode=ExecutionMode.SEQUENTIAL,
                    )
                    plan.steps.append(step1)

                    step2 = PlanStep(
                        step_id=len(plan.steps) + 1,
                        agent_name=agent_b_name,
                        intent=intent_b,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        mode=ExecutionMode.SEQUENTIAL,
                        depends_on=step1.step_id,
                        context_from=step1.step_id,
                    )
                    plan.steps.append(step2)

                    used_intents.add(intent_a)
                    used_intents.add(intent_b)
                    plan.planning_rationale = f"Sequential pair: {intent_a} → {intent_b}"

                elif reverse_key in self.SEQUENTIAL_PAIRS:
                    # Reverse order
                    agent_a_name, agent_b_name = self.SEQUENTIAL_PAIRS[reverse_key]
                    step1 = PlanStep(step_id=len(plan.steps)+1, agent_name=agent_a_name,
                                    intent=intent_b, entity_type=entity_type, entity_id=entity_id)
                    plan.steps.append(step1)
                    step2 = PlanStep(step_id=len(plan.steps)+1, agent_name=agent_b_name,
                                    intent=intent_a, entity_type=entity_type, entity_id=entity_id,
                                    depends_on=step1.step_id, context_from=step1.step_id)
                    plan.steps.append(step2)
                    used_intents.update([intent_a, intent_b])

        # Remaining intents → parallel execution
        for intent in intents:
            if intent in used_intents:
                continue
            agent = self._find_agent(intent, entity_type)
            if agent:
                plan.steps.append(PlanStep(
                    step_id=len(plan.steps) + 1,
                    agent_name=agent.name,
                    intent=intent,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    mode=ExecutionMode.PARALLEL,
                ))

        # Estimate latency
        seq_steps = [s for s in plan.steps if s.mode == ExecutionMode.SEQUENTIAL]
        par_steps = [s for s in plan.steps if s.mode == ExecutionMode.PARALLEL]
        plan.estimated_latency_ms = len(seq_steps) * 800 + (400 if par_steps else 0)

        return plan

    def _find_agent(self, intent: str, entity_type: str) -> Optional[AgentDefinition]:
        """Find the best agent for an intent + entity."""
        # Try entity-based first
        agent = self.registry.get_for_intent(intent, entity_type)
        if agent:
            return agent

        # Try intent keyword mapping
        norm_intent = self._normalize_intent(intent)
        domain = self.INTENT_DOMAIN_MAP.get(norm_intent)
        if domain:
            return self.registry.get_by_domain(domain)

        return None

    def _normalize_intent(self, intent: str) -> str:
        """Normalize intent string for matching."""
        intent = intent.lower().strip()
        # Remove common suffixes
        for suffix in ["_lookup", "_create", "_list", "_get", "_update", "_delete"]:
            if intent.endswith(suffix):
                intent = intent[:-len(suffix)]
        return intent

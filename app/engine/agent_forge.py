"""
Agent Forge — MARS dynamic agent creation for COSMOS.

When no existing pipeline has >60% confidence for a query, the forge
dynamically creates a specialized handler by:
1. Analyzing what domain expertise is missing
2. Building a targeted prompt template with domain constraints
3. Registering the new handler for reuse on similar queries
4. Running the query through the forged handler

Forged agents persist across the session and can be promoted to
permanent handlers based on usage metrics.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


FORGE_THRESHOLD = 0.6  # Minimum confidence to skip forging


class AgentCapability(str, Enum):
    """What a forged agent can do."""
    ORDER_TRACE = "order_trace"
    NDR_DIAGNOSIS = "ndr_diagnosis"
    BILLING_DISPUTE = "billing_dispute"
    COURIER_COMPARE = "courier_compare"
    SYNC_DEBUG = "sync_debug"
    ROLE_AUDIT = "role_audit"
    CROSS_REPO_NAV = "cross_repo_navigation"
    CUSTOM = "custom"


@dataclass
class ForgedAgent:
    """A dynamically created specialized agent."""
    agent_id: str
    name: str
    capability: AgentCapability
    domain_expertise: str
    triggers: List[str]       # Keywords that route to this agent
    prompt_template: str      # System prompt for this agent
    tools_available: List[str]
    operating_rules: List[str]
    anti_patterns: List[str]
    created_at: float = field(default_factory=time.time)
    usage_count: int = 0
    avg_confidence: float = 0.0
    promotion_candidate: bool = False  # Ready for permanent registration


@dataclass
class ForgeResult:
    """Result of a forge operation."""
    forged: bool = False
    agent: Optional[ForgedAgent] = None
    response: Optional[str] = None
    confidence: float = 0.0
    latency_ms: float = 0.0
    reused: bool = False  # Was an existing forged agent reused?


# Pre-defined forge templates for common Shiprocket domains
_FORGE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "order_trace": {
        "capability": AgentCapability.ORDER_TRACE,
        "domain_expertise": "End-to-end order lifecycle tracing: creation -> manifest -> pickup -> transit -> delivery",
        "triggers": ["trace", "track", "lifecycle", "journey", "where is", "order path"],
        "tools": ["vector_search", "graph_traverse", "page_role"],
        "rules": [
            "Always trace the full path: order -> shipment -> courier -> delivery",
            "Include timestamps at each stage when available",
            "Flag any stage where delay > expected SLA",
        ],
        "anti_patterns": [
            "Never guess delivery dates -- only state confirmed ETAs",
            "Never expose internal courier contract rates to sellers",
        ],
        "prompt": (
            "You are an Order Trace Specialist for Shiprocket. "
            "Your expertise is tracing the complete lifecycle of an order "
            "from creation through delivery. Use the provided knowledge graph "
            "paths and KB context to reconstruct the order journey. "
            "Always include: current status, location, next expected action, "
            "and any delays with reasons."
        ),
    },
    "ndr_diagnosis": {
        "capability": AgentCapability.NDR_DIAGNOSIS,
        "domain_expertise": "NDR root cause analysis: failed delivery reasons, reattempt eligibility, RTO triggers",
        "triggers": ["ndr", "non-delivery", "failed delivery", "undelivered", "reattempt", "rto"],
        "tools": ["vector_search", "graph_traverse"],
        "rules": [
            "Classify NDR reason: customer unavailable, address issue, refused, damaged",
            "Check reattempt eligibility before suggesting reattempt",
            "If 3+ failed attempts, recommend address verification before reattempt",
        ],
        "anti_patterns": [
            "Never auto-trigger RTO without seller confirmation",
            "Never blame the customer -- use neutral language",
        ],
        "prompt": (
            "You are an NDR Diagnosis Specialist for Shiprocket. "
            "Your expertise is analyzing why deliveries fail and recommending "
            "the best next action. Classify the NDR reason, check attempt history, "
            "and recommend: reattempt, address correction, or RTO. "
            "Always explain the reason clearly to the seller."
        ),
    },
    "billing_dispute": {
        "capability": AgentCapability.BILLING_DISPUTE,
        "domain_expertise": "Billing and weight discrepancy resolution: freight charges, COD remittance, wallet debits",
        "triggers": ["billing", "charge", "overcharge", "weight", "discrepancy", "refund", "wallet", "debit"],
        "tools": ["vector_search", "page_role"],
        "rules": [
            "Always show the charge breakdown: base freight + weight surcharge + COD fee + tax",
            "Compare charged weight vs actual weight when disputing",
            "Reference the rate card applicable to the seller's plan",
        ],
        "anti_patterns": [
            "Never promise refunds without verification",
            "Never expose internal margin calculations",
        ],
        "prompt": (
            "You are a Billing Resolution Specialist for Shiprocket. "
            "Your expertise is resolving billing disputes: weight discrepancies, "
            "overcharges, COD remittance delays, and wallet issues. "
            "Always provide a clear charge breakdown and explain each component. "
            "If a discrepancy is found, outline the refund/credit process."
        ),
    },
    "sync_debug": {
        "capability": AgentCapability.SYNC_DEBUG,
        "domain_expertise": "System sync debugging: webhook failures, status propagation, ICRM <-> seller panel mismatches",
        "triggers": ["sync", "not updating", "mismatch", "webhook", "status stuck", "system"],
        "tools": ["vector_search", "graph_traverse", "cross_repo", "page_role"],
        "rules": [
            "Trace the data path: source system -> API -> webhook -> target system",
            "Check webhook delivery status and retry count",
            "Compare timestamps between systems to find propagation delay",
        ],
        "anti_patterns": [
            "Never expose internal system URLs or credentials",
            "Never suggest manual DB fixes to sellers",
        ],
        "prompt": (
            "You are a System Sync Debugger for Shiprocket. "
            "Your expertise is diagnosing why data isn't syncing between systems. "
            "Trace the data flow path, check webhook delivery status, "
            "and identify where the propagation broke. Explain in seller-friendly "
            "terms what happened and when it will be resolved."
        ),
    },
    "courier_compare": {
        "capability": AgentCapability.COURIER_COMPARE,
        "domain_expertise": "Courier performance comparison: delivery rates, SLA adherence, NDR rates, cost efficiency",
        "triggers": ["courier", "compare", "performance", "best courier", "cheapest", "fastest"],
        "tools": ["vector_search", "graph_traverse"],
        "rules": [
            "Compare on: delivery rate, avg delivery time, NDR %, cost per shipment",
            "Account for zone/weight slab when comparing costs",
            "Include sample size -- don't compare 10 shipments with 10,000",
        ],
        "anti_patterns": [
            "Never share one seller's courier rates with another",
            "Never recommend a courier without data backing",
        ],
        "prompt": (
            "You are a Courier Performance Analyst for Shiprocket. "
            "Your expertise is comparing courier partners on delivery performance, "
            "cost, and reliability. Always provide data-backed comparisons "
            "with proper sample sizes and zone context."
        ),
    },
    "role_audit": {
        "capability": AgentCapability.ROLE_AUDIT,
        "domain_expertise": "Role and permission auditing: who can access what, permission gaps, compliance checks",
        "triggers": ["role", "permission", "access", "who can", "audit", "compliance"],
        "tools": ["page_role", "cross_repo"],
        "rules": [
            "Always reference the role_matrix.yaml for authoritative permissions",
            "Flag any permission that seems overly broad for the role",
            "Check cross-repo permissions -- admin access should be stricter than seller",
        ],
        "anti_patterns": [
            "Never suggest granting admin permissions to resolve access issues",
            "Never expose the full permission matrix to non-admin users",
        ],
        "prompt": (
            "You are a Role & Permission Auditor for Shiprocket. "
            "Your expertise is analyzing who has access to what, finding "
            "permission gaps, and ensuring compliance. Reference the "
            "authoritative role matrix and flag any concerning patterns."
        ),
    },
}


class AgentForge:
    """
    Dynamically creates specialized agents when existing pipelines
    don't have sufficient confidence for a query.

    Maintains a registry of forged agents for session-level reuse.
    Agents that perform well can be promoted to permanent handlers.
    """

    def __init__(self, react_engine=None):
        self.react_engine = react_engine
        self._forged_agents: Dict[str, ForgedAgent] = {}
        self._usage_log: List[Dict] = []

    def should_forge(self, max_confidence: float) -> bool:
        """Check if we need to forge a new agent based on confidence threshold."""
        return max_confidence < FORGE_THRESHOLD

    async def forge_and_execute(
        self,
        query: str,
        intent: str,
        entity: str,
        context: Dict[str, Any],
        pipeline_confidences: Dict[str, float] = None,
    ) -> ForgeResult:
        """
        Either reuse an existing forged agent or create a new one,
        then execute the query through it.
        """
        t0 = time.monotonic()
        pipeline_confidences = pipeline_confidences or {}

        # Check if we have a matching forged agent already
        existing = self._find_matching_agent(query, intent)
        if existing:
            result = await self._execute_with_agent(existing, query, context)
            result.reused = True
            result.latency_ms = (time.monotonic() - t0) * 1000
            existing.usage_count += 1
            return result

        # Forge a new agent
        agent = self._forge_agent(query, intent, entity, pipeline_confidences)
        if agent:
            self._forged_agents[agent.agent_id] = agent
            result = await self._execute_with_agent(agent, query, context)
            result.forged = True
            result.latency_ms = (time.monotonic() - t0) * 1000

            logger.info(
                "agent_forge.created",
                agent_id=agent.agent_id,
                name=agent.name,
                capability=agent.capability.value,
            )

            return result

        return ForgeResult(
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    def _find_matching_agent(self, query: str, intent: str) -> Optional[ForgedAgent]:
        """Find an existing forged agent that matches the query."""
        query_lower = query.lower()
        for agent in self._forged_agents.values():
            # Check if any trigger keyword matches
            if any(trigger in query_lower for trigger in agent.triggers):
                return agent
        return None

    def _forge_agent(
        self,
        query: str,
        intent: str,
        entity: str,
        confidences: Dict[str, float],
    ) -> Optional[ForgedAgent]:
        """Create a new specialized agent based on query analysis."""
        query_lower = query.lower()

        # Try to match a pre-defined template
        for template_key, template in _FORGE_TEMPLATES.items():
            triggers = template["triggers"]
            if any(trigger in query_lower for trigger in triggers):
                agent_id = f"forged_{template_key}_{int(time.time())}"
                return ForgedAgent(
                    agent_id=agent_id,
                    name=f"Forged: {template_key.replace('_', ' ').title()}",
                    capability=template["capability"],
                    domain_expertise=template["domain_expertise"],
                    triggers=triggers,
                    prompt_template=template["prompt"],
                    tools_available=template["tools"],
                    operating_rules=template["rules"],
                    anti_patterns=template["anti_patterns"],
                )

        # No template match -- create a generic custom agent
        agent_id = f"forged_custom_{int(time.time())}"
        return ForgedAgent(
            agent_id=agent_id,
            name=f"Forged: Custom ({intent}/{entity})",
            capability=AgentCapability.CUSTOM,
            domain_expertise=f"Specialized in {intent} queries about {entity}",
            triggers=query_lower.split()[:5],  # Use first 5 words as triggers
            prompt_template=(
                f"You are a specialized assistant for Shiprocket helpdesk. "
                f"The user is asking about {entity} with intent to {intent}. "
                f"Use the provided context to give a precise, data-backed answer. "
                f"If you don't have enough information, say so clearly."
            ),
            tools_available=["vector_search"],
            operating_rules=[
                "Only answer based on provided context",
                "State uncertainty clearly",
            ],
            anti_patterns=[
                "Never fabricate data",
                "Never expose internal system details",
            ],
        )

    async def _execute_with_agent(
        self,
        agent: ForgedAgent,
        query: str,
        context: Dict[str, Any],
    ) -> ForgeResult:
        """Execute query through a forged agent."""
        if not self.react_engine:
            return ForgeResult(
                agent=agent,
                response=f"[Forged Agent: {agent.name}] No execution engine available.",
                confidence=0.3,
            )

        # Build augmented context with agent's prompt template
        augmented_context = {
            "system_prompt": agent.prompt_template,
            "agent_rules": agent.operating_rules,
            "pipeline_context": str(context),
        }

        try:
            result = await self.react_engine.process(query, augmented_context)
            confidence = result.confidence

            # Update agent stats
            agent.avg_confidence = (
                (agent.avg_confidence * agent.usage_count + confidence)
                / (agent.usage_count + 1)
            )

            # Check promotion eligibility
            if agent.usage_count >= 5 and agent.avg_confidence >= 0.7:
                agent.promotion_candidate = True

            return ForgeResult(
                agent=agent,
                response=result.response,
                confidence=confidence,
            )
        except Exception as e:
            logger.error("agent_forge.execute_failed", agent=agent.agent_id, error=str(e))
            return ForgeResult(
                agent=agent,
                response=f"[Forged Agent Error] {str(e)}",
                confidence=0.0,
            )

    def list_forged_agents(self) -> List[Dict[str, Any]]:
        """List all forged agents with stats."""
        return [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "capability": a.capability.value,
                "domain_expertise": a.domain_expertise,
                "triggers": a.triggers,
                "usage_count": a.usage_count,
                "avg_confidence": round(a.avg_confidence, 2),
                "promotion_candidate": a.promotion_candidate,
            }
            for a in self._forged_agents.values()
        ]

    def get_promotion_candidates(self) -> List[ForgedAgent]:
        """Get agents ready for promotion to permanent handlers."""
        return [a for a in self._forged_agents.values() if a.promotion_candidate]

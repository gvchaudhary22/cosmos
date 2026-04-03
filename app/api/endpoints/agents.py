"""
Agent registry endpoints — reads dynamic agents from graph_nodes table.

Provides:
  GET /agents/registry  — List all agents with tools, domains, API counts
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

import structlog

logger = structlog.get_logger()

router = APIRouter()


class AgentResponse(BaseModel):
    name: str
    tier: str = "core"
    domain: str = ""
    domains: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    tools_count: int = 0
    api_count: int = 0
    source: str = "kb"


class AgentRegistryResponse(BaseModel):
    agents: List[AgentResponse] = Field(default_factory=list)
    total: int = 0


def _classify_tier(agent_name: str, api_count: int, tools_count: int) -> str:
    """Classify agent tier based on name patterns and volume."""
    core_agents = {
        "order_ops", "shipment_ops", "courier_ops",
        "settings_admin", "billing_wallet", "ndr_resolver",
    }
    specialized_agents = {
        "return_exchange", "channel_sync", "catalog_products",
        "weight_dispute", "international_ship", "report_analytics",
    }
    if agent_name in core_agents:
        return "CORE"
    if agent_name in specialized_agents:
        return "SPECIALIZED"
    if api_count >= 50 or tools_count >= 5:
        return "CORE"
    if api_count >= 20:
        return "SPECIALIZED"
    return "OPERATIONAL"


@router.get("/registry", response_model=AgentRegistryResponse)
async def list_agents():
    """List all agents from graph_nodes + graph_edges (KB-driven)."""
    try:
        async with AsyncSessionLocal() as session:
            # Get all agent nodes
            rows = await session.execute(text("""
                SELECT id, label, domain, properties
                FROM graph_nodes
                WHERE node_type = 'agent'
                ORDER BY label
            """))
            agents_raw = rows.fetchall()

            if not agents_raw:
                return AgentRegistryResponse(agents=[], total=0)

            # Build agent map
            agents: Dict[str, AgentResponse] = {}
            for row in agents_raw:
                name = row.label or row.id.replace("agent:", "")
                props = row.properties or {}
                domains = []
                if row.domain:
                    domains.append(row.domain)

                agents[name] = AgentResponse(
                    name=name,
                    domain=row.domain or "",
                    domains=domains,
                    source="kb",
                )

            # Get tool assignments: which tools belong to which agent
            tool_edges = await session.execute(text("""
                SELECT DISTINCT e1.target_id as tool_id, e2.target_id as agent_id
                FROM graph_edges e1
                JOIN graph_edges e2 ON e1.source_id = e2.source_id
                WHERE e1.edge_type = 'implements_tool'
                  AND e2.edge_type = 'assigned_to_agent'
            """))
            for row in tool_edges.fetchall():
                tool_name = row.tool_id.replace("tool:", "")
                agent_name = row.agent_id.replace("agent:", "")
                if agent_name in agents:
                    if tool_name not in agents[agent_name].tools:
                        agents[agent_name].tools.append(tool_name)

            # Get API counts per agent
            api_counts = await session.execute(text("""
                SELECT target_id as agent_id, COUNT(*) as cnt
                FROM graph_edges
                WHERE edge_type = 'assigned_to_agent'
                GROUP BY target_id
            """))
            for row in api_counts.fetchall():
                agent_name = row.agent_id.replace("agent:", "")
                if agent_name in agents:
                    agents[agent_name].api_count = row.cnt

            # Get domains from linked APIs
            domain_edges = await session.execute(text("""
                SELECT e.target_id as agent_id, n.domain
                FROM graph_edges e
                JOIN graph_nodes n ON e.source_id = n.id
                WHERE e.edge_type = 'assigned_to_agent'
                  AND n.domain IS NOT NULL AND n.domain != ''
                GROUP BY e.target_id, n.domain
            """))
            for row in domain_edges.fetchall():
                agent_name = row.agent_id.replace("agent:", "")
                if agent_name in agents and row.domain not in agents[agent_name].domains:
                    agents[agent_name].domains.append(row.domain)

            # Finalize
            result = []
            for agent in agents.values():
                agent.tools_count = len(agent.tools)
                agent.tier = _classify_tier(agent.name, agent.api_count, agent.tools_count)
                if not agent.domain and agent.domains:
                    agent.domain = agent.domains[0]
                result.append(agent)

            # Sort: CORE first, then SPECIALIZED, then OPERATIONAL
            tier_order = {"CORE": 0, "SPECIALIZED": 1, "OPERATIONAL": 2}
            result.sort(key=lambda a: (tier_order.get(a.tier, 3), a.name))

            return AgentRegistryResponse(agents=result, total=len(result))

    except Exception as e:
        logger.error("agents.registry_failed", error=str(e))
        return AgentRegistryResponse(agents=[], total=0)

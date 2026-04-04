"""
KB-Driven Registry — Auto-registers tools, agents, skills from the knowledge base.

Replaces hardcoded Python definitions with KB-driven discovery.
Reads from GraphRAG nodes (tool:, agent:, intent:) + KB high.yaml files.

Flow:
  1. Pipeline ingests KB → creates graph nodes (tool:X, agent:Y, intent:Z)
  2. At startup (or after pipeline run): sync_all() reads graph → builds registries
  3. Runtime uses KB-driven registries instead of hardcoded ones

What it creates:
  - DynamicTool: tool with name, params, risk, endpoint from KB
  - DynamicAgent: agent with tools, scope, handoff rules from KB
  - DynamicSkill: skill with triggers, actions, intent from KB

Coexists with hardcoded tools — KB tools supplement, don't replace existing ones.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


# ===================================================================
# Dynamic Tool (created from KB)
# ===================================================================

@dataclass
class DynamicTool:
    """A tool auto-discovered from KB with OpenAI-compatible parameter schema."""
    name: str                           # tool_candidate from KB
    domain: str                         # classification.domain
    risk_level: str                     # low / medium / high
    read_write: str                     # READ / WRITE
    endpoints: List[Dict[str, str]]     # [{method, path, controller}, ...]
    params: List[Dict[str, str]]        # [{name, type, validation}, ...]
    response_fields: List[str]          # [id, status, ...]
    events: List[str]                   # events triggered
    jobs: List[str]                     # jobs dispatched
    agent_owner: str                    # which agent owns this
    api_count: int = 0                  # how many APIs map to this tool
    api_layer: str = "system"           # system / process / experience (from APILayerClassifier)
    source: str = "kb"


@dataclass
class DynamicAgent:
    """An agent auto-discovered from KB with system prompt and handoff rules."""
    name: str                           # agent_assignment.owner
    domains: Set[str] = field(default_factory=set)
    tools: Set[str] = field(default_factory=set)
    api_count: int = 0
    secondary_for: Set[str] = field(default_factory=set)
    source: str = "kb"
    # Enriched fields (populated by _enrich_agents)
    display_name: str = ""
    tier: str = "operational"
    system_prompt: str = ""
    anti_patterns: List[str] = field(default_factory=list)
    handoff_rules: Dict[str, str] = field(default_factory=dict)


@dataclass
class DynamicSkill:
    """A skill auto-discovered from KB with triggers, steps, and required params."""
    name: str                           # intent_tags.primary
    tools: Set[str] = field(default_factory=set)
    domains: Set[str] = field(default_factory=set)
    example_queries: List[str] = field(default_factory=list)
    api_count: int = 0
    source: str = "kb"
    # Enriched fields (populated by _enrich_skills)
    triggers: List[str] = field(default_factory=list)
    steps: List[Dict[str, str]] = field(default_factory=list)
    required_params: List[Dict[str, str]] = field(default_factory=list)


# ===================================================================
# KB-Driven Registry
# ===================================================================

class KBDrivenRegistry:
    """Reads graph nodes + KB files to build dynamic tool/agent/skill registries.

    Usage:
        registry = KBDrivenRegistry(kb_path)
        await registry.sync_all()

        # Access discovered tools/agents/skills
        tools = registry.tools          # {name: DynamicTool}
        agents = registry.agents        # {name: DynamicAgent}
        skills = registry.skills        # {name: DynamicSkill}
    """

    def __init__(self, kb_path: str = ""):
        self.kb_path = kb_path
        self.tools: Dict[str, DynamicTool] = {}
        self.agents: Dict[str, DynamicAgent] = {}
        self.skills: Dict[str, DynamicSkill] = {}
        self._synced = False

    async def sync_all(self):
        """Read graph + KB to discover and register all tools, agents, skills."""
        logger.info("kb_registry.sync_start")

        try:
            # Method 1: Read from GraphRAG nodes (fast, already indexed)
            await self._sync_from_graph()
        except Exception as e:
            logger.warning("kb_registry.graph_sync_failed", error=str(e))

        # Method 2: Enrich from KB files (params, response, examples)
        if self.kb_path:
            try:
                self._enrich_from_kb()
            except Exception as e:
                logger.warning("kb_registry.kb_enrich_failed", error=str(e))

        # Enrich agents with system prompts, handoff rules, anti-patterns
        self._enrich_agents()

        # Enrich skills with trigger keywords, steps, required params
        self._enrich_skills()

        # Classify tools into API layers (Experience > Process > System)
        try:
            from app.engine.api_layer import APILayerClassifier
            layer_classifier = APILayerClassifier()
            layer_map = layer_classifier.classify_all(self.tools)
            for tool_name, layer in layer_map.items():
                if tool_name in self.tools:
                    self.tools[tool_name].api_layer = layer
            logger.info("kb_registry.api_layers_classified", layers=len(layer_map))
        except Exception as e:
            logger.debug("kb_registry.api_layer_classification_failed", error=str(e))

        # Bifurcate coarse tools into finer-grained ones
        await self.bifurcate_tools()

        # Write enriched properties back to graph_nodes in MARS DB
        await self._writeback_enriched_properties()

        self._synced = True
        logger.info("kb_registry.sync_complete",
                     tools=len(self.tools),
                     agents=len(self.agents),
                     skills=len(self.skills))

    # ------------------------------------------------------------------
    # Sync from GraphRAG (graph_nodes + graph_edges)
    # ------------------------------------------------------------------

    async def _sync_from_graph(self):
        """Read tool:, agent:, intent: nodes from graph_nodes table."""
        async with AsyncSessionLocal() as session:
            # Get all tool nodes
            rows = await session.execute(text("""
                SELECT id, label, domain, properties
                FROM graph_nodes
                WHERE node_type = 'tool'
                ORDER BY label
            """))
            for row in rows.fetchall():
                raw_props = row.properties or {}
                props = raw_props if isinstance(raw_props, dict) else (
                    __import__("json").loads(raw_props) if isinstance(raw_props, str) else {}
                )
                name = row.label or row.id.replace("tool:", "")
                self.tools[name] = DynamicTool(
                    name=name,
                    domain=row.domain or props.get("domain", ""),
                    risk_level=props.get("risk_level", "medium"),
                    read_write=props.get("read_write_type", "READ"),
                    endpoints=[],
                    params=[],
                    response_fields=[],
                    events=[],
                    jobs=[],
                    agent_owner="",
                )

            # Get all agent nodes
            rows = await session.execute(text("""
                SELECT id, label, domain, properties
                FROM graph_nodes
                WHERE node_type = 'agent'
                ORDER BY label
            """))
            for row in rows.fetchall():
                name = row.label or row.id.replace("agent:", "")
                self.agents[name] = DynamicAgent(
                    name=name,
                    domains={row.domain} if row.domain else set(),
                )

            # Get all intent nodes (= skills)
            rows = await session.execute(text("""
                SELECT id, label, domain, properties
                FROM graph_nodes
                WHERE node_type = 'intent'
                ORDER BY label
            """))
            for row in rows.fetchall():
                name = row.label or row.id.replace("intent:", "")
                self.skills[name] = DynamicSkill(
                    name=name,
                    domains={row.domain} if row.domain else set(),
                )

            # Get edges to link tools → agents and tools → intents
            edges = await session.execute(text("""
                SELECT source_id, target_id, edge_type, properties
                FROM graph_edges
                WHERE edge_type IN ('implements_tool', 'assigned_to_agent', 'has_intent')
                ORDER BY source_id
            """))
            for edge in edges.fetchall():
                src = edge.source_id       # api:xxx
                tgt = edge.target_id       # tool:xxx or agent:xxx or intent:xxx
                etype = edge.edge_type
                props = edge.properties or {}

                if etype == "implements_tool":
                    tool_name = tgt.replace("tool:", "")
                    if tool_name in self.tools:
                        self.tools[tool_name].api_count += 1

                elif etype == "assigned_to_agent":
                    agent_name = tgt.replace("agent:", "")
                    if agent_name in self.agents:
                        self.agents[agent_name].api_count += 1
                        # Find which tool this API implements
                        api_id = src

                elif etype == "has_intent":
                    intent_name = tgt.replace("intent:", "")
                    if intent_name in self.skills:
                        self.skills[intent_name].api_count += 1

            # Link tools to agents via shared APIs
            tool_agent_edges = await session.execute(text("""
                SELECT DISTINCT e1.target_id as tool_id, e2.target_id as agent_id
                FROM graph_edges e1
                JOIN graph_edges e2 ON e1.source_id = e2.source_id
                WHERE e1.edge_type = 'implements_tool'
                  AND e2.edge_type = 'assigned_to_agent'
            """))
            for row in tool_agent_edges.fetchall():
                tool_name = row.tool_id.replace("tool:", "")
                agent_name = row.agent_id.replace("agent:", "")
                if tool_name in self.tools:
                    self.tools[tool_name].agent_owner = agent_name
                if agent_name in self.agents:
                    self.agents[agent_name].tools.add(tool_name)

            # Link skills to tools via shared APIs
            tool_intent_edges = await session.execute(text("""
                SELECT DISTINCT e1.target_id as tool_id, e2.target_id as intent_id
                FROM graph_edges e1
                JOIN graph_edges e2 ON e1.source_id = e2.source_id
                WHERE e1.edge_type = 'implements_tool'
                  AND e2.edge_type = 'has_intent'
            """))
            for row in tool_intent_edges.fetchall():
                tool_name = row.tool_id.replace("tool:", "")
                intent_name = row.intent_id.replace("intent:", "")
                if intent_name in self.skills:
                    self.skills[intent_name].tools.add(tool_name)

    # ------------------------------------------------------------------
    # Enrich from KB files (params, response, examples)
    # ------------------------------------------------------------------

    def _enrich_from_kb(self):
        """Read high.yaml files to get params, response fields, examples."""
        import yaml
        from pathlib import Path

        kb = Path(self.kb_path)
        if not kb.exists():
            return

        for repo_dir in sorted(kb.iterdir()):
            if not repo_dir.is_dir():
                continue

            apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            if not apis_dir.exists():
                continue

            for api_dir in apis_dir.iterdir():
                if not api_dir.is_dir():
                    continue

                hf = api_dir / "high.yaml"
                if not hf.exists():
                    continue

                try:
                    data = yaml.safe_load(open(hf))
                    if not data:
                        continue
                except Exception:
                    continue

                # Get tool name
                tags = data.get("tool_agent_tags", {})
                if not isinstance(tags, dict):
                    continue
                tool_block = tags.get("tool_assignment", {})
                tool_name = tool_block.get("tool_candidate", "")
                if not tool_name or tool_name not in self.tools:
                    continue

                tool = self.tools[tool_name]

                # Enrich with endpoint info
                ov = data.get("overview", {})
                if isinstance(ov, dict):
                    api = ov.get("api", {})
                    if api.get("path"):
                        tool.endpoints.append({
                            "method": api.get("method", "?"),
                            "path": api["path"],
                            "controller": api.get("controller", "unknown"),
                        })

                # Enrich with params (take from first API that has them)
                if not tool.params:
                    req = data.get("request_schema", {})
                    if isinstance(req, dict):
                        contract = req.get("contract", {})
                        params = contract.get("required", contract.get("discovered_params", []))
                        if isinstance(params, list):
                            tool.params = [
                                {"name": p.get("name", ""), "type": p.get("type", "string"),
                                 "validation": p.get("validation", "")}
                                for p in params[:30] if isinstance(p, dict)
                            ]

                # Enrich with response fields
                if not tool.response_fields:
                    resp = data.get("response_fields", {})
                    if isinstance(resp, dict):
                        tool.response_fields = resp.get("fields", [])[:20]
                        tool.events = resp.get("events_triggered", [])
                        tool.jobs = resp.get("jobs_dispatched", [])

                # Enrich skills with example queries
                examples = data.get("examples", {})
                if isinstance(examples, dict):
                    intent_block = tags.get("intent_tags", {})
                    primary_intent = intent_block.get("primary", "")
                    if primary_intent and primary_intent in self.skills:
                        skill = self.skills[primary_intent]
                        for pair in examples.get("param_extraction_pairs", [])[:5]:
                            if isinstance(pair, dict) and pair.get("query"):
                                if pair["query"] not in skill.example_queries:
                                    skill.example_queries.append(pair["query"])

                # Enrich agent domains
                agent_block = tags.get("agent_assignment", {})
                agent_name = agent_block.get("owner", "")
                if agent_name and agent_name in self.agents:
                    classification = ov.get("classification", {}) if isinstance(ov, dict) else {}
                    domain = classification.get("domain", "")
                    if domain:
                        self.agents[agent_name].domains.add(domain)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tool(self, name: str) -> Optional[DynamicTool]:
        return self.tools.get(name)

    def get_agent(self, name: str) -> Optional[DynamicAgent]:
        return self.agents.get(name)

    def get_agent_for_domain(self, domain: str) -> Optional[DynamicAgent]:
        for agent in self.agents.values():
            if domain in agent.domains:
                return agent
        return None

    def get_skill(self, name: str) -> Optional[DynamicSkill]:
        return self.skills.get(name)

    def get_tools_for_agent(self, agent_name: str) -> List[DynamicTool]:
        agent = self.agents.get(agent_name)
        if not agent:
            return []
        return [self.tools[t] for t in agent.tools if t in self.tools]

    async def bifurcate_tools(self):
        """Split coarse tools into finer-grained tools based on API path patterns.

        A tool with 300+ APIs like 'orders_create' gets split into:
        orders_create_orders (66), orders_create_hyperlocal (43), orders_cancel (29), etc.
        """
        if not self.tools:
            return

        ACTION_GROUPS = {
            "lookup": {"get", "show", "detail", "fetch", "status", "track", "timeline", "info"},
            "list": {"list", "index", "search", "filter", "count"},
            "create": {"create", "store", "add", "import", "upload", "adhoc", "bulk"},
            "cancel": {"cancel", "delete", "destroy", "remove", "archive"},
            "update": {"update", "edit", "modify", "patch", "assign", "reassign"},
            "export": {"export", "download", "report", "manifest", "print", "invoice"},
            "action": {"refund", "recharge", "credit", "verify", "check", "escalate", "sync", "block", "reattempt"},
        }

        new_tools = {}
        for name, tool in self.tools.items():
            if tool.api_count <= 50:
                # Small enough — keep as-is
                new_tools[name] = tool
                continue

            # Split by action group from endpoint paths
            sub_tools = {}
            for ep in tool.endpoints:
                path = ep.get("path", "").lower()
                segs = [s for s in path.split("/") if s and s not in ("api", "v1", "internal", "external", "admin", "app") and not s.startswith("{")]

                action = "general"
                for seg in reversed(segs):
                    for group, keywords in ACTION_GROUPS.items():
                        if any(kw in seg for kw in keywords):
                            action = group
                            break
                    if action != "general":
                        break

                sub_name = f"{name}_{action}"
                if sub_name not in sub_tools:
                    sub_tools[sub_name] = DynamicTool(
                        name=sub_name, domain=tool.domain, risk_level=tool.risk_level,
                        read_write=tool.read_write, endpoints=[], params=tool.params,
                        response_fields=tool.response_fields, events=tool.events,
                        jobs=tool.jobs, agent_owner=tool.agent_owner,
                    )
                sub_tools[sub_name].endpoints.append(ep)
                sub_tools[sub_name].api_count += 1

            # Keep splits with >= 3 APIs, merge rest back
            for sn, st in sub_tools.items():
                if st.api_count >= 3:
                    new_tools[sn] = st
                else:
                    misc_name = f"{name}_misc"
                    if misc_name not in new_tools:
                        new_tools[misc_name] = DynamicTool(
                            name=misc_name, domain=tool.domain, risk_level=tool.risk_level,
                            read_write=tool.read_write, endpoints=[], params=tool.params,
                            response_fields=tool.response_fields, events=tool.events,
                            jobs=tool.jobs, agent_owner=tool.agent_owner,
                        )
                    new_tools[misc_name].endpoints.extend(st.endpoints)
                    new_tools[misc_name].api_count += st.api_count

        # Second pass: split tools still >50 APIs by path resource
        final_tools = {}
        for name, tool in new_tools.items():
            if tool.api_count <= 50:
                final_tools[name] = tool
                continue

            # Split by first meaningful path segment (the resource)
            sub = {}
            for ep in tool.endpoints:
                path = ep.get("path", "").lower()
                segs = [s for s in path.split("/")
                        if s and s not in ("api", "v1", "v1.1", "internal", "external",
                                           "admin", "app", "oneapp") and not s.startswith("{")]
                resource = segs[0] if segs else "misc"
                # Clean resource name
                resource = resource.replace("-", "_")[:20]
                sub_name = f"{name}_{resource}"

                if sub_name not in sub:
                    sub[sub_name] = DynamicTool(
                        name=sub_name, domain=tool.domain, risk_level=tool.risk_level,
                        read_write=tool.read_write, endpoints=[], params=tool.params,
                        response_fields=tool.response_fields, events=tool.events,
                        jobs=tool.jobs, agent_owner=tool.agent_owner,
                    )
                sub[sub_name].endpoints.append(ep)
                sub[sub_name].api_count += 1

            # Keep splits >= 3, merge rest
            for sn, st in sub.items():
                if st.api_count >= 3:
                    final_tools[sn] = st
                else:
                    misc = f"{name}_misc"
                    if misc not in final_tools:
                        final_tools[misc] = DynamicTool(
                            name=misc, domain=tool.domain, risk_level=tool.risk_level,
                            read_write=tool.read_write, endpoints=[], params=tool.params,
                            response_fields=tool.response_fields, events=tool.events,
                            jobs=tool.jobs, agent_owner=tool.agent_owner,
                        )
                    final_tools[misc].endpoints.extend(st.endpoints)
                    final_tools[misc].api_count += st.api_count

        old_count = len(self.tools)
        self.tools = final_tools

        # Update agent tool lists
        for agent in self.agents.values():
            new_agent_tools = set()
            for old_tool in agent.tools:
                for new_name in new_tools:
                    if new_name.startswith(old_tool) or new_name == old_tool:
                        new_agent_tools.add(new_name)
            agent.tools = new_agent_tools

        logger.info("kb_registry.bifurcated", old_tools=old_count, new_tools=len(self.tools))

    # ------------------------------------------------------------------
    # Improvement 1: Enrich tool nodes with OpenAI-compatible param schemas
    # (already done in _enrich_from_kb — params, response_fields, endpoints)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Improvement 2: Enrich skill/intent nodes with triggers + steps
    # ------------------------------------------------------------------

    def _enrich_skills(self):
        """Derive skill definitions from tool/agent relationships and KB data.

        For each skill (intent), build:
        - triggers: keywords that activate this skill
        - steps: sequence of actions (api_call → respond)
        - required_params: from linked tools' params
        - response_format: from linked tools' response_fields
        """
        for skill_name, skill in self.skills.items():
            # Triggers: derive from skill name + domain keywords
            triggers = []
            parts = skill_name.replace("_", " ").split()
            # Add natural language variants
            if len(parts) >= 2:
                triggers.append(" ".join(parts))  # "orders create"
                triggers.append(f"{parts[-1]} {parts[0]}")  # "create orders"
            # Add example queries already collected from KB
            for eq in skill.example_queries[:5]:
                if eq not in triggers:
                    triggers.append(eq)

            # Steps: derive from linked tools
            steps = []
            for tool_name in sorted(skill.tools):
                tool = self.tools.get(tool_name)
                if tool:
                    rw = tool.read_write if hasattr(tool, 'read_write') else "READ"
                    steps.append({
                        "type": "api_call",
                        "tool": tool_name,
                        "read_write": rw,
                        "description": f"Call {tool_name} ({rw})",
                    })
            steps.append({"type": "respond", "description": "Format and return results to user"})

            # Required params: union of all linked tools' required params
            required_params = []
            seen_params = set()
            for tool_name in skill.tools:
                tool = self.tools.get(tool_name)
                if tool and hasattr(tool, 'params'):
                    for p in tool.params:
                        pname = p.get("name", "") if isinstance(p, dict) else ""
                        if pname and pname not in seen_params:
                            seen_params.add(pname)
                            required_params.append(p)

            # Store enriched data
            skill.triggers = triggers
            skill.steps = steps
            skill.required_params = required_params

    # ------------------------------------------------------------------
    # Improvement 3: Enrich agent nodes with system prompt + handoff rules
    # ------------------------------------------------------------------

    def _enrich_agents(self):
        """Derive agent definitions from their tools, domains, and relationships.

        For each agent, build:
        - display_name: human-readable name
        - system_prompt: behavioral instructions
        - anti_patterns: what the agent should NOT do
        - handoff_rules: which agents to hand off to and when
        - tier: core/specialized/operational
        """
        CORE_AGENTS = {
            "order_ops_agent", "shipment_ops_agent", "courier_ops_agent",
            "admin_agent", "finance_agent", "ndr_agent",
        }
        SPECIALIZED_AGENTS = {
            "catalog_agent", "auth_agent", "integrations_agent",
            "analytics_agent", "support_agent",
        }

        # Build handoff map: agent → set of agents it shares APIs with
        agent_cooccurrence: Dict[str, Set[str]] = {}
        for agent_name in self.agents:
            agent_cooccurrence[agent_name] = set()
        for tool in self.tools.values():
            owner = tool.agent_owner if hasattr(tool, 'agent_owner') else ""
            if owner:
                for other_agent in self.agents:
                    if other_agent != owner and other_agent in (
                        getattr(tool, 'secondary_agents', set()) or set()
                    ):
                        agent_cooccurrence.setdefault(owner, set()).add(other_agent)

        # Domain-based handoff rules
        DOMAIN_HANDOFFS = {
            "orders": {"shipment_ops_agent": "when order ships", "finance_agent": "when refund needed", "ndr_agent": "when delivery fails"},
            "shipments": {"ndr_agent": "when NDR occurs", "order_ops_agent": "for order context", "courier_ops_agent": "for courier issues"},
            "billing": {"order_ops_agent": "for order verification", "support_agent": "for escalation"},
            "courier": {"shipment_ops_agent": "for tracking", "finance_agent": "for billing disputes"},
            "ndr": {"shipment_ops_agent": "for reattempt", "order_ops_agent": "for order context", "support_agent": "for escalation"},
            "settings": {"auth_agent": "for login issues", "finance_agent": "for plan/billing"},
            "catalog": {"order_ops_agent": "when product linked to order"},
            "auth": {"admin_agent": "for account lockouts", "support_agent": "for password resets"},
        }

        # Anti-patterns by domain
        DOMAIN_ANTI_PATTERNS = {
            "orders": ["Never process refunds directly", "Never delete order records", "Never modify pricing"],
            "shipments": ["Never reassign courier after delivery", "Never modify AWB after pickup"],
            "billing": ["Never issue credits without verification", "Never expose full payment details"],
            "courier": ["Never share courier credentials", "Never bypass serviceability checks"],
            "ndr": ["Never auto-RTO without 3 attempts", "Never share customer phone externally"],
            "settings": ["Never change plan without confirmation", "Never expose API keys"],
        }

        for agent_name, agent in self.agents.items():
            # Display name
            display_name = agent_name.replace("_agent", "").replace("_", " ").title() + " Agent"

            # Tier
            if agent_name in CORE_AGENTS:
                tier = "core"
            elif agent_name in SPECIALIZED_AGENTS:
                tier = "specialized"
            elif agent.api_count >= 500:
                tier = "core"
            elif agent.api_count >= 100:
                tier = "specialized"
            else:
                tier = "operational"

            # System prompt
            domain_list = sorted(agent.domains) if agent.domains else ["general"]
            tool_names = sorted(agent.tools) if agent.tools else []
            read_tools = [t for t in tool_names if self.tools.get(t) and self.tools[t].read_write == "READ"]
            write_tools = [t for t in tool_names if self.tools.get(t) and self.tools[t].read_write != "READ"]

            system_prompt = (
                f"You are the {display_name}, responsible for {', '.join(domain_list)} operations. "
                f"You have access to {len(tool_names)} tools ({len(read_tools)} read, {len(write_tools)} write). "
            )
            if write_tools:
                system_prompt += f"For write operations ({', '.join(write_tools[:3])}), always verify data before executing. "
            system_prompt += "If you cannot resolve the query within 3 attempts, escalate to a supervisor."

            # Handoff rules
            primary_domain = domain_list[0] if domain_list else ""
            handoff_rules = DOMAIN_HANDOFFS.get(primary_domain, {})
            # Also add co-occurring agents
            for co_agent in agent_cooccurrence.get(agent_name, set()):
                if co_agent not in handoff_rules:
                    handoff_rules[co_agent] = "shared context"

            # Anti-patterns
            anti_patterns = DOMAIN_ANTI_PATTERNS.get(primary_domain, [])

            # Store enriched data
            agent.display_name = display_name
            agent.tier = tier
            agent.system_prompt = system_prompt
            agent.anti_patterns = anti_patterns
            agent.handoff_rules = handoff_rules

    # ------------------------------------------------------------------
    # Writeback: push enriched properties to graph_nodes in MARS DB
    # ------------------------------------------------------------------

    async def _writeback_enriched_properties(self):
        """Write enriched tool/agent/skill properties back to graph_nodes table."""
        try:
            async with AsyncSessionLocal() as session:
                updated = 0

                # Write enriched tool properties
                for name, tool in self.tools.items():
                    props = {
                        "tool_group": getattr(tool, 'domain', ''),
                        "read_write_type": getattr(tool, 'read_write', ''),
                        "risk_level": getattr(tool, 'risk_level', ''),
                        "api_count": getattr(tool, 'api_count', 0),
                        "agent_owner": getattr(tool, 'agent_owner', ''),
                        # Improvement 1: parameter schemas
                        "parameters": getattr(tool, 'params', [])[:20],
                        "response_fields": getattr(tool, 'response_fields', [])[:20],
                        "events": getattr(tool, 'events', []),
                        "jobs": getattr(tool, 'jobs', []),
                        "endpoints_sample": [
                            {"method": e.get("method", ""), "path": e.get("path", ""), "controller": e.get("controller", "")}
                            for e in getattr(tool, 'endpoints', [])[:10]
                        ],
                    }
                    await session.execute(
                        text("UPDATE graph_nodes SET properties = :props, updated_at = NOW() WHERE id = :id"),
                        {"id": f"tool:{name}", "props": json.dumps(props, default=str)},
                    )
                    updated += 1

                # Write enriched agent properties
                for name, agent in self.agents.items():
                    props = {
                        "display_name": getattr(agent, 'display_name', name),
                        "tier": getattr(agent, 'tier', 'operational'),
                        "system_prompt": getattr(agent, 'system_prompt', ''),
                        "anti_patterns": getattr(agent, 'anti_patterns', []),
                        "handoff_rules": {k: v for k, v in getattr(agent, 'handoff_rules', {}).items()},
                        "tools": sorted(agent.tools) if agent.tools else [],
                        "domains": sorted(agent.domains) if agent.domains else [],
                        "api_count": agent.api_count,
                        "max_loops": 3,
                        "escalation": "after 3 failed attempts → escalate to supervisor",
                    }
                    await session.execute(
                        text("UPDATE graph_nodes SET properties = :props, updated_at = NOW() WHERE id = :id"),
                        {"id": f"agent:{name}", "props": json.dumps(props, default=str)},
                    )
                    updated += 1

                # Write enriched skill properties
                for name, skill in self.skills.items():
                    props = {
                        "display_name": name.replace("_", " ").title(),
                        "triggers": getattr(skill, 'triggers', [])[:20],
                        "steps": getattr(skill, 'steps', []),
                        "required_params": getattr(skill, 'required_params', [])[:15],
                        "tools": sorted(skill.tools) if skill.tools else [],
                        "domains": sorted(skill.domains) if skill.domains else [],
                        "example_queries": skill.example_queries[:10],
                        "api_count": skill.api_count,
                    }
                    await session.execute(
                        text("UPDATE graph_nodes SET properties = :props, updated_at = NOW() WHERE id = :id"),
                        {"id": f"intent:{name}", "props": json.dumps(props, default=str)},
                    )
                    updated += 1

                await session.commit()
                logger.info("kb_registry.writeback_complete", updated=updated)

        except Exception as e:
            logger.error("kb_registry.writeback_failed", error=str(e))

    def get_stats(self) -> Dict[str, Any]:
        return {
            "tools": len(self.tools),
            "agents": len(self.agents),
            "skills": len(self.skills),
            "tools_with_params": sum(1 for t in self.tools.values() if t.params),
            "tools_write": sum(1 for t in self.tools.values() if t.read_write == "WRITE"),
            "tools_read": sum(1 for t in self.tools.values() if t.read_write == "READ"),
            "synced": self._synced,
            "total_apis": sum(t.api_count for t in self.tools.values()),
        }

    def to_summary(self) -> Dict[str, Any]:
        """Full summary for debug/display."""
        return {
            "tools": {
                name: {
                    "domain": t.domain, "risk": t.risk_level, "rw": t.read_write,
                    "apis": t.api_count, "params": len(t.params),
                    "response_fields": len(t.response_fields),
                    "agent": t.agent_owner,
                }
                for name, t in sorted(self.tools.items())
            },
            "agents": {
                name: {
                    "domains": sorted(a.domains), "tools": sorted(a.tools),
                    "apis": a.api_count,
                }
                for name, a in sorted(self.agents.items())
            },
            "skills": {
                name: {
                    "tools": sorted(s.tools), "domains": sorted(s.domains),
                    "examples": s.example_queries[:3], "apis": s.api_count,
                }
                for name, s in sorted(self.skills.items())
            },
        }

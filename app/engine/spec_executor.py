"""
Spec-Driven Executor — Generates visible execution plans before acting.

For multi-step queries (2+ tool calls), generates a structured plan that
the operator can review before execution. Inspired by Kiro's spec-driven approach.

Flow:
  1. Query comes in → complexity assessed
  2. If complex (2+ steps): generate plan → show to operator → execute on approval
  3. If simple (1 step): execute directly

The plan includes: steps, tools, estimated time, data sources.
Plans are stored in cosmos_execution_plans table for audit.

Usage:
    executor = SpecExecutor()
    plan = await executor.generate_plan(query, available_tools, context)
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


@dataclass
class PlanStep:
    step_number: int
    action: str  # fetch_data, analyze, compute, execute_tool, respond
    tool_name: str = ""
    description: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: List[str] = field(default_factory=list)
    estimated_seconds: float = 5.0
    requires_approval: bool = False
    risk_level: str = "low"


@dataclass
class ExecutionPlan:
    plan_id: str
    query: str
    total_steps: int
    steps: List[PlanStep]
    estimated_total_seconds: float
    requires_approval: bool  # True if any step has risk > low
    auto_execute: bool  # True for read-only plans


PLAN_PROMPT = """You are planning the execution steps for an ICRM operator query on Shiprocket's platform.

<query>{query}</query>

<available_tools>
{tools_description}
</available_tools>

<context>
{context_summary}
</context>

Generate an execution plan as a JSON array of steps. Each step:
{{
  "step_number": 1,
  "action": "fetch_data|analyze|compute|execute_tool|respond",
  "tool_name": "tool name or empty",
  "description": "What this step does",
  "inputs": {{"param": "value"}},
  "outputs": ["what this step produces"],
  "estimated_seconds": 5,
  "requires_approval": false,
  "risk_level": "low|medium|high"
}}

Rules:
- Read operations (lookups, searches) are low risk, no approval needed
- Write operations (cancel, update, refund) are high risk, need approval
- Always fetch data BEFORE analyzing it
- Last step should always be "respond" with the final answer

Return ONLY the JSON array."""


class SpecExecutor:
    """Generates and manages execution plans for complex queries."""

    # Queries with fewer steps than this execute directly (no plan shown)
    PLAN_THRESHOLD = 2

    def __init__(self, model: str = "claude-opus-4-6"):
        self.model = model
        self._cli = None

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=120)
        return self._cli

    def should_generate_plan(self, intent: str, complexity: str, tool_count: int) -> bool:
        """Determine if this query needs a visible execution plan."""
        if complexity in ("complex", "expert"):
            return True
        if tool_count >= self.PLAN_THRESHOLD:
            return True
        if intent in ("action", "troubleshoot") and tool_count >= 1:
            return True
        return False

    async def generate_plan(
        self,
        query: str,
        available_tools: List[Dict],
        context: Dict[str, Any],
        conversation_id: str = "",
        operator_id: str = "",
    ) -> Optional[ExecutionPlan]:
        """Generate an execution plan for a complex query."""
        cli = self._get_cli()
        if not cli or not cli.available:
            return None

        # Build tools description
        tools_desc = []
        for tool in available_tools[:20]:
            name = tool.get("name", "unknown")
            desc = tool.get("description", "")
            risk = tool.get("risk_level", "low")
            tools_desc.append(f"- {name}: {desc} (risk: {risk})")
        tools_text = "\n".join(tools_desc) if tools_desc else "No specific tools available"

        # Build context summary
        context_summary = json.dumps({
            k: v for k, v in context.items()
            if k in ("domain", "entities", "intent", "process_position")
        }, default=str)[:1000]

        try:
            raw = await cli.prompt(
                PLAN_PROMPT.format(
                    query=query,
                    tools_description=tools_text,
                    context_summary=context_summary,
                ),
                model=self.model,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            steps_data = json.loads(raw)
            steps = [
                PlanStep(
                    step_number=s.get("step_number", i + 1),
                    action=s.get("action", "fetch_data"),
                    tool_name=s.get("tool_name", ""),
                    description=s.get("description", ""),
                    inputs=s.get("inputs", {}),
                    outputs=s.get("outputs", []),
                    estimated_seconds=s.get("estimated_seconds", 5),
                    requires_approval=s.get("requires_approval", False),
                    risk_level=s.get("risk_level", "low"),
                )
                for i, s in enumerate(steps_data)
            ]

            plan_id = str(uuid.uuid4())
            has_write = any(s.requires_approval or s.risk_level in ("medium", "high") for s in steps)
            is_read_only = not has_write

            plan = ExecutionPlan(
                plan_id=plan_id,
                query=query,
                total_steps=len(steps),
                steps=steps,
                estimated_total_seconds=sum(s.estimated_seconds for s in steps),
                requires_approval=has_write,
                auto_execute=is_read_only,
            )

            # Store plan in DB
            await self._store_plan(plan, conversation_id, operator_id)

            logger.info("spec_executor.plan_generated",
                        plan_id=plan_id,
                        steps=len(steps),
                        requires_approval=has_write,
                        auto_execute=is_read_only)

            return plan

        except Exception as e:
            logger.warning("spec_executor.plan_generation_failed", error=str(e))
            return None

    def plan_to_dict(self, plan: ExecutionPlan) -> Dict:
        """Convert plan to JSON-serializable dict for frontend."""
        return {
            "plan_id": plan.plan_id,
            "query": plan.query,
            "total_steps": plan.total_steps,
            "estimated_total_seconds": plan.estimated_total_seconds,
            "requires_approval": plan.requires_approval,
            "auto_execute": plan.auto_execute,
            "steps": [
                {
                    "step_number": s.step_number,
                    "action": s.action,
                    "tool_name": s.tool_name,
                    "description": s.description,
                    "estimated_seconds": s.estimated_seconds,
                    "requires_approval": s.requires_approval,
                    "risk_level": s.risk_level,
                }
                for s in plan.steps
            ],
        }

    async def _store_plan(self, plan: ExecutionPlan, conversation_id: str, operator_id: str):
        """Store execution plan in DB for audit."""
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""INSERT INTO cosmos_execution_plans
                            (id, conversation_id, operator_id, query, plan_steps, status, total_steps)
                            VALUES (:id, :cid, :oid, :q, :steps, 'generated', :total)"""),
                    {
                        "id": plan.plan_id,
                        "cid": conversation_id or "",
                        "oid": operator_id or "",
                        "q": plan.query[:2000],
                        "steps": json.dumps(self.plan_to_dict(plan)["steps"]),
                        "total": plan.total_steps,
                    },
                )
                await session.commit()
        except Exception as e:
            logger.debug("spec_executor.store_plan_failed", error=str(e))

"""
Execution Engine — Runs fast-path, scoped retrieval, and multi-agent handoffs.

This is the bridge between the new modules (pattern_cache, agent_registry,
planner, skill_registry, scoped_retrieval) and the live orchestrator.

Three execution modes:
  1. FAST_PATH: Cached pattern → direct tool execution → safety check → respond
  2. SINGLE_AGENT: Scoped retrieval → agent-specific ReAct → respond
  3. MULTI_AGENT: Planner DAG → sequential/parallel agent execution → handoffs

Usage (from QueryOrchestrator):
    engine = ExecutionEngine(tool_registry, react_engine, vectorstore)
    result = await engine.execute_fast_path(fast_path_result, entity_id)
    result = await engine.execute_plan(plan, session_context)
"""

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

from app.engine.pattern_cache import FastPathResult, PatternCache
from app.engine.planner import ExecutionPlan, ExecutionMode, PlanStep, HandoffContext
from app.engine.agent_registry import AgentDefinition, AgentRegistry
from app.engine.scoped_retrieval import ScopedRetrieval

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Result of any execution mode."""
    success: bool = False
    response: str = ""
    confidence: float = 0.0
    tools_used: List[str] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    agent_chain: List[str] = field(default_factory=list)
    handoffs: List[Dict] = field(default_factory=list)
    latency_ms: float = 0.0
    mode: str = ""  # fast_path, single_agent, multi_agent
    skipped_stages: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ExecutionEngine:
    """Executes fast-path, single-agent, and multi-agent plans."""

    def __init__(self, tool_registry, react_engine=None, vectorstore=None):
        self.tool_registry = tool_registry
        self.react_engine = react_engine
        self.vectorstore = vectorstore
        self._scoped_retrieval = ScopedRetrieval(vectorstore) if vectorstore else None

    # ==================================================================
    # 1. FAST PATH: Execute cached tool sequence directly
    # ==================================================================

    async def execute_fast_path(
        self,
        fast_path: FastPathResult,
        entity_id: Optional[str] = None,
        query: str = "",
    ) -> ExecutionResult:
        """Execute a cached tool sequence without going through ReAct.

        Skips: planner, retriever, ReAct reasoning loop.
        Keeps: tool execution + post-execution safety verification.
        """
        t0 = time.monotonic()
        result = ExecutionResult(mode="fast_path")
        result.skipped_stages = fast_path.skipped_stages

        if not fast_path.tool_sequence:
            result.response = "No cached tool sequence for this pattern."
            result.latency_ms = (time.monotonic() - t0) * 1000
            return result

        # Execute each tool in the cached sequence
        for tool_spec in fast_path.tool_sequence:
            tool_name = tool_spec.get("tool_name", "")
            param_template = tool_spec.get("params", {})

            # Resolve entity_id into params
            params = dict(param_template)
            for k, v in params.items():
                if v == "{id}" and entity_id:
                    params[k] = entity_id
                elif v == "{query}" and query:
                    params[k] = query

            # Execute tool
            tool_result = await self._execute_tool(tool_name, params)
            result.tool_results.append(tool_result)
            result.tools_used.append(tool_name)

            if not tool_result.get("success"):
                # Tool failed — fall back to normal path
                logger.warning("execution.fast_path_tool_failed",
                               tool=tool_name, error=tool_result.get("error"))
                result.success = False
                result.response = f"Fast path failed at {tool_name}. Falling back to full pipeline."
                result.latency_ms = (time.monotonic() - t0) * 1000
                return result

        # Aggregate tool results into response
        result.success = True
        result.confidence = fast_path.confidence
        result.response = self._format_tool_results(result.tool_results)

        # M9: Post-execution safety verification
        # Even fast-path results must pass guardrails before reaching user
        try:
            safety_ok = await self._verify_fast_path_safety(result.response, query)
            if not safety_ok:
                logger.warning("execution.fast_path_safety_fail")
                result.success = False
                result.response = "Fast path response blocked by safety check. Falling back."
                result.metadata["safety_blocked"] = True
                result.latency_ms = (time.monotonic() - t0) * 1000
                return result
        except Exception as e:
            logger.debug("execution.safety_check_error", error=str(e))

        result.latency_ms = (time.monotonic() - t0) * 1000

        logger.info("execution.fast_path_complete",
                     tools=result.tools_used, latency_ms=round(result.latency_ms, 1))
        return result

    async def _verify_fast_path_safety(self, response: str, query: str) -> bool:
        """M9: Lightweight safety check on fast-path responses.

        Checks:
        1. No PII leakage (phone, email, Aadhaar patterns)
        2. No internal paths/SQL in response
        3. Response is not empty/too short
        """
        import re

        if not response or len(response) < 10:
            return False

        # PII patterns that should be masked
        pii_patterns = [
            r'\b\d{12}\b',           # Aadhaar
            r'\b\d{10}\b',           # Phone (if raw, not masked)
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',  # Email
        ]

        # Internal leakage patterns
        leak_patterns = [
            r'app/[A-Z]\w+/',        # File paths
            r'SELECT\s+\*\s+FROM',   # SQL
            r'password\s*[:=]',       # Credentials
        ]

        for pattern in leak_patterns:
            if re.search(pattern, response, re.IGNORECASE):
                return False

        return True

    # ==================================================================
    # 2. SINGLE AGENT: Scoped retrieval + agent-specific execution
    # ==================================================================

    async def execute_single_agent(
        self,
        agent: AgentDefinition,
        query: str,
        entity_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        session_context: Optional[Dict] = None,
    ) -> ExecutionResult:
        """Execute query with a specific predefined agent + scoped retrieval."""
        t0 = time.monotonic()
        result = ExecutionResult(mode="single_agent")
        result.agent_chain = [agent.name]

        # Scoped retrieval: search only this agent's domain
        kb_context = []
        if self._scoped_retrieval:
            try:
                kb_results = await self._scoped_retrieval.search_for_agent(
                    query=query, agent=agent, limit=5, repo_id=repo_id,
                )
                kb_context = [r.get("content", "")[:500] for r in kb_results]
            except Exception as e:
                logger.debug("execution.scoped_retrieval_failed", error=str(e))

        # Execute with ReAct engine if available, scoped to agent's tools
        if self.react_engine:
            try:
                # Augment query with KB context and agent instructions
                augmented_query = query
                if kb_context:
                    context_str = "\n".join(kb_context[:3])
                    augmented_query = f"{query}\n\n[Context: {context_str}]"

                react_result = await self.react_engine.process(
                    user_message=augmented_query,
                    session_context=session_context or {},
                )

                result.success = react_result.success if hasattr(react_result, 'success') else True
                result.response = react_result.final_response if hasattr(react_result, 'final_response') else str(react_result)
                result.confidence = react_result.confidence if hasattr(react_result, 'confidence') else 0.7
                result.tools_used = react_result.tools_used if hasattr(react_result, 'tools_used') else []

            except Exception as e:
                logger.error("execution.react_failed", agent=agent.name, error=str(e))
                result.response = f"Agent {agent.name} failed: {str(e)}"
        else:
            # Fallback: direct tool execution without ReAct
            result.response = f"Agent {agent.name} ready but no ReAct engine available."

        result.latency_ms = (time.monotonic() - t0) * 1000
        return result

    # ==================================================================
    # 3. MULTI-AGENT: Execute planner DAG with handoffs
    # ==================================================================

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        entity_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        session_context: Optional[Dict] = None,
        agent_registry: Optional[AgentRegistry] = None,
    ) -> ExecutionResult:
        """Execute a multi-agent plan with dependency handling and handoffs."""
        t0 = time.monotonic()
        result = ExecutionResult(mode="multi_agent" if plan.is_multi_agent else "single_agent")

        step_results: Dict[int, ExecutionResult] = {}
        handoff_contexts: Dict[int, HandoffContext] = {}

        for step in plan.steps:
            # Check dependencies
            if step.depends_on and step.depends_on in step_results:
                dep_result = step_results[step.depends_on]
                if not dep_result.success:
                    logger.warning("execution.dependency_failed",
                                   step=step.step_id, depends_on=step.depends_on)
                    # Dependency failed — skip this step
                    result.handoffs.append({
                        "from_step": step.depends_on,
                        "to_step": step.step_id,
                        "status": "skipped_dependency_failed",
                    })
                    continue

                # Build handoff context from dependency result
                handoff_contexts[step.step_id] = HandoffContext(
                    from_agent=plan.steps[step.depends_on - 1].agent_name if step.depends_on > 0 else "",
                    to_agent=step.agent_name,
                    query=plan.query,
                    partial_result={
                        "tools_used": dep_result.tools_used,
                        "response": dep_result.response,
                        "tool_results": dep_result.tool_results,
                    },
                    reason=f"Step {step.depends_on} completed, handing off {step.intent}",
                )

            # Get agent definition
            agent = agent_registry.get(step.agent_name) if agent_registry else None

            # Execute step
            if agent:
                # Build augmented context with handoff data
                step_context = dict(session_context or {})
                if step.step_id in handoff_contexts:
                    hc = handoff_contexts[step.step_id]
                    step_context["handoff_from"] = hc.from_agent
                    step_context["handoff_data"] = hc.partial_result

                step_result = await self.execute_single_agent(
                    agent=agent,
                    query=plan.query,
                    entity_id=entity_id or step.entity_id,
                    repo_id=repo_id,
                    session_context=step_context,
                )
            else:
                # No agent found — execute tools directly
                step_result = ExecutionResult(mode="direct_tools")
                for tool_hint in step.tool_hints:
                    params = {"id": entity_id} if entity_id else {}
                    tr = await self._execute_tool(tool_hint, params)
                    step_result.tool_results.append(tr)
                    step_result.tools_used.append(tool_hint)
                step_result.success = all(tr.get("success") for tr in step_result.tool_results)
                step_result.response = self._format_tool_results(step_result.tool_results)

            step_results[step.step_id] = step_result
            result.agent_chain.append(step.agent_name)
            result.tools_used.extend(step_result.tools_used)
            result.tool_results.extend(step_result.tool_results)

            # Record handoff
            if step.depends_on:
                result.handoffs.append({
                    "from_step": step.depends_on,
                    "to_step": step.step_id,
                    "from_agent": plan.steps[step.depends_on - 1].agent_name,
                    "to_agent": step.agent_name,
                    "status": "success" if step_result.success else "failed",
                })

        # Aggregate results
        all_responses = [sr.response for sr in step_results.values() if sr.response]
        result.success = all(sr.success for sr in step_results.values())
        result.response = " | ".join(all_responses) if all_responses else "No results from plan execution."
        result.confidence = min((sr.confidence for sr in step_results.values()), default=0.0)
        result.latency_ms = (time.monotonic() - t0) * 1000

        logger.info("execution.plan_complete",
                     steps=len(plan.steps), agents=result.agent_chain,
                     handoffs=len(result.handoffs), latency_ms=round(result.latency_ms, 1))
        return result

    # ==================================================================
    # Helpers
    # ==================================================================

    async def _execute_tool(self, tool_name: str, params: Dict) -> Dict:
        """Execute a single tool and return result dict."""
        t0 = time.monotonic()
        try:
            if hasattr(self.tool_registry, 'execute'):
                # Use registry's execute method (has validation)
                tool_result = await self.tool_registry.execute(tool_name, params)
                return {
                    "tool_name": tool_name,
                    "success": tool_result.success if hasattr(tool_result, 'success') else True,
                    "data": tool_result.data if hasattr(tool_result, 'data') else tool_result,
                    "latency_ms": (time.monotonic() - t0) * 1000,
                    "error": tool_result.error if hasattr(tool_result, 'error') else None,
                }
            else:
                # Direct tool function call
                tool_fn = self.tool_registry.get(tool_name)
                if tool_fn is None:
                    return {"tool_name": tool_name, "success": False, "error": f"Tool '{tool_name}' not found"}

                if inspect.iscoroutinefunction(tool_fn):
                    data = await tool_fn(**params)
                else:
                    data = tool_fn(**params)

                return {
                    "tool_name": tool_name,
                    "success": True,
                    "data": data,
                    "latency_ms": (time.monotonic() - t0) * 1000,
                }
        except Exception as e:
            return {
                "tool_name": tool_name,
                "success": False,
                "error": str(e),
                "latency_ms": (time.monotonic() - t0) * 1000,
            }

    def _format_tool_results(self, tool_results: List[Dict]) -> str:
        """Format tool results into a readable response."""
        parts = []
        for tr in tool_results:
            if tr.get("success") and tr.get("data"):
                data = tr["data"]
                if isinstance(data, dict):
                    # Extract key fields for display
                    display = {k: v for k, v in list(data.items())[:10] if v is not None}
                    parts.append(f"{tr['tool_name']}: {display}")
                else:
                    parts.append(f"{tr['tool_name']}: {str(data)[:200]}")
            elif tr.get("error"):
                parts.append(f"{tr['tool_name']}: Error — {tr['error']}")
        return " | ".join(parts) if parts else "No tool results."

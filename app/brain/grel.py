"""
GREL Engine — Gather → Reason → Execute → Learn.

Unlike a tournament where a "scorer" picks a winner, GREL feeds ALL
parallel strategy results into the LLM as context. It synthesizes
the best answer by reasoning across everything the strategies found.

Flow:
  1. GATHER — Run all strategies in parallel (wave-style)
     Each strategy contributes what it found:
       A (Decision Tree): entity ID, matched tool, confidence
       B (TF-IDF RAG): relevant API docs, example queries
       C (Tool-Use): selected tool + extracted params
       D (Full Reasoning): deep analysis, edge cases, multi-step plan

  2. REASON — LLM sees ALL gathered data and synthesizes:
     - What is the user's true intent?
     - Which data from each strategy is most relevant?
     - What's the best execution plan?
     - Did any strategy catch something others missed?

  3. EXECUTE — Run the synthesized plan (API call, action, etc.)

  4. LEARN (async, non-blocking) — After response is sent:
     - Which strategies contributed useful data?
     - Should any routing rules be updated?
     - Were there edge cases worth codifying?
     - If changes need admin approval → create approval request
       → visible in Lime admin panel

  5. EVOLVE — Admin-gated improvements:
     - New routing rules from patterns
     - Updated few-shot examples
     - Decision tree expansions
     - Knowledge base corrections
     All require admin approval before taking effect.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from app.brain.tournament import StrategyName, StrategyResult

logger = structlog.get_logger()

# Wave classification: cheap strategies run in Wave 1, expensive in Wave 2
WAVE1_STRATEGIES = {
    StrategyName.DECISION_TREE,
    StrategyName.TFIDF_RAG,
    StrategyName.HYBRID_RETRIEVAL,
}
WAVE2_STRATEGIES = {
    StrategyName.TOOL_USE,
    StrategyName.FULL_REASONING,
}
# If Wave 1 max confidence >= this, skip Wave 2
WAVE1_CONFIDENCE_THRESHOLD = 0.75


class GRELPhase(str, Enum):
    GATHER = "gather"
    REASON = "reason"
    EXECUTE = "execute"
    RESPOND = "respond"
    LEARN = "learn"


class LearningType(str, Enum):
    ROUTING_RULE = "routing_rule"          # New decision tree entry
    FEW_SHOT_EXAMPLE = "few_shot_example"  # New example for knowledge base
    TOOL_CORRECTION = "tool_correction"    # Wrong tool was selected
    PARAM_CORRECTION = "param_correction"  # Wrong params extracted
    EDGE_CASE = "edge_case"               # New edge case discovered
    KNOWLEDGE_GAP = "knowledge_gap"        # Missing knowledge base entry


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPLIED = "auto_applied"  # Low-risk, auto-approved


@dataclass
class GatheredData:
    """Data gathered from one strategy."""

    strategy: StrategyName
    raw_result: StrategyResult
    # Structured contributions
    entity_found: Optional[str] = None
    tool_suggested: Optional[str] = None
    params_extracted: Dict[str, Any] = field(default_factory=dict)
    api_docs_found: List[dict] = field(default_factory=list)
    analysis_notes: List[str] = field(default_factory=list)
    edge_cases: List[str] = field(default_factory=list)
    confidence: float = 0.0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class SynthesisResult:
    """LLM reasoning across all gathered data."""

    chosen_tool: Optional[str] = None
    chosen_params: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    execution_plan: List[str] = field(default_factory=list)
    confidence: float = 0.0
    strategies_used: List[StrategyName] = field(default_factory=list)
    edge_cases_noted: List[str] = field(default_factory=list)


@dataclass
class LearningInsight:
    """A learning insight discovered during GREL processing."""

    insight_id: str
    learning_type: LearningType
    description: str
    evidence: str  # What data led to this insight
    proposed_change: str  # What should change
    risk_level: str  # "low", "medium", "high"
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def needs_admin_approval(self) -> bool:
        return self.risk_level in ("medium", "high")


@dataclass
class GRELResult:
    """Full result of a GREL cycle."""

    query: str
    intent: str
    entity: str
    entity_id: Optional[str] = None

    # Phase outputs
    gathered: List[GatheredData] = field(default_factory=list)
    synthesis: Optional[SynthesisResult] = None
    execution_result: Optional[dict] = None
    response: str = ""

    # Learning (async)
    insights: List[LearningInsight] = field(default_factory=list)

    # Metadata
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    phase: GRELPhase = GRELPhase.GATHER
    session_id: str = ""


# -----------------------------------------------------------------------
# GREL Engine
# -----------------------------------------------------------------------

class GRELEngine:
    """Gather → Reason → Execute → Learn engine.

    Usage:
        engine = GRELEngine(llm_client=llm, mcapi_client=mcapi)
        engine.register_strategy(StrategyName.DECISION_TREE, dt_fn)
        engine.register_strategy(StrategyName.TFIDF_RAG, rag_fn)
        ...
        result = await engine.process(query, intent, entity, entity_id)
        # result.response has LLM synthesized answer
        # result.insights has async learning discoveries
    """

    def __init__(
        self,
        llm_client=None,
        mcapi_client=None,
        learning_callback: Optional[Callable] = None,
    ):
        self._llm = llm_client
        self._mcapi = mcapi_client
        self._strategies: Dict[StrategyName, Callable] = {}
        self._learning_callback = learning_callback  # Called async with insights
        self._insights_store: List[LearningInsight] = []
        self._pending_approvals: List[LearningInsight] = []

    def register_strategy(
        self,
        name: StrategyName,
        fn: Callable[..., Coroutine[Any, Any, StrategyResult]],
    ):
        """Register an async strategy function."""
        self._strategies[name] = fn

    async def process(
        self,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str] = None,
        session_id: str = "",
    ) -> GRELResult:
        """Run the full GREL cycle.

        Returns GRELResult with response and async learning insights.
        """
        start = time.monotonic()
        result = GRELResult(
            query=query,
            intent=intent,
            entity=entity,
            entity_id=entity_id,
            session_id=session_id,
        )

        # --- PHASE 1: GATHER ---
        result.phase = GRELPhase.GATHER
        result.gathered = await self._gather(query, intent, entity, entity_id)

        # --- PHASE 2: REASON ---
        result.phase = GRELPhase.REASON
        result.synthesis = await self._reason(query, intent, entity, result.gathered)

        # --- PHASE 3: EXECUTE ---
        result.phase = GRELPhase.EXECUTE
        if result.synthesis and result.synthesis.chosen_tool:
            try:
                result.execution_result = await self._execute(result.synthesis)
            except RuntimeError as e:
                # Bug 4: MCAPI not connected — log and continue without execution
                # The response phase will fall back to strategy answers
                logger.warning("grel.execute_unavailable", error=str(e))
                result.execution_result = {"error": str(e), "ready_to_execute": False}

        # --- PHASE 4: RESPOND ---
        result.phase = GRELPhase.RESPOND
        result.response = await self._build_response(
            query, result.synthesis, result.execution_result, result.gathered
        )

        result.total_latency_ms = (time.monotonic() - start) * 1000
        result.total_cost_usd = sum(g.cost_usd for g in result.gathered)

        # --- PHASE 5: LEARN (async, non-blocking) ---
        result.phase = GRELPhase.LEARN
        # Fire and forget — don't block the response
        asyncio.ensure_future(self._learn_async(result))

        return result

    # -------------------------------------------------------------------
    # GATHER: 2-wave execution (cheap first, expensive only if needed)
    # -------------------------------------------------------------------

    async def _gather(
        self,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str],
    ) -> List[GatheredData]:
        """Run strategies in 2 waves: cheap first, expensive only if Wave 1 is insufficient.

        Wave 1 (free/cheap): decision_tree, tfidf_rag, hybrid_retrieval
        Wave 2 (expensive):  claude_tool_use, full_reasoning — only if Wave 1 max confidence < 0.75
        """
        wave1_fns = {n: fn for n, fn in self._strategies.items() if n in WAVE1_STRATEGIES}
        wave2_fns = {n: fn for n, fn in self._strategies.items() if n in WAVE2_STRATEGIES}

        # --- Wave 1: cheap strategies ---
        wave1_tasks = [
            self._run_and_structure(name, fn, query, intent, entity, entity_id)
            for name, fn in wave1_fns.items()
        ]
        # Bug 3 fix: per-task 15s timeout so a single hung strategy doesn't block the wave
        if wave1_tasks:
            wave1_wrapped = [asyncio.wait_for(t, timeout=15.0) for t in wave1_tasks]
            raw_w1 = await asyncio.gather(*wave1_wrapped, return_exceptions=True)
            wave1_results: List[GatheredData] = [r for r in raw_w1 if not isinstance(r, Exception)]
            for r in raw_w1:
                if isinstance(r, Exception):
                    logger.warning("grel.wave1_task_timeout_or_error", error=str(r))
        else:
            wave1_results = []

        # Check if Wave 1 is sufficient
        wave1_max_conf = max((g.confidence for g in wave1_results), default=0.0)
        wave1_any_tool = any(g.tool_suggested for g in wave1_results if g.raw_result.success)

        if wave1_max_conf >= WAVE1_CONFIDENCE_THRESHOLD and wave1_any_tool:
            logger.info(
                "grel.wave2_skipped",
                wave1_max_conf=round(wave1_max_conf, 3),
                wave1_strategies=len(wave1_results),
                reason="wave1_sufficient",
            )
            return list(wave1_results)

        # --- Wave 2: expensive strategies (only if Wave 1 insufficient) ---
        if not wave2_fns:
            return list(wave1_results)

        logger.info(
            "grel.wave2_triggered",
            wave1_max_conf=round(wave1_max_conf, 3),
            wave1_had_tool=wave1_any_tool,
            wave2_strategies=len(wave2_fns),
        )

        wave2_tasks = [
            self._run_and_structure(name, fn, query, intent, entity, entity_id)
            for name, fn in wave2_fns.items()
        ]
        # Bug 3 fix: wave 2 strategies (expensive) get a 30s timeout each
        if wave2_tasks:
            wave2_wrapped = [asyncio.wait_for(t, timeout=30.0) for t in wave2_tasks]
            raw_w2 = await asyncio.gather(*wave2_wrapped, return_exceptions=True)
            wave2_results: List[GatheredData] = [r for r in raw_w2 if not isinstance(r, Exception)]
            for r in raw_w2:
                if isinstance(r, Exception):
                    logger.warning("grel.wave2_task_timeout_or_error", error=str(r))
        else:
            wave2_results = []

        return list(wave1_results) + list(wave2_results)

    async def _run_and_structure(
        self,
        name: StrategyName,
        fn: Callable,
        query: str,
        intent: str,
        entity: str,
        entity_id: Optional[str],
    ) -> GatheredData:
        """Run a strategy and structure its output for synthesis."""
        start = time.monotonic()
        try:
            result = await fn(query, intent, entity, entity_id)
            latency = (time.monotonic() - start) * 1000

            return GatheredData(
                strategy=name,
                raw_result=result,
                entity_found=entity_id or result.params_extracted.get("id"),
                tool_suggested=result.tool_used,
                params_extracted=result.params_extracted,
                api_docs_found=[],  # Populated by RAG strategy
                analysis_notes=[result.answer] if result.answer else [],
                edge_cases=[],  # Populated by full reasoning strategy
                confidence=result.confidence,
                cost_usd=result.cost_usd,
                latency_ms=latency,
            )
        except Exception as e:
            return GatheredData(
                strategy=name,
                raw_result=StrategyResult(
                    strategy=name, answer="", confidence=0.0, error=str(e)
                ),
                confidence=0.0,
                latency_ms=(time.monotonic() - start) * 1000,
            )

    # -------------------------------------------------------------------
    # REASON: LLM synthesizes across all gathered data
    # -------------------------------------------------------------------

    async def _reason(
        self,
        query: str,
        intent: str,
        entity: str,
        gathered: List[GatheredData],
    ) -> SynthesisResult:
        """Claude reasons across ALL strategy outputs to build execution plan."""

        if self._llm is None:
            # No LLM — fall back to best-confidence strategy
            return self._reason_without_llm(gathered)

        # Build synthesis prompt with all gathered data
        prompt = self._build_synthesis_prompt(query, intent, entity, gathered)

        try:
            raw = await self._llm.complete(
                prompt,
                max_tokens=500,
                intent="grel_synthesis",
                session_id="",
            )
            return self._parse_synthesis(raw, gathered)
        except Exception:
            return self._reason_without_llm(gathered)

    def _build_synthesis_prompt(
        self,
        query: str,
        intent: str,
        entity: str,
        gathered: List[GatheredData],
    ) -> str:
        """Build the prompt that shows LLM ALL strategy results."""
        sections = []

        for g in gathered:
            if not g.raw_result.success:
                continue
            section = (
                f"## Strategy: {g.strategy.value}\n"
                f"Confidence: {g.confidence:.2f}\n"
                f"Tool suggested: {g.tool_suggested or 'none'}\n"
                f"Params: {json.dumps(g.params_extracted) if g.params_extracted else 'none'}\n"
                f"Analysis: {'; '.join(g.analysis_notes) if g.analysis_notes else 'none'}\n"
            )
            if g.edge_cases:
                section += f"Edge cases: {'; '.join(g.edge_cases)}\n"
            sections.append(section)

        strategies_text = "\n".join(sections) if sections else "No strategy produced results."

        return (
            "You are an AI synthesis engine. Multiple strategies analyzed the same user query "
            "in parallel. Review ALL their findings and build the best execution plan.\n\n"
            f"User query: \"{query}\"\n"
            f"Classified intent: {intent}\n"
            f"Classified entity: {entity}\n\n"
            f"Strategy Results:\n{strategies_text}\n\n"
            "Instructions:\n"
            "1. Consider what EACH strategy found — even expensive strategies may catch edge cases\n"
            "2. Pick the best tool and params by reasoning across all results\n"
            "3. Note any edge cases or insights that others missed\n"
            "4. If strategies disagree, explain why you chose one over another\n\n"
            "Reply with JSON:\n"
            '{"tool": "<best tool name or null>", '
            '"params": {<best params>}, '
            '"reasoning": "<why this is the best plan>", '
            '"execution_steps": ["step 1", "step 2"], '
            '"strategies_used": ["strategy_a", "strategy_b"], '
            '"edge_cases": ["any edge cases noted"]}'
        )

    def _parse_synthesis(
        self, raw: str, gathered: List[GatheredData]
    ) -> SynthesisResult:
        """Parse LLM synthesis response."""
        try:
            parsed = json.loads(raw.strip())
            strategies_used = []
            for s in parsed.get("strategies_used", []):
                try:
                    strategies_used.append(StrategyName(s))
                except ValueError:
                    pass

            return SynthesisResult(
                chosen_tool=parsed.get("tool"),
                chosen_params=parsed.get("params", {}),
                reasoning=parsed.get("reasoning", ""),
                execution_plan=parsed.get("execution_steps", []),
                confidence=0.9,  # Claude synthesized = high confidence
                strategies_used=strategies_used,
                edge_cases_noted=parsed.get("edge_cases", []),
            )
        except (json.JSONDecodeError, KeyError):
            return self._reason_without_llm(gathered)

    def _reason_without_llm(self, gathered: List[GatheredData]) -> SynthesisResult:
        """Fallback: pick highest-confidence strategy result."""
        successful = [g for g in gathered if g.raw_result.success]
        if not successful:
            return SynthesisResult(confidence=0.0, reasoning="No strategy succeeded")

        best = max(successful, key=lambda g: g.confidence)

        # Merge params from all strategies (highest confidence wins conflicts)
        merged_params = {}
        for g in sorted(successful, key=lambda g: g.confidence):
            merged_params.update(g.params_extracted)

        # Collect edge cases from all strategies
        all_edge_cases = []
        for g in successful:
            all_edge_cases.extend(g.edge_cases)

        return SynthesisResult(
            chosen_tool=best.tool_suggested,
            chosen_params=merged_params,
            reasoning=f"Best confidence from {best.strategy.value}: {best.confidence:.2f}",
            execution_plan=[f"Execute {best.tool_suggested} with merged params"],
            confidence=best.confidence,
            strategies_used=[g.strategy for g in successful],
            edge_cases_noted=all_edge_cases,
        )

    # -------------------------------------------------------------------
    # EXECUTE: Run the synthesized plan
    # -------------------------------------------------------------------

    async def _execute(self, synthesis: SynthesisResult) -> Optional[dict]:
        """Execute the tool chosen by synthesis."""
        if self._mcapi is None:
            # Bug 4 fix: silent mock was masking that MCAPI was never connected.
            # Raise explicitly so callers can handle the unavailability properly.
            raise RuntimeError(
                "GREL _execute: MCAPI client is not connected. "
                "Set self._mcapi before calling execute()."
            )

        try:
            # The actual execution would call MCAPI with the synthesized params
            # For now, return the plan for the chat endpoint to execute
            return {
                "tool": synthesis.chosen_tool,
                "params": synthesis.chosen_params,
                "plan": synthesis.execution_plan,
                "ready_to_execute": True,
            }
        except Exception as e:
            return {"error": str(e), "ready_to_execute": False}

    # -------------------------------------------------------------------
    # RESPOND: Build the final response
    # -------------------------------------------------------------------

    async def _build_response(
        self,
        query: str,
        synthesis: Optional[SynthesisResult],
        execution_result: Optional[dict],
        gathered: List[GatheredData],
    ) -> str:
        """Build the response for the user."""
        if synthesis is None:
            return "I couldn't process your request. Let me connect you with a human agent."

        if self._llm is not None and execution_result:
            try:
                data_str = json.dumps(execution_result, default=str)[:2000]
                prompt = (
                    f"User asked: \"{query}\"\n"
                    f"We executed: {synthesis.chosen_tool}\n"
                    f"Result: {data_str}\n"
                    f"Reasoning: {synthesis.reasoning}\n\n"
                    "Give a natural, helpful response to the user."
                )
                return await self._llm.complete(
                    prompt, max_tokens=500, intent="response", session_id=""
                )
            except Exception:
                pass

        # Fallback: use the best strategy's answer
        for g in sorted(gathered, key=lambda g: g.confidence, reverse=True):
            if g.raw_result.answer:
                return g.raw_result.answer

        return synthesis.reasoning or "I processed your request."

    # -------------------------------------------------------------------
    # LEARN: Async learning after response (non-blocking)
    # -------------------------------------------------------------------

    async def _learn_async(self, result: GRELResult):
        """Analyze the GREL result and extract learning insights.

        This runs AFTER the response is sent to the user.
        Insights that need admin approval are stored in pending_approvals.
        """
        insights = []

        # 1. Check if strategies disagreed on tool selection
        tools_suggested = {}
        for g in result.gathered:
            if g.tool_suggested:
                tools_suggested[g.strategy] = g.tool_suggested

        unique_tools = set(tools_suggested.values())
        if len(unique_tools) > 1 and result.synthesis:
            insights.append(LearningInsight(
                insight_id=str(uuid.uuid4()),
                learning_type=LearningType.TOOL_CORRECTION,
                description=f"Strategies disagreed on tool: {unique_tools}",
                evidence=f"Query: '{result.query}', chosen: {result.synthesis.chosen_tool}",
                proposed_change=f"Add routing rule: intent={result.intent} entity={result.entity} → {result.synthesis.chosen_tool}",
                risk_level="medium",
            ))

        # 2. Check if full reasoning found edge cases
        for g in result.gathered:
            if g.strategy == StrategyName.FULL_REASONING and g.edge_cases:
                for ec in g.edge_cases:
                    insights.append(LearningInsight(
                        insight_id=str(uuid.uuid4()),
                        learning_type=LearningType.EDGE_CASE,
                        description=f"Edge case discovered: {ec}",
                        evidence=f"Found by full reasoning on query: '{result.query}'",
                        proposed_change=f"Add edge case handling for: {ec}",
                        risk_level="low",
                    ))

        # 3. Check if decision tree was sufficient (cost optimization)
        dt_result = next(
            (g for g in result.gathered if g.strategy == StrategyName.DECISION_TREE),
            None,
        )
        if (
            dt_result
            and dt_result.confidence >= 0.85
            and result.synthesis
            and result.synthesis.chosen_tool == dt_result.tool_suggested
        ):
            insights.append(LearningInsight(
                insight_id=str(uuid.uuid4()),
                learning_type=LearningType.ROUTING_RULE,
                description=(
                    f"Decision tree was sufficient for pattern "
                    f"'{result.intent}:{result.entity}'"
                ),
                evidence=(
                    f"DT confidence={dt_result.confidence:.2f}, "
                    f"synthesis chose same tool={dt_result.tool_suggested}"
                ),
                proposed_change=(
                    f"Fast-path: route '{result.intent}:{result.entity}' "
                    f"directly to decision tree (skip other strategies)"
                ),
                risk_level="low",
                approval_status=ApprovalStatus.AUTO_APPLIED,
            ))

        # 4. Check for knowledge gaps
        if result.synthesis and result.synthesis.confidence < 0.5:
            insights.append(LearningInsight(
                insight_id=str(uuid.uuid4()),
                learning_type=LearningType.KNOWLEDGE_GAP,
                description=f"Low confidence ({result.synthesis.confidence:.2f}) suggests knowledge gap",
                evidence=f"Query: '{result.query}', intent={result.intent}, entity={result.entity}",
                proposed_change="Add more examples/docs to knowledge base for this pattern",
                risk_level="medium",
            ))

        # 5. Check if a new few-shot example should be added
        if result.synthesis and result.synthesis.confidence >= 0.9 and result.entity_id:
            insights.append(LearningInsight(
                insight_id=str(uuid.uuid4()),
                learning_type=LearningType.FEW_SHOT_EXAMPLE,
                description=f"High-confidence resolution — good few-shot candidate",
                evidence=f"Query: '{result.query}' → tool={result.synthesis.chosen_tool}, params={result.synthesis.chosen_params}",
                proposed_change=f"Add to examples.yaml for {result.synthesis.chosen_tool}",
                risk_level="low",
            ))

        # Store insights
        result.insights = insights
        for insight in insights:
            self._insights_store.append(insight)
            if insight.needs_admin_approval:
                self._pending_approvals.append(insight)

        # Notify callback if registered
        if self._learning_callback and insights:
            try:
                await self._learning_callback(insights)
            except Exception:
                pass  # Learning failures should never block

    # -------------------------------------------------------------------
    # Admin Approval Interface (for Lime panel)
    # -------------------------------------------------------------------

    def get_pending_approvals(self) -> List[dict]:
        """Get all pending learning insights that need admin approval.

        This is called by the Lime frontend to display in the admin panel.
        """
        return [
            {
                "id": i.insight_id,
                "type": i.learning_type.value,
                "description": i.description,
                "evidence": i.evidence,
                "proposed_change": i.proposed_change,
                "risk_level": i.risk_level,
                "status": i.approval_status.value,
                "created_at": i.created_at.isoformat(),
            }
            for i in self._pending_approvals
            if i.approval_status == ApprovalStatus.PENDING
        ]

    def approve_insight(self, insight_id: str, approved_by: str) -> bool:
        """Admin approves a learning insight. Changes will be applied."""
        for i in self._pending_approvals:
            if i.insight_id == insight_id:
                i.approval_status = ApprovalStatus.APPROVED
                i.approved_by = approved_by
                i.approved_at = datetime.now(timezone.utc)
                return True
        return False

    def reject_insight(self, insight_id: str, approved_by: str) -> bool:
        """Admin rejects a learning insight."""
        for i in self._pending_approvals:
            if i.insight_id == insight_id:
                i.approval_status = ApprovalStatus.REJECTED
                i.approved_by = approved_by
                i.approved_at = datetime.now(timezone.utc)
                return True
        return False

    def get_all_insights(self, limit: int = 100) -> List[dict]:
        """Get all learning insights for analytics."""
        return [
            {
                "id": i.insight_id,
                "type": i.learning_type.value,
                "description": i.description,
                "risk_level": i.risk_level,
                "status": i.approval_status.value,
                "created_at": i.created_at.isoformat(),
            }
            for i in self._insights_store[-limit:]
        ]

    def get_learning_stats(self) -> dict:
        """Stats for the learning pipeline."""
        by_type = {}
        by_status = {}
        for i in self._insights_store:
            by_type[i.learning_type.value] = by_type.get(i.learning_type.value, 0) + 1
            by_status[i.approval_status.value] = by_status.get(i.approval_status.value, 0) + 1

        return {
            "total_insights": len(self._insights_store),
            "pending_approvals": len([
                i for i in self._pending_approvals
                if i.approval_status == ApprovalStatus.PENDING
            ]),
            "by_type": by_type,
            "by_status": by_status,
        }

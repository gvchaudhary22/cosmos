"""
Core ReAct reasoning engine for COSMOS.

Processes user queries through a loop of:
    REASON -> ACT -> OBSERVE -> EVALUATE -> REFLECT -> RESPOND

Max 3 iterations before escalation.
"""

import asyncio
import inspect
import structlog
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from cosmos.app.engine.classifier import ClassifyResult, Intent
from cosmos.app.engine.confidence import score_confidence

logger = structlog.get_logger()


class ReActPhase(str, Enum):
    REASON = "reason"
    ACT = "act"
    OBSERVE = "observe"
    EVALUATE = "evaluate"
    REFLECT = "reflect"


@dataclass
class ToolCall:
    tool_name: str
    params: Dict[str, Any]


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any
    latency_ms: float
    error: Optional[str] = None


@dataclass
class ReActStep:
    phase: ReActPhase
    content: str
    confidence: float = 0.0
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReActResult:
    response: str
    confidence: float
    steps: List[ReActStep]
    tools_used: List[str]
    total_loops: int
    total_latency_ms: float
    escalated: bool = False
    escalation_reason: Optional[str] = None


class ReActEngine:
    """
    ReAct reasoning engine for COSMOS.
    Max 3 iterations before escalation.
    """

    MAX_LOOPS = 3
    MAX_LOOPS_COMPLEX = 5  # More iterations for complex multi-step queries

    def __init__(self, classifier, tool_registry, llm_client, guardrails, approval_engine=None):
        self.classifier = classifier
        self.tool_registry = tool_registry
        self.llm = llm_client
        self.guardrails = guardrails
        self.approval_engine = approval_engine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(
        self, user_message: str, session_context: Optional[Dict] = None
    ) -> ReActResult:
        """
        Main entry point. Processes user message through ReAct loop.

        Flow per iteration:
        1. REASON  — classify intent, select tools
        2. ACT     — execute selected tools (parallel if independent)
        3. OBSERVE — parse and validate tool results
        4. EVALUATE — score confidence
             >= 0.8  -> proceed to respond
             0.5-0.8 -> respond with caveat
             0.3-0.5 -> ask clarification
             < 0.3   -> escalate
        5. REFLECT — verify response quality (final iteration only)

        If confidence < 0.5 and loops < MAX_LOOPS, loop back to REASON
        with accumulated context from previous iterations.
        """
        steps: List[ReActStep] = []
        tools_used: List[str] = []
        start_time = time.time()
        accumulated_context: Dict[str, Any] = {}
        loop = 0

        # Use more loops for complex queries (multi-intent, multi-entity)
        is_complex = session_context.get("complexity") == "complex" if isinstance(session_context, dict) else False
        max_loops = self.MAX_LOOPS_COMPLEX if is_complex else self.MAX_LOOPS

        for loop in range(max_loops):
            logger.info("react.loop_start", loop=loop, message=user_message[:80])

            # --- REASON ---
            reason_step = await self._reason(
                user_message, session_context, accumulated_context, loop
            )
            steps.append(reason_step)

            if not reason_step.tool_calls:
                if accumulated_context:
                    # Retry with no tools available — low confidence
                    reason_step.confidence = min(reason_step.confidence, 0.2)
                elif self.llm is not None:
                    # No tools but LLM available — direct answer path
                    reason_step.confidence = max(reason_step.confidence, 0.7)
                else:
                    reason_step.confidence = reason_step.confidence or 0.5
                break

            # --- ACT ---
            act_step = await self._act(reason_step.tool_calls)
            steps.append(act_step)
            tools_used.extend(tc.tool_name for tc in reason_step.tool_calls)

            # --- OBSERVE ---
            observe_step = self._observe(act_step.tool_results)
            steps.append(observe_step)

            # --- EVALUATE ---
            eval_step = self._evaluate(observe_step, reason_step)
            steps.append(eval_step)

            if eval_step.confidence >= 0.5:
                # Good enough — reflect and respond
                reflect_step = await self._reflect(user_message, steps)
                steps.append(reflect_step)
                break

            # Low confidence — accumulate context and retry
            accumulated_context[f"loop_{loop}"] = {
                "tools_tried": [tc.tool_name for tc in reason_step.tool_calls],
                "results_summary": observe_step.content,
                "confidence": eval_step.confidence,
            }
            logger.info(
                "react.low_confidence_retry",
                loop=loop,
                confidence=eval_step.confidence,
            )

        # --- Build final response ---
        final_confidence = steps[-1].confidence if steps else 0.0
        escalated = final_confidence < 0.3

        response = await self._build_response(user_message, steps, final_confidence)

        result = ReActResult(
            response=response,
            confidence=final_confidence,
            steps=steps,
            tools_used=list(set(tools_used)),
            total_loops=min(loop + 1, self.MAX_LOOPS),
            total_latency_ms=(time.time() - start_time) * 1000,
            escalated=escalated,
            escalation_reason=(
                "Low confidence after max iterations" if escalated else None
            ),
        )

        logger.info(
            "react.complete",
            confidence=result.confidence,
            loops=result.total_loops,
            escalated=result.escalated,
            latency_ms=round(result.total_latency_ms, 1),
        )
        return result

    # ------------------------------------------------------------------
    # REASON
    # ------------------------------------------------------------------

    async def _reason(
        self,
        message: str,
        session_ctx: Optional[Dict],
        accumulated: Dict,
        loop_num: int,
    ) -> ReActStep:
        """Classify intent and select tools."""
        classification: ClassifyResult = self.classifier.classify(message)

        # If rule-based classification is ambiguous and LLM is available, try AI
        if classification.needs_ai and self.llm is not None:
            try:
                classification = await self.classifier.classify_with_ai(
                    message, self.llm
                )
            except Exception as exc:
                logger.warning("react.reason.ai_classify_failed", error=str(exc))

        # Select tools based on intent + entity
        tool_calls = self._select_tools(classification, accumulated, loop_num)

        # Build reasoning summary
        prev_summary = ""
        if accumulated:
            prev_tools = []
            for v in accumulated.values():
                prev_tools.extend(v.get("tools_tried", []))
            prev_summary = f" Previous attempts used: {prev_tools}."

        content = (
            f"Intent={classification.intent.value}, "
            f"Entity={classification.entity.value}, "
            f"EntityID={classification.entity_id}, "
            f"Confidence={classification.confidence:.2f}, "
            f"Tools=[{', '.join(tc.tool_name for tc in tool_calls)}]."
            f"{prev_summary}"
        )

        return ReActStep(
            phase=ReActPhase.REASON,
            content=content,
            confidence=classification.confidence,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------------
    # ACT
    # ------------------------------------------------------------------

    async def _act(self, tool_calls: List[ToolCall]) -> ReActStep:
        """Execute tools. Run independent tools in parallel with asyncio.gather."""

        async def _execute_one(tc: ToolCall) -> ToolResult:
            t0 = time.time()
            try:
                tool_fn = self.tool_registry.get(tc.tool_name)
                if tool_fn is None:
                    return ToolResult(
                        tool_name=tc.tool_name,
                        success=False,
                        data=None,
                        latency_ms=(time.time() - t0) * 1000,
                        error=f"Tool '{tc.tool_name}' not found in registry",
                    )

                # Support both sync and async tool functions
                if inspect.iscoroutinefunction(tool_fn):
                    data = await tool_fn(**tc.params)
                else:
                    data = tool_fn(**tc.params)

                return ToolResult(
                    tool_name=tc.tool_name,
                    success=True,
                    data=data,
                    latency_ms=(time.time() - t0) * 1000,
                )
            except Exception as exc:
                logger.error(
                    "react.act.tool_error", tool=tc.tool_name, error=str(exc)
                )
                return ToolResult(
                    tool_name=tc.tool_name,
                    success=False,
                    data=None,
                    latency_ms=(time.time() - t0) * 1000,
                    error=str(exc),
                )

        # Execute all tools in parallel
        results = await asyncio.gather(*[_execute_one(tc) for tc in tool_calls])

        content = "; ".join(
            f"{r.tool_name}: {'OK' if r.success else 'FAIL'} ({r.latency_ms:.0f}ms)"
            for r in results
        )

        return ReActStep(
            phase=ReActPhase.ACT,
            content=content,
            tool_results=list(results),
        )

    # ------------------------------------------------------------------
    # OBSERVE
    # ------------------------------------------------------------------

    def _observe(self, tool_results: List[ToolResult]) -> ReActStep:
        """Parse tool results, check for errors and empty results."""
        summaries: List[str] = []
        has_data = False
        error_count = 0

        for r in tool_results:
            if not r.success:
                summaries.append(f"[{r.tool_name}] ERROR: {r.error}")
                error_count += 1
            elif r.data is None or r.data == "" or r.data == [] or r.data == {}:
                summaries.append(f"[{r.tool_name}] returned empty result")
            else:
                has_data = True
                # Truncate large payloads for the summary
                data_str = str(r.data)
                if len(data_str) > 500:
                    data_str = data_str[:500] + "..."
                summaries.append(f"[{r.tool_name}] {data_str}")

        content = "\n".join(summaries) if summaries else "No tool results."

        # Observation-level confidence hint
        total = len(tool_results)
        success_count = total - error_count
        obs_confidence = (success_count / total) if total > 0 else 0.0
        if not has_data:
            obs_confidence *= 0.5  # penalise empty results

        return ReActStep(
            phase=ReActPhase.OBSERVE,
            content=content,
            confidence=obs_confidence,
            tool_results=tool_results,
        )

    # ------------------------------------------------------------------
    # EVALUATE
    # ------------------------------------------------------------------

    def _evaluate(self, observe_step: ReActStep, reason_step: ReActStep) -> ReActStep:
        """
        Score confidence using weighted formula:
          0.4 * tool_success_rate
          0.3 * result_completeness
          0.2 * intent_clarity
          0.1 * entity_match
        """
        results = observe_step.tool_results
        total = len(results) if results else 1

        # tool_success_rate: fraction of successful tool calls
        successes = sum(1 for r in results if r.success)
        tool_success_rate = successes / total

        # result_completeness: fraction of successful calls that returned data
        data_count = sum(
            1
            for r in results
            if r.success and r.data is not None and r.data != "" and r.data != [] and r.data != {}
        )
        result_completeness = (data_count / total) if total > 0 else 0.0

        # intent_clarity: from the reason step's classification confidence
        intent_clarity = reason_step.confidence

        # entity_match: 1.0 if we had tool calls and at least one returned data, else 0.5
        entity_match = 1.0 if data_count > 0 else 0.5

        confidence = score_confidence(
            tool_success_rate, result_completeness, intent_clarity, entity_match
        )

        # Determine evaluation summary
        if confidence >= 0.8:
            verdict = "HIGH — proceeding to respond"
        elif confidence >= 0.5:
            verdict = "MEDIUM — responding with caveat"
        elif confidence >= 0.3:
            verdict = "LOW — may need clarification"
        else:
            verdict = "VERY LOW — escalation likely"

        content = (
            f"Confidence={confidence:.2f} ({verdict}). "
            f"tool_success={tool_success_rate:.2f}, "
            f"completeness={result_completeness:.2f}, "
            f"intent_clarity={intent_clarity:.2f}, "
            f"entity_match={entity_match:.2f}"
        )

        return ReActStep(
            phase=ReActPhase.EVALUATE,
            content=content,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # REFLECT
    # ------------------------------------------------------------------

    async def _reflect(
        self, original_message: str, steps: List[ReActStep]
    ) -> ReActStep:
        """Verify response quality and consistency."""

        # Gather tool data from observe steps
        tool_data_parts: List[str] = []
        for step in steps:
            if step.phase == ReActPhase.OBSERVE:
                tool_data_parts.append(step.content)

        tool_data_summary = "\n".join(tool_data_parts)

        # Check consistency: does the data actually answer the question?
        last_eval = next(
            (s for s in reversed(steps) if s.phase == ReActPhase.EVALUATE), None
        )
        eval_confidence = last_eval.confidence if last_eval else 0.0

        # If LLM is available, ask it to verify
        if self.llm is not None:
            try:
                reflection_prompt = (
                    "You are verifying whether tool results answer the user's question.\n\n"
                    f"User question: \"{original_message}\"\n\n"
                    f"Tool results:\n{tool_data_summary}\n\n"
                    "Does the data sufficiently answer the question? "
                    "Reply with a JSON: {\"sufficient\": true/false, \"confidence\": 0.0-1.0, \"note\": \"...\"}"
                )
                raw = await self.llm.complete(reflection_prompt, max_tokens=150)
                import json

                parsed = json.loads(raw.strip())
                reflect_confidence = float(parsed.get("confidence", eval_confidence))
                note = parsed.get("note", "")
                content = f"Reflection: confidence={reflect_confidence:.2f}. {note}"
                return ReActStep(
                    phase=ReActPhase.REFLECT,
                    content=content,
                    confidence=min(reflect_confidence, 1.0),
                )
            except Exception as exc:
                logger.warning("react.reflect.llm_failed", error=str(exc))

        # Fallback: carry forward evaluation confidence
        content = (
            f"Reflection (rule-based): carrying forward eval confidence={eval_confidence:.2f}."
        )
        return ReActStep(
            phase=ReActPhase.REFLECT,
            content=content,
            confidence=eval_confidence,
        )

    # ------------------------------------------------------------------
    # BUILD RESPONSE
    # ------------------------------------------------------------------

    async def _build_response(
        self, message: str, steps: List[ReActStep], confidence: float
    ) -> str:
        """
        Build final response based on confidence:
          >= 0.8  : direct answer
          0.5-0.8 : answer with caveat
          0.3-0.5 : ask clarification
          < 0.3   : escalation message
        """

        # Collect all tool result data and check if tools were attempted
        tool_data: List[Any] = []
        tools_attempted = False
        pending_approvals: List[Dict] = []
        for step in steps:
            if step.phase in (ReActPhase.ACT, ReActPhase.OBSERVE):
                if step.tool_results:
                    tools_attempted = True
                for tr in step.tool_results:
                    if tr.success and tr.data is not None:
                        # Check if this is a pending_approval response from a write tool
                        if isinstance(tr.data, dict) and tr.data.get("status") == "pending_approval":
                            pending_approvals.append(tr.data)
                        else:
                            tool_data.append(tr.data)

        # If any write tool returned pending_approval, inform the user
        if pending_approvals:
            messages = []
            for pa in pending_approvals:
                messages.append(pa.get("message", f"Action '{pa.get('tool_name')}' requires approval."))
            return " ".join(messages)

        # Check escalation first — tools were tried but confidence is very low
        if confidence < 0.3 and tools_attempted:
            return (
                "I wasn't able to find a confident answer to your question. "
                "I'm escalating this to a human agent who can help you further."
            )

        # If no tools were called (direct answer path), use LLM
        if not tool_data and not tools_attempted:
            if self.llm is not None:
                try:
                    return await self.llm.complete(
                        f"Answer the following question concisely:\n{message}",
                        max_tokens=500,
                    )
                except Exception:
                    pass
            return "I don't have enough information to answer that. Could you provide more details?"

        # Summarise tool data
        data_summary = "\n".join(str(d) for d in tool_data)

        if confidence < 0.5:
            return (
                "I found some information but I'm not fully confident in the answer. "
                "Could you clarify your question? Here's what I found so far:\n\n"
                f"{data_summary}"
            )

        # For confidence >= 0.5, use LLM to synthesize a natural response
        if self.llm is not None:
            try:
                caveat = (
                    ""
                    if confidence >= 0.8
                    else " Note: indicate moderate confidence in your answer."
                )
                synthesis_prompt = (
                    f"Based on the following data, answer the user's question naturally and concisely.\n\n"
                    f"User question: \"{message}\"\n\n"
                    f"Data:\n{data_summary}\n\n"
                    f"Provide a helpful, direct answer.{caveat}"
                )
                return await self.llm.complete(synthesis_prompt, max_tokens=500)
            except Exception:
                pass

        # Fallback without LLM
        prefix = ""
        if confidence < 0.8:
            prefix = "Based on the information available (with moderate confidence): "
        return f"{prefix}{data_summary}"

    # ------------------------------------------------------------------
    # Tool selection helpers
    # ------------------------------------------------------------------

    def _select_tools(
        self,
        classification: ClassifyResult,
        accumulated: Dict,
        loop_num: int,
    ) -> List[ToolCall]:
        """
        Select tools from the registry based on intent, entity, and
        what has already been tried in previous loops.
        """
        available = self.tool_registry.list_tools() if hasattr(self.tool_registry, "list_tools") else []

        # Collect tools already tried
        tried: set = set()
        for v in accumulated.values():
            tried.update(v.get("tools_tried", []))

        intent = classification.intent
        entity = classification.entity
        entity_id = classification.entity_id

        # Build base params from classification
        base_params: Dict[str, Any] = {}
        if entity_id:
            base_params["entity_id"] = entity_id
        if entity.value != "unknown":
            base_params["entity"] = entity.value

        selected: List[ToolCall] = []

        # Intent-to-tool mapping convention:
        #   "{intent}_{entity}" is the primary tool name
        #   "{intent}" is a fallback generic tool
        #   On retry loops, try "search_{entity}" or "fallback_{intent}"

        primary_name = f"{intent.value}_{entity.value}"
        generic_name = intent.value
        search_name = f"search_{entity.value}"
        fallback_name = f"fallback_{intent.value}"

        candidates = [primary_name, generic_name]
        if loop_num > 0:
            candidates.extend([search_name, fallback_name])

        for name in candidates:
            if name in tried:
                continue
            if self.tool_registry.get(name) is not None:
                selected.append(ToolCall(tool_name=name, params=dict(base_params)))

        # If nothing matched from convention, try any available tool that
        # matches the entity name
        if not selected and available:
            for tool_name in available:
                if tool_name in tried:
                    continue
                if entity.value in tool_name or intent.value in tool_name:
                    selected.append(
                        ToolCall(tool_name=tool_name, params=dict(base_params))
                    )

        return selected

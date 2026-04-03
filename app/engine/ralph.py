"""
RALPH Self-Correction — MARS post-response quality loop.

Reflect → Act → Learn → Plan → Help

Runs after every response to:
1. Reflect: Did the response actually answer what was asked?
2. Act: If not, identify what went wrong and propose a fix
3. Learn: Record the correction for future training (distillation)
4. Plan: If a retry is needed, adjust the approach
5. Help: If stuck after 3 cycles, escalate to human

Integrates with Kafka FEEDBACK_SUBMITTED events for continuous learning.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class CorrectionType(str, Enum):
    NONE = "none"                    # Response is good
    MISSING_INTENT = "missing_intent"  # Didn't address all sub-intents
    LOW_CONFIDENCE = "low_confidence"  # Below threshold
    HALLUCINATION = "hallucination"    # Response contains claims not in context
    WRONG_ENTITY = "wrong_entity"      # Answered about wrong entity
    INCOMPLETE = "incomplete"          # Partial answer
    ESCALATE = "escalate"              # Can't fix, need human


class RALPHVerdict(str, Enum):
    PASS = "pass"          # Response is good, ship it
    RETRY = "retry"        # Fixable, retry with adjusted approach
    ESCALATE = "escalate"  # Not fixable by AI, escalate to human


@dataclass
class ReflectionResult:
    """Result of the Reflect phase."""
    intents_covered: List[Dict[str, bool]] = field(default_factory=list)
    # [{intent: str, covered: bool, evidence: str}]
    confidence_ok: bool = False
    context_grounded: bool = False  # Response is grounded in provided context
    entity_correct: bool = False
    issues: List[str] = field(default_factory=list)
    correction_type: CorrectionType = CorrectionType.NONE


@dataclass
class ActionResult:
    """Result of the Act phase — what to fix."""
    hypothesis: str = ""            # What went wrong
    proposed_fix: str = ""          # How to fix it
    adjusted_query: Optional[str] = None   # Rewritten query for retry
    adjusted_context: Optional[Dict] = None  # Additional context to inject


@dataclass
class LearnResult:
    """Result of the Learn phase — what to record."""
    should_record: bool = False
    distillation_entry: Optional[Dict] = None
    # {query, bad_response, correction_type, fix_applied, improved_response}
    feedback_event: Optional[Dict] = None


@dataclass
class RALPHResult:
    """Complete RALPH cycle result."""
    verdict: RALPHVerdict
    reflection: ReflectionResult
    action: Optional[ActionResult] = None
    learn: Optional[LearnResult] = None
    cycles_run: int = 1
    improved_response: Optional[str] = None
    total_latency_ms: float = 0.0
    escalation_reason: Optional[str] = None


class RALPHEngine:
    """
    Post-response self-correction loop.

    Runs after each response to verify quality. If issues are found:
    - Cycle 1: Reflect + Act (try to fix)
    - Cycle 2: If still bad, try different approach
    - Cycle 3: Give up and escalate to human (Help phase)

    Max 3 cycles to prevent infinite loops (MARS anti-pattern: recursive drift).
    """

    MAX_CYCLES = 3
    CONFIDENCE_THRESHOLD = 0.5

    def __init__(self, event_bus=None, react_engine=None):
        """
        Args:
            event_bus: Kafka EventBus for recording corrections
            react_engine: ReActEngine for retrying with adjusted approach
        """
        self.event_bus = event_bus
        self.react_engine = react_engine

    async def evaluate(
        self,
        query: str,
        response: str,
        confidence: float,
        intents: List[Dict],
        context: Dict[str, Any],
        tools_used: List[str] = None,
    ) -> RALPHResult:
        """
        Run RALPH evaluation on a response. Returns verdict and optional correction.

        Quick path: If reflection finds no issues, returns PASS immediately.
        """
        total_start = time.monotonic()
        tools_used = tools_used or []

        # --- Phase 1: REFLECT ---
        reflection = self._reflect(query, response, confidence, intents, context)

        if reflection.correction_type == CorrectionType.NONE:
            return RALPHResult(
                verdict=RALPHVerdict.PASS,
                reflection=reflection,
                total_latency_ms=(time.monotonic() - total_start) * 1000,
            )

        logger.info(
            "ralph.issue_detected",
            correction_type=reflection.correction_type.value,
            issues=reflection.issues,
        )

        # --- Phase 2: ACT ---
        action = self._act(query, response, reflection, context)

        # --- Phase 3: LEARN ---
        learn = self._learn(query, response, reflection, action)

        # Try to fix via retry if we have an engine
        improved_response = None
        cycles = 1

        if self.react_engine and action.adjusted_query:
            for cycle in range(1, self.MAX_CYCLES):
                cycles = cycle + 1
                try:
                    adjusted_ctx = {
                        "pipeline_context": str(context),
                        "ralph_correction": action.hypothesis,
                        "ralph_fix": action.proposed_fix,
                    }
                    if action.adjusted_context:
                        adjusted_ctx.update(action.adjusted_context)

                    result = await self.react_engine.process(
                        action.adjusted_query, adjusted_ctx
                    )

                    # Re-reflect on improved response
                    new_reflection = self._reflect(
                        query, result.response, result.confidence, intents, context
                    )

                    if new_reflection.correction_type == CorrectionType.NONE:
                        improved_response = result.response
                        logger.info("ralph.fixed", cycle=cycles)
                        break
                    elif cycle == self.MAX_CYCLES - 1:
                        # Max retries reached — escalate
                        return RALPHResult(
                            verdict=RALPHVerdict.ESCALATE,
                            reflection=reflection,
                            action=action,
                            learn=learn,
                            cycles_run=cycles,
                            total_latency_ms=(time.monotonic() - total_start) * 1000,
                            escalation_reason=(
                                f"Failed to fix after {cycles} cycles. "
                                f"Issues: {', '.join(reflection.issues)}"
                            ),
                        )
                    else:
                        # Update action for next cycle
                        action = self._act(query, result.response, new_reflection, context)

                except Exception as e:
                    logger.warning("ralph.retry_failed", cycle=cycles, error=str(e))
                    break

        # Record learning if applicable
        if learn.should_record and self.event_bus:
            await self._emit_learning(learn)

        if improved_response:
            return RALPHResult(
                verdict=RALPHVerdict.PASS,
                reflection=reflection,
                action=action,
                learn=learn,
                cycles_run=cycles,
                improved_response=improved_response,
                total_latency_ms=(time.monotonic() - total_start) * 1000,
            )

        # Couldn't fix but not critical enough to escalate
        verdict = (
            RALPHVerdict.ESCALATE
            if reflection.correction_type in (CorrectionType.HALLUCINATION, CorrectionType.WRONG_ENTITY)
            else RALPHVerdict.RETRY
        )

        return RALPHResult(
            verdict=verdict,
            reflection=reflection,
            action=action,
            learn=learn,
            cycles_run=cycles,
            total_latency_ms=(time.monotonic() - total_start) * 1000,
            escalation_reason=(
                f"Unresolved: {reflection.correction_type.value}"
                if verdict == RALPHVerdict.ESCALATE
                else None
            ),
        )

    # -------------------------------------------------------------------
    # Phase implementations
    # -------------------------------------------------------------------

    def _reflect(
        self,
        query: str,
        response: str,
        confidence: float,
        intents: List[Dict],
        context: Dict[str, Any],
    ) -> ReflectionResult:
        """Phase 1: Check if response actually answers the query."""
        result = ReflectionResult()
        response_lower = response.lower() if response else ""
        query_lower = query.lower()

        # Check 1: Confidence threshold
        result.confidence_ok = confidence >= self.CONFIDENCE_THRESHOLD
        if not result.confidence_ok:
            result.issues.append(f"Low confidence: {confidence:.2f} < {self.CONFIDENCE_THRESHOLD}")

        # Check 2: All intents addressed
        for intent in intents:
            intent_name = intent.get("intent", "unknown")
            entity_name = intent.get("entity", "unknown")

            # Simple heuristic: check if response mentions the entity
            covered = (
                entity_name.lower() in response_lower
                or entity_name == "unknown"
                or intent_name.lower() in response_lower
            )
            result.intents_covered.append({
                "intent": intent_name,
                "entity": entity_name,
                "covered": covered,
                "evidence": (
                    f"found '{entity_name}' in response"
                    if covered
                    else f"'{entity_name}' not found in response"
                ),
            })

        uncovered = [i for i in result.intents_covered if not i["covered"]]
        if uncovered:
            result.issues.append(
                f"Missing intents: {', '.join(i['intent'] for i in uncovered)}"
            )

        # Check 3: Entity correctness (if entity_id was provided)
        entity_data = context.get("entity", {})
        entity_id = entity_data.get("entity_id")
        if entity_id and entity_id not in response:
            result.entity_correct = False
            result.issues.append(f"Entity ID '{entity_id}' not referenced in response")
        else:
            result.entity_correct = True

        # Check 3b: Completeness — does response actually answer the query?
        # Detect question words in query that need answering
        question_indicators = {
            "kya": "what", "kahan": "where", "kab": "when", "kyun": "why",
            "kitna": "how much", "kaun": "who", "kaise": "how",
            "what": "what", "where": "where", "when": "when", "why": "why",
            "how": "how", "which": "which", "status": "status",
        }
        query_questions = []
        for hindi, eng in question_indicators.items():
            if hindi in query_lower:
                query_questions.append(eng)

        # If query asks multiple things (e.g., "status kya hai aur kab deliver hoga")
        # check if response addresses each
        if len(query_questions) >= 2:
            # Response should be at least 50 chars per question asked
            min_response_len = len(query_questions) * 50
            if len(response) < min_response_len:
                result.issues.append(
                    f"Response too short ({len(response)} chars) for {len(query_questions)} questions. "
                    f"Expected at least {min_response_len} chars."
                )

        # Check for "and" / "aur" / "also" in query — indicates multi-part
        multi_part_words = ["and", "aur", "also", "bhi", "saath", "plus", "along with"]
        query_parts = sum(1 for w in multi_part_words if w in query_lower)
        if query_parts > 0 and len(response) < 100:
            result.issues.append(
                f"Multi-part query ('{query_parts}' conjunctions) but response is only {len(response)} chars"
            )

        # Check 4: Context grounding (response should reference provided data)
        chunks = context.get("knowledge_chunks", [])
        if chunks:
            # Compute grounding score: what fraction of response claims are in context
            all_context_text = " ".join(
                (c.get("content", "") or "").lower() for c in chunks[:8] if isinstance(c, dict)
            )
            # Add field traces and entity data to grounded context
            for ft in context.get("field_traces", [])[:5]:
                if isinstance(ft, dict):
                    all_context_text += " " + " ".join(str(v) for v in ft.values())
            entity = context.get("entity", {})
            if isinstance(entity, dict):
                all_context_text += " " + " ".join(str(v) for v in entity.values())

            # Extract significant response terms and check grounding
            stopwords = {"the", "and", "for", "are", "but", "not", "you", "all", "can",
                         "this", "that", "with", "have", "from", "will", "your", "please",
                         "based", "available", "information"}
            response_terms = {w for w in response_lower.split() if len(w) > 4 and w not in stopwords}
            if response_terms:
                grounded_count = sum(1 for t in response_terms if t in all_context_text)
                grounding_score = grounded_count / len(response_terms)
                result.context_grounded = grounding_score >= 0.3
                if grounding_score < 0.3:
                    result.issues.append(
                        f"Low evidence grounding ({grounding_score:.0%}). "
                        f"Only {grounded_count}/{len(response_terms)} response terms found in KB context."
                    )
                elif grounding_score < 0.5:
                    result.issues.append(
                        f"Moderate grounding ({grounding_score:.0%}). Response may contain unverified claims."
                    )
            else:
                result.context_grounded = True
        else:
            result.context_grounded = True  # No context to ground against

        # Determine correction type
        if not result.issues:
            result.correction_type = CorrectionType.NONE
        elif not result.confidence_ok:
            result.correction_type = CorrectionType.LOW_CONFIDENCE
        elif uncovered:
            result.correction_type = CorrectionType.MISSING_INTENT
        elif not result.entity_correct:
            result.correction_type = CorrectionType.WRONG_ENTITY
        elif not result.context_grounded:
            result.correction_type = CorrectionType.HALLUCINATION
        else:
            result.correction_type = CorrectionType.INCOMPLETE

        return result

    def _act(
        self,
        query: str,
        response: str,
        reflection: ReflectionResult,
        context: Dict,
    ) -> ActionResult:
        """Phase 2: Formulate hypothesis and fix."""
        result = ActionResult()

        correction = reflection.correction_type

        if correction == CorrectionType.MISSING_INTENT:
            uncovered = [i for i in reflection.intents_covered if not i["covered"]]
            missing_intents = ", ".join(i["intent"] for i in uncovered)
            result.hypothesis = f"Response missed these intents: {missing_intents}"
            result.proposed_fix = "Retry with explicit instruction to address all intents"
            result.adjusted_query = (
                f"{query}\n\n[INSTRUCTION: You MUST address these topics in your response: "
                f"{missing_intents}. The previous response missed them.]"
            )

        elif correction == CorrectionType.LOW_CONFIDENCE:
            result.hypothesis = "Low confidence suggests insufficient context"
            result.proposed_fix = "Retry with broader search and explicit context injection"
            result.adjusted_query = query
            result.adjusted_context = {"force_all_pipelines": True}

        elif correction == CorrectionType.WRONG_ENTITY:
            entity_id = context.get("entity", {}).get("entity_id", "?")
            result.hypothesis = f"Response about wrong entity, expected {entity_id}"
            result.proposed_fix = f"Retry with explicit entity_id constraint"
            result.adjusted_query = (
                f"{query}\n\n[INSTRUCTION: Answer specifically about entity ID: {entity_id}]"
            )

        elif correction == CorrectionType.HALLUCINATION:
            result.hypothesis = "Response contains claims not grounded in provided context"
            result.proposed_fix = "Retry with strict grounding instruction"
            result.adjusted_query = (
                f"{query}\n\n[INSTRUCTION: Only use information from the provided context. "
                f"Do not make claims that are not supported by the KB data.]"
            )

        elif correction == CorrectionType.INCOMPLETE:
            result.hypothesis = "Response is incomplete — missing details"
            result.proposed_fix = "Retry with instruction to be thorough"
            result.adjusted_query = (
                f"{query}\n\n[INSTRUCTION: Provide a complete, detailed answer. "
                f"The previous response was too brief.]"
            )

        return result

    def _learn(
        self,
        query: str,
        response: str,
        reflection: ReflectionResult,
        action: ActionResult,
    ) -> LearnResult:
        """Phase 3: Record correction for future training."""
        if reflection.correction_type == CorrectionType.NONE:
            return LearnResult(should_record=False)

        return LearnResult(
            should_record=True,
            distillation_entry={
                "query": query,
                "bad_response": response[:500],  # truncate for storage
                "correction_type": reflection.correction_type.value,
                "issues": reflection.issues,
                "hypothesis": action.hypothesis,
                "fix_applied": action.proposed_fix,
            },
            feedback_event={
                "type": "ralph_self_correction",
                "correction_type": reflection.correction_type.value,
                "issues_count": len(reflection.issues),
            },
        )

    async def _emit_learning(self, learn: LearnResult):
        """Emit learning event to Kafka for distillation pipeline."""
        if not self.event_bus or not learn.feedback_event:
            return
        try:
            from app.events.kafka_bus import LearningInsightEvent
            event = LearningInsightEvent(
                insight_type="ralph_correction",
                data=learn.distillation_entry,
            )
            await self.event_bus.produce_learning_insight(event)
        except Exception as e:
            logger.warning("ralph.emit_learning_failed", error=str(e))

    def to_summary(self, result: RALPHResult) -> Dict[str, Any]:
        """Format RALPH result for API response."""
        summary = {
            "verdict": result.verdict.value,
            "cycles_run": result.cycles_run,
            "total_latency_ms": round(result.total_latency_ms, 1),
        }

        if result.reflection.issues:
            summary["issues_detected"] = result.reflection.issues
            summary["correction_type"] = result.reflection.correction_type.value

        if result.improved_response:
            summary["response_improved"] = True

        if result.escalation_reason:
            summary["escalation_reason"] = result.escalation_reason

        return summary

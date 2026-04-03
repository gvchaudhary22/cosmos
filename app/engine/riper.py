"""
RIPER Workflow — MARS five-phase query processing.

Research → Innovate → Plan → Execute → Review

For COMPLEX queries (classified by RequestClassifier), RIPER adds structured
reasoning phases around the core ReAct execution. For QUICK/STANDARD queries,
use RIPER Lite (Research → Plan → Execute → Verify).

Each phase produces a typed artifact that feeds into the next phase.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field

# Factuality system prompt — injected into every LLM call context
FACTUALITY_PROMPT = """CRITICAL RESPONSE RULES:
1. Use ONLY facts from the provided context documents. Do not generate fictional examples.
2. Never invent order IDs, AWB numbers, shipment IDs, company IDs, or customer data.
3. If the context doesn't contain the answer, say "I don't have this information in the knowledge base."
4. Cite sources using markers: [1], [2], [3] referencing the numbered context sections above.
5. For actions, always state preconditions and side effects from the action contract.
6. For workflows, state the current state and valid next transitions from the state machine.
7. For field questions, trace: page field → API endpoint → database table.column.
8. Never guess status values — use only documented enum values from the KB.
9. If multiple conflicting sources exist, state the conflict and recommend the higher-trust source.
10. Structure your response: Answer first, then supporting evidence with [N] citations.
"""
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class RIPERPhase(str, Enum):
    RESEARCH = "research"
    INNOVATE = "innovate"
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"


@dataclass
class PhaseArtifact:
    """Output of a single RIPER phase."""
    phase: RIPERPhase
    content: Any
    latency_ms: float = 0.0
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResearchArtifact:
    """What we know, what we don't, and what constraints exist."""
    known_facts: List[str] = field(default_factory=list)
    unknowns: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    relevant_context: Dict[str, Any] = field(default_factory=dict)
    entities_found: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InnovateArtifact:
    """Multiple approaches to answering the query, with trade-offs."""
    approaches: List[Dict[str, Any]] = field(default_factory=list)
    # Each approach: {name, description, tools_needed, estimated_confidence, trade_offs}
    recommended: Optional[str] = None
    reasoning: str = ""


@dataclass
class PlanArtifact:
    """Concrete execution plan: which pipelines, in what order."""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    # Each step: {step_id, action, pipeline, params, depends_on}
    success_criteria: List[str] = field(default_factory=list)
    estimated_latency_ms: float = 0.0


@dataclass
class ExecuteArtifact:
    """Results from executing the plan."""
    step_results: List[Dict[str, Any]] = field(default_factory=list)
    # Each: {step_id, status, result, latency_ms, error}
    response: str = ""
    confidence: float = 0.0
    tools_used: List[str] = field(default_factory=list)


@dataclass
class ReviewArtifact:
    """Verification of execution against success criteria."""
    criteria_met: List[Dict[str, Any]] = field(default_factory=list)
    # Each: {criterion, met: bool, evidence}
    all_intents_addressed: bool = False
    quality_score: float = 0.0
    issues: List[str] = field(default_factory=list)
    final_response: str = ""


@dataclass
class RIPERResult:
    """Complete RIPER workflow result."""
    phases: List[PhaseArtifact] = field(default_factory=list)
    final_response: str = ""
    confidence: float = 0.0
    tools_used: List[str] = field(default_factory=list)
    total_latency_ms: float = 0.0
    mode: str = "full"  # "full" or "lite"
    quality_score: float = 0.0


class RIPEREngine:
    """
    Five-phase structured reasoning for complex queries.

    Full RIPER (for COMPLEX queries):
        Research → Innovate → Plan → Execute → Review

    RIPER Lite (for STANDARD queries):
        Research → Plan → Execute → Review

    The engine wraps around an orchestrator and optional ReAct engine to add
    structured reasoning phases.
    """

    def __init__(self, orchestrator=None, react_engine=None):
        """
        Args:
            orchestrator: QueryOrchestrator for pipeline execution
            react_engine: ReActEngine for LLM-based reasoning
        """
        self.orchestrator = orchestrator
        self.react_engine = react_engine

    async def process_full(
        self,
        query: str,
        context: Dict[str, Any],
        intents: List[Dict] = None,
    ) -> RIPERResult:
        """Full RIPER: Research → Innovate → Plan → Execute → Review."""
        total_start = time.monotonic()
        result = RIPERResult(mode="full")
        intents = intents or []

        # Phase 1: RESEARCH
        research = await self._research(query, context, intents)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.RESEARCH,
            content=research,
            latency_ms=research.metadata.get("latency_ms", 0),
            confidence=0.0,
        ))

        # Phase 2: INNOVATE
        innovate = await self._innovate(query, research)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.INNOVATE,
            content=innovate,
            latency_ms=innovate.metadata.get("latency_ms", 0),
        ))

        # Phase 3: PLAN
        plan = await self._plan(query, research, innovate)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.PLAN,
            content=plan,
            latency_ms=plan.metadata.get("latency_ms", 0),
        ))

        # Phase 4: EXECUTE
        execute = await self._execute(query, plan, context)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.EXECUTE,
            content=execute,
            latency_ms=execute.metadata.get("latency_ms", 0),
            confidence=execute.confidence,
        ))

        # Phase 5: REVIEW
        review = await self._review(query, intents, execute, plan)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.REVIEW,
            content=review,
            latency_ms=review.metadata.get("latency_ms", 0),
        ))

        result.final_response = review.final_response or execute.response
        result.confidence = execute.confidence
        result.tools_used = execute.tools_used
        result.quality_score = review.quality_score
        result.total_latency_ms = (time.monotonic() - total_start) * 1000

        logger.info(
            "riper.complete",
            mode="full",
            phases=len(result.phases),
            confidence=round(result.confidence, 2),
            quality=round(result.quality_score, 2),
            total_ms=round(result.total_latency_ms, 1),
        )

        return result

    async def process_lite(
        self,
        query: str,
        context: Dict[str, Any],
        intents: List[Dict] = None,
    ) -> RIPERResult:
        """RIPER Lite: Research → Plan → Execute → Review (skip Innovate)."""
        total_start = time.monotonic()
        result = RIPERResult(mode="lite")
        intents = intents or []

        # Phase 1: RESEARCH
        research = await self._research(query, context, intents)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.RESEARCH, content=research,
            latency_ms=research.metadata.get("latency_ms", 0),
        ))

        # Phase 3: PLAN (skip innovate)
        plan = await self._plan(query, research, innovate=None)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.PLAN, content=plan,
            latency_ms=plan.metadata.get("latency_ms", 0),
        ))

        # Phase 4: EXECUTE
        execute = await self._execute(query, plan, context)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.EXECUTE, content=execute,
            latency_ms=execute.metadata.get("latency_ms", 0),
            confidence=execute.confidence,
        ))

        # Phase 5: REVIEW
        review = await self._review(query, intents, execute, plan)
        result.phases.append(PhaseArtifact(
            phase=RIPERPhase.REVIEW, content=review,
            latency_ms=review.metadata.get("latency_ms", 0),
        ))

        result.final_response = review.final_response or execute.response
        result.confidence = execute.confidence
        result.tools_used = execute.tools_used
        result.quality_score = review.quality_score
        result.total_latency_ms = (time.monotonic() - total_start) * 1000

        logger.info(
            "riper.complete",
            mode="lite",
            phases=len(result.phases),
            confidence=round(result.confidence, 2),
            quality=round(result.quality_score, 2),
            total_ms=round(result.total_latency_ms, 1),
        )

        return result

    # -------------------------------------------------------------------
    # Phase implementations
    # -------------------------------------------------------------------

    async def _research(
        self, query: str, context: Dict, intents: List[Dict]
    ) -> ResearchArtifact:
        """Phase 1: Extract known facts, unknowns, and constraints."""
        t0 = time.monotonic()
        artifact = ResearchArtifact()

        # Known facts from pipeline context
        if "knowledge_chunks" in context:
            chunks = context["knowledge_chunks"]
            artifact.known_facts.append(f"{len(chunks)} relevant KB chunks found")
            for chunk in chunks[:3]:
                content = chunk.get("content", "")[:100]
                artifact.known_facts.append(f"KB: {content}")

        if "page_context" in context:
            pages = context["page_context"].get("pages", [])
            artifact.known_facts.append(f"{len(pages)} matching pages found")
            for p in pages[:2]:
                artifact.known_facts.append(
                    f"Page: {p.get('page_id', '?')} (route={p.get('route', '?')})"
                )

        if "graph_traversal" in context:
            traversals = context["graph_traversal"].get("traversals", [])
            artifact.known_facts.append(f"{len(traversals)} graph paths found")

        # Entities
        if "entity" in context:
            entity = context["entity"]
            artifact.entities_found = entity
            if entity.get("entity_id"):
                artifact.known_facts.append(f"Entity ID: {entity['entity_id']}")
            else:
                artifact.unknowns.append("No entity_id extracted — may need clarification")

        # Intents as constraints
        for intent in intents:
            artifact.constraints.append(
                f"Must address intent: {intent.get('intent', '?')} "
                f"(entity={intent.get('entity', '?')}, conf={intent.get('confidence', 0):.2f})"
            )

        # Check for unknowns
        if not artifact.known_facts:
            artifact.unknowns.append("No pipeline data available — limited context")

        # Multi-intent = constraint
        if len(intents) > 1:
            artifact.constraints.append(
                f"Multi-intent query: {len(intents)} sub-intents must all be addressed"
            )

        artifact.relevant_context = context
        artifact.metadata = {"latency_ms": (time.monotonic() - t0) * 1000}

        return artifact

    async def _innovate(self, query: str, research: ResearchArtifact) -> InnovateArtifact:
        """Phase 2: Generate 3+ approaches with trade-offs."""
        t0 = time.monotonic()
        artifact = InnovateArtifact()

        # Approach 1: Direct KB answer (fastest, might miss nuance)
        artifact.approaches.append({
            "name": "direct_kb",
            "description": "Answer directly from KB chunks without further graph traversal",
            "tools_needed": ["vector_search"],
            "estimated_confidence": 0.6 if research.known_facts else 0.2,
            "trade_offs": "Fast but may miss relationship context",
        })

        # Approach 2: Graph-augmented answer (deeper, slower)
        artifact.approaches.append({
            "name": "graph_augmented",
            "description": "Use KB chunks as start nodes, traverse graph for full context",
            "tools_needed": ["vector_search", "graph_traverse"],
            "estimated_confidence": 0.8 if research.known_facts else 0.4,
            "trade_offs": "More complete but 100-200ms slower",
        })

        # Approach 3: Cross-repo diagnostic (most complete, most expensive)
        has_system_context = any("system" in str(u).lower() or "sync" in str(u).lower()
                                  for u in research.unknowns + research.constraints)
        artifact.approaches.append({
            "name": "cross_repo_diagnostic",
            "description": "Full cross-repo comparison with field-level tracing",
            "tools_needed": ["vector_search", "graph_traverse", "cross_repo", "page_role"],
            "estimated_confidence": 0.9 if has_system_context else 0.5,
            "trade_offs": "Most thorough but highest latency and token cost",
        })

        # Select best approach
        best = max(artifact.approaches, key=lambda a: a["estimated_confidence"])
        artifact.recommended = best["name"]
        artifact.reasoning = (
            f"Selected '{best['name']}' with estimated confidence "
            f"{best['estimated_confidence']:.1f}. "
            f"Known facts: {len(research.known_facts)}, "
            f"Unknowns: {len(research.unknowns)}"
        )

        artifact.metadata = {"latency_ms": (time.monotonic() - t0) * 1000}
        return artifact

    async def _plan(
        self,
        query: str,
        research: ResearchArtifact,
        innovate: Optional[InnovateArtifact] = None,
    ) -> PlanArtifact:
        """Phase 3: Create concrete execution plan.

        When a workflow state machine is available in context, validates
        planned steps against allowed_transitions and uses the decision
        matrix for conditional branching.
        """
        t0 = time.monotonic()
        artifact = PlanArtifact()

        # Check for workflow context (from KB retrieval)
        workflow_ctx = getattr(self, '_workflow_context', None)
        action_contracts = getattr(self, '_action_contracts', None) or {}

        # Determine which approach to use
        if innovate and innovate.recommended:
            approach = next(
                (a for a in innovate.approaches if a["name"] == innovate.recommended),
                innovate.approaches[0] if innovate.approaches else None,
            )
            tools = approach.get("tools_needed", []) if approach else ["vector_search"]
        else:
            tools = ["vector_search", "graph_traverse"] if research.known_facts else ["vector_search"]

        # Build execution steps
        step_id = 0
        for tool in tools:
            step_id += 1
            artifact.steps.append({
                "step_id": step_id,
                "action": f"run_{tool}",
                "pipeline": tool,
                "params": {"query": query},
                "depends_on": [],
            })

        # Workflow-guided planning: validate steps against state machine
        if workflow_ctx and isinstance(workflow_ctx, dict):
            state_machine = workflow_ctx.get("state_machine", {})
            allowed_transitions = state_machine.get("allowed_transitions", [])
            decision_matrix = workflow_ctx.get("decision_matrix", {})
            action_map = workflow_ctx.get("action_map", [])

            if allowed_transitions:
                step_id += 1
                artifact.steps.append({
                    "step_id": step_id,
                    "action": "validate_state_transition",
                    "pipeline": "workflow_validator",
                    "params": {
                        "query": query,
                        "allowed_transitions": allowed_transitions[:10],
                        "current_state": workflow_ctx.get("current_state", "unknown"),
                    },
                    "depends_on": [],
                })
                artifact.success_criteria.append(
                    "Planned action respects workflow state machine transitions"
                )

            # Add decision matrix as context for conditional branching
            if decision_matrix:
                decisions = decision_matrix.get("decisions", [])
                for dec in decisions[:3]:
                    artifact.success_criteria.append(
                        f"Consider decision: {dec.get('name', '')} ({dec.get('condition', '')})"
                    )

            # Map actions to their contracts for approval/risk checking
            for am in action_map[:5]:
                linked = am.get("linked_action_contract", "")
                if linked and linked in action_contracts:
                    contract = action_contracts[linked]
                    approval = contract.get("approval_mode", "auto")
                    risk = contract.get("risk_level", "low")
                    if approval != "auto" or risk in ("high", "critical"):
                        artifact.success_criteria.append(
                            f"Action '{am.get('action_name','')}' requires {approval} approval (risk: {risk})"
                        )

        # Add LLM synthesis step
        step_id += 1
        artifact.steps.append({
            "step_id": step_id,
            "action": "llm_synthesize",
            "pipeline": "react_engine",
            "params": {"query": query, "context": "all_pipeline_results"},
            "depends_on": list(range(1, step_id)),
        })

        # Success criteria from research constraints
        for constraint in research.constraints:
            artifact.success_criteria.append(f"Response addresses: {constraint}")

        artifact.success_criteria.append("Response confidence >= 0.5")
        artifact.success_criteria.append("All detected intents addressed in response")

        artifact.estimated_latency_ms = len(tools) * 80 + 200
        artifact.metadata = {
            "latency_ms": (time.monotonic() - t0) * 1000,
            "workflow_guided": workflow_ctx is not None,
        }

        return artifact

    @staticmethod
    def _build_attributed_context(context: Dict) -> str:
        """Build context string with source attribution for every chunk.
        This ensures the LLM knows WHERE each fact comes from and can cite it."""
        parts = [FACTUALITY_PROMPT, ""]

        # Knowledge chunks with source attribution
        chunks = context.get("knowledge_chunks", [])
        if chunks:
            parts.append("## Retrieved Knowledge (cite these sources):")
            for i, c in enumerate(chunks[:8]):
                if isinstance(c, dict):
                    meta = c.get("metadata", {}) or {}
                    source = f"{c.get('entity_type', '?')}:{c.get('entity_id', '?')}"
                    pillar = meta.get("pillar", "?")
                    trust = c.get("trust_score", 0.5)
                    content = (c.get("content", "") or "")[:400]
                    parts.append(f"\n[Source {i+1}: {source} | Pillar: {pillar} | Trust: {trust:.1f}]")
                    parts.append(content)

        # Action contracts
        # G8 fix: Special handling for P6, P7, P8 docs (not just P6)
        for c in chunks:
            if not isinstance(c, dict):
                continue
            pillar = (c.get("metadata", {}) or {}).get("pillar", "")
            if pillar == "pillar_6":
                parts.append(f"\n## Action Contract: {c.get('entity_id', '?')}")
                parts.append((c.get("content", "") or "")[:400])
            elif pillar == "pillar_7":
                parts.append(f"\n## Workflow Runbook: {c.get('entity_id', '?')}")
                parts.append((c.get("content", "") or "")[:400])
            elif pillar == "pillar_8":
                parts.append(f"\n## Negative Routing: {c.get('entity_id', '?')}")
                parts.append((c.get("content", "") or "")[:200])
            elif pillar == "entity_hub":
                parts.append(f"\n## Entity Hub: {c.get('entity_id', '?')}")
                parts.append((c.get("content", "") or "")[:500])

        # Field traces
        field_traces = context.get("field_traces", [])
        if field_traces and isinstance(field_traces, list):
            parts.append("\n## Field Traces (page → API → table.column):")
            for ft in field_traces[:5]:
                if isinstance(ft, dict):
                    parts.append(f"- {ft.get('page_field', '?')} → {ft.get('api_endpoint', '?')} → {ft.get('db_table', '?')}.{ft.get('db_column', '?')}")

        # Entity info
        entity = context.get("entity", {})
        if isinstance(entity, dict) and entity.get("entity_id"):
            parts.append(f"\n## Resolved Entity: {entity.get('entity_type', '?')}={entity['entity_id']}")

        return "\n".join(parts)

    def set_workflow_context(self, workflow: Dict) -> None:
        """Inject workflow state machine for plan validation."""
        self._workflow_context = workflow

    def set_action_contracts(self, contracts: Dict[str, Dict]) -> None:
        """Inject action contracts for approval/risk checking in plan phase."""
        self._action_contracts = contracts

    async def _execute(
        self, query: str, plan: PlanArtifact, context: Dict
    ) -> ExecuteArtifact:
        """Phase 4: Execute the plan using orchestrator and/or ReAct engine."""
        t0 = time.monotonic()
        artifact = ExecuteArtifact()

        # If we have a react engine, use it for the main execution
        if self.react_engine:
            # Build context with factuality rules and source-attributed chunks
            context_text = self._build_attributed_context(context)
            session_context = {
                "system_rules": FACTUALITY_PROMPT,
                "pipeline_context": context_text,
                "riper_plan": [s["action"] for s in plan.steps],
            }
            result = await self.react_engine.process(query, session_context)
            artifact.response = result.response
            artifact.confidence = result.confidence
            artifact.tools_used = result.tools_used

            for i, step in enumerate(plan.steps):
                artifact.step_results.append({
                    "step_id": step["step_id"],
                    "status": "success",
                    "result": f"Executed via ReAct engine",
                    "latency_ms": result.total_latency_ms / max(len(plan.steps), 1),
                })
        else:
            # Fallback: return context as-is
            artifact.response = f"Based on analysis: {len(context)} context items available"
            artifact.confidence = 0.4
            for step in plan.steps:
                artifact.step_results.append({
                    "step_id": step["step_id"],
                    "status": "skipped",
                    "result": "No execution engine available",
                    "latency_ms": 0,
                })

        artifact.metadata = {"latency_ms": (time.monotonic() - t0) * 1000}
        return artifact

    async def _review(
        self,
        query: str,
        intents: List[Dict],
        execute: ExecuteArtifact,
        plan: PlanArtifact,
    ) -> ReviewArtifact:
        """Phase 5: Verify response quality against success criteria."""
        t0 = time.monotonic()
        artifact = ReviewArtifact()

        response = execute.response.lower() if execute.response else ""

        # Check each success criterion
        for criterion in plan.success_criteria:
            if "confidence >= 0.5" in criterion:
                met = execute.confidence >= 0.5
                artifact.criteria_met.append({
                    "criterion": criterion,
                    "met": met,
                    "evidence": f"confidence={execute.confidence:.2f}",
                })
            elif "intents addressed" in criterion.lower():
                # Check if response mentions key terms from each intent
                all_addressed = True
                for intent in intents:
                    entity = intent.get("entity", "")
                    if entity and entity.lower() not in response and entity != "unknown":
                        all_addressed = False
                        break
                artifact.criteria_met.append({
                    "criterion": criterion,
                    "met": all_addressed,
                    "evidence": f"checked {len(intents)} intents",
                })
                artifact.all_intents_addressed = all_addressed
            else:
                # Generic criterion — assume met if we have a response
                artifact.criteria_met.append({
                    "criterion": criterion,
                    "met": bool(execute.response),
                    "evidence": "response present" if execute.response else "no response",
                })

        # Quality score
        met_count = sum(1 for c in artifact.criteria_met if c["met"])
        total_criteria = len(artifact.criteria_met)
        artifact.quality_score = met_count / total_criteria if total_criteria > 0 else 0.0

        # Issues
        for c in artifact.criteria_met:
            if not c["met"]:
                artifact.issues.append(f"UNMET: {c['criterion']} — {c['evidence']}")

        # If quality is too low, flag for potential retry
        if artifact.quality_score < 0.5:
            artifact.issues.append("LOW QUALITY: Consider RALPH self-correction loop")

        # LLM-powered quality check: run when react_engine is available and
        # heuristic quality score is below the 0.7 threshold.
        if self.react_engine is not None and artifact.quality_score < 0.7:
            llm = getattr(self.react_engine, "llm", None)
            if llm is not None:
                criteria_list = [c["criterion"] for c in artifact.criteria_met]
                review_prompt = (
                    "Does this response fully answer the query?\n"
                    f"Query: {query}\n"
                    f"Response: {execute.response[:500]}\n"
                    f"Criteria: {criteria_list}\n"
                    "Respond with JSON: "
                    "{\"quality_score\": 0.0-1.0, \"issues\": [], "
                    "\"improved_response\": null or string}"
                )
                try:
                    raw = await llm.complete(review_prompt, max_tokens=600)
                    parsed = json.loads(raw.strip())
                    llm_score = float(parsed.get("quality_score", artifact.quality_score))
                    llm_issues = parsed.get("issues", [])
                    llm_improved = parsed.get("improved_response", None)

                    if llm_score > artifact.quality_score:
                        logger.info(
                            "riper.review.llm_quality_upgrade",
                            heuristic=round(artifact.quality_score, 2),
                            llm=round(llm_score, 2),
                        )
                        artifact.quality_score = llm_score

                    if llm_issues:
                        artifact.issues.extend(
                            [f"LLM: {i}" for i in llm_issues if i not in artifact.issues]
                        )

                    if llm_improved:
                        artifact.final_response = llm_improved
                        logger.info("riper.review.llm_response_improved")
                except Exception as exc:
                    logger.warning("riper.review.llm_failed", error=str(exc))

        if not artifact.final_response:
            artifact.final_response = execute.response
        artifact.metadata = {"latency_ms": (time.monotonic() - t0) * 1000}

        return artifact

    def to_summary(self, result: RIPERResult) -> Dict[str, Any]:
        """Format RIPER result for API response."""
        return {
            "mode": result.mode,
            "phases": [
                {
                    "phase": p.phase.value,
                    "latency_ms": round(p.latency_ms, 1),
                    "confidence": round(p.confidence, 2) if p.confidence else None,
                }
                for p in result.phases
            ],
            "confidence": round(result.confidence, 2),
            "quality_score": round(result.quality_score, 2),
            "tools_used": result.tools_used,
            "total_latency_ms": round(result.total_latency_ms, 1),
        }

    # ------------------------------------------------------------------
    # Phase 6a: Token-level streaming final response
    # ------------------------------------------------------------------

    async def stream_final_response(
        self,
        query: str,
        context: Dict,
        intents: List[Dict] = None,
        complexity: str = "standard",
    ):
        """
        Streaming RIPER: runs RESEARCH + PLAN phases normally (non-streaming),
        then streams the EXECUTE (final answer) phase token-by-token via
        LLMClient.stream().

        Yields:
          - {"event": "phase", "phase": "research"|"plan", "latency_ms": float}
          - {"event": "chunk", "text": str}   — one per LLM token chunk
          - {"event": "done",  "confidence": float, "tools_used": list, "latency_ms": float}

        Falls back to process_full/process_lite + chunked yield when
        LLMClient is not accessible or streaming is unavailable.
        """
        from typing import AsyncIterator
        import time as _time

        intents = intents or []
        total_start = _time.monotonic()

        # ── Phase 1: RESEARCH (non-streaming) ───────────────────────────────
        t0 = _time.monotonic()
        try:
            research = await self._research(query, context, intents)
        except Exception:
            research = None
        yield {"event": "phase", "phase": "research",
               "latency_ms": round((_time.monotonic() - t0) * 1000, 1)}

        # ── Phase 2/3: PLAN (non-streaming; skip INNOVATE for speed) ────────
        t0 = _time.monotonic()
        try:
            if complexity == "complex" and research:
                innovate = await self._innovate(query, research)
            else:
                innovate = None
            plan = await self._plan(query, research, innovate=innovate)
        except Exception:
            plan = None
        yield {"event": "phase", "phase": "plan",
               "latency_ms": round((_time.monotonic() - t0) * 1000, 1)}

        # ── Phase 4: EXECUTE — stream tokens ────────────────────────────────
        # Try to get LLMClient directly from react_engine for true streaming.
        llm_client = None
        if self.react_engine:
            llm_client = getattr(self.react_engine, "llm_client", None)
            if llm_client is None:
                # Try nested attribute patterns
                llm_client = getattr(getattr(self.react_engine, "_llm", None),
                                     "client", None)

        confidence = 0.5
        tools_used: List[str] = []
        t0 = _time.monotonic()

        if llm_client is not None and hasattr(llm_client, "stream"):
            # True token-level streaming via LLMClient.stream()
            # Build the execute prompt: plan context + user query
            plan_text = ""
            if plan and plan.steps:
                plan_text = "\n".join(f"- {s['action']}" for s in plan.steps[:5])
            context_text = str(context)[:2000]  # cap to avoid token overflow

            execute_prompt = (
                f"Plan:\n{plan_text}\n\n"
                f"Context summary:\n{context_text}\n\n"
                f"Answer this query concisely and accurately:\n{query}"
            )

            try:
                async for chunk in llm_client.stream(
                    prompt=execute_prompt,
                    max_tokens=800,
                    intent="answer",
                    confidence=0.75,
                ):
                    if isinstance(chunk, str) and chunk:
                        yield {"event": "chunk", "text": chunk}
                    elif isinstance(chunk, dict):
                        # LLMClient may return a dict with text key on first/last chunk
                        text = chunk.get("text", "")
                        if text:
                            yield {"event": "chunk", "text": text}
                        confidence = chunk.get("confidence", confidence)
                        tools_used = chunk.get("tools_used", tools_used)
            except Exception as _stream_err:
                # Streaming failed — fall back to process and chunk
                try:
                    execute_artifact = await self._execute(query, plan or _noop_plan(), context)
                    text = execute_artifact.response or ""
                    for i in range(0, len(text), 40):
                        yield {"event": "chunk", "text": text[i:i + 40]}
                    confidence = execute_artifact.confidence
                    tools_used = execute_artifact.tools_used
                except Exception:
                    yield {"event": "chunk", "text": "[Streaming unavailable]"}

        else:
            # No streaming client — call _execute() and emit in small chunks
            try:
                execute_artifact = await self._execute(query, plan or _noop_plan(), context)
                text = execute_artifact.response or ""
                for i in range(0, len(text), 40):
                    yield {"event": "chunk", "text": text[i:i + 40]}
                confidence = execute_artifact.confidence
                tools_used = execute_artifact.tools_used
            except Exception as _exec_err:
                yield {"event": "chunk", "text": "[Execution failed]"}

        total_ms = round((_time.monotonic() - total_start) * 1000, 1)
        yield {"event": "done", "confidence": confidence,
               "tools_used": tools_used, "latency_ms": total_ms}


def _noop_plan():
    """Minimal plan artifact for fallback when planning failed."""
    from dataclasses import dataclass, field as _field
    from typing import List as _List

    @dataclass
    class _MinPlan:
        steps: _List = _field(default_factory=list)
        metadata: dict = _field(default_factory=dict)

    return _MinPlan()

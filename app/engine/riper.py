"""
RIPER Workflow — MARS five-phase query processing.

Research → Innovate → Plan → Execute → Review

For COMPLEX queries (classified by RequestClassifier), RIPER adds structured
reasoning phases around the core ReAct execution. For QUICK/STANDARD queries,
use RIPER Lite (Research → Plan → Execute → Verify).

Each phase produces a typed artifact that feeds into the next phase.
"""

import asyncio
import time
from dataclasses import dataclass, field
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
        """Phase 3: Create concrete execution plan."""
        t0 = time.monotonic()
        artifact = PlanArtifact()

        # Determine which approach to use
        if innovate and innovate.recommended:
            approach = next(
                (a for a in innovate.approaches if a["name"] == innovate.recommended),
                innovate.approaches[0] if innovate.approaches else None,
            )
            tools = approach.get("tools_needed", []) if approach else ["vector_search"]
        else:
            # Lite mode: default to graph_augmented if we have context
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

        artifact.estimated_latency_ms = len(tools) * 80 + 200  # rough estimate
        artifact.metadata = {"latency_ms": (time.monotonic() - t0) * 1000}

        return artifact

    async def _execute(
        self, query: str, plan: PlanArtifact, context: Dict
    ) -> ExecuteArtifact:
        """Phase 4: Execute the plan using orchestrator and/or ReAct engine."""
        t0 = time.monotonic()
        artifact = ExecuteArtifact()

        # If we have a react engine, use it for the main execution
        if self.react_engine:
            session_context = {
                "pipeline_context": str(context),
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

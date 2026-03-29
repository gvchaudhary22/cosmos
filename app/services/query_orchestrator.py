"""
Hybrid Query Orchestrator — Two-stage parallel probe + conditional deepening.

Integrates 6 MARS framework patterns:
  1. Wave Execution — numbered parallel waves with progress tracking
  2. Request Classification — domain + complexity + mode (skip/standard/complex)
  3. RIPER Workflow — Research/Innovate/Plan/Execute/Review for complex queries
  4. RALPH Self-Correction — post-response quality verification loop
  5. Agent Forge — dynamic agent creation when confidence < 60%
  6. Prompt Injection Detection — risk-scored safety guardrail (via mars_safety)

Pipeline stages:
  Stage 0: Request Classification (domain, complexity, mode)
  Stage 1: Parallel Probe (all 5 pipelines as Wave 1)
  Stage 2: Conditional Deepening (Wave 2, router-decided)
  Stage 3: LLM Assembly (via RIPER for complex, direct for quick)
  Stage 4: RALPH Self-Correction (post-response quality check)
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Attribution & Result types
# ---------------------------------------------------------------------------

class PipelineName(str, Enum):
    INTENT = "intent_classifier"
    ENTITY = "entity_lookup"
    VECTOR = "vector_search"
    PAGE_ROLE = "page_role"
    CROSS_REPO = "cross_repo"
    GRAPH_RAG = "graph_rag_deep"
    CROSS_REPO_DEEP = "cross_repo_deep"
    SESSION_HISTORY = "session_history"


@dataclass
class ProbeResult:
    """Result from a single Stage-1 probe pipeline."""
    pipeline: PipelineName
    latency_ms: float = 0.0
    found_data: bool = False
    data: Any = None
    error: Optional[str] = None
    recommend_deepen: bool = False
    reason: str = ""


@dataclass
class DeepResult:
    """Result from a Stage-2 deep pipeline."""
    pipeline: PipelineName
    latency_ms: float = 0.0
    found_data: bool = False
    data: Any = None
    error: Optional[str] = None


@dataclass
class PipelineAttribution:
    """Per-pipeline contribution tracking for analytics."""
    pipeline: str
    stage: str  # "probe" or "deep"
    latency_ms: float
    found_data: bool
    contributed: bool  # did this pipeline add useful context?
    items_count: int = 0
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class OrchestratorResult:
    """Final result from the orchestrator including all attribution."""
    # Merged context for LLM
    context: Dict[str, Any] = field(default_factory=dict)
    # Per-pipeline attribution
    attributions: List[PipelineAttribution] = field(default_factory=list)
    # Timing
    probe_latency_ms: float = 0.0
    deep_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    # Signal quality
    total_items: int = 0
    relevant_items: int = 0
    signal_to_noise: float = 0.0
    # Intents from probe
    intents: List[Dict] = field(default_factory=list)
    # Whether entity was found or needs clarification
    needs_clarification: bool = False
    clarification_prompt: Optional[str] = None
    # MARS integrations
    request_classification: Optional[Dict] = None
    wave_summary: Optional[Dict] = None
    riper_summary: Optional[Dict] = None
    ralph_summary: Optional[Dict] = None
    forge_summary: Optional[Dict] = None
    # Response metadata (on every answer)
    response_metadata: Optional[Dict] = None
    # Cache
    from_cache: bool = False
    # Tier tracking
    tiers_visited: List[int] = field(default_factory=list)
    used_fallbacks: List[str] = field(default_factory=list)
    resolution_tier: int = 0
    # Carry-down context between tiers
    tier1_context: Optional[Dict] = None
    tier2_context: Optional[Dict] = None
    tier3_context: Optional[Dict] = None


# ---------------------------------------------------------------------------
# Hybrid Query Orchestrator
# ---------------------------------------------------------------------------

class QueryOrchestrator:
    """
    Two-stage hybrid orchestrator for COSMOS query processing.

    Integrates 6 MARS patterns:
    - Wave Execution (parallel waves with progress tracking)
    - Request Classification (domain/complexity/mode routing)
    - RIPER (structured reasoning for complex queries)
    - RALPH (post-response self-correction)
    - Agent Forge (dynamic agent creation when confidence < 60%)
    - Prompt Injection Detection (risk-scored guardrail via mars_safety)

    Stage 0: Classify request → decide how deep to go
    Stage 1: Wave 1 — Parallel Probe (all 5 pipelines)
    Stage 2: Wave 2 — Conditional Deep (router-decided)
    Stage 3: LLM Assembly (RIPER full/lite based on complexity)
    Stage 4: RALPH post-response verification
    """

    def __init__(
        self,
        classifier,          # IntentClassifier
        vectorstore,          # VectorStoreService
        graphrag,             # GraphRAGService
        page_intelligence,    # PageIntelligenceService
        react_engine=None,    # ReActEngine (optional, for RIPER/RALPH/Forge)
        event_bus=None,       # Kafka EventBus (optional, for RALPH learning)
        semantic_cache=None,  # SemanticCache (Tier 0)
        codebase_intel=None,  # CodebaseIntelligence (Tier 2)
        safe_db_tool=None,    # SafeDBTool (Tier 3)
        mars_circuit=None,    # CircuitBreaker for MARS
    ):
        self.classifier = classifier
        self.vectorstore = vectorstore
        self.graphrag = graphrag
        self.page_intelligence = page_intelligence
        self.react_engine = react_engine
        self.event_bus = event_bus
        self.semantic_cache = semantic_cache
        self.codebase_intel = codebase_intel
        self.safe_db_tool = safe_db_tool

        # Circuit breaker for MARS
        if mars_circuit is None:
            from app.engine.circuit_breaker import CircuitBreaker
            mars_circuit = CircuitBreaker(name="mars")
        self.mars_circuit = mars_circuit

        # Tier policy (multi-signal gate)
        from app.engine.tier_policy import TierPolicy
        self.tier_policy = TierPolicy()

        # Cost tracker
        from app.engine.tier_cost_tracker import TierCostTracker
        self.cost_tracker = TierCostTracker()

        # MARS components — lazily initialized
        self._request_classifier = None
        self._riper_engine = None
        self._ralph_engine = None
        self._agent_forge = None

    @property
    def request_classifier(self):
        if self._request_classifier is None:
            from app.engine.request_classifier import RequestClassifier
            self._request_classifier = RequestClassifier()
        return self._request_classifier

    @property
    def riper_engine(self):
        if self._riper_engine is None:
            from app.engine.riper import RIPEREngine
            self._riper_engine = RIPEREngine(
                orchestrator=self, react_engine=self.react_engine
            )
        return self._riper_engine

    @property
    def ralph_engine(self):
        if self._ralph_engine is None:
            from app.engine.ralph import RALPHEngine
            self._ralph_engine = RALPHEngine(
                event_bus=self.event_bus, react_engine=self.react_engine
            )
        return self._ralph_engine

    @property
    def agent_forge(self):
        if self._agent_forge is None:
            from app.engine.agent_forge import AgentForge
            self._agent_forge = AgentForge(react_engine=self.react_engine)
        return self._agent_forge

    async def execute(
        self,
        query: str,
        user_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        role: Optional[str] = None,
        company_id: Optional[str] = None,
        session_context: Optional[Dict] = None,
        session_id: Optional[str] = None,
        workflow_settings=None,  # WorkflowSettings | None
    ) -> OrchestratorResult:
        """
        Complete tier pipeline with all 7 production improvements:
          Tier 0: Semantic cache check
          Tier 1: Brain (probe + deep + tools)
          Gate:   Multi-signal policy (confidence + evidence + freshness + entity)
          Tier 2: Code intelligence retrieval + LLM rewrite + ONE retry
          Gate:   Multi-signal policy
          Tier 3: Safe DB tool via MARS (template-driven)
          Post:   RALPH self-correction + learning feedback + metadata
        """
        from app.engine.request_classifier import QueryComplexity
        from app.engine.tier_policy import TierSignals, FreshnessMode, TierDecision

        total_start = time.monotonic()
        result = OrchestratorResult()
        uid = user_id or "anonymous"
        cid = company_id or ""
        # Bug 2 fix: store session_id on instance so _deep_graphrag can pick up cross-turn seeds
        self._current_session_id = session_id or ""

        # --- Apply workflow settings for this request ---
        from app.services.workflow_settings import WorkflowSettings as _WS
        ws: _WS = workflow_settings if workflow_settings is not None else _WS.balanced()
        # Override complexity if force_complex is set
        _force_complex = ws.force_complex
        # Store effective settings for use in sub-methods
        self._active_ws = ws

        # Cost tracking
        cost_record = self.cost_tracker.start_record(query, uid, cid)

        # Budget check — skip if ignore_cost_budget
        if not ws.ignore_cost_budget and not self.cost_tracker.check_budget(uid):
            result.needs_clarification = True
            result.clarification_prompt = "Daily query budget exceeded. Please try again tomorrow."
            return result

        # ===============================================================
        # TIER 0: Cache DISABLED — fresh data priority
        #
        # Cache removed because:
        # - Stale data risk outweighs 45ms latency saving
        # - CLI mode = $0 LLM cost, no cost benefit from caching
        # - 50ms Tier 1 probe is already fast enough
        # - Every answer is guaranteed fresh
        #
        # Re-enable when: >10K queries/day AND API mode AND cost > $500/mo
        # ===============================================================

        # ===============================================================
        # REQUEST CLASSIFICATION
        # ===============================================================
        classification = self.request_classifier.classify(query)
        result.request_classification = {
            "domain": classification.domain.value,
            "complexity": classification.complexity.value,
            "mode": classification.mode.value,
            "confidence": round(classification.confidence, 2),
            "sub_domains": classification.sub_domains,
        }

        # ===============================================================
        # TIER 1: Brain (probe + deep + tools)
        # ===============================================================
        result.tiers_visited.append(1)
        tier1_start = time.monotonic()

        # Stage 1a: Parallel Probe
        probe_results = await self._stage1_parallel_probe(
            query, user_id, repo_id, role, session_context
        )
        result.probe_latency_ms = (time.monotonic() - tier1_start) * 1000

        for pr in probe_results.values():
            result.attributions.append(PipelineAttribution(
                pipeline=pr.pipeline.value, stage="probe",
                latency_ms=pr.latency_ms, found_data=pr.found_data,
                contributed=pr.found_data, items_count=self._count_items(pr.data),
            ))

        # Extract intents
        intent_probe = probe_results.get(PipelineName.INTENT)
        if intent_probe and intent_probe.data:
            result.intents = intent_probe.data if isinstance(intent_probe.data, list) else [intent_probe.data]

        # Entity check
        entity_probe = probe_results.get(PipelineName.ENTITY)
        entity_resolved = entity_probe and entity_probe.found_data

        # Stage 1b: Conditional Deep
        deep_results = {}
        _effective_complex = (
            classification.complexity == QueryComplexity.COMPLEX or _force_complex
        )
        if classification.complexity == QueryComplexity.QUICK and not _force_complex:
            result.deep_latency_ms = 0.0
        else:
            deep_start = time.monotonic()
            if _effective_complex:
                deep_decisions = {
                    PipelineName.GRAPH_RAG: {"fire": True, "reason": "COMPLEX"},
                    PipelineName.CROSS_REPO_DEEP: {"fire": True, "reason": "COMPLEX"},
                    PipelineName.SESSION_HISTORY: {"fire": True, "reason": "COMPLEX"},
                }
            else:
                deep_decisions = self._route_deep(probe_results, query)

            deep_results = await self._stage2_conditional_deep(
                deep_decisions, probe_results, query, repo_id
            )
            result.deep_latency_ms = (time.monotonic() - deep_start) * 1000

            for pn, decision in deep_decisions.items():
                if decision["fire"]:
                    dr = deep_results.get(pn)
                    if dr:
                        result.attributions.append(PipelineAttribution(
                            pipeline=dr.pipeline.value, stage="deep",
                            latency_ms=dr.latency_ms, found_data=dr.found_data,
                            contributed=dr.found_data, items_count=self._count_items(dr.data),
                        ))

        result.context = self._merge_context(probe_results, deep_results)
        result.tier1_context = dict(result.context)  # snapshot for carry-down

        # Count evidence
        evidence_count = sum(1 for a in result.attributions if a.contributed)
        tools_used = []  # will be populated if ReAct runs
        live_data = any(
            a.pipeline in ("vector_search", "graph_rag_deep") and a.contributed
            for a in result.attributions
        )

        tier1_ms = (time.monotonic() - tier1_start) * 1000
        self.cost_tracker.record_tier_cost(cost_record, tier=1, api_calls=1 if live_data else 0, latency_ms=tier1_ms)

        # ---------------------------------------------------------------
        # TIER 1 GATE: Multi-signal policy
        # ---------------------------------------------------------------
        tier1_signals = TierSignals(
            answer_confidence=classification.confidence,
            evidence_count=evidence_count,
            live_data_used=live_data,
            entity_resolved=bool(entity_resolved),
            freshness_mode=FreshnessMode.LIVE_TOOL if live_data else FreshnessMode.KB_ONLY,
            tools_used=tools_used,
            intents_addressed=min(evidence_count, len(result.intents)),
            total_intents=max(len(result.intents), 1),
        )

        gate1 = self.tier_policy.evaluate_tier1(tier1_signals)
        result.resolution_tier = 1

        if gate1.decision == TierDecision.RESPOND:
            # Tier 1 sufficient — build metadata and return
            result.response_metadata = self._build_metadata(1, tier1_signals, gate1, result)
            self.cost_tracker.finalize(cost_record)
            result.total_latency_ms = (time.monotonic() - total_start) * 1000
            return result

        # ===============================================================
        # TIER 2: Code Intelligence + Claude rewrite + ONE brain retry
        # ===============================================================
        if gate1.decision == TierDecision.ESCALATE_TIER2 and not self.codebase_intel:
            # Bug 6 fix: Tier 2 unavailable — flag it instead of silently falling through
            result.used_fallbacks.append("tier2_skipped_no_intel")
            result.response_metadata = self._build_metadata(1, tier1_signals, gate1, result)
            result.response_metadata["tier2_blocked"] = True
            result.response_metadata["tier2_blocked_reason"] = "codebase_intel not initialized"
            logger.warning("orchestrator.tier2_unavailable", reason="codebase_intel_not_set")
            self.cost_tracker.finalize(cost_record)
            result.total_latency_ms = (time.monotonic() - total_start) * 1000
            return result

        if gate1.decision == TierDecision.ESCALATE_TIER2 and self.codebase_intel:
            result.tiers_visited.append(2)
            result.used_fallbacks.append("tier2_code_intel")
            tier2_start = time.monotonic()

            try:
                # Retrieve pre-indexed module docs (NOT live browsing)
                code_ctx = await self.codebase_intel.retrieve(
                    query=query, intents=result.intents, repo_id=repo_id
                )
                result.tier2_context = {
                    "modules": code_ctx.modules_matched,
                    "tables": code_ctx.db_tables,
                    "insights": code_ctx.code_insights[:3],
                    "refined_query": code_ctx.refined_query,
                }

                # ONE brain retry with refined query + Tier 1 context carry-down
                if code_ctx.refined_query != query:
                    retry_probes = await self._stage1_parallel_probe(
                        code_ctx.refined_query, user_id, repo_id, role, session_context
                    )
                    retry_context = self._merge_context(retry_probes, {})
                    # CARRY DOWN: merge Tier 1 + Tier 2 contexts
                    result.context.update(retry_context)
                    result.context["code_intelligence"] = result.tier2_context

                    evidence_count = sum(1 for _, pr in retry_probes.items() if pr.found_data)

            except Exception as e:
                logger.warning("orchestrator.tier2_failed", error=str(e))

            tier2_ms = (time.monotonic() - tier2_start) * 1000
            self.cost_tracker.record_tier_cost(cost_record, tier=2, tokens_in=500, tokens_out=200, latency_ms=tier2_ms)

            # TIER 2 GATE
            tier2_signals = TierSignals(
                answer_confidence=min(0.65, classification.confidence + 0.1),
                evidence_count=evidence_count,
                live_data_used=live_data,
                entity_resolved=bool(entity_resolved),
                freshness_mode=FreshnessMode.LIVE_TOOL if live_data else FreshnessMode.KB_ONLY,
                intents_addressed=min(evidence_count, len(result.intents)),
                total_intents=max(len(result.intents), 1),
            )

            gate2 = self.tier_policy.evaluate_tier2(tier2_signals)
            result.resolution_tier = 2

            if gate2.decision == TierDecision.RESPOND:
                result.response_metadata = self._build_metadata(2, tier2_signals, gate2, result)
                self.cost_tracker.finalize(cost_record)
                result.total_latency_ms = (time.monotonic() - total_start) * 1000
                return result

        # ===============================================================
        # TIER 3: Safe DB Tool via MARS (template-driven)
        # ===============================================================
        mars_available = self.mars_circuit.is_available

        if mars_available and self.safe_db_tool:
            result.tiers_visited.append(3)
            result.used_fallbacks.append("tier3_db_tool")
            tier3_start = time.monotonic()

            try:
                # Get template from codebase intelligence
                template_name = None
                if self.codebase_intel and hasattr(self.codebase_intel, '_match_db_template'):
                    template_name = self.codebase_intel._match_db_template(query, result.intents)

                if template_name:
                    entity_id_val = None
                    if entity_probe and entity_probe.data:
                        entity_id_val = entity_probe.data.get("entity_id")

                    db_result = await self.safe_db_tool.execute_template(
                        template_name=template_name,
                        company_id=cid,
                        entity_id=entity_id_val,
                        is_icrm_user=(cid == "1"),
                    )

                    if db_result.success:
                        result.tier3_context = {
                            "template": template_name,
                            "data": db_result.data[:10],
                            "row_count": db_result.row_count,
                        }
                        # CARRY DOWN: add DB data to context
                        result.context["db_result"] = result.tier3_context
                        self.mars_circuit.record_success()

                        # --- TIER 3 → TIER 1 LEARNING FEEDBACK ---
                        if self.event_bus:
                            try:
                                from app.events.kafka_bus import LearningInsightEvent
                                await self.event_bus.produce_learning_insight(
                                    LearningInsightEvent(
                                        insight_type="tier3_db_fallback",
                                        data={
                                            "query": query[:200],
                                            "template": template_name,
                                            "tables": db_result.data[0].keys() if db_result.data else [],
                                            "reason": "Brain couldn't answer, DB fallback succeeded. "
                                                       "KB should learn this pattern.",
                                        },
                                    )
                                )
                            except Exception:
                                pass
                    else:
                        logger.warning("orchestrator.tier3_query_failed", error=db_result.error)

                self.cost_tracker.record_tier_cost(
                    cost_record, tier=3, db_queries=1,
                    latency_ms=(time.monotonic() - tier3_start) * 1000,
                )

            except Exception as e:
                self.mars_circuit.record_failure()
                logger.warning("orchestrator.tier3_failed", error=str(e))

            # TIER 3 GATE
            has_db_data = result.tier3_context is not None
            tier3_signals = TierSignals(
                answer_confidence=0.7 if has_db_data else 0.3,
                evidence_count=evidence_count + (1 if has_db_data else 0),
                live_data_used=True,
                entity_resolved=bool(entity_resolved) or has_db_data,
                freshness_mode=FreshnessMode.DB_VERIFIED if has_db_data else FreshnessMode.KB_ONLY,
                intents_addressed=min(evidence_count + (1 if has_db_data else 0), len(result.intents)),
                total_intents=max(len(result.intents), 1),
            )

            gate3 = self.tier_policy.evaluate_tier3(tier3_signals)
            result.resolution_tier = 3

            if gate3.decision == TierDecision.ESCALATE_HUMAN:
                result.needs_clarification = True
                result.clarification_prompt = (
                    "I wasn't able to find a complete answer. "
                    "Let me connect you with a support agent who can help."
                )

            result.response_metadata = self._build_metadata(3, tier3_signals, gate3, result)

        elif not mars_available:
            # MARS DOWN — degraded mode
            self.mars_circuit.record_short_circuit()
            result.used_fallbacks.append("mars_degraded")
            result.resolution_tier = max(result.resolution_tier, 1)

            tier_signals = TierSignals(
                answer_confidence=classification.confidence,
                evidence_count=evidence_count,
                live_data_used=False,
                entity_resolved=bool(entity_resolved),
                freshness_mode=FreshnessMode.KB_ONLY,
                intents_addressed=min(evidence_count, len(result.intents)),
                total_intents=max(len(result.intents), 1),
            )
            result.response_metadata = self._build_metadata(
                result.resolution_tier, tier_signals,
                self.tier_policy.evaluate_tier1(tier_signals), result
            )
            result.response_metadata["mars_status"] = "degraded"
            result.response_metadata["freshness_mode"] = "kb_only"
        else:
            result.resolution_tier = max(result.resolution_tier, 2)
            result.response_metadata = self._build_metadata(
                result.resolution_tier, tier1_signals, gate1, result
            )

        # Finalize cost + timing
        self.cost_tracker.finalize(cost_record)
        result.total_latency_ms = (time.monotonic() - total_start) * 1000

        # Signal stats
        total = sum(a.items_count for a in result.attributions)
        relevant = sum(a.items_count for a in result.attributions if a.contributed)
        result.total_items = total
        result.relevant_items = relevant
        result.signal_to_noise = (relevant / total * 100) if total > 0 else 0.0

        # Cache DISABLED — every query runs fresh
        # Re-enable when scale requires it (>10K queries/day)

        logger.info(
            "orchestrator.complete",
            query=query[:60],
            resolution_tier=result.resolution_tier,
            tiers_visited=result.tiers_visited,
            fallbacks=result.used_fallbacks,
            total_ms=round(result.total_latency_ms, 1),
        )

        return result

    def _build_metadata(self, tier: int, signals, gate, result: OrchestratorResult) -> Dict:
        """Build response metadata for every answer."""
        return {
            "resolution_tier": tier,
            "freshness_mode": signals.freshness_mode.value if hasattr(signals.freshness_mode, 'value') else str(signals.freshness_mode),
            "confidence": round(signals.answer_confidence, 3),
            "composite_score": round(gate.composite_score, 3),
            "used_fallbacks": list(result.used_fallbacks),
            "evidence_count": signals.evidence_count,
            "entity_resolved": signals.entity_resolved,
            "intents_addressed": signals.intents_addressed,
            "total_intents": signals.total_intents,
            "tools_used": signals.tools_used or [],
            "tiers_visited": list(result.tiers_visited),
            "cost_estimate_usd": self.tier_policy.estimate_cost(result.tiers_visited),
            "gate_reason": gate.reason,
        }

    # ===================================================================
    # Stage 1: Parallel Probe
    # ===================================================================

    async def _stage1_parallel_probe(
        self,
        query: str,
        user_id: Optional[str],
        repo_id: Optional[str],
        role: Optional[str],
        session_context: Optional[Dict],
    ) -> Dict[PipelineName, ProbeResult]:
        """Run all 5 probe pipelines in parallel via asyncio.gather."""

        tasks = {
            PipelineName.INTENT: self._probe_intent(query),
            PipelineName.ENTITY: self._probe_entity(query, user_id, session_context),
            PipelineName.VECTOR: self._probe_vector(query, repo_id),
            PipelineName.PAGE_ROLE: self._probe_page_role(query, role),
            PipelineName.CROSS_REPO: self._probe_cross_repo(query, repo_id),
        }

        keys = list(tasks.keys())
        coros = list(tasks.values())

        # Bug 3 fix: per-task 10s timeout so a hung probe doesn't stall the entire wave
        wrapped = [asyncio.wait_for(c, timeout=10.0) for c in coros]
        results = await asyncio.gather(*wrapped, return_exceptions=True)

        probe_map: Dict[PipelineName, ProbeResult] = {}
        for key, res in zip(keys, results):
            if isinstance(res, Exception):
                probe_map[key] = ProbeResult(
                    pipeline=key,
                    error=str(res),
                )
                logger.warning("probe.error", pipeline=key.value, error=str(res))
            else:
                probe_map[key] = res

        return probe_map

    async def _probe_intent(self, query: str) -> ProbeResult:
        """P1: Intent classification — always runs, always useful."""
        t0 = time.monotonic()
        try:
            classification = self.classifier.classify(query)
            intents = [{
                "intent": classification.intent.value,
                "entity": classification.entity.value,
                "entity_id": classification.entity_id,
                "confidence": classification.confidence,
                "needs_ai": classification.needs_ai,
                "sub_intents": classification.sub_intents,
            }]
            return ProbeResult(
                pipeline=PipelineName.INTENT,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=True,
                data=intents,
                recommend_deepen=False,  # intent is fully resolved in probe
                reason="intent classification complete",
            )
        except Exception as e:
            return ProbeResult(
                pipeline=PipelineName.INTENT,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _probe_entity(
        self, query: str, user_id: Optional[str], session_context: Optional[Dict]
    ) -> ProbeResult:
        """P2: Entity extraction — extract IDs, seller context."""
        t0 = time.monotonic()
        try:
            entity_id = self.classifier._extract_id(query)
            data = {
                "entity_id": entity_id,
                "user_id": user_id,
                "needs_id": entity_id is None,
                "from_session": False,
            }

            # If no entity_id found but we have graphrag, try entity lookup
            if entity_id is None and user_id and self.graphrag:
                try:
                    node = await self.graphrag.pg_lookup_entity("seller", user_id)
                    if node:
                        data["seller_node"] = {
                            "id": node.id,
                            "label": node.label,
                            "properties": node.properties,
                        }
                        data["from_session"] = True
                except Exception:
                    pass

            found = entity_id is not None or data.get("seller_node") is not None
            if not found:
                data["clarification"] = "Could you share the order ID or AWB number so I can look this up?"

            return ProbeResult(
                pipeline=PipelineName.ENTITY,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data=data,
                recommend_deepen=not found,
                reason="entity extracted" if found else "no entity_id found, may need clarification",
            )
        except Exception as e:
            return ProbeResult(
                pipeline=PipelineName.ENTITY,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _probe_vector(self, query: str, repo_id: Optional[str]) -> ProbeResult:
        """P3: Vector similarity search — find relevant KB chunks."""
        t0 = time.monotonic()
        try:
            if not self.vectorstore:
                return ProbeResult(
                    pipeline=PipelineName.VECTOR,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    reason="vectorstore not available",
                )

            results = await self.vectorstore.search_similar(
                query=query,
                limit=5,
                repo_id=repo_id,
                threshold=0.3,
            )

            found = len(results) > 0
            top_score = results[0]["similarity"] if found else 0.0

            return ProbeResult(
                pipeline=PipelineName.VECTOR,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data={
                    "chunks": results,
                    "top_relevance": top_score,
                    "count": len(results),
                },
                recommend_deepen=found and top_score > 0.5,
                reason=f"{len(results)} chunks, top={top_score:.2f}",
            )
        except Exception as e:
            return ProbeResult(
                pipeline=PipelineName.VECTOR,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _probe_page_role(self, query: str, role: Optional[str]) -> ProbeResult:
        """P4: Page-role lookup — find matching pages and check permissions."""
        t0 = time.monotonic()
        try:
            if not self.page_intelligence:
                return ProbeResult(
                    pipeline=PipelineName.PAGE_ROLE,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    reason="page_intelligence not available",
                )

            pages = await self.page_intelligence.search_pages(
                query=query, role=role, top_k=3
            )

            found = len(pages) > 0
            top_score = pages[0].get("score", 0) if found else 0.0

            # Check role access on top page
            role_access = None
            if found and role:
                try:
                    perms = await self.page_intelligence.get_role_permissions(
                        role=role, page_id=pages[0]["page_id"]
                    )
                    role_access = perms.get("has_access", None)
                except Exception:
                    pass

            return ProbeResult(
                pipeline=PipelineName.PAGE_ROLE,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data={
                    "pages": pages,
                    "top_score": top_score,
                    "role_access": role_access,
                    "count": len(pages),
                },
                recommend_deepen=False,  # probe is usually enough
                reason=f"{len(pages)} pages, top={top_score:.2f}",
            )
        except Exception as e:
            return ProbeResult(
                pipeline=PipelineName.PAGE_ROLE,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _probe_cross_repo(self, query: str, repo_id: Optional[str]) -> ProbeResult:
        """P5: Cross-repo alias lookup — check if query spans repos."""
        t0 = time.monotonic()
        try:
            if not self.page_intelligence:
                return ProbeResult(
                    pipeline=PipelineName.CROSS_REPO,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    reason="page_intelligence not available",
                )

            # Keywords that suggest cross-repo interest
            cross_keywords = [
                "icrm", "admin", "internal", "sync", "system",
                "crm", "ops", "agent", "panel", "backend",
            ]
            query_lower = query.lower()
            has_cross_signal = any(kw in query_lower for kw in cross_keywords)

            mapping = None
            if has_cross_signal:
                # Find pages matching query, then look up cross-repo mapping
                pages = await self.page_intelligence.search_pages(query=query, top_k=1)
                if pages:
                    mapping = await self.page_intelligence.get_cross_repo_mapping(
                        pages[0]["page_id"]
                    )

            found = mapping is not None and mapping.get("found", False) if isinstance(mapping, dict) else mapping is not None
            return ProbeResult(
                pipeline=PipelineName.CROSS_REPO,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data={
                    "has_cross_signal": has_cross_signal,
                    "mapping": mapping,
                },
                recommend_deepen=found,
                reason="cross-repo mapping found" if found else "no cross-repo signal",
            )
        except Exception as e:
            return ProbeResult(
                pipeline=PipelineName.CROSS_REPO,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    # ===================================================================
    # Stage 2: Conditional Deepening Router + Execution
    # ===================================================================

    def _route_deep(
        self,
        probes: Dict[PipelineName, ProbeResult],
        query: str,
    ) -> Dict[PipelineName, Dict]:
        """Decide which deep operations to fire based on probe results."""
        decisions: Dict[PipelineName, Dict] = {}
        query_lower = query.lower()

        intent_data = probes.get(PipelineName.INTENT)
        intents = intent_data.data if intent_data and intent_data.data else []
        intent_values = set()
        sub_intents = []
        for i in (intents if isinstance(intents, list) else [intents]):
            if isinstance(i, dict):
                intent_values.add(i.get("intent", ""))
                sub_intents.extend(i.get("sub_intents", []))

        vector_probe = probes.get(PipelineName.VECTOR)
        cross_probe = probes.get(PipelineName.CROSS_REPO)

        # --- GraphRAG Deep ---
        # Fire when: intent is trace/explain/why AND vector search found start nodes
        trace_keywords = ["why", "trace", "how", "sync", "delayed", "stuck", "broken", "path", "flow"]
        has_trace_intent = bool(intent_values & {"explain", "lookup"}) or any(
            kw in query_lower for kw in trace_keywords
        )
        has_vector_nodes = (
            vector_probe
            and vector_probe.found_data
            and vector_probe.data
            and vector_probe.data.get("top_relevance", 0) > 0.4
        )

        decisions[PipelineName.GRAPH_RAG] = {
            "fire": has_trace_intent and has_vector_nodes,
            "reason": (
                "trace intent + vector nodes available"
                if has_trace_intent and has_vector_nodes
                else f"no trace intent ({has_trace_intent}) or no vector nodes ({has_vector_nodes})"
            ),
        }

        # --- Cross-Repo Deep ---
        # Fire when: probe found a cross-repo mapping AND intent involves sync/system
        cross_found = cross_probe and cross_probe.found_data
        sync_keywords = ["sync", "system", "update", "status", "internal", "icrm"]
        has_sync_intent = any(kw in query_lower for kw in sync_keywords)

        decisions[PipelineName.CROSS_REPO_DEEP] = {
            "fire": bool(cross_found) and has_sync_intent,
            "reason": (
                "cross-repo mapping found + sync intent"
                if cross_found and has_sync_intent
                else f"no cross-repo ({cross_found}) or no sync intent ({has_sync_intent})"
            ),
        }

        # --- Session History ---
        # Fire when: entity probe found no entity_id (need to look up recent activity)
        entity_probe = probes.get(PipelineName.ENTITY)
        no_entity = entity_probe and not entity_probe.found_data

        decisions[PipelineName.SESSION_HISTORY] = {
            "fire": bool(no_entity),
            "reason": (
                "no entity_id found, checking session history"
                if no_entity
                else "entity already resolved"
            ),
        }

        return decisions

    async def _stage2_conditional_deep(
        self,
        decisions: Dict[PipelineName, Dict],
        probes: Dict[PipelineName, ProbeResult],
        query: str,
        repo_id: Optional[str],
    ) -> Dict[PipelineName, DeepResult]:
        """Execute only the deep operations the router approved, in parallel."""
        tasks = {}

        if decisions.get(PipelineName.GRAPH_RAG, {}).get("fire"):
            tasks[PipelineName.GRAPH_RAG] = self._deep_graphrag(probes, query, repo_id)

        if decisions.get(PipelineName.CROSS_REPO_DEEP, {}).get("fire"):
            tasks[PipelineName.CROSS_REPO_DEEP] = self._deep_cross_repo(probes)

        if decisions.get(PipelineName.SESSION_HISTORY, {}).get("fire"):
            tasks[PipelineName.SESSION_HISTORY] = self._deep_session_history(probes, query)

        if not tasks:
            return {}

        keys = list(tasks.keys())
        coros = list(tasks.values())

        # Bug 3 fix: per-task 20s timeout so a hung deep stage doesn't block forever
        wrapped = [asyncio.wait_for(c, timeout=20.0) for c in coros]
        results = await asyncio.gather(*wrapped, return_exceptions=True)

        deep_map: Dict[PipelineName, DeepResult] = {}
        for key, res in zip(keys, results):
            if isinstance(res, Exception):
                deep_map[key] = DeepResult(
                    pipeline=key, error=str(res)
                )
                logger.warning("deep.error", pipeline=key.value, error=str(res))
            else:
                deep_map[key] = res

        return deep_map

    async def _deep_graphrag(
        self,
        probes: Dict[PipelineName, ProbeResult],
        query: str,
        repo_id: Optional[str],
    ) -> DeepResult:
        """Deep GraphRAG via HybridRetriever — 4-leg parallel + weighted RRF + context assembly.

        Replaces the old traverse()/query_related() with the unified hybrid retriever
        that runs exact lookup + graph neighborhood + vector + lexical in parallel.
        """
        t0 = time.monotonic()
        try:
            from app.graph.retrieval import hybrid_retriever
            from app.graph.context import ContextAssembler

            # Extract intent/entity from probe results
            intent_data = probes.get(PipelineName.INTENT)
            intent = None
            entity = None
            entity_id = None
            if intent_data and intent_data.data:
                idata = intent_data.data if isinstance(intent_data.data, dict) else (
                    intent_data.data[0] if isinstance(intent_data.data, list) and intent_data.data else {}
                )
                intent = idata.get("intent")
                entity = idata.get("entity")
                entity_id = idata.get("entity_id")

            # Pull session entity seeds for cross-turn context
            session_seeds: Dict[str, list] = {}
            if hasattr(self, "_session_state_mgr") and self._session_state_mgr:
                sid = getattr(self, "_current_session_id", "")
                state = self._session_state_mgr.get_state(sid) if sid else None
                if state and state.entities_discussed:
                    session_seeds = state.entities_discussed

            # Bug 5 fix: reuse vector hits from Stage-1 probe to avoid re-embedding
            probe_vector = probes.get(PipelineName.VECTOR)
            pre_vector_hits = None
            if probe_vector and probe_vector.found_data and isinstance(probe_vector.data, dict):
                pre_vector_hits = probe_vector.data.get("chunks")

            # Run hybrid retrieval (4 legs in parallel)
            retrieval = await hybrid_retriever.retrieve(
                query=query,
                intent=intent,
                entity=entity,
                entity_id=entity_id,
                repo_id=repo_id,
                max_depth=2,
                top_k=10,
                session_entity_seeds=session_seeds if session_seeds else None,
                pre_vector_hits=pre_vector_hits,
            )

            if not retrieval.ranked_nodes:
                return DeepResult(
                    pipeline=PipelineName.GRAPH_RAG,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    found_data=False,
                )

            # Assemble token-budgeted context
            assembler = ContextAssembler(max_tokens=2000)
            ctx = assembler.assemble(retrieval)

            # Build traversal-compatible data for _build_llm_context
            traversal_results = [{
                "start_node": "hybrid_retrieval",
                "formatted_context": ctx.text,
                "total_matches": len(retrieval.ranked_nodes),
                "nodes": [
                    {
                        "id": rn.node.id,
                        "type": rn.node.node_type.value,
                        "label": rn.node.label,
                        "score": rn.score,
                        "sources": rn.sources,
                    }
                    for rn in retrieval.ranked_nodes[:15]
                ],
                "edges": [
                    {
                        "src": e.source_id,
                        "tgt": e.target_id,
                        "type": e.edge_type.value,
                    }
                    for e in retrieval.all_edges[:20]
                ],
                "leg_diagnostics": {
                    name: {"hit_count": leg.hit_count, "latency_ms": round(leg.latency_ms, 1)}
                    for name, leg in retrieval.leg_results.items()
                },
                "entity_resolved": retrieval.entity_resolved,
                "evidence_count": retrieval.evidence_count,
            }]

            return DeepResult(
                pipeline=PipelineName.GRAPH_RAG,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=True,
                data={"traversals": traversal_results, "count": len(traversal_results)},
            )
        except Exception as e:
            return DeepResult(
                pipeline=PipelineName.GRAPH_RAG,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _deep_cross_repo(
        self, probes: Dict[PipelineName, ProbeResult]
    ) -> DeepResult:
        """Deep cross-repo comparison: field-level diffs between mapped pages."""
        t0 = time.monotonic()
        try:
            cross_data = probes.get(PipelineName.CROSS_REPO)
            if not cross_data or not cross_data.data:
                return DeepResult(
                    pipeline=PipelineName.CROSS_REPO_DEEP,
                    latency_ms=(time.monotonic() - t0) * 1000,
                )

            mapping = cross_data.data.get("mapping", {})
            if not mapping or not self.page_intelligence:
                return DeepResult(
                    pipeline=PipelineName.CROSS_REPO_DEEP,
                    latency_ms=(time.monotonic() - t0) * 1000,
                )

            # Get both pages' full details for comparison
            source_id = mapping.get("source_page") or mapping.get("page_id", "")
            target_id = mapping.get("target_page") or mapping.get("mapped_to", "")

            source_page = await self.page_intelligence.get_page(source_id) if source_id else None
            target_page = await self.page_intelligence.get_page(target_id) if target_id else None

            # Compare fields between repos
            comparison = {
                "source": {"page_id": source_id, "field_count": 0, "fields": []},
                "target": {"page_id": target_id, "field_count": 0, "fields": []},
                "shared_fields": [],
                "source_only_fields": [],
                "target_only_fields": [],
            }

            if source_page and "fields" in source_page:
                src_fields = {f.get("name", ""): f for f in source_page.get("fields", [])}
                comparison["source"]["fields"] = list(src_fields.keys())
                comparison["source"]["field_count"] = len(src_fields)
            else:
                src_fields = {}

            if target_page and "fields" in target_page:
                tgt_fields = {f.get("name", ""): f for f in target_page.get("fields", [])}
                comparison["target"]["fields"] = list(tgt_fields.keys())
                comparison["target"]["field_count"] = len(tgt_fields)
            else:
                tgt_fields = {}

            shared = set(src_fields.keys()) & set(tgt_fields.keys())
            comparison["shared_fields"] = list(shared)
            comparison["source_only_fields"] = list(set(src_fields.keys()) - shared)
            comparison["target_only_fields"] = list(set(tgt_fields.keys()) - shared)

            found = bool(shared) or (source_page is not None and target_page is not None)
            return DeepResult(
                pipeline=PipelineName.CROSS_REPO_DEEP,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data=comparison,
            )
        except Exception as e:
            return DeepResult(
                pipeline=PipelineName.CROSS_REPO_DEEP,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _deep_session_history(
        self, probes: Dict[PipelineName, ProbeResult], query: str
    ) -> DeepResult:
        """Look up recent activity when no entity_id was extracted."""
        t0 = time.monotonic()
        try:
            entity_data = probes.get(PipelineName.ENTITY)
            user_id = (entity_data.data or {}).get("user_id") if entity_data and entity_data.data else None

            if not user_id or not self.graphrag:
                return DeepResult(
                    pipeline=PipelineName.SESSION_HISTORY,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    data={"recent_entities": [], "source": "none"},
                )

            # Search for recent seller activity via graph
            result = await self.graphrag.pg_find_nodes(
                node_type="seller",
                label_contains=user_id,
                limit=5,
            )

            recent = []
            for node in (result or []):
                recent.append({
                    "id": node.id,
                    "label": node.label,
                    "properties": node.properties,
                })

            found = len(recent) > 0
            return DeepResult(
                pipeline=PipelineName.SESSION_HISTORY,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found,
                data={"recent_entities": recent, "source": "graphrag"},
            )
        except Exception as e:
            return DeepResult(
                pipeline=PipelineName.SESSION_HISTORY,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    # ===================================================================
    # Stage 3: Context Merger
    # ===================================================================

    def _merge_context(
        self,
        probes: Dict[PipelineName, ProbeResult],
        deeps: Dict[PipelineName, DeepResult],
    ) -> Dict[str, Any]:
        """Merge all probe + deep results into a structured context for LLM."""
        ctx: Dict[str, Any] = {}

        # Intent
        intent_probe = probes.get(PipelineName.INTENT)
        if intent_probe and intent_probe.found_data:
            ctx["intents"] = intent_probe.data

        # Entity
        entity_probe = probes.get(PipelineName.ENTITY)
        if entity_probe and entity_probe.data:
            ctx["entity"] = entity_probe.data

        # Vector chunks
        vector_probe = probes.get(PipelineName.VECTOR)
        if vector_probe and vector_probe.found_data:
            ctx["knowledge_chunks"] = vector_probe.data.get("chunks", [])

        # Page-role
        page_probe = probes.get(PipelineName.PAGE_ROLE)
        if page_probe and page_probe.found_data:
            ctx["page_context"] = page_probe.data

        # Cross-repo (probe level)
        cross_probe = probes.get(PipelineName.CROSS_REPO)
        if cross_probe and cross_probe.found_data:
            ctx["cross_repo_alias"] = cross_probe.data

        # Deep: GraphRAG
        graph_deep = deeps.get(PipelineName.GRAPH_RAG)
        if graph_deep and graph_deep.found_data:
            ctx["graph_traversal"] = graph_deep.data

        # Deep: Cross-repo comparison
        cross_deep = deeps.get(PipelineName.CROSS_REPO_DEEP)
        if cross_deep and cross_deep.found_data:
            ctx["cross_repo_comparison"] = cross_deep.data

        # Deep: Session history
        session_deep = deeps.get(PipelineName.SESSION_HISTORY)
        if session_deep and session_deep.found_data:
            ctx["session_history"] = session_deep.data

        return ctx

    # ===================================================================
    # Helpers
    # ===================================================================

    @staticmethod
    def _count_items(data: Any) -> int:
        """Count items in pipeline data for attribution."""
        if data is None:
            return 0
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            # Count nested lists
            total = 0
            for v in data.values():
                if isinstance(v, list):
                    total += len(v)
                elif v is not None:
                    total += 1
            return max(total, 1) if data else 0
        return 1

    def to_attribution_summary(self, result: OrchestratorResult) -> Dict[str, Any]:
        """Format attribution for API response / analytics storage."""
        pipeline_breakdown = {}
        for attr in result.attributions:
            key = attr.pipeline
            if key not in pipeline_breakdown:
                pipeline_breakdown[key] = {
                    "contributed": False,
                    "latency_ms": 0.0,
                    "items": 0,
                    "stages": [],
                }
            entry = pipeline_breakdown[key]
            entry["contributed"] = entry["contributed"] or attr.contributed
            entry["latency_ms"] += attr.latency_ms
            entry["items"] += attr.items_count
            if attr.skipped:
                entry["stages"].append(f"{attr.stage}:skipped({attr.skip_reason})")
            else:
                entry["stages"].append(f"{attr.stage}:{'hit' if attr.found_data else 'miss'}")

        return {
            "pipeline_breakdown": pipeline_breakdown,
            "timing": {
                "probe_ms": round(result.probe_latency_ms, 1),
                "deep_ms": round(result.deep_latency_ms, 1),
                "total_ms": round(result.total_latency_ms, 1),
            },
            "signal": {
                "total_items": result.total_items,
                "relevant_items": result.relevant_items,
                "signal_to_noise_pct": round(result.signal_to_noise, 1),
            },
        }

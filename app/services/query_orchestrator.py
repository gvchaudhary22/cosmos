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
    # Wave 3 (LangGraph) and Wave 4 (Neo4j) enrichment contexts
    wave3_context: Optional[Dict] = None
    wave4_context: Optional[Dict] = None


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

        # New: Pattern cache, agent registry, planner, skills, scoped retrieval, KB registry
        self._pattern_cache = None
        self._agent_registry = None
        self._agent_planner = None
        self._skill_registry = None
        self._scoped_retrieval = None
        self._kb_registry = None

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

    @property
    def pattern_cache(self):
        if self._pattern_cache is None:
            from app.engine.pattern_cache import PatternCache
            self._pattern_cache = PatternCache()
        return self._pattern_cache

    @property
    def agent_registry(self):
        if self._agent_registry is None:
            from app.engine.agent_registry import AgentRegistry
            self._agent_registry = AgentRegistry()
        return self._agent_registry

    @property
    def agent_planner(self):
        if self._agent_planner is None:
            from app.engine.planner import AgentPlanner
            self._agent_planner = AgentPlanner(self.agent_registry)
        return self._agent_planner

    @property
    def skill_registry(self):
        if self._skill_registry is None:
            from app.engine.skill_registry import SkillRegistry
            self._skill_registry = SkillRegistry()
        return self._skill_registry

    @property
    def scoped_retrieval(self):
        if self._scoped_retrieval is None:
            from app.engine.scoped_retrieval import ScopedRetrieval
            self._scoped_retrieval = ScopedRetrieval(self.vectorstore)
        return self._scoped_retrieval

    @property
    def kb_registry(self):
        if self._kb_registry is None:
            from app.engine.kb_driven_registry import KBDrivenRegistry
            kb_path = getattr(self, '_kb_path', '')
            self._kb_registry = KBDrivenRegistry(kb_path=kb_path)
        return self._kb_registry

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
        on_wave_progress=None,   # Phase 5f: async callable(wave_id, task_id, status, data)
        seller_token: Optional[str] = None,  # Pre-fetched by MARS SSO — used for live API calls
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

        # Hinglish normalization: convert Hindi-English mixed queries to clean English
        # before intent classification. Uses lightweight keyword mapping (no LLM call).
        # Claude-based normalization happens at the LIME/MARS layer before reaching COSMOS.
        query = self._normalize_hinglish(query)

        # Query decomposition: split multi-part questions for separate retrieval
        sub_queries = self._decompose_query(query)
        if len(sub_queries) > 1:
            logger.info("orchestrator.query_decomposed", original=query[:80],
                        sub_queries=len(sub_queries))
        # Store for multi-retrieval merge later
        self._sub_queries = sub_queries

        # Bug 2 fix: store session_id on instance so _deep_graphrag can pick up cross-turn seeds
        self._current_session_id = session_id or ""

        # --- Apply workflow settings for this request ---
        from app.services.workflow_settings import WorkflowSettings as _WS
        ws: _WS = workflow_settings if workflow_settings is not None else _WS.balanced()
        # Override complexity if force_complex is set
        _force_complex = ws.force_complex
        # Store effective settings for use in sub-methods
        self._active_ws = ws

        # Phase 6b: Multi-turn context compression.
        # Before the pipeline starts, check if the session has accumulated enough
        # turns to benefit from compression (>= COMPRESS_THRESHOLD).  If so,
        # compress older turns into a compact summary block and prepend it to
        # session_context so all downstream stages see the condensed history.
        if session_id:
            try:
                from app.engine.session_state import SessionStateManager as _SSM
                _session_mgr: Optional[_SSM] = getattr(self, "_session_manager", None)
                if _session_mgr is None:
                    # Lazily create a shared manager on the orchestrator instance
                    _session_mgr = _SSM()
                    self._session_manager = _session_mgr

                if _session_mgr.get_state(session_id) is None:
                    # Create session state if it doesn't exist yet
                    _session_mgr.create_session(session_id, uid, cid)

                if _session_mgr.should_compress(session_id):
                    # Run async compression (LLM or keyword fallback)
                    _llm_client = getattr(self.react_engine, "llm_client", None) if self.react_engine else None
                    await _session_mgr.compress_history(session_id, llm_client=_llm_client)
                    logger.info("orchestrator.session_compressed", session_id=session_id)

                # Prepend compressed context prefix to session_context dict
                _ctx_prefix = _session_mgr.get_compressed_context_prefix(session_id)
                if _ctx_prefix:
                    if session_context is None:
                        session_context = {}
                    session_context.setdefault("compressed_history", _ctx_prefix)
            except Exception as _e6b:
                logger.debug("orchestrator.session_compress_error", error=str(_e6b))

        # Phase 5f: WaveExecutor — tracks per-wave progress for SSE streaming.
        # If on_wave_progress is provided (e.g. from /chat/stream endpoint),
        # progress events are emitted as each wave starts/completes.
        # WaveExecutor is used purely for progress tracking here — it doesn't
        # replace asyncio.gather() because the waves already run in parallel
        # within the existing pipeline.  The callback is stored on self so
        # sub-methods (_stage1, _deep_graphrag, etc.) can call it directly.
        self._wave_progress_cb = on_wave_progress
        if on_wave_progress is not None:
            from app.engine.wave_executor import WaveExecutor
            self._wave_executor = WaveExecutor(on_progress=on_wave_progress)
        else:
            self._wave_executor = None

        # Inject pre-fetched seller token from MARS into session_context so tools
        # can use it for live Shiprocket API calls without a separate SSO login.
        if seller_token:
            if session_context is None:
                session_context = {}
            session_context["seller_token"] = seller_token
            session_context["sso_token"] = seller_token
            logger.info("orchestrator.seller_token_injected",
                        company_id=cid, token_preview=seller_token[:8] + "...")

        # Cost tracking
        cost_record = self.cost_tracker.start_record(query, uid, cid)

        # Budget check — skip if ignore_cost_budget
        if not ws.ignore_cost_budget and not self.cost_tracker.check_budget(uid):
            result.needs_clarification = True
            result.clarification_prompt = "Daily query budget exceeded. Please try again tomorrow."
            return result

        # ===============================================================
        # TIER 0: Pattern Match Fast Path
        #
        # Check if this query matches a high-confidence cached pattern.
        # If yes: skip M2-M8 (planner, retriever, ReAct), directly execute
        # the cached tool sequence. 4x faster for repeat query types.
        # Pattern must have: 30+ successes AND 0.90+ confidence.
        # Scoped by: intent + entity_type + company_id + normalized query.
        # ===============================================================
        try:
            await self.pattern_cache.load_patterns()
            # Quick classify for pattern matching (fast, no LLM)
            quick_class = self.classifier.classify(query)
            intent_str = quick_class.intent.value if hasattr(quick_class, 'intent') else "unknown"
            entity_str = quick_class.entity.value if hasattr(quick_class, 'entity') else "unknown"

            fast_path = self.pattern_cache.match(
                query=query, intent=intent_str, entity_type=entity_str,
                repo_id=repo_id or "", role=role or "",
            )
            if fast_path.hit:
                logger.info("orchestrator.fast_path_hit",
                            pattern=fast_path.pattern_key, conf=fast_path.confidence)

                # EXECUTE the cached tool sequence (read-only patterns only)
                from app.engine.execution_engine import ExecutionEngine
                exec_engine = ExecutionEngine(
                    tool_registry=getattr(self.react_engine, 'tool_registry', None),
                    react_engine=self.react_engine,
                    vectorstore=self.vectorstore,
                )
                # Extract entity_id from quick classification
                eid = quick_class.entity_id if hasattr(quick_class, 'entity_id') else None

                fp_result = await exec_engine.execute_fast_path(
                    fast_path=fast_path, entity_id=eid, query=query,
                )

                if fp_result.success:
                    result.response_metadata = {
                        "tier": 0, "fast_path": True,
                        "pattern_key": fast_path.pattern_key,
                        "skipped_stages": fast_path.skipped_stages,
                        "tools_used": fp_result.tools_used,
                        "latency_ms": round(fp_result.latency_ms, 1),
                    }
                    result.context = {"fast_path_response": fp_result.response}
                    result.tool_results = fp_result.tool_results

                    # Record success for confidence building
                    await self.pattern_cache.record_success(
                        query=query, intent=intent_str, entity_type=entity_str,
                        tool_sequence=fast_path.tool_sequence,
                        repo_id=repo_id or "", role=role or "",
                        latency_ms=fp_result.latency_ms,
                    )
                    result.total_latency_ms = (time.monotonic() - total_start) * 1000
                    return result
                else:
                    # Fast path failed — fall through to normal pipeline
                    logger.warning("orchestrator.fast_path_execution_failed",
                                   error=fp_result.response)
                    await self.pattern_cache.record_failure(
                        query=query, intent=intent_str, entity_type=entity_str,
                    )
        except Exception as e:
            logger.debug("orchestrator.fast_path_error", error=str(e))

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
        # Store for scoped retrieval to use
        self._active_classification = result.request_classification

        # ===============================================================
        # QUERY INTELLIGENCE LAYER (Claude-powered prompt optimization)
        # Before waves execute, Claude analyzes the raw query and generates
        # an enriched retrieval plan with: search terms, expected pillars,
        # entity hints, and query mode. This gives waves precise instructions
        # instead of relying on keyword matching.
        # ===============================================================
        query_intel = await self._enrich_query_with_claude(
            query, result.request_classification, role, cid,
        )
        if query_intel:
            # Store enriched query for downstream use
            self._query_intel = query_intel
            # Use enriched search query for retrieval if available
            enriched_query = query_intel.get("search_query", query)
            if enriched_query and len(enriched_query) > 5:
                query = enriched_query
                logger.info("orchestrator.query_enriched",
                            original=query[:60], enriched=enriched_query[:60],
                            entities=query_intel.get("entities", []),
                            pillars=query_intel.get("target_pillars", []))

        # ===============================================================
        # TIER 1: Brain (probe + deep + tools)
        # ===============================================================
        result.tiers_visited.append(1)
        tier1_start = time.monotonic()

        # Phase 4e: W2 speculative prefetch — start Legs 3+4 immediately
        # with the raw query in parallel with Wave 1 probes.
        # Legs 3 (vector) and 4 (lexical) don't need Wave 1 intent/entity.
        # When Wave 1 finishes we pass pre_vector_hits to skip re-embedding.
        _prefetch_task: Optional[asyncio.Task] = None
        if classification.complexity != QueryComplexity.QUICK:
            _prefetch_task = asyncio.create_task(
                self._prefetch_wave2_legs3_and_4(query, repo_id)
            )

        # Emit Wave 1 start progress
        await self._emit_wave_progress(1, "wave1_scope_detect", "running",
                                       {"query": query[:80]})

        # Stage 1a: Parallel Probe (runs while prefetch is in flight)
        probe_results = await self._stage1_parallel_probe(
            query, user_id, repo_id, role, session_context
        )
        result.probe_latency_ms = (time.monotonic() - tier1_start) * 1000

        # Emit Wave 1 done
        await self._emit_wave_progress(1, "wave1_scope_detect", "completed",
                                       {"latency_ms": round(result.probe_latency_ms, 1),
                                        "intents": len(result.intents)})

        # Collect prefetch results (almost always done by now — overlapped with W1)
        _prefetch_hits: Optional[List[Dict]] = None
        if _prefetch_task is not None:
            try:
                _prefetch_hits = await asyncio.wait_for(_prefetch_task, timeout=0.5)
            except (asyncio.TimeoutError, Exception):
                _prefetch_hits = None

        # Inject prefetch hits into vector probe result so _deep_graphrag
        # reuses them via pre_vector_hits (avoids re-embedding)
        if _prefetch_hits:
            vector_probe = probe_results.get(PipelineName.VECTOR)
            if vector_probe and not (vector_probe.found_data and
                                     vector_probe.data.get("chunks")):
                if not vector_probe.data:
                    vector_probe = ProbeResult(
                        pipeline=PipelineName.VECTOR,
                        found_data=bool(_prefetch_hits),
                        data={"chunks": _prefetch_hits,
                              "top_relevance": _prefetch_hits[0].get("score", 0.5)
                              if _prefetch_hits else 0.0},
                        latency_ms=vector_probe.latency_ms,
                    )
                    probe_results[PipelineName.VECTOR] = vector_probe

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

        # ---------------------------------------------------------------
        # MULTI-AGENT DETECTION: If 2+ intents, create execution plan
        # and run via ExecutionEngine with handoffs
        # ---------------------------------------------------------------
        if len(result.intents) >= 2:
            try:
                intent_strs = []
                entity_str_plan = ""
                entity_id_plan = None
                for i_data in result.intents:
                    if isinstance(i_data, dict):
                        intent_strs.append(i_data.get("intent", "unknown"))
                        if not entity_str_plan:
                            entity_str_plan = i_data.get("entity", "unknown")
                        if not entity_id_plan:
                            entity_id_plan = i_data.get("entity_id")
                    elif isinstance(i_data, str):
                        intent_strs.append(i_data)

                if len(intent_strs) >= 2:
                    plan = self.agent_planner.plan(
                        query=query, intents=intent_strs,
                        entity_type=entity_str_plan, entity_id=entity_id_plan,
                    )

                    if plan.is_multi_agent and plan.steps:
                        logger.info("orchestrator.multi_agent_plan",
                                    steps=len(plan.steps),
                                    agents=[s.agent_name for s in plan.steps])

                        from app.engine.execution_engine import ExecutionEngine
                        exec_engine = ExecutionEngine(
                            tool_registry=getattr(self.react_engine, 'tool_registry', None),
                            react_engine=self.react_engine,
                            vectorstore=self.vectorstore,
                        )

                        plan_result = await exec_engine.execute_plan(
                            plan=plan,
                            entity_id=entity_id_plan,
                            repo_id=repo_id,
                            session_context=session_context,
                            agent_registry=self.agent_registry,
                        )

                        if plan_result.success:
                            result.context = {
                                "multi_agent_response": plan_result.response,
                                "agent_chain": plan_result.agent_chain,
                                "handoffs": plan_result.handoffs,
                            }
                            result.tool_results = plan_result.tool_results
                            result.response_metadata = {
                                "tier": 1, "multi_agent": True,
                                "agents": plan_result.agent_chain,
                                "handoffs": len(plan_result.handoffs),
                                "latency_ms": round(plan_result.latency_ms, 1),
                            }
                            # Record success for pattern cache learning
                            tool_seq = [{"tool_name": t} for t in plan_result.tools_used]
                            if quick_class:
                                await self.pattern_cache.record_success(
                                    query=query, intent=intent_strs[0],
                                    entity_type=entity_str_plan,
                                    tool_sequence=tool_seq,
                                    agent_name=",".join(plan_result.agent_chain),
                                    latency_ms=plan_result.latency_ms,
                                    repo_id=repo_id or "", role=role or "",
                                )
                            result.total_latency_ms = (time.monotonic() - total_start) * 1000
                            return result
            except Exception as e:
                logger.warning("orchestrator.multi_agent_failed", error=str(e))
                # Fall through to normal single-agent path

        # Stage 1b: Conditional Deep
        # Quality override: force Wave 2 for admin/ICRM queries (quality > cost)
        _admin_override = (role or "").lower() in ("admin", "icrm_admin", "operator", "support")
        deep_results = {}
        _effective_complex = (
            classification.complexity == QueryComplexity.COMPLEX or _force_complex or _admin_override
        )
        if classification.complexity == QueryComplexity.QUICK and not _force_complex and not _admin_override:
            result.deep_latency_ms = 0.0
        else:
            deep_start = time.monotonic()
            await self._emit_wave_progress(2, "wave2_deep_retrieval", "running",
                                           {"complex": _effective_complex})
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
            await self._emit_wave_progress(2, "wave2_deep_retrieval", "completed",
                                           {"latency_ms": round(result.deep_latency_ms, 1)})

            for pn, decision in deep_decisions.items():
                if decision["fire"]:
                    dr = deep_results.get(pn)
                    if dr:
                        result.attributions.append(PipelineAttribution(
                            pipeline=dr.pipeline.value, stage="deep",
                            latency_ms=dr.latency_ms, found_data=dr.found_data,
                            contributed=dr.found_data, items_count=self._count_items(dr.data),
                        ))

        result.context = await self._merge_context(probe_results, deep_results)
        result.tier1_context = dict(result.context)  # snapshot for carry-down

        # ===============================================================
        # WAVE 3: LangGraph stateful reasoning
        # Runs independently after W1+W2. Uses merged context to reason
        # about gaps, refine entities, and select tools iteratively.
        # Output feeds into Wave 4 as refined entity targets.
        # ===============================================================
        # Auto-enable Wave 3 for ambiguous or action queries (low confidence, action intent)
        _w3_auto = False
        if not ws.wave3_langgraph_enabled:
            cls_conf = classification.confidence if hasattr(classification, 'confidence') else (
                (result.request_classification or {}).get("confidence", 1.0)
            )
            cls_domain = (result.request_classification or {}).get("domain", "")
            _w3_auto = (
                cls_conf < 0.6  # ambiguous query
                or cls_domain in ("action", "process", "workflow", "troubleshoot")
                or _admin_override
            )
            if _w3_auto:
                logger.info("orchestrator.wave3_auto_enabled", reason="ambiguous_or_action",
                            confidence=cls_conf, domain=cls_domain)

        if ws.wave3_langgraph_enabled or _w3_auto:
            await self._emit_wave_progress(3, "wave3_langgraph",
                                           "running" if not ws.wave3_shadow_mode else "running_shadow",
                                           {"shadow": ws.wave3_shadow_mode})
            try:
                w3_result = await asyncio.wait_for(
                    self._stage3_langgraph(query, result.context, result.intents, ws),
                    timeout=ws.wave3_timeout_sec,
                )
                if w3_result:
                    result.wave3_context = w3_result
                    await self._emit_wave_progress(3, "wave3_langgraph", "completed",
                                                   {"shadow": ws.wave3_shadow_mode,
                                                    "confidence": w3_result.get("confidence", 0)})
                    if ws.wave3_shadow_mode:
                        # Shadow mode: log for comparison only, do NOT inject into context
                        logger.info("orchestrator.wave3_shadow",
                                    entities=len(w3_result.get("refined_entities", [])),
                                    confidence=w3_result.get("confidence", 0),
                                    shadow=True)
                    else:
                        result.context["wave3_reasoning"] = w3_result
                        logger.info("orchestrator.wave3_done",
                                    entities=len(w3_result.get("refined_entities", [])),
                                    confidence=w3_result.get("confidence", 0))
            except asyncio.TimeoutError:
                logger.warning("orchestrator.wave3_timeout", sec=ws.wave3_timeout_sec)
            except Exception as w3_err:
                logger.warning("orchestrator.wave3_failed", error=str(w3_err))

        # ===============================================================
        # WAVE 3.5: Module-hint targeted vector search
        # Uses module_hint from LangGraph enrichment to run a precise
        # metadata-filtered vector search for module docs.
        # Runs only when wave3 produced a module_hint and vectorstore available.
        # This bypasses broken graph BFS when cross_links are empty.
        # ===============================================================
        _w3_ctx = result.wave3_context or {}
        _module_hint = _w3_ctx.get("module_hint", "")
        if _module_hint and self.vectorstore:
            try:
                _enriched_q = _w3_ctx.get("enriched_query", "") or query
                _module_chunks = await asyncio.wait_for(
                    self.vectorstore.search_similar(
                        query=_enriched_q,
                        limit=6,
                        entity_type="module_doc",
                        module=_module_hint,
                        repo_id=repo_id,
                        threshold=0.2,
                    ),
                    timeout=5.0,
                )
                if _module_chunks:
                    result.context["module_chunks"] = _module_chunks
                    logger.info("orchestrator.module_hint_search",
                                module=_module_hint, hits=len(_module_chunks))
            except Exception as _mh_err:
                logger.warning("orchestrator.module_hint_search_failed", error=str(_mh_err))

        # ===============================================================
        # WAVE 4: Neo4j targeted graph traversal
        # Runs independently after W3. Uses W3-refined entities for
        # targeted BFS (not keyword search) — much more precise.
        # Falls back silently if Neo4j is unavailable.
        # ===============================================================
        if ws.wave4_neo4j_enabled:
            await self._emit_wave_progress(4, "wave4_neo4j",
                                           "running" if not ws.wave4_shadow_mode else "running_shadow",
                                           {"shadow": ws.wave4_shadow_mode})
            try:
                w4_result = await asyncio.wait_for(
                    self._stage4_neo4j(query, result.context, ws),
                    timeout=ws.wave4_timeout_sec,
                )
                if w4_result:
                    result.wave4_context = w4_result
                    await self._emit_wave_progress(4, "wave4_neo4j", "completed",
                                                   {"shadow": ws.wave4_shadow_mode,
                                                    "paths": w4_result.get("path_count", 0)})
                    if ws.wave4_shadow_mode:
                        # Shadow mode: log for comparison only, do NOT inject into context
                        logger.info("orchestrator.wave4_shadow",
                                    paths=w4_result.get("path_count", 0),
                                    shadow=True)
                    else:
                        result.context["wave4_graph"] = w4_result
                        logger.info("orchestrator.wave4_done",
                                    paths=w4_result.get("path_count", 0))
            except asyncio.TimeoutError:
                logger.warning("orchestrator.wave4_timeout", sec=ws.wave4_timeout_sec)
            except Exception as w4_err:
                logger.warning("orchestrator.wave4_failed", error=str(w4_err))

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
        # CLARIFICATION GATE (COSMOS pattern — middle confidence tier)
        # When confidence is 0.3-0.6 AND KB evidence is sparse AND entity
        # is unresolved, ask ONE targeted clarifying question instead of
        # returning a low-confidence answer. This is the active clarification
        # path — the 0.3-0.6 tier gets a specific question, not just an
        # uncertainty marker. Only skips for QUICK queries (entity ID already
        # requested by entity probe) and budget-exceeded cases.
        # ---------------------------------------------------------------
        _cls_conf = (result.request_classification or {}).get("confidence", 1.0)
        if (
            not result.needs_clarification
            and 0.3 <= _cls_conf < 0.6
            and evidence_count < 3
            and not entity_resolved
            and classification.complexity != QueryComplexity.QUICK
        ):
            clarification = self._generate_clarification_question(
                query=query,
                intents=result.intents,
                entity_probe=probe_results.get(PipelineName.ENTITY),
                context=result.context,
            )
            if clarification:
                result.needs_clarification = True
                result.clarification_prompt = clarification
                result.response_metadata = {
                    "resolution_tier": 0,
                    "reason": "clarification_needed",
                    "cls_confidence": round(_cls_conf, 2),
                    "evidence_count": evidence_count,
                }
                self.cost_tracker.finalize(cost_record)
                result.total_latency_ms = (time.monotonic() - total_start) * 1000
                logger.info(
                    "orchestrator.clarification_gate_fired",
                    confidence=round(_cls_conf, 2),
                    evidence_count=evidence_count,
                    query=query[:60],
                )
                return result

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
                    retry_context = await self._merge_context(retry_probes, {})
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

        # ── Grounding Verification: Check factual claims against evidence ──
        # Runs on the final response before returning to user.
        try:
            response_text = ""
            if isinstance(result.context, dict):
                response_text = result.context.get("content", "") or result.context.get("response", "") or ""
            if response_text and len(response_text) > 50:
                from app.engine.grounding import GroundingVerifier
                verifier = GroundingVerifier()
                chunks = result.context.get("knowledge_chunks", []) if isinstance(result.context, dict) else []
                grounding_result = await verifier.verify(response_text, chunks)
                if result.response_metadata is None:
                    result.response_metadata = {}
                result.response_metadata["grounding"] = {
                    "score": grounding_result.grounding_score,
                    "verified_claims": grounding_result.verified_claims,
                    "unverified_claims": grounding_result.unverified_claims,
                    "total_claims": grounding_result.total_claims,
                    "sources": grounding_result.sources_used,
                }
                # If grounding score is low, add disclaimer to context
                if grounding_result.grounding_score < 0.5 and grounding_result.total_claims > 0:
                    result.response_metadata["low_grounding_warning"] = True
        except Exception as e:
            logger.debug("orchestrator.grounding_failed", error=str(e))

        # ── Learning Memory: Record interaction for future improvement ──
        try:
            from app.engine.learning_memory import LearningMemory
            memory = LearningMemory()
            tools_used_names = [a.pipeline for a in result.attributions if a.contributed]
            domain = ""
            if result.intents:
                domain = result.intents[0].get("domain", "") if isinstance(result.intents[0], dict) else ""
            await memory.record_interaction(
                operator_id=uid,
                query=query[:500],
                response=str(result.context.get("content", ""))[:500] if isinstance(result.context, dict) else "",
                tools_used=tools_used_names,
                domain=domain,
            )

            # Session entity tracking (STATE.md pattern from COSMOS):
            # When an entity was resolved, store it so future turns in the
            # same session can skip re-retrieval for the same entity.
            if entity_resolved and self._current_session_id:
                entity_data = {}
                if entity_probe and entity_probe.data:
                    entity_data = entity_probe.data
                entity_id_val = entity_data.get("entity_id", "")
                entity_type_val = entity_data.get("entity_type", "unknown")
                if entity_id_val:
                    await memory.record_entity_resolution(
                        operator_id=uid,
                        session_id=self._current_session_id,
                        entity_id=entity_id_val,
                        entity_type=entity_type_val,
                        resolved_data={
                            "entity_id": entity_id_val,
                            "domain": domain,
                            "query_snippet": query[:100],
                        },
                    )
        except Exception as e:
            logger.debug("orchestrator.learning_memory_failed", error=str(e))

        logger.info(
            "orchestrator.complete",
            query=query[:60],
            resolution_tier=result.resolution_tier,
            tiers_visited=result.tiers_visited,
            fallbacks=result.used_fallbacks,
            total_ms=round(result.total_latency_ms, 1),
        )

        return result

    def _generate_clarification_question(
        self,
        query: str,
        intents: List[Dict],
        entity_probe,
        context: Dict,
    ) -> Optional[str]:
        """Generate ONE targeted clarifying question when query is ambiguous.

        COSMOS clarification gate pattern: detect what specifically blocks
        retrieval and ask a precise question to unlock it. Returns None
        when no targeted question can be generated (fall through to normal
        uncertain answer path).

        Ambiguity types detected (priority order):
        1. Missing entity ID (order, AWB, seller) — most common blocker
        2. Intent ambiguous between lookup/action
        3. Temporal scope missing for report-type queries
        """
        q = query.lower()

        # 1. Entity probe ran but found no entity → ask for the ID
        if entity_probe is not None and not entity_probe.found_data:
            # Use clarification hint from probe data if available
            probe_hint = (entity_probe.data or {}).get("clarification", "")
            if probe_hint:
                return probe_hint

            # Derive entity type from intent or query keywords
            intent_domain = ""
            if intents:
                first = intents[0]
                intent_domain = (first.get("domain", "") if isinstance(first, dict) else "").lower()

            if any(w in q for w in ("awb", "shipment", "tracking", "track")):
                return "Could you share the AWB or tracking number so I can look this up?"
            if any(w in q for w in ("order", "order_id", "order id")):
                return "Could you share the order ID so I can pull up the details?"
            if any(w in q for w in ("seller", "company", "account", "merchant")):
                return "Could you provide the seller ID or company name?"
            if "ticket" in q or "issue" in q or "complaint" in q:
                return "Could you share the ticket or complaint reference number?"

        # 2. Intent is ambiguous between lookup and action
        if intents:
            intent_vals = [
                (i.get("intent", "") if isinstance(i, dict) else "").lower()
                for i in intents
            ]
            if all(v in ("unknown", "") for v in intent_vals):
                is_action = any(w in q for w in ("cancel", "update", "change", "assign", "create", "delete", "modify"))
                is_lookup = any(w in q for w in ("status", "check", "where", "what", "show", "find"))
                if is_action and is_lookup:
                    return "Are you looking to check the current status, or would you like to make a change?"
                if is_action and not is_lookup:
                    return "Which order or shipment should I apply this change to?"

        # 3. Temporal scope missing for report/list queries
        is_report = any(w in q for w in ("report", "list", "show all", "how many", "count", "total"))
        has_time = any(w in q for w in ("today", "yesterday", "week", "month", "last", "this", "since", "from", "to"))
        if is_report and not has_time:
            return "What time period should I look at — today, this week, or last month?"

        return None

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

            # Detect query mode for retrieval routing
            query_mode = self._detect_query_mode(query)

            # Use Claude Query Intelligence hints if available
            _intel = getattr(self, '_query_intel', None) or {}
            if _intel.get("intent"):
                query_mode = _intel["intent"]  # Claude's intent overrides keyword detection
            target_pillar = None
            if _intel.get("target_pillars"):
                # Use first target pillar as primary filter
                target_pillar = _intel["target_pillars"][0]

            # Use scoped retrieval if agent is identified, else global search
            agent = None
            try:
                if hasattr(self, '_active_classification'):
                    ac = self._active_classification
                    entity_str = ac.get("domain", "")
                    agent = self.agent_registry.get_for_intent("lookup", entity_str)
            except Exception:
                pass

            if agent and self.scoped_retrieval:
                # Pass capability based on query mode
                cap = {"act": "action", "diagnose": "workflow"}.get(query_mode)
                results = await self.scoped_retrieval.search_for_agent(
                    query=query, agent=agent, limit=5,
                    threshold=0.3, repo_id=repo_id, capability=cap,
                )
            else:
                # G4+G5 fix: lower threshold for act/diagnose (P6/P7 embeddings are newer, lower base scores)
                # Don't use query_mode filter exclusively — also do a fallback unfiltered search
                _threshold = 0.25 if query_mode in ("act", "diagnose") else 0.3
                results = await self.vectorstore.search_similar(
                    query=query,
                    limit=5,
                    repo_id=repo_id,
                    threshold=_threshold,
                    query_mode=query_mode if query_mode not in ("lookup", None) else None,
                    pillar=target_pillar,
                )

                # G4 fix: if filtered search returned <3 results, do unfiltered fallback
                if len(results) < 3:
                    fallback = await self.vectorstore.search_similar(
                        query=query, limit=5, repo_id=repo_id, threshold=_threshold,
                    )
                    seen = {r.get("entity_id") for r in results}
                    for fb in fallback:
                        if fb.get("entity_id") not in seen:
                            results.append(fb)
                            seen.add(fb.get("entity_id"))

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

    # Keywords that signal a page/role/field query — used for page_signal detection
    _PAGE_SIGNAL_KEYWORDS = frozenset([
        "page", "tab", "button", "screen", "panel", "view", "dashboard",
        "field", "column", "role", "access", "permission", "who can",
        "can see", "visible", "show", "display", "ui", "frontend",
        "seller", "admin", "agent", "icrm", "portal",
    ])

    @staticmethod
    def _extract_field_candidates(query: str) -> list:
        """Extract candidate field names from a query using simple heuristics.

        Looks for snake_case tokens and camelCase words that are likely field names.
        Returns a deduplicated list of up to 5 candidates.
        """
        import re
        candidates = []
        # snake_case words (at least 2 parts: e.g. wallet_balance, tracking_status)
        snake = re.findall(r'\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b', query)
        candidates.extend(snake)
        # camelCase words
        camel = re.findall(r'\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b', query)
        candidates.extend(camel)
        # Words after "field", "column", "value of", "show", "display"
        trigger = re.findall(
            r'(?:field|column|value of|show|display|update)\s+["\']?([a-z][a-z0-9_]+)["\']?',
            query.lower(),
        )
        candidates.extend(trigger)
        # Deduplicate preserving order, cap at 5
        seen: set = set()
        result = []
        for c in candidates:
            if c not in seen and len(c) > 3:
                seen.add(c)
                result.append(c)
                if len(result) >= 5:
                    break
        return result

    async def _probe_page_role(self, query: str, role: Optional[str]) -> ProbeResult:
        """P4: Page-role lookup — find matching pages, check permissions, trace fields.

        Also detects page_signal (keyword probe) and runs field_trace for any
        snake_case / camelCase field names detected in the query. This provides
        deterministic field→API→column answers for ICRM UI queries without
        requiring W5 to be fully wired yet.
        """
        t0 = time.monotonic()
        try:
            if not self.page_intelligence:
                return ProbeResult(
                    pipeline=PipelineName.PAGE_ROLE,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    reason="page_intelligence not available",
                )

            # page_signal: keyword probe (zero cost — just string matching)
            query_lower = query.lower()
            page_signal = any(kw in query_lower for kw in self._PAGE_SIGNAL_KEYWORDS)

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

            # Field trace: try to resolve field names mentioned in the query
            field_traces = []
            if page_signal or found:
                candidates = self._extract_field_candidates(query)
                for field_name in candidates:
                    try:
                        traces = await self.page_intelligence.get_field_trace(field_name)
                        if traces:
                            field_traces.extend(traces[:3])  # cap per field
                            if len(field_traces) >= 10:
                                break
                    except Exception:
                        pass

            return ProbeResult(
                pipeline=PipelineName.PAGE_ROLE,
                latency_ms=(time.monotonic() - t0) * 1000,
                found_data=found or bool(field_traces),
                data={
                    "pages": pages,
                    "top_score": top_score,
                    "role_access": role_access,
                    "count": len(pages),
                    "page_signal": page_signal,
                    "field_traces": field_traces,
                },
                recommend_deepen=False,  # probe is usually enough
                reason=f"{len(pages)} pages, top={top_score:.2f}, field_traces={len(field_traces)}",
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
    # Phase 5f: Wave progress emission helper
    # ===================================================================

    async def _emit_wave_progress(
        self,
        wave_id: int,
        task_id: str,
        status: str,
        data: Optional[Dict] = None,
    ) -> None:
        """Emit a progress event via the on_wave_progress callback if registered.

        Called at each wave start/completion so the SSE endpoint can stream
        real-time progress to the client.  Also records to the active OTEL span
        if tracing is configured.  Fails silently — progress errors must never
        block the main query pipeline.
        """
        # Phase 6d: record wave event to active OTEL span
        try:
            from app.monitoring.otel_tracing import record_wave_event
            _active_span = getattr(self, "_active_otel_span", None)
            record_wave_event(_active_span, wave_id, task_id, status, data)
        except Exception:
            pass

        if self._wave_progress_cb is None:
            return
        try:
            await self._wave_progress_cb(wave_id, task_id, status, data or {})
        except Exception as _prog_err:
            logger.debug("orchestrator.wave_progress_error",
                         wave=wave_id, task=task_id, error=str(_prog_err))

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

        # --- GraphRAG Deep (HybridRetriever) ---
        # Fire when: vector search returned any hits (threshold lowered from 0.4 to 0.2).
        # This ensures HybridRetriever runs for all non-trivial queries, not just
        # trace/explain intent. The 4-leg RRF retrieval always improves evidence quality.
        has_vector_nodes = (
            vector_probe
            and vector_probe.found_data
            and vector_probe.data
            and vector_probe.data.get("top_relevance", 0) > 0.2
        )

        decisions[PipelineName.GRAPH_RAG] = {
            "fire": bool(has_vector_nodes),
            "reason": (
                "vector hits available → running 4-leg HybridRetriever"
                if has_vector_nodes
                else "no vector hits above threshold"
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

            # Phase 2: Neighbor chunk expansion (sibling evidence from same parent_doc_id)
            # Pass pre_vector_hits as the primary source — they carry full metadata
            # JSON (parent_doc_id + chunk_index) regardless of whether the hit resolved
            # to a real graph node or a proxy node.
            neighbor_chunks = []
            try:
                from app.services.neighbor_expander import NeighborExpander
                expander = NeighborExpander(window=1, max_parents=5)
                existing_ids = {rn.node.id for rn in retrieval.ranked_nodes}
                expand_result = await expander.expand(
                    retrieval.ranked_nodes,
                    exclude_entity_ids=existing_ids,
                    vector_hits=pre_vector_hits,  # preferred: full metadata always present
                )
                neighbor_chunks = expand_result.neighbor_chunks
            except Exception as _ne:
                logger.debug("orchestrator.neighbor_expand_failed", error=str(_ne))

            # Phase 2: Module doc unification (merge module_doc vector hits with graph nodes)
            module_context = await self._unify_module_docs(retrieval)

            # Assemble token-budgeted context with Phase 2 extras
            max_ctx = getattr(self._active_ws, 'max_context_tokens', 8000) if self._active_ws else 8000
            assembler = ContextAssembler(max_tokens=max_ctx)
            ctx = assembler.assemble_with_extras(
                retrieval,
                neighbor_chunks=neighbor_chunks,
                module_context=module_context,
            )

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
                data={
                    "traversals": traversal_results,
                    "count": len(traversal_results),
                    "neighbor_chunks": [
                        {
                            "entity_type": nc.entity_type,
                            "entity_id": nc.entity_id,
                            "content": nc.content,
                            "chunk_type": nc.chunk_type,
                            "chunk_index": nc.chunk_index,
                            "parent_doc_id": nc.parent_doc_id,
                            "section": nc.section,
                        }
                        for nc in neighbor_chunks
                    ],
                    "module_context": module_context,
                },
            )
        except Exception as e:
            return DeepResult(
                pipeline=PipelineName.GRAPH_RAG,
                latency_ms=(time.monotonic() - t0) * 1000,
                error=str(e),
            )

    async def _prefetch_wave2_legs3_and_4(
        self, query: str, repo_id: Optional[str]
    ) -> List[Dict]:
        """Phase 4e: Speculative prefetch — run vector + lexical search immediately.

        Starts before Wave 1 finishes. Returns raw vector hits so _deep_graphrag
        can pass them as pre_vector_hits to HybridRetriever (skips re-embedding).

        Only Legs 3 (vector similarity) and 4 (lexical GIN) are run here because:
          - Leg 1 needs entity_id from Wave 1 (exact lookup)
          - Leg 2 needs intent from Wave 1 (graph neighborhood seeding)
          - Legs 3+4 only need the raw query text → can start immediately

        Savings: ~80–100ms per request (the vector embedding call overlaps
        entirely with the ~100ms Wave 1 probe execution).
        """
        try:
            if self.vectorstore is None:
                return []
            hits = await self.vectorstore.search(
                query=query,
                top_k=10,
                repo_id=repo_id,
            )
            return hits or []
        except Exception as exc:
            logger.debug("orchestrator.prefetch_failed", error=str(exc))
            return []

    async def _unify_module_docs(self, retrieval) -> Dict[str, Any]:
        """Phase 2: Merge module_doc vector hits with graph module node context.

        For each ranked node with entity_type='module_doc', looks up the
        corresponding graph module node and merges section text + graph edges
        into a unified module_context dict keyed by module name.
        """
        module_context: Dict[str, Any] = {}
        try:
            for rn in retrieval.ranked_nodes:
                props = rn.node.properties or {}
                # module_doc hits come from Pillar 5 vector ingestion
                entity_type = props.get("_entity_type") or props.get("entity_type", "")
                if entity_type != "module_doc":
                    continue

                meta = props.get("metadata") or props
                module_name = meta.get("module") or props.get("module", "")
                if not module_name or module_name in module_context:
                    continue

                section_content = props.get("_content") or props.get("content", "")
                section = meta.get("section", "")

                # Look up the graph module node
                graph_edges: List[str] = []
                try:
                    if self.graphrag:
                        module_node_id = f"module:{module_name}"
                        edges_result = await self.graphrag.pg_get_edges(
                            node_id=module_node_id, limit=10
                        )
                        for e in (edges_result or []):
                            edge_str = f"{e.get('edge_type', '?')} → {e.get('target_id', '?')}"
                            graph_edges.append(edge_str)
                except Exception:
                    pass

                module_context[module_name] = {
                    "sections": [f"[{section}] {section_content}"] if section_content else [],
                    "graph_edges": graph_edges,
                }

                if len(module_context) >= 3:
                    break

        except Exception as exc:
            logger.debug("orchestrator.module_doc_unify_failed", error=str(exc))

        return module_context

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

    async def _merge_context(
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

        # Vector chunks — enrich with source attribution for factuality
        vector_probe = probes.get(PipelineName.VECTOR)
        if vector_probe and vector_probe.found_data:
            chunks = vector_probe.data.get("chunks", [])
            # Add source attribution prefix to each chunk's content
            for chunk in chunks:
                if isinstance(chunk, dict) and "content" in chunk:
                    meta = chunk.get("metadata", {}) or {}
                    source_id = chunk.get("entity_id", "unknown")
                    pillar = meta.get("pillar", "")
                    trust = chunk.get("trust_score", 0.5)
                    chunk["_source_label"] = f"[{pillar}:{source_id} trust={trust:.1f}]"
            # Parent-child chunking: if a child chunk matches, include parent for broader context
            # e.g., if "table:orders:states_constants" matches, also include "table:orders:identity_core"
            parent_ids_to_fetch = set()
            seen_ids = {c.get("entity_id", "") for c in chunks if isinstance(c, dict)}
            for chunk in chunks:
                if isinstance(chunk, dict):
                    meta = chunk.get("metadata", {}) or {}
                    parent_id = meta.get("parent_doc_id", "")
                    if parent_id and parent_id not in seen_ids:
                        parent_ids_to_fetch.add(parent_id)

            if parent_ids_to_fetch and self.vectorstore:
                try:
                    for pid in list(parent_ids_to_fetch)[:3]:  # Max 3 parents
                        # Search by entity_id to find parent chunk
                        parent_results = await self.vectorstore.search_similar(
                            query=pid, limit=1, threshold=0.0,
                        )
                        for pr in parent_results:
                            if pr.get("entity_id") and pr["entity_id"] not in seen_ids:
                                pr["_source_label"] = f"[parent:{pr.get('entity_id','')}]"
                                pr["_is_parent_context"] = True
                                chunks.append(pr)
                                seen_ids.add(pr["entity_id"])
                except Exception:
                    pass  # Parent fetch is best-effort

            ctx["knowledge_chunks"] = chunks

        # Page-role + field traces
        page_probe = probes.get(PipelineName.PAGE_ROLE)
        if page_probe and page_probe.found_data:
            ctx["page_context"] = page_probe.data
            # Surface field traces at top level for easy LLM context injection
            field_traces = (page_probe.data or {}).get("field_traces", [])
            if field_traces:
                ctx["field_traces"] = field_traces

        # Cross-repo (probe level)
        cross_probe = probes.get(PipelineName.CROSS_REPO)
        if cross_probe and cross_probe.found_data:
            ctx["cross_repo_alias"] = cross_probe.data

        # Deep: GraphRAG (includes neighbor_chunks and module_context from Phase 2)
        graph_deep = deeps.get(PipelineName.GRAPH_RAG)
        if graph_deep and graph_deep.found_data:
            ctx["graph_traversal"] = graph_deep.data
            # Surface neighbor_chunks and module_context at top level for easy LLM injection
            if graph_deep.data:
                neighbor_chunks = graph_deep.data.get("neighbor_chunks", [])
                if neighbor_chunks:
                    ctx["neighbor_chunks"] = neighbor_chunks
                module_context = graph_deep.data.get("module_context", {})
                if module_context:
                    ctx["module_context"] = module_context

        # Deep: Cross-repo comparison
        cross_deep = deeps.get(PipelineName.CROSS_REPO_DEEP)
        if cross_deep and cross_deep.found_data:
            ctx["cross_repo_comparison"] = cross_deep.data

        # Deep: Session history
        session_deep = deeps.get(PipelineName.SESSION_HISTORY)
        if session_deep and session_deep.found_data:
            ctx["session_history"] = session_deep.data

        # ── Process Engine: Add business process lifecycle context ──
        # If we resolved an entity with a status, tell Claude where it is in the lifecycle.
        try:
            from app.engine.process_engine import ProcessEngine
            pe = ProcessEngine()
            entity_data = ctx.get("entity", {})
            status = None
            if isinstance(entity_data, dict):
                status = entity_data.get("status") or entity_data.get("current_status")
            if status:
                process_context = pe.get_context_for_llm(str(status))
                if process_context:
                    ctx["process_position"] = process_context
                    position = pe.get_process_position(str(status))
                    if position:
                        ctx["valid_actions"] = position.valid_actions
                        ctx["risk_factors"] = position.risk_factors
        except Exception as e:
            logger.debug("orchestrator.process_engine_failed", error=str(e))

        return ctx

    # ===================================================================
    # Helpers
    # ===================================================================

    @staticmethod
    # =========================================================================
    # WAVE 3: LangGraph stateful reasoning
    # =========================================================================

    async def _stage3_langgraph(
        self,
        query: str,
        merged_context: Dict[str, Any],
        intents: List[Dict],
        ws,
    ) -> Dict[str, Any]:
        """
        Wave 3 — LangGraph multi-step stateful reasoning.

        Takes the merged W1+W2 context and iteratively:
          1. Analyses what is still missing / uncertain
          2. Selects tools/retrievers to fill the gap
          3. Executes them (respects max_iterations from WorkflowSettings)
          4. Returns: refined_entities, tool_plan, reasoning_trace, confidence

        Falls back to a lightweight gap-analysis if langgraph is not installed.
        """
        from app.graph.langgraph_pipeline import build_wave_pipeline, WaveState

        # Build (or retrieve cached) pipeline — compiled once per process
        _llm = getattr(self.react_engine, "llm", None) if self.react_engine else None
        # Get Neo4j service for combined LangGraph+Neo4j retrieval
        _neo4j = None
        try:
            from app.services.neo4j_graph import neo4j_graph_service
            if neo4j_graph_service.available:
                _neo4j = neo4j_graph_service
        except Exception:
            pass

        pipeline = build_wave_pipeline(
            vectorstore=self.vectorstore,
            graphrag=self.graphrag,
            react_engine=self.react_engine,
            llm_client=_llm,
            neo4j_service=_neo4j,
            confidence_threshold=ws.wave1_confidence_threshold,
        )

        # Build initial state from W1+W2 context
        # Extract entities from context and intents
        known_entities = []
        entity_data = merged_context.get("entity", {})
        if entity_data.get("entity_id"):
            known_entities.append({
                "type": entity_data.get("entity_type", "unknown"),
                "value": str(entity_data["entity_id"]),
            })

        # Also extract entity_id from intents
        for intent in intents:
            if isinstance(intent, dict) and intent.get("entity_id"):
                known_entities.append({
                    "type": intent.get("entity", "unknown"),
                    "value": str(intent["entity_id"]),
                })

        # Extract structured KB data from merged_context for W3
        chunks = merged_context.get("knowledge_chunks", [])
        p6_actions = [c for c in chunks if isinstance(c, dict) and
                      (c.get("metadata", {}) or {}).get("pillar") == "pillar_6"]
        p7_workflows = [c for c in chunks if isinstance(c, dict) and
                        (c.get("metadata", {}) or {}).get("pillar") == "pillar_7"]
        field_traces = merged_context.get("field_traces", [])
        page_ctx = merged_context.get("page_context", {})
        graph_traversal = merged_context.get("graph_traversal", {})
        neighbor_chunks = merged_context.get("neighbor_chunks", [])

        # Build graph_hits from W2 graph traversal (was previously empty)
        graph_hits = []
        if isinstance(graph_traversal, dict):
            for node in graph_traversal.get("nodes", [])[:10]:
                if isinstance(node, dict):
                    graph_hits.append({
                        "node_id": node.get("id", ""),
                        "label": node.get("label", ""),
                        "node_type": node.get("node_type", ""),
                        "depth": node.get("depth", 0),
                    })

        initial_state: WaveState = {
            "query": query,
            "raw_query": query,          # enrichment node will overwrite query; raw preserved here
            "enriched_query": "",
            "intent_keywords": [],
            "api_hint": "",
            "module_hint": "",
            "enrichment_latency_ms": 0.0,
            "user_id": "",
            "session_id": getattr(self, "_current_session_id", ""),
            "confidence_threshold": ws.wave1_confidence_threshold,
            # Pre-populate from W1+W2 so Wave 3 knows what we already have
            "vector_hits": [
                {"doc_id": c.get("id", ""), "content": c.get("content", ""), "score": c.get("similarity", 0.5)}
                for c in chunks[:5]
            ],
            "graph_hits": graph_hits,
            "entity_hit": entity_data if entity_data else None,
            "wave1_confidence": 0.5,  # start fresh — W3 re-evaluates
            "wave2_triggered": False,
            "pipeline_backend": "langgraph-wave3",
            "embedding_model": "auto",
            # Structured KB context from P4/P6/P7
            "action_contracts": [
                {"action_id": c.get("entity_id", ""), "content": c.get("content", ""),
                 "domain": (c.get("metadata", {}) or {}).get("domain", "")}
                for c in p6_actions[:5]
            ],
            "workflow_states": [
                {"workflow_id": c.get("entity_id", ""), "content": c.get("content", ""),
                 "domain": (c.get("metadata", {}) or {}).get("domain", "")}
                for c in p7_workflows[:5]
            ],
            "field_traces": field_traces[:10] if isinstance(field_traces, list) else [],
            "page_context": page_ctx if isinstance(page_ctx, dict) else None,
            "neighbor_chunks": [
                {"content": nc.get("content", "")[:200], "entity_type": nc.get("entity_type", "")}
                for nc in (neighbor_chunks[:5] if isinstance(neighbor_chunks, list) else [])
            ],
            "assembled_context_text": "",  # populated below if ContextAssembler available
        }

        # Run pipeline (langgraph or fallback)
        # thread_id = session_id so MemorySaver keyed per conversation turn
        _session_id = getattr(self, "_current_session_id", "") or ""
        _config = {"configurable": {"thread_id": _session_id}} if _session_id else {}
        try:
            if hasattr(pipeline, "ainvoke"):
                final_state = await pipeline.ainvoke(initial_state, config=_config)
            else:
                final_state = await pipeline(initial_state, config=_config)
        except Exception as exc:
            logger.warning("wave3._pipeline_invoke_failed", error=str(exc))
            return {}

        # Extract refined entities from final hits
        refined_entities = list(known_entities)  # start with what we knew
        for hit in final_state.get("final_hits", []):
            tool_name = hit.get("tool_name", "")
            if tool_name and not any(e.get("value") == tool_name for e in refined_entities):
                refined_entities.append({"type": "tool", "value": tool_name})

        # Extract matched action/workflow IDs from W3 state
        matched_actions = [a.get("action_id", "") for a in final_state.get("action_contracts", [])
                           if a.get("action_id")]
        matched_workflows = [w.get("workflow_id", "") for w in final_state.get("workflow_states", [])
                             if w.get("workflow_id")]

        return {
            "refined_entities": refined_entities,
            "tool_plan": [h.get("tool_name") for h in final_state.get("final_hits", []) if h.get("tool_name")],
            "reasoning_trace": final_state.get("wave2_reasoning", ""),
            "confidence": final_state.get("final_confidence", 0.0),
            "wave2_triggered": final_state.get("wave2_triggered", False),
            "total_latency_ms": final_state.get("total_latency_ms", 0.0),
            "additional_chunks": final_state.get("vector_hits", []),
            "matched_actions": matched_actions,
            "matched_workflows": matched_workflows,
            "vector_hits_provided": len(initial_state.get("vector_hits", [])),
            # Enrichment signals — visible in wave_trace for debugging
            "enriched_query": final_state.get("enriched_query", ""),
            "intent_keywords": final_state.get("intent_keywords", []),
            "api_hint": final_state.get("api_hint", ""),
            "module_hint": final_state.get("module_hint", ""),
            "enrichment_latency_ms": final_state.get("enrichment_latency_ms", 0.0),
        }

    # =========================================================================
    # WAVE 4: Neo4j targeted graph traversal
    # =========================================================================

    async def _stage4_neo4j(
        self,
        query: str,
        merged_context: Dict[str, Any],
        ws,
    ) -> Dict[str, Any]:
        """
        Wave 4 — Neo4j targeted graph enrichment.

        Uses entities from merged_context (including W3 refinements) to do
        targeted multi-hop BFS in Neo4j — NOT keyword BFS, but starting from
        specific entity IDs that W3 already pinpointed.

        Falls back silently to empty dict if Neo4j is not available.
        """
        from app.services.neo4j_graph import neo4j_graph_service

        if not neo4j_graph_service.available:
            # Connect lazily — non-blocking, just try once
            await neo4j_graph_service.connect()

        if not neo4j_graph_service.available:
            return {}  # Neo4j not running — silent no-op

        # Collect all known entity IDs from W1+W2+W3 context
        entity_targets: List[Dict[str, str]] = []

        # From W3 refined entities (most accurate)
        w3 = merged_context.get("wave3_reasoning", {})
        for ent in w3.get("refined_entities", []):
            if ent.get("type") and ent.get("value"):
                entity_targets.append(ent)

        # From W1 entity lookup
        entity_data = merged_context.get("entity", {})
        if entity_data.get("entity_id"):
            entity_targets.append({
                "type": entity_data.get("entity_type", "order_id"),
                "value": str(entity_data["entity_id"]),
            })

        # Extract API paths from knowledge chunks (entity_type=api_path)
        for chunk in merged_context.get("knowledge_chunks", [])[:3]:
            meta = chunk.get("metadata", {})
            path = meta.get("endpoint") or meta.get("api_path")
            if path:
                entity_targets.append({"type": "api_path", "value": path})

        if not entity_targets:
            # No entities to traverse — fall back to keyword BFS
            hits = await neo4j_graph_service.bfs_query(
                query_text=query[:100],
                max_depth=ws.wave4_max_depth,
                limit=20,
            )
        else:
            # Targeted traversal: look up each entity then BFS from its node
            all_hits: List[Dict] = []
            lookup_tasks = [
                neo4j_graph_service.entity_lookup(e["type"], e["value"])
                for e in entity_targets[:5]  # cap at 5 starting points
            ]
            lookup_results = await asyncio.gather(*lookup_tasks, return_exceptions=True)

            # BFS from resolved nodes
            resolved_node_ids = []
            for lr in lookup_results:
                if isinstance(lr, dict) and lr.get("node_id"):
                    resolved_node_ids.append(lr["node_id"])

            if resolved_node_ids:
                # Multi-hop from resolved entity nodes
                bfs_tasks = [
                    neo4j_graph_service.bfs_query(
                        query_text=nid,  # search by node_id label
                        max_depth=ws.wave4_max_depth,
                        limit=15,
                    )
                    for nid in resolved_node_ids[:3]
                ]
                bfs_results = await asyncio.gather(*bfs_tasks, return_exceptions=True)
                for br in bfs_results:
                    if isinstance(br, list):
                        all_hits.extend(br)
            else:
                # Entities not in Neo4j yet — keyword BFS as fallback
                all_hits = await neo4j_graph_service.bfs_query(
                    query_text=query[:100],
                    max_depth=ws.wave4_max_depth,
                    limit=20,
                )

            hits = all_hits

        if not hits:
            return {}

        # Format for LLM context: deduplicate and rank by depth
        seen = set()
        unique_hits = []
        for h in sorted(hits, key=lambda x: x.get("depth", 99)):
            key = h.get("node_id", h.get("label", ""))
            if key and key not in seen:
                seen.add(key)
                unique_hits.append(h)

        relationship_context = "\n".join(
            f"  [{h.get('node_type', '?')}] {h.get('label', h.get('node_id', '?'))} "
            f"(depth={h.get('depth', '?')}, repo={h.get('repo_id', '?')})"
            for h in unique_hits[:20]
        )

        return {
            "path_count": len(unique_hits),
            "entity_targets_used": len(entity_targets),
            "paths": unique_hits[:20],
            "relationship_context": relationship_context,
            "source": "neo4j_wave4",
        }

    # Common Hinglish → English mappings for ICRM queries
    _HINGLISH_MAP = {
        # Actions
        "karo": "do", "kar do": "do", "karna hai": "need to", "karna": "do",
        "bhejo": "send", "batao": "tell", "dikhao": "show", "nikalo": "extract",
        "chala do": "run", "rok do": "stop", "badlo": "change", "hatao": "remove",
        # Questions
        "kya hai": "what is", "kahan hai": "where is", "kab": "when",
        "kyun": "why", "kyu": "why", "kaise": "how", "kitna": "how much",
        "kaun": "who", "konsa": "which",
        # Entities
        "mera": "my", "mere": "my", "is": "this", "yeh": "this", "woh": "that",
        "paisa": "money", "payment": "payment",
        # Status
        "nahi hua": "not done", "nahi ho raha": "not working", "ho gaya": "completed",
        "stuck hai": "is stuck", "pending hai": "is pending",
        # Domain
        "courier wala": "courier agent", "delivery boy": "delivery agent",
        "pickup": "pickup", "shipment": "shipment",
        # COD
        "COD ka paisa": "COD remittance", "COD remit": "COD remittance",
    }

    async def _enrich_query_with_claude(
        self, query: str, classification: Dict, role: Optional[str], company_id: str,
    ) -> Optional[Dict]:
        """Claude-powered Query Intelligence Layer.

        Before waves execute, Claude analyzes the raw query and generates
        an optimized retrieval plan. This transforms vague user queries into
        precise search instructions for each wave.

        Example:
          Input:  "I placed my order on 15 March but still not picked up"
          Output: {
            "search_query": "order pickup status not scheduled delayed",
            "entities": [{"type": "date", "value": "2026-03-15"}],
            "intent": "diagnose",
            "target_pillars": ["pillar_7", "pillar_6", "pillar_1"],
            "expected_docs": ["workflow.pickup", "action.schedule_pickup", "table:orders"],
            "complexity": "medium",
            "follow_up_needed": false
          }

        Cost: ~$0.001 per query (150 input + 100 output tokens via Claude Haiku)
        Latency: ~200ms (runs before Wave 1, but saves time by making retrieval precise)
        """
        try:
            import httpx
            import json

            api_key = os.environ.get("AIGATEWAY_API_KEY", "")
            api_url = os.environ.get("AIGATEWAY_URL", "https://aigateway.shiprocket.in")
            if not api_key:
                return None

            domain = classification.get("domain", "unknown")
            mode = classification.get("mode", "lookup")

            prompt = f"""You are a query optimizer for Shiprocket's ICRM copilot. Analyze this user query and produce a JSON retrieval plan.

User query: "{query}"
Detected domain: {domain}
User role: {role or "operator"}
Company: {company_id or "unknown"}

Your job: Convert this query into optimized search instructions.

Return ONLY valid JSON with these fields:
{{
  "search_query": "optimized search text for vector retrieval (remove filler words, add synonyms)",
  "entities": [{{"type": "order_id|awb|company_id|phone|date", "value": "extracted value"}}],
  "intent": "lookup|diagnose|act|explain",
  "target_pillars": ["pillar_1|pillar_3|pillar_4|pillar_6|pillar_7|entity_hub"],
  "expected_docs": ["table:orders", "action.orders.create_order", "workflow.shipments.pickup"],
  "complexity": "simple|medium|complex",
  "follow_up_needed": false
}}

Rules:
- search_query should be 5-15 words, focused, no filler
- entities: extract ALL specific IDs (order numbers, AWB, dates, phone numbers)
- target_pillars: which KB pillars are most likely to have the answer
- expected_docs: specific doc IDs that would answer this query
- If the query is ambiguous, set follow_up_needed: true"""

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{api_url}/api/v1/chat/completion",
                    headers={"x-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "provider": "anthropic",
                        "project_key": "cosmos",
                        "user_prompt": prompt,
                        "max_tokens": 200,
                        "temperature": 0.0,
                    },
                )

                if resp.status_code != 200:
                    return None

                data = resp.json()
                output = data.get("data", {}).get("output", {}).get("summary", "")
                if not output:
                    # Try alternate response format
                    choices = data.get("choices", [])
                    if choices:
                        output = choices[0].get("message", {}).get("content", "")

                if not output:
                    return None

                # Parse JSON from response
                # Handle cases where Claude wraps in ```json
                output = output.strip()
                if output.startswith("```"):
                    output = output.split("\n", 1)[-1].rsplit("```", 1)[0]

                return json.loads(output)

        except Exception as e:
            logger.debug("orchestrator.query_intel_failed", error=str(e))
            return None

    @staticmethod
    def _decompose_query(query: str) -> List[str]:
        """Decompose multi-part queries into sub-queries for separate retrieval.

        "Show me order status AND billing details" → ["order status", "billing details"]
        "cancel karo aur refund bhi" → ["cancel karo", "refund bhi"]

        Single-part queries return [original_query].
        """
        q = query.lower()

        # Split on conjunctions
        conjunctions = [" and ", " aur ", " also ", " bhi ", " plus ", " along with ", " as well as "]
        parts = [query]
        for conj in conjunctions:
            new_parts = []
            for part in parts:
                if conj in part.lower():
                    splits = part.lower().split(conj)
                    # Reconstruct with original case approximately
                    idx = 0
                    for s in splits:
                        s = s.strip()
                        if s and len(s) > 3:  # Skip tiny fragments
                            new_parts.append(s)
                else:
                    new_parts.append(part)
            parts = new_parts

        # Clean and filter
        result = [p.strip() for p in parts if len(p.strip()) > 5]

        # Max 3 sub-queries to prevent explosion
        return result[:3] if result else [query]

    @classmethod
    def _normalize_hinglish(cls, query: str) -> str:
        """Normalize Hinglish queries to English for better intent matching.

        This is a lightweight keyword replacement (no LLM call).
        Full Claude-based normalization should happen at LIME/MARS layer
        before reaching COSMOS using a system prompt like:
          'Convert the following Hinglish text to clean English,
           preserving all entity IDs (order numbers, AWB, etc.)'
        """
        normalized = query
        # Sort by length descending to replace longer phrases first
        for hindi, english in sorted(cls._HINGLISH_MAP.items(), key=lambda x: -len(x[0])):
            if hindi in normalized.lower():
                # Case-insensitive replacement preserving original case
                import re
                normalized = re.sub(re.escape(hindi), english, normalized, flags=re.IGNORECASE)
        return normalized

    @staticmethod
    def _detect_query_mode(query: str) -> str:
        """Detect query mode from natural language for retrieval routing.

        Modes:
          lookup  — "what is X?", "find X", "show me X"
          diagnose — "why did X?", "what went wrong?", "stuck", "failed"
          act     — "cancel X", "update X", "trigger X", "do X"
          explain — "how does X work?", "what happens if?"
        """
        q = query.lower()

        # Act: imperative action queries
        act_signals = ["cancel", "update", "trigger", "process", "schedule", "assign",
                       "reschedule", "refund", "send", "mark", "approve", "reject",
                       "create", "delete", "karo", "chala do", "kar do", "bhejo"]
        if any(s in q for s in act_signals):
            return "act"

        # Diagnose: causal/investigation queries
        diag_signals = ["why", "stuck", "failed", "error", "wrong", "not working",
                        "issue", "problem", "kyun", "kyu", "nahi ho raha",
                        "what happened", "what went wrong", "diagnose", "investigate",
                        "reason", "root cause", "status mismatch"]
        if any(s in q for s in diag_signals):
            return "diagnose"

        # Explain: how/what-if queries
        explain_signals = ["how does", "how do", "what happens if", "what will happen",
                           "explain", "kaise", "kya hoga", "flow", "lifecycle",
                           "process of", "steps for"]
        if any(s in q for s in explain_signals):
            return "explain"

        # Default: lookup
        return "lookup"

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

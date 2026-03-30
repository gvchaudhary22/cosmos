"""
Hybrid Chat API — Two-stage parallel probe + conditional deepening.

POST /cosmos/api/v1/hybrid/chat          — Full hybrid response
POST /cosmos/api/v1/hybrid/chat/stream   — SSE streaming with pipeline events
GET  /cosmos/api/v1/hybrid/analytics     — Pipeline attribution analytics
"""

import asyncio
import json
import time
from uuid import UUID, uuid4
from typing import Optional, Dict, Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.query_orchestrator import QueryOrchestrator, OrchestratorResult

logger = structlog.get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class HybridChatRequest(BaseModel):
    session_id: Optional[UUID] = None
    message: str = Field(..., min_length=1, max_length=10000)
    user_id: str
    repo_id: Optional[str] = None
    role: Optional[str] = None
    company_id: Optional[str] = None
    channel: str = "web"
    debug: bool = False  # Include pipeline_breakdown in response
    metadata: dict = Field(default_factory=dict)


class PipelineBreakdown(BaseModel):
    pipeline: str
    contributed: bool
    latency_ms: float
    items: int
    stages: list


class HybridChatResponse(BaseModel):
    session_id: UUID
    message_id: UUID
    content: str
    intents: list = Field(default_factory=list)
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_prompt: Optional[str] = None
    tools_used: list = Field(default_factory=list)
    # Attribution (only when debug=True)
    pipeline_breakdown: Optional[Dict[str, Any]] = None
    timing: Optional[Dict[str, float]] = None
    signal: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_orchestrator(request: Request) -> Optional[QueryOrchestrator]:
    return getattr(request.app.state, "query_orchestrator", None)


def _get_react_engine(request: Request):
    return getattr(request.app.state, "react_engine", None)


def _build_llm_context(orch_result: OrchestratorResult) -> str:
    """Format orchestrator context into a text prompt for the LLM."""
    parts = []

    # Intents
    if orch_result.intents:
        intents_str = ", ".join(
            f"{i.get('intent', '?')}({i.get('entity', '?')}, conf={i.get('confidence', 0):.2f})"
            for i in orch_result.intents
        )
        parts.append(f"[Intents] {intents_str}")

    ctx = orch_result.context

    # Entity
    entity = ctx.get("entity", {})
    if entity:
        eid = entity.get("entity_id") or "unknown"
        uid = entity.get("user_id") or "unknown"
        parts.append(f"[Entity] entity_id={eid}, user_id={uid}")

    # Knowledge chunks from vector search
    chunks = ctx.get("knowledge_chunks", [])
    if chunks:
        parts.append(f"[Knowledge Base — {len(chunks)} relevant chunks]")
        for i, chunk in enumerate(chunks[:5]):
            content = chunk.get("content", "")[:300]
            sim = chunk.get("similarity", 0)
            parts.append(f"  Chunk {i+1} (sim={sim:.2f}): {content}")

    # Graph traversal paths
    graph = ctx.get("graph_traversal", {})
    traversals = graph.get("traversals", [])
    if traversals:
        parts.append(f"[Graph Traversal — {len(traversals)} paths]")
        for t in traversals:
            if "formatted_context" in t:
                parts.append(f"  {t['formatted_context'][:500]}")
            else:
                nodes = t.get("nodes", [])
                edges = t.get("edges", [])
                path_str = " → ".join(
                    f"{n.get('label', n.get('id', '?'))}({n.get('type', '?')})"
                    for n in nodes[:6]
                )
                parts.append(f"  From {t.get('start_node', '?')}: {path_str}")
                if len(nodes) > 6:
                    parts.append(f"    ... +{len(nodes)-6} more nodes, {len(edges)} edges")

    # Page context
    page_ctx = ctx.get("page_context", {})
    pages = page_ctx.get("pages", [])
    if pages:
        parts.append(f"[Page Context — {len(pages)} matching pages]")
        for p in pages[:3]:
            parts.append(
                f"  {p.get('page_id', '?')} | route={p.get('route', '?')} | "
                f"domain={p.get('domain', '?')} | roles={p.get('roles_required', [])}"
            )
        role_access = page_ctx.get("role_access")
        if role_access is not None:
            parts.append(f"  Role access: {'granted' if role_access else 'DENIED'}")

    # Cross-repo
    cross = ctx.get("cross_repo_comparison", {})
    if cross and cross.get("shared_fields"):
        parts.append(f"[Cross-Repo Comparison]")
        parts.append(f"  Source: {cross.get('source', {}).get('page_id', '?')} ({cross.get('source', {}).get('field_count', 0)} fields)")
        parts.append(f"  Target: {cross.get('target', {}).get('page_id', '?')} ({cross.get('target', {}).get('field_count', 0)} fields)")
        parts.append(f"  Shared fields: {', '.join(cross.get('shared_fields', [])[:10])}")
        target_only = cross.get("target_only_fields", [])
        if target_only:
            parts.append(f"  Target-only fields (admin has, seller doesn't): {', '.join(target_only[:10])}")
    elif ctx.get("cross_repo_alias", {}).get("mapping"):
        mapping = ctx["cross_repo_alias"]["mapping"]
        parts.append(f"[Cross-Repo Alias] {mapping}")

    # Session history
    session = ctx.get("session_history", {})
    recent = session.get("recent_entities", [])
    if recent:
        parts.append(f"[Session History — {len(recent)} recent entities]")
        for e in recent[:3]:
            parts.append(f"  {e.get('label', e.get('id', '?'))}")

    # Phase 2: Field traces (deterministic field → API → DB column)
    field_traces = ctx.get("field_traces", [])
    if field_traces:
        parts.append(f"[Field Traces — {len(field_traces)} traces]")
        for ft in field_traces[:5]:
            if isinstance(ft, dict):
                field_name = ft.get("field_name", ft.get("field", "?"))
                api = ft.get("api_endpoint", ft.get("api", "?"))
                col = ft.get("db_column", ft.get("column", ""))
                line = f"  {field_name} → {api}"
                if col:
                    line += f" → {col}"
                parts.append(line)

    # Phase 2: Neighbor chunks (sibling evidence from same parent document)
    neighbor_chunks = ctx.get("neighbor_chunks", [])
    if neighbor_chunks:
        parts.append(f"[Neighbor Chunks — {len(neighbor_chunks)} sibling chunks]")
        for nc in neighbor_chunks[:3]:
            chunk_type = nc.get("chunk_type", "")
            content = nc.get("content", "")[:200]
            section = nc.get("section", "")
            label = f"{chunk_type}/{section}" if section else chunk_type
            parts.append(f"  [{label}] {content}")

    # Phase 2: Module context (module_doc vector + graph node unified)
    module_context = ctx.get("module_context", {})
    if module_context:
        parts.append(f"[Module Context — {len(module_context)} module(s)]")
        for mod_name, mod_data in list(module_context.items())[:3]:
            if isinstance(mod_data, dict):
                parts.append(f"  [{mod_name}]")
                for sec in mod_data.get("sections", [])[:2]:
                    parts.append(f"    {str(sec)[:200]}")
                edges = mod_data.get("graph_edges", [])
                if edges:
                    parts.append(f"    edges: {', '.join(edges[:4])}")

    # Wave 3: LangGraph reasoning trace + refined entities
    w3 = ctx.get("wave3_reasoning", {})
    if w3:
        refined = w3.get("refined_entities", [])
        tool_plan = w3.get("tool_plan", [])
        trace = w3.get("reasoning_trace", "")
        if refined:
            parts.append(f"[Wave 3 — LangGraph Refined Entities ({len(refined)})]")
            for ent in refined[:6]:
                parts.append(f"  {ent.get('type', '?')}={ent.get('value', '?')}")
        if tool_plan:
            parts.append(f"[Wave 3 — Suggested Tools: {', '.join(str(t) for t in tool_plan[:5])}]")
        if trace:
            parts.append(f"[Wave 3 — Reasoning] {trace[:300]}")

    # Wave 4: Neo4j targeted graph paths
    w4 = ctx.get("wave4_graph", {})
    if w4 and w4.get("path_count", 0) > 0:
        rel_ctx = w4.get("relationship_context", "")
        parts.append(
            f"[Wave 4 — Neo4j Graph ({w4['path_count']} nodes, "
            f"{w4.get('entity_targets_used', 0)} entity targets)]"
        )
        if rel_ctx:
            parts.append(rel_ctx[:500])

    return "\n".join(parts) if parts else "[No context gathered from pipelines]"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=HybridChatResponse)
async def hybrid_chat(request: Request, chat_req: HybridChatRequest):
    """
    Hybrid chat: parallel probe all 5 pipelines, conditionally deepen,
    then assemble answer via LLM. Returns attribution when debug=True.
    """
    orchestrator = _get_orchestrator(request)
    session_id = chat_req.session_id or uuid4()

    if orchestrator is None:
        return HybridChatResponse(
            session_id=session_id,
            message_id=uuid4(),
            content="[COSMOS] Hybrid orchestrator not initialized.",
        )

    # Read effective workflow settings from cache
    _settings_cache = getattr(request.app.state, "settings_cache", None)
    _workflow_settings = _settings_cache.get() if _settings_cache else None

    # ---------------------------------------------------------------
    # PRE-EXECUTION GUARDRAILS (15 guards: injection, access, rate, cost)
    # ---------------------------------------------------------------
    react_engine = _get_react_engine(request)
    if react_engine and react_engine.guardrails:
        try:
            pre_context = {
                "query": chat_req.message,
                "user_id": chat_req.user_id,
                "company_id": chat_req.company_id,
                "role": chat_req.role,
                "channel": chat_req.channel,
                "session_id": str(session_id),
            }
            pre_result = await react_engine.guardrails.run_pre(pre_context)
            if pre_result.action.value == "block":
                return HybridChatResponse(
                    session_id=session_id,
                    message_id=uuid4(),
                    content=pre_result.reason or "Your request was blocked by safety checks.",
                    confidence=0.0,
                )
        except Exception as guard_err:
            logger.warning("hybrid_chat.pre_guardrail_error", error=str(guard_err))

    # Run two-stage pipeline
    orch_result = await orchestrator.execute(
        query=chat_req.message,
        user_id=chat_req.user_id,
        repo_id=chat_req.repo_id,
        role=chat_req.role,
        session_context=chat_req.metadata,
        session_id=str(session_id),
        workflow_settings=_workflow_settings,
    )

    # If we need clarification, return early
    if orch_result.needs_clarification:
        resp = HybridChatResponse(
            session_id=session_id,
            message_id=uuid4(),
            content=orch_result.clarification_prompt or "Could you provide more details?",
            intents=[i for i in orch_result.intents],
            needs_clarification=True,
            clarification_prompt=orch_result.clarification_prompt,
        )
        if chat_req.debug:
            attribution = orchestrator.to_attribution_summary(orch_result)
            resp.pipeline_breakdown = attribution["pipeline_breakdown"]
            resp.timing = attribution["timing"]
            resp.signal = attribution["signal"]
        return resp

    # ---------------------------------------------------------------
    # Stage 3: LLM Assembly via RIPER (MARS pattern #3)
    # COMPLEX → full RIPER, STANDARD → RIPER Lite, QUICK → direct
    # ---------------------------------------------------------------
    llm_context = _build_llm_context(orch_result)
    classification = orch_result.request_classification or {}
    complexity = classification.get("complexity", "standard")

    engine = _get_react_engine(request)
    content = ""
    confidence = 0.0
    tools_used = []
    llm_latency = 0.0

    # Check if fast-path already produced a result (Tier 0)
    fast_path_response = (orch_result.context or {}).get("fast_path_response")
    if fast_path_response:
        content = fast_path_response
        confidence = (orch_result.response_metadata or {}).get("confidence", 0.9)
        tools_used = (orch_result.response_metadata or {}).get("tools_used", [])
        llm_latency = (orch_result.response_metadata or {}).get("latency_ms", 0)

    elif orchestrator.riper_engine:
        # RIPER for ALL non-cached queries
        # COMPLEX → Full RIPER (R+I+P+E+R), STANDARD/QUICK → RIPER Lite (R+P+E+R)
        t0 = time.monotonic()
        try:
            if complexity == "complex":
                riper_result = await asyncio.wait_for(
                    orchestrator.riper_engine.process_full(
                        query=chat_req.message,
                        context=orch_result.context,
                        intents=orch_result.intents,
                    ),
                    timeout=30.0,  # 30s max for complex queries
                )
            else:
                riper_result = await asyncio.wait_for(
                    orchestrator.riper_engine.process_lite(
                        query=chat_req.message,
                        context=orch_result.context,
                        intents=orch_result.intents,
                    ),
                    timeout=15.0,  # 15s max for standard/quick
                )

            llm_latency = (time.monotonic() - t0) * 1000
            content = riper_result.final_response
            confidence = riper_result.confidence
            tools_used = riper_result.tools_used
            orch_result.riper_summary = orchestrator.riper_engine.to_summary(riper_result)

        except (asyncio.TimeoutError, Exception) as riper_err:
            # Circuit breaker: RIPER/LLM failed → fall back to tool-only response
            llm_latency = (time.monotonic() - t0) * 1000
            logger.warning("hybrid_chat.riper_failed",
                           error=str(riper_err), latency_ms=round(llm_latency, 1))

            # Build tool-only response from orchestrator context
            ctx = orch_result.context or {}
            tool_data = ctx.get("multi_agent_response") or ctx.get("fast_path_response")
            if tool_data:
                content = tool_data
                confidence = 0.6
            else:
                # Last resort: use KB chunks directly
                chunks = ctx.get("knowledge_chunks", [])
                if chunks:
                    content = "Based on available information: " + " ".join(
                        c.get("content", "")[:200] for c in chunks[:3]
                    )
                    confidence = 0.4
                else:
                    content = "I'm having trouble processing this request. Please try again or rephrase your question."
                    confidence = 0.2

    elif engine is not None:
        # Fallback: direct ReAct (only if RIPER unavailable)
        augmented_context = {
            "user_id": chat_req.user_id,
            "company_id": chat_req.company_id,
            "channel": chat_req.channel,
            "pipeline_context": llm_context,
        }
        augmented_context.update(chat_req.metadata)
        t0 = time.monotonic()
        result = await engine.process(chat_req.message, augmented_context)
        llm_latency = (time.monotonic() - t0) * 1000
        content = result.response
        confidence = result.confidence
        tools_used = result.tools_used
    else:
        content = f"Based on pipeline analysis:\n\n{llm_context}"
        confidence = 0.5

    # ---------------------------------------------------------------
    # Agent Forge (MARS pattern #5) — if confidence < 60%, forge
    # ---------------------------------------------------------------
    if confidence < 0.6 and orchestrator.agent_forge:
        forge_result = await orchestrator.agent_forge.forge_and_execute(
            query=chat_req.message,
            intent=orch_result.intents[0].get("intent", "unknown") if orch_result.intents else "unknown",
            entity=orch_result.intents[0].get("entity", "unknown") if orch_result.intents else "unknown",
            context=orch_result.context,
        )
        if forge_result.response and forge_result.confidence > confidence:
            content = forge_result.response
            confidence = forge_result.confidence
            orch_result.forge_summary = {
                "forged": forge_result.forged,
                "reused": forge_result.reused,
                "agent_name": forge_result.agent.name if forge_result.agent else None,
                "agent_capability": forge_result.agent.capability.value if forge_result.agent else None,
                "latency_ms": round(forge_result.latency_ms, 1),
            }

    # ---------------------------------------------------------------
    # Stage 4: RALPH Self-Correction (MARS pattern #4)
    # ---------------------------------------------------------------
    if orchestrator.ralph_engine and content:
        ralph_result = await orchestrator.ralph_engine.evaluate(
            query=chat_req.message,
            response=content,
            confidence=confidence,
            intents=orch_result.intents,
            context=orch_result.context,
            tools_used=tools_used,
        )
        orch_result.ralph_summary = orchestrator.ralph_engine.to_summary(ralph_result)

        if ralph_result.improved_response:
            content = ralph_result.improved_response

        if ralph_result.verdict.value == "escalate":
            confidence = min(confidence, 0.3)

    # ---------------------------------------------------------------
    # PATTERN LEARNING: Record every successful resolution
    # Builds pattern confidence over time (30+ successes → fast path)
    # ---------------------------------------------------------------
    if content and confidence >= 0.5 and tools_used:
        try:
            intent_for_cache = "unknown"
            entity_for_cache = "unknown"
            if orch_result.intents:
                i0 = orch_result.intents[0]
                intent_for_cache = i0.get("intent", "unknown") if isinstance(i0, dict) else str(i0)
                entity_for_cache = i0.get("entity", "unknown") if isinstance(i0, dict) else "unknown"

            tool_seq = [{"tool_name": t} for t in tools_used]
            await orchestrator.pattern_cache.record_success(
                query=chat_req.message,
                intent=intent_for_cache,
                entity_type=entity_for_cache,
                tool_sequence=tool_seq,
                repo_id=chat_req.repo_id if hasattr(chat_req, 'repo_id') else "",
                role=chat_req.role if hasattr(chat_req, 'role') else "",
                latency_ms=llm_latency,
            )
        except Exception:
            pass  # non-critical

    # ---------------------------------------------------------------
    # POST-EXECUTION GUARDRAILS (10 guards: PII mask, leakage, hallucination, legal)
    # ---------------------------------------------------------------
    if react_engine and react_engine.guardrails and content:
        try:
            post_context = {
                "query": chat_req.message,
                "response": content,
                "user_id": chat_req.user_id,
                "company_id": chat_req.company_id,
                "role": chat_req.role,
                "tools_used": tools_used,
                "confidence": confidence,
                "intents": orch_result.intents,
                "context": orch_result.context,
            }
            post_result = await react_engine.guardrails.run_post(post_context)
            if post_result.action.value == "block":
                content = post_result.reason or "Response blocked by safety checks. Please contact support."
                confidence = 0.0
            elif post_result.action.value == "mask" and post_result.modified_data:
                content = post_result.modified_data  # PII-masked version
        except Exception as guard_err:
            logger.warning("hybrid_chat.post_guardrail_error", error=str(guard_err))

    # ---------------------------------------------------------------
    # Build response
    # ---------------------------------------------------------------
    resp = HybridChatResponse(
        session_id=session_id,
        message_id=uuid4(),
        content=content,
        intents=orch_result.intents,
        confidence=confidence,
        tools_used=tools_used,
    )

    if chat_req.debug:
        attribution = orchestrator.to_attribution_summary(orch_result)
        resp.pipeline_breakdown = attribution["pipeline_breakdown"]
        resp.timing = {
            **attribution["timing"],
            "llm_ms": round(llm_latency, 1),
        }
        resp.signal = attribution["signal"]
        # Include MARS integration summaries
        if orch_result.request_classification:
            resp.pipeline_breakdown["_request_classification"] = orch_result.request_classification
        if orch_result.riper_summary:
            resp.pipeline_breakdown["_riper"] = orch_result.riper_summary
        if orch_result.ralph_summary:
            resp.pipeline_breakdown["_ralph"] = orch_result.ralph_summary
        if orch_result.forge_summary:
            resp.pipeline_breakdown["_agent_forge"] = orch_result.forge_summary
        if orch_result.wave3_context:
            resp.pipeline_breakdown["_wave3_langgraph"] = orch_result.wave3_context
        if orch_result.wave4_context:
            resp.pipeline_breakdown["_wave4_neo4j"] = orch_result.wave4_context

    return resp


@router.post("/chat/stream")
async def hybrid_chat_stream(request: Request, chat_req: HybridChatRequest):
    """
    SSE streaming hybrid chat. Emits per-pipeline events so the frontend
    can show real-time progress through each stage.

    Events:
    - stage:probe_start     — Stage 1 starting
    - probe:{pipeline}      — Individual probe result
    - stage:probe_complete  — All probes done
    - stage:deep_start      — Stage 2 starting
    - deep:{pipeline}       — Individual deep result (or skipped)
    - stage:deep_complete   — Deep stage done
    - stage:llm_start       — LLM assembly starting
    - chunk                 — LLM response text chunks
    - attribution           — Full pipeline breakdown
    - done                  — Final metadata
    - error                 — Error information
    """
    orchestrator = _get_orchestrator(request)
    session_id = str(chat_req.session_id or uuid4())

    async def generate():
        try:
            if orchestrator is None:
                yield _sse("error", {"message": "Hybrid orchestrator not initialized"})
                return

            # --- Stage 1: Parallel Probe ---
            yield _sse("stage:probe_start", {"pipelines": [
                "intent_classifier", "entity_lookup", "vector_search",
                "page_role", "cross_repo",
            ]})

            probe_start = time.monotonic()
            probe_results = await orchestrator._stage1_parallel_probe(
                query=chat_req.message,
                user_id=chat_req.user_id,
                repo_id=chat_req.repo_id,
                role=chat_req.role,
                session_context=chat_req.metadata,
            )
            probe_ms = (time.monotonic() - probe_start) * 1000

            # Emit individual probe results
            for pipeline, pr in probe_results.items():
                yield _sse(f"probe:{pipeline.value}", {
                    "found_data": pr.found_data,
                    "latency_ms": round(pr.latency_ms, 1),
                    "recommend_deepen": pr.recommend_deepen,
                    "reason": pr.reason,
                    "error": pr.error,
                })

            yield _sse("stage:probe_complete", {
                "latency_ms": round(probe_ms, 1),
                "pipelines_with_data": [
                    p.value for p, r in probe_results.items() if r.found_data
                ],
            })

            # --- Stage 2: Conditional Deepening ---
            deep_decisions = orchestrator._route_deep(probe_results, chat_req.message)

            fired = [p.value for p, d in deep_decisions.items() if d["fire"]]
            skipped = [p.value for p, d in deep_decisions.items() if not d["fire"]]

            yield _sse("stage:deep_start", {
                "firing": fired,
                "skipping": skipped,
                "decisions": {p.value: d for p, d in deep_decisions.items()},
            })

            deep_start = time.monotonic()
            deep_results = await orchestrator._stage2_conditional_deep(
                deep_decisions, probe_results, chat_req.message, chat_req.repo_id,
            )
            deep_ms = (time.monotonic() - deep_start) * 1000

            for pipeline, dr in deep_results.items():
                yield _sse(f"deep:{pipeline.value}", {
                    "found_data": dr.found_data,
                    "latency_ms": round(dr.latency_ms, 1),
                    "error": dr.error,
                })

            yield _sse("stage:deep_complete", {"latency_ms": round(deep_ms, 1)})

            # --- Build full orchestrator result for attribution ---
            orch_result = OrchestratorResult()
            orch_result.probe_latency_ms = probe_ms
            orch_result.deep_latency_ms = deep_ms
            orch_result.context = orchestrator._merge_context(probe_results, deep_results)

            # Extract intents
            from app.services.query_orchestrator import PipelineName
            intent_probe = probe_results.get(PipelineName.INTENT)
            if intent_probe and intent_probe.data:
                orch_result.intents = intent_probe.data if isinstance(intent_probe.data, list) else [intent_probe.data]

            # --- Stage 3: LLM Assembly ---
            yield _sse("stage:llm_start", {})

            llm_context = _build_llm_context(orch_result)
            engine = _get_react_engine(request)

            if engine is not None:
                augmented_context = {
                    "user_id": chat_req.user_id,
                    "company_id": chat_req.company_id,
                    "channel": chat_req.channel,
                    "pipeline_context": llm_context,
                }
                augmented_context.update(chat_req.metadata)

                t0 = time.monotonic()
                result = await engine.process(chat_req.message, augmented_context)
                llm_ms = (time.monotonic() - t0) * 1000

                # Stream response in chunks
                response_text = result.response
                chunk_size = 60
                for i in range(0, len(response_text), chunk_size):
                    yield _sse("chunk", {"text": response_text[i:i + chunk_size]})

                confidence = result.confidence
                tools_used = result.tools_used
            else:
                content = f"Based on pipeline analysis:\n\n{llm_context}"
                chunk_size = 60
                for i in range(0, len(content), chunk_size):
                    yield _sse("chunk", {"text": content[i:i + chunk_size]})
                confidence = 0.5
                tools_used = []
                llm_ms = 0.0

            # Attribution
            # Build attribution manually for streaming
            attribution = {
                "pipeline_breakdown": {},
                "timing": {
                    "probe_ms": round(probe_ms, 1),
                    "deep_ms": round(deep_ms, 1),
                    "llm_ms": round(llm_ms, 1),
                    "total_ms": round(probe_ms + deep_ms + llm_ms, 1),
                },
            }
            for p, pr in probe_results.items():
                attribution["pipeline_breakdown"][p.value] = {
                    "stage": "probe",
                    "contributed": pr.found_data,
                    "latency_ms": round(pr.latency_ms, 1),
                }
            for p, dr in deep_results.items():
                attribution["pipeline_breakdown"][p.value] = {
                    "stage": "deep",
                    "contributed": dr.found_data,
                    "latency_ms": round(dr.latency_ms, 1),
                }
            for p, d in deep_decisions.items():
                if not d["fire"] and p.value not in attribution["pipeline_breakdown"]:
                    attribution["pipeline_breakdown"][p.value] = {
                        "stage": "deep",
                        "contributed": False,
                        "skipped": True,
                        "reason": d["reason"],
                    }

            yield _sse("attribution", attribution)

            yield _sse("done", {
                "session_id": session_id,
                "confidence": confidence,
                "tools_used": tools_used,
                "intents": orch_result.intents,
            })

        except Exception as exc:
            logger.error("hybrid_chat_stream.error", error=str(exc))
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/analytics")
async def pipeline_analytics(request: Request):
    """
    Returns aggregated pipeline attribution analytics.
    Use this to identify which pipelines are core vs. burden.
    """
    # For now, return the current orchestrator configuration.
    # In production, this would query a query_analytics table.
    orchestrator = _get_orchestrator(request)
    if orchestrator is None:
        return {"status": "orchestrator not initialized"}

    return {
        "status": "active",
        "architecture": "hybrid_two_stage",
        "stage_1_probes": [
            "intent_classifier",
            "entity_lookup",
            "vector_search",
            "page_role",
            "cross_repo",
        ],
        "stage_2_deep_candidates": [
            "graph_rag_deep",
            "cross_repo_deep",
            "session_history",
        ],
        "routing_rules": {
            "graph_rag_deep": "fires when trace/why intent + vector nodes found (top_relevance > 0.4)",
            "cross_repo_deep": "fires when cross-repo mapping found + sync/system keywords",
            "session_history": "fires when no entity_id extracted from query",
        },
        "tip": "Add ?debug=true to /hybrid/chat to see per-query attribution breakdown",
    }


# ---------------------------------------------------------------------------
# SSE Helper
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

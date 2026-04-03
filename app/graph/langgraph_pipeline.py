"""
LangGraph stateful wave pipeline for COSMOS.

Wraps the existing GREL Wave 1 / Wave 2 pattern in a proper LangGraph
StateGraph with checkpointing, conditional routing, and parallel fan-out.

Wave architecture:
  START
    │
    ▼
  [wave1_probe]  ── parallel: vector_search + graph_bfs + entity_lookup
    │
    ▼
  [confidence_gate]  ── if confidence >= threshold → DONE
    │                   else → wave2_deepen
    ▼
  [wave2_deepen]  ── parallel: tool_use + full_reasoning (LLM)
    │
    ▼
  [merge_results]
    │
    ▼
  END

State is serializable (all primitives) — supports LangGraph checkpointing.
Pipeline is built once per process (module-level singleton) and reused.
Checkpointer is MemorySaver (in-memory, keyed by thread_id == session_id).

Requires:  pip install langgraph langchain-core
Falls back gracefully if langgraph is not installed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple as Tuple_, TypedDict

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Systematic debugging helpers (superpowers: systematic-debugging skill)
# ---------------------------------------------------------------------------

def _ralph_diagnose(exc: Exception, query: str, context_hits: list) -> None:
    """
    Systematic debugging gate (superpowers: systematic-debugging skill).
    Logs a structured root-cause investigation before falling back.
    This output appears in logs and helps engineers diagnose wave failures
    without needing to reproduce the error.
    """
    import traceback
    exc_type = type(exc).__name__
    exc_tb = traceback.format_exc()

    # Pattern analysis: classify the failure domain
    failure_domain = "unknown"
    if "embedding" in str(exc).lower() or "vector" in str(exc).lower():
        failure_domain = "vectorstore"
    elif "graph" in str(exc).lower() or "node" in str(exc).lower():
        failure_domain = "graph_retrieval"
    elif "llm" in str(exc).lower() or "complete" in str(exc).lower() or "openai" in str(exc).lower():
        failure_domain = "llm_client"
    elif "timeout" in str(exc).lower() or "asyncio" in str(exc).lower():
        failure_domain = "timeout"
    elif "json" in str(exc).lower() or "parse" in str(exc).lower():
        failure_domain = "json_parse"
    elif "sql" in str(exc).lower() or "postgres" in str(exc).lower() or "asyncpg" in str(exc).lower():
        failure_domain = "database"

    import structlog as _structlog
    _log = _structlog.get_logger(__name__)
    _log.error(
        "ralph.wave_failure_diagnosed",
        exc_type=exc_type,
        failure_domain=failure_domain,
        query_len=len(query),
        context_hits_count=len(context_hits) if context_hits else 0,
        architecture_question=(
            f"Domain={failure_domain}: Is the assumption correct that {_domain_assumption(failure_domain)}?"
        ),
        traceback_tail=exc_tb[-500:] if len(exc_tb) > 500 else exc_tb,
    )


def _domain_assumption(domain: str) -> str:
    """Returns the key architectural assumption for each failure domain."""
    assumptions = {
        "vectorstore": "the embedding model is loaded and the cosmos_embeddings table is populated",
        "graph_retrieval": "graph_nodes and graph_edges tables have data for this repo_id",
        "llm_client": "the LLM client is initialized and the API key is valid",
        "timeout": "the wave timeout thresholds match actual service latencies",
        "json_parse": "the LLM always returns valid JSON when prompted with JSON-only instruction",
        "database": "the PostgreSQL connection pool is healthy and not exhausted",
        "unknown": "the input data is well-formed and all dependencies are initialized",
    }
    return assumptions.get(domain, assumptions["unknown"])


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class WaveState(TypedDict, total=False):
    """Mutable pipeline state shared across all graph nodes."""
    # Inputs
    query: str
    user_id: str
    session_id: str
    repo_id: Optional[str]
    confidence_threshold: float          # default 0.75 (from WorkflowSettings)

    # Wave 1 outputs
    vector_hits: List[Dict[str, Any]]    # [{doc_id, score, content, tool_name}]
    graph_hits: List[Dict[str, Any]]     # [{node_id, label, node_type, depth}]
    entity_hit: Optional[Dict[str, Any]] # first exact entity match or None
    wave1_confidence: float              # 0..1 score from RRF merge
    wave1_latency_ms: float

    # Wave 2 outputs (only set when wave 1 confidence is below threshold)
    wave2_tool_results: List[Dict[str, Any]]
    wave2_reasoning: str
    wave2_confidence: float
    wave2_latency_ms: float

    # Final merged results
    final_hits: List[Dict[str, Any]]
    final_confidence: float
    total_latency_ms: float

    # Metadata
    wave2_triggered: bool
    pipeline_backend: str                # "langgraph" | "fallback"
    embedding_model: str

    # Query enrichment (set by query_enrichment node)
    raw_query: str                             # original user query, unchanged — used by LLM response layer
    enriched_query: str                        # canonical + keywords — used by all retrieval legs
    intent_keywords: List[str]                 # ["wallet", "credit", "NDR"] extracted by enrichment
    api_hint: str                              # "/billing/wallet" or "" if none detected
    module_hint: str                           # "billing" or "" if none detected
    enrichment_latency_ms: float

    # Structured KB context (injected from W1+W2 merged_context)
    action_contracts: List[Dict[str, Any]]     # P6 action contracts matching query domain
    workflow_states: List[Dict[str, Any]]      # P7 workflow state machines/decision matrices
    field_traces: List[Dict[str, Any]]         # P4 field→API→table.column chains
    page_context: Optional[Dict[str, Any]]     # P4 page + role context
    assembled_context_text: str                # Pre-assembled ContextAssembler output
    neighbor_chunks: List[Dict[str, Any]]      # Sibling evidence from same parent_doc


# ---------------------------------------------------------------------------
# Module-level singleton — built once per process, shared across all calls
# ---------------------------------------------------------------------------

_PIPELINE_CACHE: Dict[str, Any] = {}   # key → compiled pipeline
_CHECKPOINTER: Any = None              # MemorySaver singleton (or None if langgraph missing)


def _get_checkpointer() -> Any:
    """Return a module-level MemorySaver. Instantiated once."""
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore[import]
        _CHECKPOINTER = MemorySaver()
        logger.info("langgraph.checkpointer_ready", backend="MemorySaver")
    except ImportError:
        _CHECKPOINTER = None
    return _CHECKPOINTER


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion helper
# ---------------------------------------------------------------------------

def _rrf_merge(
    *hit_lists: List[Dict[str, Any]],
    key: str = "doc_id",
    k: int = 60,
    top_n: int = 10,
) -> Tuple_[List[Dict[str, Any]], float]:
    """
    Merge multiple ranked lists via Reciprocal Rank Fusion.
    Returns (merged_list, top_score_confidence).
    """
    scores: Dict[str, float] = {}
    payloads: Dict[str, Dict[str, Any]] = {}

    for hit_list in hit_lists:
        for rank, hit in enumerate(hit_list, start=1):
            doc_id = hit.get(key, hit.get("node_id", str(rank)))
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in payloads:
                payloads[doc_id] = hit

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    merged = [payloads[did] for did in sorted_ids[:top_n]]

    top_score = scores[sorted_ids[0]] if sorted_ids else 0.0
    max_possible = sum(1.0 / (k + r) for r in range(1, len(hit_lists) + 1))
    confidence = min(top_score / max_possible, 1.0) if max_possible > 0 else 0.0

    return merged, confidence


# ---------------------------------------------------------------------------
# Pipeline builder  (call once per process — results are cached)
# ---------------------------------------------------------------------------

def build_wave_pipeline(
    vectorstore=None,      # VectorStoreService or compatible
    graphrag=None,         # GraphRAGService or Neo4jGraphService
    react_engine=None,     # ReActEngine for Wave 2 tool_use
    llm_client=None,       # LLM client with .complete(prompt, max_tokens) for _full_reasoning
    neo4j_service=None,    # Neo4jGraphService for deep chain scoring
    confidence_threshold: float = 0.75,
):
    """
    Build (or return cached) compiled LangGraph pipeline.

    The pipeline is keyed by confidence_threshold and cached globally.
    On first call it compiles the graph with a MemorySaver checkpointer.
    Subsequent calls with the same threshold return the cached instance.

    Falls back to a plain async callable when langgraph is not installed.

    Parameters
    ----------
    vectorstore:          service with .search(query, top_k) → List[{doc_id, score, content}]
    graphrag:             service with .bfs_query(q) or .query_related(q) → hits
    react_engine:         ReActEngine with .run(query, context) → result dict
    llm_client:           client with .complete(prompt, max_tokens) → str  (for Wave 2 reasoning)
    confidence_threshold: Wave 2 trigger threshold (from WorkflowSettings)
    """
    cache_key = f"pipeline:{confidence_threshold}"
    if cache_key in _PIPELINE_CACHE:
        # Update the service references in case they changed (e.g. re-init)
        cached = _PIPELINE_CACHE[cache_key]
        if hasattr(cached, "_cosmos_services"):
            cached._cosmos_services.update({
                "vectorstore": vectorstore,
                "graphrag": graphrag,
                "react_engine": react_engine,
                "llm_client": llm_client,
                "neo4j": neo4j_service,
            })
        return cached

    checkpointer = _get_checkpointer()

    try:
        from langgraph.graph import StateGraph, END  # type: ignore[import]
        pipeline = _build_langgraph(
            vectorstore, graphrag, react_engine, llm_client,
            confidence_threshold, checkpointer, StateGraph, END,
            neo4j_service=neo4j_service,
        )
        logger.info("langgraph_pipeline.built", threshold=confidence_threshold,
                    checkpointer=type(checkpointer).__name__ if checkpointer else "none")
    except ImportError:
        logger.warning("langgraph.not_installed", hint="pip install langgraph langchain-core")
        pipeline = _build_fallback(vectorstore, graphrag, react_engine, llm_client,
                                   confidence_threshold)

    _PIPELINE_CACHE[cache_key] = pipeline
    return pipeline


def _build_langgraph(vectorstore, graphrag, react_engine, llm_client,
                     threshold, checkpointer, StateGraph, END,
                     neo4j_service=None):
    """Build the real LangGraph pipeline with checkpointing.

    Pipeline flow:
      query_enrichment → wave1_probe → adaptive_retrieve → confidence_gate
                                                              ↓ (low)
                                                        neo4j_deepen → wave2_deepen → merge_results
                                                              ↓ (high)
                                                        merge_results → END
    """

    # Mutable service container attached to the pipeline object so the cache
    # update path in build_wave_pipeline can refresh references.
    services: Dict[str, Any] = {
        "vectorstore": vectorstore,
        "graphrag": graphrag,
        "react_engine": react_engine,
        "llm_client": llm_client,
        "neo4j": neo4j_service,
    }

    graph = StateGraph(WaveState)

    # ── Node: Query enrichment (runs before Wave 1) ───────────────────────────
    # Normalises raw user query → canonical form + keywords + entity hints.
    # All retrieval legs (vector, graph BFS, lexical) use enriched_query.
    # raw_query is preserved in state so the final LLM response layer answers
    # what the user actually asked.

    async def query_enrichment(state: WaveState) -> WaveState:
        raw = state["query"]
        t0 = time.monotonic()
        llm = services["llm_client"]

        if llm is None:
            # No LLM available — pass through unchanged
            return {
                **state,
                "raw_query": raw,
                "enriched_query": raw,
                "intent_keywords": [],
                "api_hint": "",
                "module_hint": "",
                "enrichment_latency_ms": 0.0,
            }

        prompt = (
            "You are an ICRM query normalizer for a logistics platform (Shiprocket).\n"
            "Convert the user query into structured retrieval signals. "
            "Respond with JSON only — no explanation.\n\n"
            f'Query: "{raw}"\n\n'
            "Output format:\n"
            '{\n'
            '  "canonical": "concise English restatement of the query (max 15 words)",\n'
            '  "keywords": ["keyword1", "keyword2"],  // 3-6 technical terms\n'
            '  "api_hint": "/api/vN/path or empty string",\n'
            '  "module_hint": "module name or empty string"\n'
            '}'
        )

        enriched: Dict[str, Any] = {}
        try:
            raw_resp = await llm.complete(prompt, max_tokens=150)
            import json as _json, re as _re
            m = _re.search(r'\{.*\}', raw_resp, _re.DOTALL)
            if m:
                enriched = _json.loads(m.group())
        except Exception as exc:
            logger.warning("query_enrichment.failed", error=str(exc))

        canonical = (enriched.get("canonical") or "").strip()
        keywords: List[str] = [k for k in (enriched.get("keywords") or []) if isinstance(k, str)]
        api_hint = (enriched.get("api_hint") or "").strip()
        module_hint = (enriched.get("module_hint") or "").strip()

        # enriched_query = canonical form + top keywords appended
        # This is what vector/graph/lexical legs use
        enriched_query = canonical if canonical else raw
        if keywords:
            enriched_query = enriched_query + " " + " ".join(keywords[:5])

        latency_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "query_enrichment.done",
            raw=raw[:80],
            enriched=enriched_query[:80],
            keywords=keywords,
            api_hint=api_hint,
            module_hint=module_hint,
            latency_ms=round(latency_ms, 1),
        )

        return {
            **state,
            "raw_query": raw,
            "query": enriched_query,       # overwrite — retrieval legs use this
            "enriched_query": enriched_query,
            "intent_keywords": keywords,
            "api_hint": api_hint,
            "module_hint": module_hint,
            "enrichment_latency_ms": latency_ms,
        }

    # ── Node: Wave 1 probe (parallel vector + graph + entity) ────────────────

    async def wave1_probe(state: WaveState) -> WaveState:
        query = state["query"]          # enriched_query at this point
        repo_id = state.get("repo_id")
        t0 = time.monotonic()

        vector_hits, graph_hits, entity_hit = await asyncio.gather(
            _vector_search(services["vectorstore"], query, repo_id),
            _graph_bfs(services["graphrag"], query, repo_id),
            _entity_lookup(services["graphrag"], query),
        )

        merged, confidence = _rrf_merge(vector_hits, graph_hits, key="doc_id", top_n=10)
        if entity_hit:
            confidence = min(confidence + 0.15, 1.0)

        latency_ms = (time.monotonic() - t0) * 1000
        logger.info("wave1_probe.done", confidence=round(confidence, 3),
                    hits=len(merged), latency_ms=round(latency_ms, 1))

        return {
            **state,
            "vector_hits": vector_hits,
            "graph_hits": graph_hits,
            "entity_hit": entity_hit,
            "wave1_confidence": confidence,
            "wave1_latency_ms": latency_ms,
            "final_hits": merged,
            "final_confidence": confidence,
            "wave2_triggered": False,
            "pipeline_backend": "langgraph",
        }

    # ── Node: Wave 2 deepen (tool_use + LLM reasoning) ──────────────────────

    async def wave2_deepen(state: WaveState) -> WaveState:
        query = state["query"]
        t0 = time.monotonic()

        tool_results, reasoning = await asyncio.gather(
            _tool_use(services["react_engine"], query, state.get("final_hits", [])),
            _full_reasoning(query, state.get("final_hits", []), services["llm_client"]),
        )

        tool_conf = 0.85 if tool_results else 0.0
        reasoning_conf = 0.80 if reasoning else 0.0
        wave2_conf = max(tool_conf, reasoning_conf, state.get("wave1_confidence", 0.0))

        latency_ms = (time.monotonic() - t0) * 1000
        logger.info("wave2_deepen.done", confidence=round(wave2_conf, 3),
                    latency_ms=round(latency_ms, 1))

        return {
            **state,
            "wave2_tool_results": tool_results,
            "wave2_reasoning": reasoning,
            "wave2_confidence": wave2_conf,
            "wave2_latency_ms": latency_ms,
            "wave2_triggered": True,
        }

    # ── Node: Merge final results ─────────────────────────────────────────────

    async def merge_results(state: WaveState) -> WaveState:
        final_hits = list(state.get("final_hits", []))
        for tr in state.get("wave2_tool_results", []):
            if tr not in final_hits:
                final_hits.append(tr)

        final_conf = max(
            state.get("wave2_confidence", 0.0),
            state.get("wave1_confidence", 0.0),
        )
        total_ms = state.get("wave1_latency_ms", 0.0) + state.get("wave2_latency_ms", 0.0)

        return {
            **state,
            "final_hits": final_hits[:10],
            "final_confidence": final_conf,
            "total_latency_ms": total_ms,
        }

    # ── Confidence routing ────────────────────────────────────────────────────

    def confidence_gate(state: WaveState) -> str:
        conf = state.get("wave1_confidence", 0.0)
        thr = state.get("confidence_threshold", threshold)
        if conf >= thr:
            logger.debug("confidence_gate.skip_wave2", confidence=conf, threshold=thr)
            return "done"
        logger.debug("confidence_gate.trigger_wave2", confidence=conf, threshold=thr)
        return "wave2"

    # ── Node: Adaptive retrieve — evaluates evidence sufficiency, triggers Neo4j if needed
    async def adaptive_retrieve(state: WaveState) -> WaveState:
        """Evaluate if Wave 1 found enough evidence. If not, trigger targeted Neo4j retrieval.

        Checks:
        - Action query but no action_contract in results → Neo4j chain scoring
        - Workflow query but no state_machine → Neo4j domain traversal
        - Field trace query but no trace → Neo4j multi-hop chain
        """
        t0 = time.monotonic()
        neo4j = services.get("neo4j")
        confidence = state.get("wave1_confidence", 0.0)
        action_contracts = state.get("action_contracts", [])
        workflow_states = state.get("workflow_states", [])
        field_traces = state.get("field_traces", [])
        graph_hits = list(state.get("graph_hits", []))
        query = state.get("query", "")

        # Detect query mode from keywords
        q_lower = query.lower()
        is_action = any(w in q_lower for w in ["cancel", "update", "trigger", "process", "karo", "schedule", "assign"])
        is_diagnosis = any(w in q_lower for w in ["why", "stuck", "failed", "kyun", "wrong", "issue"])
        is_field = any(w in q_lower for w in ["field", "column", "where does", "source", "trace", "kahan"])

        neo4j_hits = []
        if neo4j and hasattr(neo4j, "available") and neo4j.available:
            try:
                # Gather seed IDs from entity_hit and existing graph_hits
                seeds = []
                entity = state.get("entity_hit")
                if entity and isinstance(entity, dict):
                    seeds.append(entity.get("entity_id", entity.get("node_id", "")))
                for gh in graph_hits[:3]:
                    if isinstance(gh, dict) and gh.get("node_id"):
                        seeds.append(gh["node_id"])
                seeds = [s for s in seeds if s]

                if seeds:
                    if is_action and not action_contracts:
                        # Need action contracts — find them via Neo4j chain scoring
                        neo4j_hits = await neo4j.score_chains(
                            seed_ids=seeds, target_types=["action_contract"], max_depth=4,
                        )
                    elif is_diagnosis and not workflow_states:
                        # Need workflow state machines
                        neo4j_hits = await neo4j.score_chains(
                            seed_ids=seeds, target_types=["workflow"], max_depth=4,
                        )
                    elif is_field and not field_traces:
                        # Need field→API→table chain
                        neo4j_hits = await neo4j.score_chains(
                            seed_ids=seeds, target_types=["table", "api_endpoint", "page"], max_depth=4,
                        )
                    elif confidence < 0.6:
                        # Low confidence — broad Neo4j expansion
                        neo4j_hits = await neo4j.score_chains(
                            seed_ids=seeds, max_depth=3,
                        )
            except Exception as e:
                logger.debug("adaptive_retrieve.neo4j_failed", error=str(e))

        # Merge Neo4j hits into graph_hits
        if neo4j_hits:
            seen = {gh.get("node_id") for gh in graph_hits if isinstance(gh, dict)}
            for hit in neo4j_hits:
                if isinstance(hit, dict) and hit.get("node_id") not in seen:
                    graph_hits.append({
                        "node_id": hit.get("node_id", ""),
                        "label": hit.get("label", ""),
                        "node_type": hit.get("node_type", ""),
                        "depth": hit.get("hops", 0),
                        "chain_weight": hit.get("chain_weight", 0),
                        "path_labels": hit.get("path_labels", []),
                        "_source": "neo4j_adaptive",
                    })
                    seen.add(hit.get("node_id"))

            # Boost confidence if Neo4j found relevant nodes
            confidence = min(confidence + 0.1 * len(neo4j_hits[:5]), 1.0)
            logger.info("adaptive_retrieve.neo4j_enriched",
                         hits=len(neo4j_hits), new_confidence=round(confidence, 2))

        return {
            **state,
            "graph_hits": graph_hits,
            "wave1_confidence": confidence,
            "wave1_latency_ms": state.get("wave1_latency_ms", 0) + (time.monotonic() - t0) * 1000,
        }

    # ── Wire the graph ────────────────────────────────────────────────────────

    graph.add_node("query_enrichment", query_enrichment)
    graph.add_node("wave1_probe", wave1_probe)
    graph.add_node("adaptive_retrieve", adaptive_retrieve)
    graph.add_node("wave2_deepen", wave2_deepen)
    graph.add_node("merge_results", merge_results)

    # Flow: enrichment → probe → adaptive_retrieve → confidence_gate
    graph.set_entry_point("query_enrichment")
    graph.add_edge("query_enrichment", "wave1_probe")
    graph.add_edge("wave1_probe", "adaptive_retrieve")

    graph.add_conditional_edges(
        "adaptive_retrieve",
        confidence_gate,
        {
            "done": "merge_results",
            "wave2": "wave2_deepen",
        },
    )
    graph.add_edge("wave2_deepen", "merge_results")
    graph.add_edge("merge_results", END)

    # Compile with checkpointer — enables multi-turn state persistence keyed by thread_id
    compiled = graph.compile(checkpointer=checkpointer)
    compiled._cosmos_services = services   # attach for cache refresh
    return compiled


# ---------------------------------------------------------------------------
# Fallback pipeline (no langgraph installed — same logic, plain async fn)
# ---------------------------------------------------------------------------

def _build_fallback(vectorstore, graphrag, react_engine, llm_client, threshold):
    """Returns an async callable with the same signature as a compiled LangGraph."""

    services: Dict[str, Any] = {
        "vectorstore": vectorstore,
        "graphrag": graphrag,
        "react_engine": react_engine,
        "llm_client": llm_client,
    }

    async def run(state: WaveState, config: Optional[Dict] = None) -> WaveState:
        # Run enrichment inline (same logic as the LangGraph node)
        raw = state["query"]
        llm = services["llm_client"]
        enriched_query = raw
        keywords: List[str] = []
        api_hint = ""
        module_hint = ""
        enrich_ms = 0.0

        if llm is not None:
            t_enrich = time.monotonic()
            prompt = (
                "You are an ICRM query normalizer for a logistics platform (Shiprocket).\n"
                "Convert the user query into structured retrieval signals. "
                "Respond with JSON only — no explanation.\n\n"
                f'Query: "{raw}"\n\n'
                "Output format:\n"
                '{\n'
                '  "canonical": "concise English restatement of the query (max 15 words)",\n'
                '  "keywords": ["keyword1", "keyword2"],\n'
                '  "api_hint": "/api/vN/path or empty string",\n'
                '  "module_hint": "module name or empty string"\n'
                '}'
            )
            try:
                import json as _json, re as _re
                raw_resp = await llm.complete(prompt, max_tokens=150)
                m = _re.search(r'\{.*\}', raw_resp, _re.DOTALL)
                if m:
                    enriched = _json.loads(m.group())
                    canonical = (enriched.get("canonical") or "").strip()
                    keywords = [k for k in (enriched.get("keywords") or []) if isinstance(k, str)]
                    api_hint = (enriched.get("api_hint") or "").strip()
                    module_hint = (enriched.get("module_hint") or "").strip()
                    enriched_query = (canonical if canonical else raw)
                    if keywords:
                        enriched_query += " " + " ".join(keywords[:5])
            except Exception as exc:
                # Systematic debugging (superpowers pattern): root cause before fallback
                _ralph_diagnose(exc, raw, [])
                logger.warning("fallback.query_enrichment.failed", error=str(exc))
            enrich_ms = (time.monotonic() - t_enrich) * 1000

        query = enriched_query
        repo_id = state.get("repo_id")
        t0 = time.monotonic()

        try:
            vector_hits, graph_hits, entity_hit = await asyncio.gather(
                _vector_search(services["vectorstore"], query, repo_id),
                _graph_bfs(services["graphrag"], query, repo_id),
                _entity_lookup(services["graphrag"], query),
            )
        except Exception as exc:
            # Systematic debugging (superpowers pattern): root cause before fallback
            _ralph_diagnose(exc, query, [])
            logger.error("fallback.wave1_gather.failed", error=str(exc))
            vector_hits, graph_hits, entity_hit = [], [], None

        merged, confidence = _rrf_merge(vector_hits, graph_hits, top_n=10)
        if entity_hit:
            confidence = min(confidence + 0.15, 1.0)

        wave1_ms = (time.monotonic() - t0) * 1000
        wave2_tool_results: List[Dict] = []
        wave2_reasoning = ""
        wave2_triggered = False
        wave2_ms = 0.0

        if confidence < threshold:
            t1 = time.monotonic()
            try:
                tool_results, reasoning = await asyncio.gather(
                    _tool_use(services["react_engine"], query, merged),
                    _full_reasoning(query, merged, services["llm_client"]),
                )
            except Exception as exc:
                # Systematic debugging (superpowers pattern): root cause before fallback
                _ralph_diagnose(exc, query, merged)
                logger.error("fallback.wave2_gather.failed", error=str(exc))
                tool_results, reasoning = [], ""
            wave2_tool_results = tool_results
            wave2_reasoning = reasoning
            wave2_triggered = True
            wave2_ms = (time.monotonic() - t1) * 1000
            confidence = max(confidence, 0.85 if tool_results else 0.80 if reasoning else confidence)
            merged.extend(r for r in tool_results if r not in merged)

        run._cosmos_services = services  # type: ignore[attr-defined]
        return {
            **state,
            "raw_query": raw,
            "query": enriched_query,
            "enriched_query": enriched_query,
            "intent_keywords": keywords,
            "api_hint": api_hint,
            "module_hint": module_hint,
            "enrichment_latency_ms": enrich_ms,
            "vector_hits": vector_hits,
            "graph_hits": graph_hits,
            "entity_hit": entity_hit,
            "wave1_confidence": confidence,
            "wave1_latency_ms": wave1_ms,
            "wave2_tool_results": wave2_tool_results,
            "wave2_reasoning": wave2_reasoning,
            "wave2_confidence": confidence,
            "wave2_latency_ms": wave2_ms,
            "final_hits": merged[:10],
            "final_confidence": confidence,
            "total_latency_ms": wave1_ms + wave2_ms + enrich_ms,
            "wave2_triggered": wave2_triggered,
            "pipeline_backend": "fallback",
        }

    run._cosmos_services = services  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# Internal retrieval helpers (safe wrappers around real services)
# ---------------------------------------------------------------------------

async def _vector_search(
    vectorstore: Any,
    query: str,
    repo_id: Optional[str],
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    if vectorstore is None:
        return []
    try:
        results = await vectorstore.search(query, top_k=top_k, repo_id=repo_id)
        return [
            {
                "doc_id": r.get("id", r.get("doc_id", "")),
                "score": float(r.get("similarity", r.get("score", 0.0))),
                "content": r.get("content", ""),
                "tool_name": r.get("tool_name", r.get("metadata", {}).get("tool_name", "")),
            }
            for r in (results or [])
        ]
    except Exception as exc:
        logger.warning("wave._vector_search.failed", error=str(exc))
        return []


async def _graph_bfs(
    graphrag: Any,
    query: str,
    repo_id: Optional[str],
    max_depth: int = 2,
) -> List[Dict[str, Any]]:
    if graphrag is None:
        return []
    try:
        if hasattr(graphrag, "bfs_query"):
            hits = await graphrag.bfs_query(query, repo_id=repo_id, max_depth=max_depth)
        else:
            result = await graphrag.query_related(query, repo_id=repo_id, max_depth=max_depth)
            hits = [
                {"doc_id": n.id, "node_id": n.id, "label": n.label,
                 "node_type": n.node_type.value, "score": 0.5}
                for n in (result.matched_nodes + result.related_nodes)
            ]
        return hits
    except Exception as exc:
        logger.warning("wave._graph_bfs.failed", error=str(exc))
        return []


async def _entity_lookup(graphrag: Any, query: str) -> Optional[Dict[str, Any]]:
    """Try to extract and resolve entities from the query string."""
    if graphrag is None:
        return None
    try:
        import re
        paths = re.findall(r"/api/v\d+/[^\s,]+", query)
        if not paths:
            return None
        for path in paths[:2]:
            if hasattr(graphrag, "entity_lookup"):
                hit = await graphrag.entity_lookup("api_path", path)
            else:
                hit = await graphrag.pg_lookup_entity("api_path", path)
            if hit:
                return hit
        return None
    except Exception as exc:
        logger.warning("wave._entity_lookup.failed", error=str(exc))
        return None


async def _tool_use(react_engine: Any, query: str, context_hits: List[Dict]) -> List[Dict]:
    """Wave 2: invoke ReAct engine with retrieved context."""
    if react_engine is None:
        return []
    try:
        context_str = "\n".join(
            h.get("content", h.get("label", "")) for h in context_hits[:5]
        )
        result = await react_engine.run(query, context=context_str)
        if result:
            return [{"doc_id": "tool_result", "content": str(result), "score": 0.9}]
        return []
    except Exception as exc:
        logger.warning("wave._tool_use.failed", error=str(exc))
        return []


async def _full_reasoning(
    query: str,
    context_hits: List[Dict],
    llm_client: Any,
) -> str:
    """
    Wave 2: synthesize a reasoning string from retrieved context via LLM.

    Uses llm_client.complete(prompt, max_tokens) when available.
    Falls back to a deterministic context summary if llm_client is None.
    """
    if not context_hits:
        return ""

    # Build context block from top-5 hits
    context_lines = []
    for i, h in enumerate(context_hits[:5], 1):
        text = h.get("content", h.get("label", ""))
        if text:
            context_lines.append(f"[{i}] {text[:300]}")

    context_block = "\n".join(context_lines)

    if llm_client is None:
        # Deterministic fallback — no LLM call
        return f"Context ({len(context_lines)} sources):\n{context_block}"

    prompt = (
        f"You are a retrieval reasoning assistant. Given the query and retrieved context, "
        f"write a concise 2-3 sentence synthesis that explains what the context tells us "
        f"about the query. Be factual. Do not hallucinate.\n\n"
        f"Query: {query}\n\n"
        f"Retrieved context:\n{context_block}\n\n"
        f"Synthesis:"
    )

    try:
        reasoning = await llm_client.complete(prompt, max_tokens=300)
        return reasoning.strip() if isinstance(reasoning, str) else str(reasoning)
    except Exception as exc:
        logger.warning("wave._full_reasoning.llm_failed", error=str(exc))
        # Fall back to deterministic summary on LLM error
        return f"Context ({len(context_lines)} sources):\n{context_block}"

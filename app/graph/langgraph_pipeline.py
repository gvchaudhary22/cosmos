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
  [wave2_deepen]  ── parallel: tool_use + full_reasoning
    │
    ▼
  [merge_results]
    │
    ▼
  END

State is serializable (all primitives) — supports LangGraph checkpointing.

Requires:  pip install langgraph langchain-core
Falls back gracefully if langgraph is not installed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, TypedDict

import structlog

logger = structlog.get_logger(__name__)

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
    pipeline_backend: str                # "current" | "neo4j-small" | "neo4j-large"
    embedding_model: str


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
    from typing import Tuple as Tuple_
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

    # Normalize top score to [0, 1] using k as reference
    top_score = scores[sorted_ids[0]] if sorted_ids else 0.0
    max_possible = sum(1.0 / (k + r) for r in range(1, len(hit_lists) + 1))
    confidence = min(top_score / max_possible, 1.0) if max_possible > 0 else 0.0

    return merged, confidence


# Fix the type hint issue above (TypedDict doesn't support forward refs well)
from typing import Tuple as Tuple_


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------

def build_wave_pipeline(
    vectorstore=None,      # VectorStoreService or compatible
    graphrag=None,         # GraphRAGService or Neo4jGraphService
    react_engine=None,     # ReActEngine for Wave 2 tool_use
    confidence_threshold: float = 0.75,
):
    """
    Build and return a LangGraph StateGraph.

    Falls back to a simple async callable if langgraph is not installed.

    Parameters
    ----------
    vectorstore:  service with .search(query, top_k) → List[{doc_id, score, content}]
    graphrag:     service with .query_related(q) or .bfs_query(q) → hits
    react_engine: ReActEngine with .run(query) → result dict
    confidence_threshold: Wave 2 trigger threshold (from WorkflowSettings)
    """
    try:
        from langgraph.graph import StateGraph, END  # type: ignore[import]
        return _build_langgraph(vectorstore, graphrag, react_engine,
                                confidence_threshold, StateGraph, END)
    except ImportError:
        logger.warning("langgraph.not_installed", hint="pip install langgraph langchain-core")
        return _build_fallback(vectorstore, graphrag, react_engine, confidence_threshold)


def _build_langgraph(vectorstore, graphrag, react_engine, threshold, StateGraph, END):
    """Build the real LangGraph pipeline."""

    graph = StateGraph(WaveState)

    # ── Node: Wave 1 probe (parallel vector + graph + entity) ────────────────

    async def wave1_probe(state: WaveState) -> WaveState:
        query = state["query"]
        repo_id = state.get("repo_id")
        t0 = time.monotonic()

        # Run vector search + graph BFS + entity lookup in parallel
        tasks = [
            _vector_search(vectorstore, query, repo_id),
            _graph_bfs(graphrag, query, repo_id),
            _entity_lookup(graphrag, query),
        ]
        vector_hits, graph_hits, entity_hit = await asyncio.gather(*tasks)

        # RRF merge across vector and graph legs
        merged, confidence = _rrf_merge(
            vector_hits, graph_hits,
            key="doc_id",
            top_n=10,
        )

        # Entity hit bumps confidence
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
        }

    # ── Node: Wave 2 deepen (tool_use + full_reasoning) ─────────────────────

    async def wave2_deepen(state: WaveState) -> WaveState:
        query = state["query"]
        t0 = time.monotonic()

        tool_results, reasoning = await asyncio.gather(
            _tool_use(react_engine, query, state.get("final_hits", [])),
            _full_reasoning(query, state.get("final_hits", [])),
        )

        # Bump confidence from Wave 2 tool results
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
        t_start = state.get("wave1_latency_ms", 0) + state.get("wave2_latency_ms", 0)

        # Merge tool results into final_hits if Wave 2 ran
        final_hits = list(state.get("final_hits", []))
        for tr in state.get("wave2_tool_results", []):
            if tr not in final_hits:
                final_hits.append(tr)

        final_conf = max(
            state.get("wave2_confidence", 0.0),
            state.get("wave1_confidence", 0.0),
        )

        return {
            **state,
            "final_hits": final_hits[:10],
            "final_confidence": final_conf,
            "total_latency_ms": t_start,
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

    # ── Wire the graph ────────────────────────────────────────────────────────

    graph.add_node("wave1_probe", wave1_probe)
    graph.add_node("wave2_deepen", wave2_deepen)
    graph.add_node("merge_results", merge_results)

    graph.set_entry_point("wave1_probe")

    graph.add_conditional_edges(
        "wave1_probe",
        confidence_gate,
        {
            "done": END,       # high confidence — skip Wave 2
            "wave2": "wave2_deepen",
        },
    )
    graph.add_edge("wave2_deepen", "merge_results")
    graph.add_edge("merge_results", END)

    compiled = graph.compile()
    logger.info("langgraph_pipeline.compiled", threshold=threshold)
    return compiled


# ---------------------------------------------------------------------------
# Fallback pipeline (no langgraph installed — same logic, plain async fn)
# ---------------------------------------------------------------------------

def _build_fallback(vectorstore, graphrag, react_engine, threshold):
    """Returns an async callable with the same signature as a compiled LangGraph."""

    async def run(state: WaveState) -> WaveState:
        query = state["query"]
        repo_id = state.get("repo_id")
        t0 = time.monotonic()

        vector_hits, graph_hits, entity_hit = await asyncio.gather(
            _vector_search(vectorstore, query, repo_id),
            _graph_bfs(graphrag, query, repo_id),
            _entity_lookup(graphrag, query),
        )

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
            tool_results, reasoning = await asyncio.gather(
                _tool_use(react_engine, query, merged),
                _full_reasoning(query, merged),
            )
            wave2_tool_results = tool_results
            wave2_reasoning = reasoning
            wave2_triggered = True
            wave2_ms = (time.monotonic() - t1) * 1000
            confidence = max(confidence, 0.85 if tool_results else 0.80)
            merged.extend(r for r in tool_results if r not in merged)

        return {
            **state,
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
            "total_latency_ms": wave1_ms + wave2_ms,
            "wave2_triggered": wave2_triggered,
        }

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
        # Supports both GraphRAGService and Neo4jGraphService
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
        # Simple heuristic: look for /api/vN/... patterns in query
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


async def _full_reasoning(query: str, context_hits: List[Dict]) -> str:
    """Wave 2: synthesize a reasoning string from retrieved context."""
    # Placeholder — in production this calls the LLM with full context
    if not context_hits:
        return ""
    labels = [h.get("content", h.get("label", ""))[:100] for h in context_hits[:5]]
    return f"Based on {len(labels)} retrieved documents: " + "; ".join(labels)

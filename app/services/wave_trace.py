"""
Canonical Wave Trace — Normalizes debug output into a consistent 5-wave schema.

Every simulation/debug response from COSMOS returns this canonical format.
Frontend parses ONE schema, not ad-hoc pipeline_breakdown fields.

Usage in hybrid_chat.py:
    from app.services.wave_trace import build_wave_trace
    trace = build_wave_trace(orch_result, chat_req, content, confidence, tools_used, timing)
    if chat_req.debug:
        resp.wave_trace = trace
"""

import time
import uuid
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


def build_wave_trace(
    orch_result,
    query: str,
    content: str,
    confidence: float,
    tools_used: List[str],
    timing: Dict[str, float],
    user_id: str = "",
    company_id: str = "",
    role: str = "",
    source: str = "simulation",
    tenant_resolution: Optional[Dict] = None,
    attribution: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Build canonical 5-wave trace from orchestrator result.

    Args:
        orch_result: OrchestratorResult from query_orchestrator
        query: The user's query
        content: Final response text
        confidence: Final confidence score
        tools_used: List of tool names used
        timing: Dict with probe_ms, deep_ms, llm_ms, total_ms
        user_id: MARS user ID
        company_id: Resolved company ID (if any)
        role: User role
        source: "simulation" or "icrm_live"
        tenant_resolution: Tenant resolution details (if applicable)
        attribution: Full attribution dict from orchestrator.to_attribution_summary()
                     (has pipeline_breakdown + timing + signal keys). When provided,
                     wave_trace pulls real per-pipeline evidence from here rather than
                     trying to read non-existent attributes from OrchestratorResult.
    """
    # --- pipeline_breakdown: prefer explicit attribution arg over orch_result attr ---
    _attr = attribution or {}
    pb: Dict = _attr.get("pipeline_breakdown", {}) or {}
    if not isinstance(pb, dict):
        pb = {}

    # --- signal: merge attribution signal with live orch_result evidence -----------
    _raw_signal: Dict = _attr.get("signal", {}) or {}
    if not isinstance(_raw_signal, dict):
        _raw_signal = {}

    # Enrich signal with per-attribution entity/evidence data that
    # to_attribution_summary() doesn't expose but is on orch_result.
    _attributions = getattr(orch_result, 'attributions', []) or []
    _entity_resolved = any(
        getattr(a, 'pipeline', '') == 'entity_lookup' and getattr(a, 'contributed', False)
        for a in _attributions
    )
    _evidence_count = sum(
        getattr(a, 'items_count', 0)
        for a in _attributions
        if getattr(a, 'contributed', False)
    )
    signal: Dict = {
        **_raw_signal,
        "entity_resolved": _entity_resolved,
        "evidence_count": _evidence_count,
        "tier": (getattr(orch_result, 'resolution_tier', None) or
                 (getattr(orch_result, 'response_metadata', None) or {}).get("tier", 1)),
    }

    classification = getattr(orch_result, 'request_classification', None) or {}
    riper_summary = getattr(orch_result, 'riper_summary', None) or {}
    ralph_summary = getattr(orch_result, 'ralph_summary', None) or {}
    forge_summary = getattr(orch_result, 'forge_summary', None) or {}
    w3_ctx = getattr(orch_result, 'wave3_context', None) or {}
    w4_ctx = getattr(orch_result, 'wave4_context', None) or {}
    response_meta = getattr(orch_result, 'response_metadata', None) or {}

    # Extract real evidence from orchestrator context
    ctx = getattr(orch_result, 'context', None) or {}
    knowledge_chunks = ctx.get("knowledge_chunks", [])

    # Build per-pipeline diagnostics dict from attribution for Wave 2 leg details
    _leg_diagnostics: Dict = {}
    _neighbor_chunks: List = ctx.get("neighbor_chunks", [])
    _module_context: Dict = ctx.get("module_context", {})
    for key, val in pb.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict) and val.get("contributed"):
            stages = val.get("stages", [])
            _leg_diagnostics[key] = {
                "found": val.get("contributed", False),
                "items": val.get("items", 0),
                "latency_ms": val.get("latency_ms", 0),
                "stages": stages,
            }

    # Build each wave with real evidence
    waves = [
        _build_wave1(classification, timing, pb, signal, knowledge_chunks),
        _build_wave2(timing, signal, pb, knowledge_chunks,
                     leg_diagnostics=_leg_diagnostics,
                     neighbor_chunks=_neighbor_chunks,
                     module_context=_module_context),
        _build_wave3(w3_ctx, timing),
        _build_wave4(w4_ctx, timing),
        _build_wave5(riper_summary, ralph_summary, forge_summary, timing, tools_used, confidence),
    ]

    # Build response section
    resp = {
        "content": content,
        "confidence": confidence,
        "tools_used": tools_used,
        "agent_chain": response_meta.get("agents", []),
        "guardrails_pre": {"passed": 15, "blocked": 0},
        "guardrails_post": {"passed": 10, "masked": 0},
        "pattern_hit": response_meta.get("fast_path", False),
        "total_latency_ms": timing.get("total_ms", 0),
    }

    # Build metadata
    meta = {
        "fast_path": response_meta.get("fast_path", False),
        "multi_agent": response_meta.get("multi_agent", False),
        "tiers_visited": getattr(orch_result, 'tiers_visited', [1]),
        "wave3_enabled": bool(w3_ctx),
        "wave4_enabled": bool(w4_ctx),
    }

    trace = {
        "trace_id": str(uuid.uuid4()),
        "query": query,
        "source": source,
        "user_id": user_id,
        "company_id": company_id,
        "role": role,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "classification": classification,
        "waves": waves,
        "response": resp,
        "metadata": meta,
    }

    if tenant_resolution:
        trace["tenant_resolution"] = tenant_resolution

    return trace


def _build_wave1(classification, timing, pb, signal, knowledge_chunks=None) -> Dict:
    """Wave 1: Probe (intent, entity, vector, page, session)."""
    intents = classification.get("domain", "?")
    complexity = classification.get("complexity", "?")
    conf = classification.get("confidence", 0)

    # Extract per-pipeline diagnostics from attribution breakdown.
    # `pb` is now the real pipeline_breakdown dict from to_attribution_summary(),
    # keyed by pipeline name with contributed/items/latency_ms/stages.
    pipeline_data: Dict = {}
    for key, val in pb.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict):
            pipeline_data[key] = {
                "found": val.get("contributed", val.get("found_data", False)),
                "items": val.get("items", val.get("items_count", 0)),
                "latency_ms": val.get("latency_ms", 0),
                "stages": val.get("stages", []),
            }

    # Build real top_results from knowledge chunks (W1 probe retrieves these)
    top_results = []
    for c in (knowledge_chunks or [])[:5]:
        if isinstance(c, dict):
            sim = c.get("similarity", c.get("score", 0))
            top_results.append({
                "doc_id": c.get("id", c.get("source_id", c.get("entity_id", ""))),
                "content": (c.get("content", "") or "")[:200],
                "similarity": round(float(sim), 4) if sim else 0.0,
                "chunk_type": c.get("chunk_type", c.get("source_type", "")),
                "parent_doc_id": c.get("parent_doc_id", c.get("entity_id", "")),
                "entity_type": c.get("entity_type", ""),
                "trust_score": c.get("trust_score", 0.0),
            })

    entity_resolved = signal.get("entity_resolved", False)
    evidence_count = signal.get("evidence_count", 0)
    top_score = top_results[0]["similarity"] if top_results else 0.0

    return {
        "wave": 1,
        "name": "Probe",
        "status": "done",
        "latency_ms": timing.get("probe_ms", 0),
        "skipped_reason": None,
        "summary": (
            f"Intent: {intents} | Complexity: {complexity} | "
            f"Conf: {conf:.2f} | Entity: {'resolved' if entity_resolved else 'not found'}"
        ),
        "key_findings": {
            "domain": intents,
            "complexity": complexity,
            "confidence": conf,
            "sub_domains": classification.get("sub_domains", []),
            "entity_resolved": entity_resolved,
            "evidence_count": evidence_count,
            "pipelines": pipeline_data,
        },
        "vector_trace": {
            "searched": True,
            "top_score": top_score,
            "top_results": top_results,
            "total_chunks": len(knowledge_chunks or []),
            "threshold": 0.6,  # default similarity threshold used in VectorStoreService
            "chunk_types": list({r["chunk_type"] for r in top_results if r.get("chunk_type")}),
            "parent_doc_ids": list({r["parent_doc_id"] for r in top_results if r.get("parent_doc_id")}),
        },
        "raw": {k: v for k, v in pb.items() if not k.startswith("_")},
    }


def _build_wave2(
    timing,
    signal,
    pb,
    knowledge_chunks=None,
    leg_diagnostics: Optional[Dict] = None,
    neighbor_chunks: Optional[List] = None,
    module_context: Optional[Dict] = None,
) -> Dict:
    """Wave 2: Deep GraphRAG (4-leg RRF fusion + neighbor expand + module unify)."""
    deep_ms = timing.get("deep_ms", 0)
    has_deep = deep_ms > 0

    # leg_diagnostics comes from the per-pipeline attribution dict built in build_wave_trace
    _legs = leg_diagnostics or {}

    # Neighbour chunks: passed directly from orch_result.context["neighbor_chunks"]
    _nb = []
    for nc in (neighbor_chunks or [])[:5]:
        if isinstance(nc, dict):
            _nb.append({
                "entity_type": nc.get("entity_type", ""),
                "chunk_type": nc.get("chunk_type", ""),
                "section": nc.get("section", ""),
                "content": (nc.get("content", "") or "")[:150],
                "parent_doc_id": nc.get("parent_doc_id", ""),
            })

    # Ranked nodes summary (from knowledge_chunks passing through deep retrieval)
    ranked_nodes = []
    for c in (knowledge_chunks or [])[:8]:
        if isinstance(c, dict):
            ranked_nodes.append({
                "node_id": c.get("source_id", c.get("id", "")),
                "entity_type": c.get("entity_type", ""),
                "score": round(float(c.get("similarity", c.get("score", 0))), 4),
                "chunk_type": c.get("chunk_type", ""),
                "domain": c.get("domain", c.get("metadata", {}).get("domain", "")),
                "source": c.get("source", "vector_search"),
            })

    # Module context summary
    module_summary: Dict = {}
    for mod_name, mod_data in (module_context or {}).items():
        if isinstance(mod_data, dict):
            module_summary[mod_name] = {
                "sections_count": len(mod_data.get("sections", [])),
                "graph_edges": mod_data.get("graph_edges", [])[:4],
                "controllers": mod_data.get("controllers", [])[:3],
            }

    evidence_count = signal.get("evidence_count", 0)
    entity_resolved = signal.get("entity_resolved", False)

    return {
        "wave": 2,
        "name": "Deep GraphRAG",
        "status": "done" if has_deep else "skipped",
        "latency_ms": deep_ms,
        "skipped_reason": "QUICK complexity — deep retrieval not needed" if not has_deep else None,
        "summary": (
            f"4-leg RRF | {evidence_count} sources | "
            f"Tier {signal.get('tier', 1)} | "
            f"{'Entity resolved' if entity_resolved else 'Entity not found'}"
        ) if has_deep else "Not triggered (QUICK query)",
        "key_findings": {
            "evidence_count": evidence_count,
            "entity_resolved": entity_resolved,
            "tier": signal.get("tier", 1),
            "leg_diagnostics": _legs,
            "neighbor_chunks_count": len(_nb),
            "ranked_nodes_count": len(ranked_nodes),
            "module_context_keys": list(module_summary.keys()),
        },
        "vector_trace": {
            "searched": has_deep,
            "pre_vector_reused": len(knowledge_chunks or []) > 0 and has_deep,
            "ranked_nodes": ranked_nodes,
            "node_sources": list({n["source"] for n in ranked_nodes}),
            "neighbor_chunks": _nb,
            "module_context": module_summary,
            "leg_diagnostics": _legs,
        },
        "raw": None,
    }


def _build_wave3(w3_ctx, timing) -> Dict:
    """Wave 3: LangGraph stateful reasoning."""
    has_w3 = bool(w3_ctx) and isinstance(w3_ctx, dict) and len(w3_ctx) > 0

    if not has_w3:
        return {
            "wave": 3,
            "name": "LangGraph",
            "status": "skipped",
            "latency_ms": 0,
            "skipped_reason": "Wave 3 not enabled or query complexity did not require it",
            "summary": "Not triggered",
            "key_findings": {},
            "vector_trace": {"searched": False},
            "raw": None,
        }

    refined = w3_ctx.get("refined_entities", [])
    tool_plan = w3_ctx.get("tool_plan", [])
    additional_chunks = w3_ctx.get("additional_chunks", [])
    vector_hits_provided = w3_ctx.get("vector_hits_provided", 0)

    return {
        "wave": 3,
        "name": "LangGraph",
        "status": "done",
        "latency_ms": w3_ctx.get("latency_ms", 0),
        "skipped_reason": None,
        "summary": f"Refined {len(refined)} entities | Tools: {', '.join(str(t) for t in tool_plan[:5]) if tool_plan else 'none'}",
        "key_findings": {
            "refined_entities": refined,
            "tool_plan": tool_plan,
            "reasoning_trace": w3_ctx.get("reasoning_trace", ""),
        },
        "vector_trace": {
            "searched": len(additional_chunks) > 0,
            "evidence_consumed": vector_hits_provided,
            "additional_chunks_pulled": len(additional_chunks),
            "refinement_reason": w3_ctx.get("refinement_reason", ""),
        },
        "raw": w3_ctx,
    }


def _build_wave4(w4_ctx, timing) -> Dict:
    """Wave 4: Neo4j targeted graph traversal."""
    has_w4 = bool(w4_ctx) and isinstance(w4_ctx, dict) and w4_ctx.get("path_count", 0) > 0

    if not has_w4:
        return {
            "wave": 4,
            "name": "Neo4j",
            "status": "skipped",
            "latency_ms": 0,
            "skipped_reason": "Wave 4 not enabled or no refined entities from Wave 3",
            "summary": "Not triggered",
            "key_findings": {},
            "vector_trace": {"searched": False},
            "raw": None,
        }

    seed_entities = w4_ctx.get("seed_entities", [])
    bfs_start_nodes = w4_ctx.get("bfs_start_nodes", [])

    return {
        "wave": 4,
        "name": "Neo4j",
        "status": "done",
        "latency_ms": w4_ctx.get("latency_ms", 0),
        "skipped_reason": None,
        "summary": f"{w4_ctx.get('path_count', 0)} graph nodes | {w4_ctx.get('entity_targets_used', 0)} entity targets",
        "key_findings": {
            "path_count": w4_ctx.get("path_count", 0),
            "entity_targets_used": w4_ctx.get("entity_targets_used", 0),
            "relationship_context": w4_ctx.get("relationship_context", "")[:300],
        },
        "vector_trace": {
            "searched": False,
            "seed_entities": seed_entities[:10],
            "bfs_start_nodes": bfs_start_nodes[:10],
            "evidence_source": w4_ctx.get("evidence_source", "wave3_refined_entities"),
        },
        "raw": w4_ctx,
    }


def _build_wave5(riper, ralph, forge, timing, tools_used, confidence) -> Dict:
    """Wave 5: RIPER + ReAct execution."""
    riper_mode = riper.get("mode", "lite") if isinstance(riper, dict) else "unknown"
    ralph_verdict = ralph.get("verdict", "unknown") if isinstance(ralph, dict) else "unknown"
    forge_agent = forge.get("agent_name") if isinstance(forge, dict) else None

    # Extract evidence bundle details
    riper_phases = riper.get("phases", []) if isinstance(riper, dict) else []
    ralph_details = ralph if isinstance(ralph, dict) else {}

    return {
        "wave": 5,
        "name": "RIPER + ReAct",
        "status": "done" if tools_used or confidence > 0 else "error",
        "latency_ms": timing.get("llm_ms", 0),
        "skipped_reason": None,
        "summary": (
            f"RIPER {riper_mode} | {len(tools_used)} tools | "
            f"Confidence: {confidence:.2f} | RALPH: {ralph_verdict}"
        ),
        "key_findings": {
            "riper_mode": riper_mode,
            "tools_used": tools_used,
            "confidence": confidence,
            "ralph_verdict": ralph_verdict,
            "forge_agent": forge_agent,
            "phases": riper_phases,
            "completeness": ralph_details.get("completeness", {}),
        },
        "vector_trace": {
            "searched": False,
            "evidence_bundle_size": len(tools_used),
            "tool_selection_reason": forge.get("selection_reason", "") if isinstance(forge, dict) else "",
            "guardrails_fired": ralph_details.get("guardrails_fired", []),
        },
        "raw": {
            "riper": riper if isinstance(riper, dict) else {},
            "ralph": ralph if isinstance(ralph, dict) else {},
            "forge": forge if isinstance(forge, dict) else {},
        },
    }

"""
GraphRAG API endpoints for COSMOS.

Provides graph ingestion, traversal, search, and context-formatting
backed by the hybrid NetworkX + PostgreSQL GraphRAGService.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional

from app.services.graphrag import graphrag_service
from app.services.graphrag_models import (
    GraphNode,
    GraphQueryRequest,
    GraphQueryResponse,
    GraphStats,
    HybridRetrieveRequest,
    HybridRetrieveResponse,
    IngestChannelRequest,
    IngestCourierRequest,
    IngestModuleDepsRequest,
    LegDiagnostics,
    NodeType,
    QueryResult,
    RetrievedNodeResponse,
    TraversalResult,
)
from app.graph.ingest import CanonicalIngestionPipeline
from app.graph.quality import EnrichmentPipeline
from app.graph.retrieval import hybrid_retriever
from app.graph.context import ContextAssembler

router = APIRouter()


# ── Ingest endpoints ──────────────────────────────────────────────────────

@router.post("/ingest/modules", response_model=dict)
async def ingest_modules(request: IngestModuleDepsRequest):
    """Ingest module dependency edges in bulk."""
    try:
        count = await graphrag_service.ingest_module_deps(
            repo_id=request.repo_id,
            modules=request.modules,
        )
        return {"ingested": count, "repo_id": request.repo_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ingest/courier", response_model=dict)
async def ingest_courier(request: IngestCourierRequest):
    """Ingest courier <-> seller / channel relationship."""
    try:
        edges = await graphrag_service.ingest_courier_relationship(
            repo_id=request.repo_id,
            courier_id=request.courier_id,
            courier_name=request.courier_name,
            seller_id=request.seller_id,
            seller_name=request.seller_name,
            channel_id=request.channel_id,
            channel_name=request.channel_name,
            ndr_count=request.ndr_count,
            properties=request.properties,
        )
        return {"edges_created": edges, "courier_id": request.courier_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ingest/channel", response_model=dict)
async def ingest_channel(request: IngestChannelRequest):
    """Ingest channel <-> seller relationship."""
    try:
        edges = await graphrag_service.ingest_channel_relationship(
            repo_id=request.repo_id,
            channel_id=request.channel_id,
            channel_name=request.channel_name,
            seller_id=request.seller_id,
            seller_name=request.seller_name,
            properties=request.properties,
        )
        return {"edges_created": edges, "channel_id": request.channel_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Query endpoints ───────────────────────────────────────────────────────

@router.get("/query", response_model=GraphQueryResponse)
async def query_graph(
    q: str = Query(..., min_length=1, description="Search keyword"),
    repo_id: Optional[str] = Query(None, description="Filter by repository"),
    max_depth: int = Query(2, ge=0, le=5, description="BFS expansion depth"),
    limit: int = Query(20, ge=1, le=100),
):
    """Keyword search over graph nodes with BFS expansion."""
    result = await graphrag_service.query_related(
        q=q, repo_id=repo_id, max_depth=max_depth, limit=limit,
    )
    context_text = await graphrag_service.format_as_context(result)
    return GraphQueryResponse(query=q, results=result, context_text=context_text)


@router.get("/traverse/{node_id}", response_model=TraversalResult)
async def traverse_node(
    node_id: str,
    max_depth: int = Query(2, ge=0, le=5),
):
    """BFS traversal from a given node."""
    result = await graphrag_service.traverse(node_id=node_id, max_depth=max_depth)
    if result.total_nodes == 0:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return result


@router.get("/stats", response_model=GraphStats)
async def get_stats():
    """Return aggregate graph statistics."""
    return await graphrag_service.get_stats()


@router.get("/nodes", response_model=List[GraphNode])
async def find_nodes(
    node_type: Optional[NodeType] = Query(None, description="Filter by node type"),
    repo_id: Optional[str] = Query(None),
    label: Optional[str] = Query(None, description="Substring match on label"),
    limit: int = Query(50, ge=1, le=200),
):
    """Find nodes by type, repo, or label substring."""
    return await graphrag_service.find_nodes(
        node_type=node_type, repo_id=repo_id,
        label_contains=label, limit=limit,
    )


# ── KB Ingestion endpoints ────────────────────────────────────────────────

@router.post("/ingest/kb", response_model=dict)
async def ingest_knowledge_base(
    kb_path: str = Query(
        "mars/knowledge_base/",
        description="Path to knowledge_base directory",
    ),
):
    """Run the canonical KB ingestion pipeline — feeds all YAML data into the typed graph."""
    try:
        pipeline = CanonicalIngestionPipeline(kb_path=kb_path)
        report = await pipeline.ingest_all()
        return {"success": True, "report": report.to_dict()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingest/enrich", response_model=dict)
async def enrich_knowledge_base(
    kb_path: str = Query(
        "mars/knowledge_base/",
        description="Path to knowledge_base directory",
    ),
):
    """Run post-ingestion quality enrichment — adds confidence, freshness, guardrails, eval cases, edge weights."""
    try:
        pipeline = EnrichmentPipeline(kb_path=kb_path)
        report = await pipeline.enrich_all()
        return {
            "success": True,
            "report": {
                "apis_enriched": report.apis_enriched,
                "avg_confidence": report.avg_confidence,
                "avg_freshness": report.avg_freshness,
                "apis_with_eval_cases": report.apis_with_eval_cases,
                "apis_with_guardrails": report.apis_with_guardrails,
                "apis_with_evidence": report.apis_with_evidence,
                "apis_with_negative_signals": report.apis_with_negative_signals,
                "errors": report.errors[:20],
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/quality/report", response_model=dict)
async def quality_report(
    kb_path: str = Query(
        "mars/knowledge_base/",
        description="Path to knowledge_base directory",
    ),
):
    """Get quality statistics — avg confidence, freshness, coverage of eval/guardrails/evidence."""
    try:
        pipeline = EnrichmentPipeline(kb_path=kb_path)
        return await pipeline.get_quality_report()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ingest/stats", response_model=dict)
async def ingest_stats(
    kb_path: str = Query(
        "mars/knowledge_base/",
        description="Path to knowledge_base directory",
    ),
):
    """Get live node/edge/lookup counts from the graph database."""
    try:
        pipeline = CanonicalIngestionPipeline(kb_path=kb_path)
        return await pipeline.get_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/path", response_model=TraversalResult)
async def shortest_path(
    source: str = Query(..., description="Source node ID"),
    target: str = Query(..., description="Target node ID"),
):
    """Find the shortest path between two nodes."""
    result = await graphrag_service.get_shortest_path(source_id=source, target_id=target)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No path found between '{source}' and '{target}'",
        )
    return result


# ── Unified hybrid retrieval ─────────────────────────────────────────────

@router.post("/retrieve", response_model=HybridRetrieveResponse)
async def hybrid_retrieve(request: HybridRetrieveRequest):
    """Unified hybrid retrieval: 4-leg parallel search + RRF fusion + token-budgeted context.

    Legs:
      1. Exact entity lookup (AWB, order_id, api_path, tool_name)
      2. Graph neighborhood (intent/domain → 2-hop BFS)
      3. Vector similarity (pgvector cosine search)
      4. BM25 keyword (Postgres full-text search)

    Returns ranked nodes fused via Reciprocal Rank Fusion and an
    assembled context string within the specified token budget.
    """
    try:
        # Run hybrid retrieval
        retrieval = await hybrid_retriever.retrieve(
            query=request.query,
            intent=request.intent,
            entity=request.entity,
            entity_id=request.entity_id,
            repo_id=request.repo_id,
            max_depth=request.max_depth,
            top_k=request.top_k,
        )

        # Assemble context within token budget
        assembler = ContextAssembler(max_tokens=request.max_context_tokens)
        ctx = assembler.assemble(retrieval)

        # Build response
        ranked = []
        for rn in retrieval.ranked_nodes:
            ranked.append(RetrievedNodeResponse(
                id=rn.node.id,
                node_type=rn.node.node_type.value,
                label=rn.node.label,
                score=rn.score,
                sources=rn.sources,
                rank_by_leg=rn.rank_by_leg,
                repo_id=rn.node.repo_id,
                domain=rn.node.properties.get("domain"),
                properties=rn.node.properties,
            ))

        leg_diags = [
            LegDiagnostics(
                leg_name=leg.leg_name,
                hit_count=leg.hit_count,
                latency_ms=round(leg.latency_ms, 1),
            )
            for leg in retrieval.leg_results.values()
        ]

        return HybridRetrieveResponse(
            query=request.query,
            intent=request.intent,
            entity=request.entity,
            entity_id=request.entity_id,
            ranked_nodes=ranked,
            context_text=ctx.text,
            context_token_estimate=ctx.token_estimate,
            leg_diagnostics=leg_diags,
            total_latency_ms=retrieval.total_latency_ms,
            total_results=len(ranked),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

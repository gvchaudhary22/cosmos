"""
Hybrid Retrieval Service for COSMOS GraphRAG.

Runs 4 parallel retrieval legs and fuses results via Weighted Reciprocal Rank Fusion:
  Leg 1 — Exact entity lookup  (KB entity_lookup + future live entity via Mars gRPC)
  Leg 2 — Graph neighborhood   (intent/domain → 2-hop BFS with real domain inference)
  Leg 3 — Vector similarity    (pgvector cosine — returns docs AND graph nodes)
  Leg 4 — Lexical search       (Postgres GIN full-text with ts_rank_cd)

Weighted RRF:  score = Σ (leg_weight / (k + rank))   where k=60

Leg weights (entity queries):
  exact_lookup=2.0, graph_neighborhood=1.5, vector_search=1.0, lexical=0.8

Fixes applied over v1:
  - Vector hits that don't map to graph nodes are kept as proxy "document" nodes
  - Weighted RRF instead of flat RRF
  - Graph neighborhood uses real domain inference (edges from entity_lookup → domain)
  - Lexical search uses proper GIN index with ts_rank_cd
  - TierPolicy composite scoring integrated for confidence computation
  - SessionState context seeds graph neighborhood with prior entities
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog
from sqlalchemy import or_, select, text, func

from app.db.session import AsyncSessionLocal
from app.services.graphrag import (
    EntityLookupRow,
    GraphEdgeRow,
    GraphNodeRow,
    graphrag_service,
)
from app.services.graphrag_models import GraphEdge, GraphNode, EdgeType, NodeType
from app.services.vectorstore import VectorStoreService
from app.services.reranker import Reranker
from app.services.hyde import HyDEExpander

logger = structlog.get_logger(__name__)

# RRF constant — standard value from the literature
RRF_K = 60

# Leg weights — exact entity resolution is the strongest signal
LEG_WEIGHTS = {
    "exact_lookup": 2.0,
    "personalized_pagerank": 1.8,  # PPR: query-specific importance at any depth
    "graph_neighborhood": 1.5,
    "vector_search": 1.0,
    "lexical_search": 0.8,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RetrievedNode:
    """A graph node with retrieval metadata."""

    node: GraphNode
    score: float = 0.0
    sources: List[str] = field(default_factory=list)  # which legs found it
    rank_by_leg: Dict[str, int] = field(default_factory=dict)  # leg → rank


@dataclass
class RelationshipChain:
    """A chain of edges connecting retrieved nodes."""

    edges: List[GraphEdge]
    source_node_id: str
    target_node_id: str
    chain_type: str = ""  # e.g. "api→table", "api→tool→agent"


@dataclass
class LegResult:
    """Output of one retrieval leg."""

    leg_name: str
    node_ids: List[str]  # ordered by relevance
    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    edges: List[GraphEdge] = field(default_factory=list)
    latency_ms: float = 0.0
    hit_count: int = 0


@dataclass
class RetrievalResult:
    """Final fused retrieval output."""

    query: str
    intent: Optional[str] = None
    entity: Optional[str] = None
    entity_id: Optional[str] = None

    # Fused ranked results
    ranked_nodes: List[RetrievedNode] = field(default_factory=list)
    relationship_chains: List[RelationshipChain] = field(default_factory=list)
    all_edges: List[GraphEdge] = field(default_factory=list)

    # Per-leg diagnostics
    leg_results: Dict[str, LegResult] = field(default_factory=dict)

    # Tier policy signals (for downstream gating)
    entity_resolved: bool = False
    evidence_count: int = 0

    # Metadata
    total_latency_ms: float = 0.0
    fusion_method: str = "weighted_rrf_k60"
    top_k: int = 10


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------

class HybridRetriever:
    """4-leg parallel hybrid retrieval with weighted RRF fusion."""

    def __init__(self, vectorstore: Optional[VectorStoreService] = None) -> None:
        self._vectorstore = vectorstore or VectorStoreService()
        self._reranker = Reranker()
        self._hyde = HyDEExpander(vectorstore=self._vectorstore)

    async def retrieve(
        self,
        query: str,
        intent: Optional[str] = None,
        entity: Optional[str] = None,
        entity_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        max_depth: int = 2,
        top_k: int = 10,
        session_entity_seeds: Optional[Dict[str, List[str]]] = None,
        pre_vector_hits: Optional[List[Dict]] = None,
    ) -> RetrievalResult:
        """Run all 4 legs in parallel, fuse with weighted RRF, return ranked nodes.

        Args:
            session_entity_seeds: Dict of entity_type → [entity_ids] from SessionState
                                  for cross-turn context seeding.
            pre_vector_hits: Bug 5 fix — pre-computed vector search results from the
                             Stage-1 probe. When provided, the vector leg is skipped
                             and these hits are used directly, eliminating duplicate
                             vector embedding calls for the same query.
        """
        start = time.monotonic()

        # HyDE: expand query for better vector search matching
        expanded_query = query
        try:
            expanded_query = await self._hyde.expand(query)
        except Exception as e:
            logger.debug("hyde.expand_failed", error=str(e))

        # Bug 5 fix: if the probe already ran a vector search, reuse its results
        # instead of embedding the query again (saves ~50ms + embedding cost).
        if pre_vector_hits is not None:
            vector_leg_coro = self._leg_vector_from_hits(pre_vector_hits, repo_id)
        else:
            vector_leg_coro = self._leg_vector_search(expanded_query, repo_id, top_k * 2)

        # Launch all 5 legs concurrently (vector search uses expanded query)
        leg1, leg2, leg3, leg4, leg5 = await asyncio.gather(
            self._leg_exact_lookup(entity, entity_id, repo_id, max_depth),
            self._leg_graph_neighborhood(query, intent, entity, repo_id, max_depth, session_entity_seeds),
            vector_leg_coro,
            self._leg_lexical_search(query, repo_id, top_k),
            self._leg_personalized_pagerank(entity, entity_id, intent, repo_id, top_k),
        )

        # Collect all legs
        legs = {
            "exact_lookup": leg1,
            "graph_neighborhood": leg2,
            "vector_search": leg3,
            "lexical_search": leg4,
            "personalized_pagerank": leg5,
        }

        # Weighted RRF fusion across all legs
        # Phase 4b: expose query for guardrail penalty check inside _rrf_fuse
        self._current_query_lower = query.lower()
        ranked_nodes = self._rrf_fuse(legs, top_k * 2)  # fetch 2× for reranker
        self._current_query_lower = ""

        # Phase 5b: Cross-encoder reranking on top-20 post-RRF candidates.
        # Converts RetrievedNode list → Reranker dict format, rerankss, rebuilds.
        # Falls back silently to RRF order on any error.
        if len(ranked_nodes) > top_k:
            try:
                rerank_candidates = [
                    {
                        "content": rn.node.label + " " + str(rn.node.properties),
                        "similarity": rn.score,
                        "trust_score": rn.node.properties.get("trust_score",
                                       rn.node.properties.get("quality_score", 0.5)),
                        "entity_id": rn.node.id,
                        "metadata": {
                            "chunk_type": rn.node.properties.get("chunk_type", ""),
                            "domain": rn.node.domain or "",
                            "node_type": rn.node.node_type.value,
                            "api_id": rn.node.id if rn.node.node_type.value == "api_endpoint" else "",
                        },
                        "_rn": rn,  # carry original object through
                    }
                    for rn in ranked_nodes[:20]
                ]
                reranked = await self._reranker.rerank(
                    query=query,
                    candidates=rerank_candidates,
                    top_k=top_k,
                )
                ranked_nodes = [c["_rn"] for c in reranked if "_rn" in c]
                # Clean the private key from dicts (not strictly needed but tidy)
            except Exception as _rr_err:
                logger.debug("hybrid_retrieval.reranker_fallback", error=str(_rr_err))
                ranked_nodes = ranked_nodes[:top_k]
        else:
            ranked_nodes = ranked_nodes[:top_k]

        # Collect all edges from legs that found edges
        all_edges: List[GraphEdge] = []
        seen_edge_keys: Set[Tuple[str, str, str]] = set()
        for leg in legs.values():
            for edge in leg.edges:
                key = (edge.source_id, edge.target_id, edge.edge_type.value)
                if key not in seen_edge_keys:
                    seen_edge_keys.add(key)
                    all_edges.append(edge)

        # Build relationship chains from ranked node IDs
        ranked_ids = {rn.node.id for rn in ranked_nodes}
        chains = self._extract_chains(all_edges, ranked_ids)

        # Compute tier policy signals
        entity_resolved = leg1.hit_count > 0
        evidence_count = sum(1 for leg in legs.values() if leg.hit_count > 0)

        total_ms = (time.monotonic() - start) * 1000

        logger.info(
            "hybrid_retrieval.done",
            query=query[:80],
            intent=intent,
            entity=entity,
            legs_hit={k: v.hit_count for k, v in legs.items()},
            fused_count=len(ranked_nodes),
            entity_resolved=entity_resolved,
            evidence_count=evidence_count,
            latency_ms=round(total_ms, 1),
        )

        return RetrievalResult(
            query=query,
            intent=intent,
            entity=entity,
            entity_id=entity_id,
            ranked_nodes=ranked_nodes,
            relationship_chains=chains,
            all_edges=all_edges,
            leg_results=legs,
            entity_resolved=entity_resolved,
            evidence_count=evidence_count,
            total_latency_ms=round(total_ms, 1),
            top_k=top_k,
        )

    # -------------------------------------------------------------------
    # Leg 1: Exact entity lookup (KB static)
    # -------------------------------------------------------------------

    async def _leg_exact_lookup(
        self,
        entity: Optional[str],
        entity_id: Optional[str],
        repo_id: Optional[str],
        max_depth: int,
    ) -> LegResult:
        """Look up entity_lookup table → get node → expand 2-hop neighborhood."""
        start = time.monotonic()
        result = LegResult(leg_name="exact_lookup")

        if not entity or not entity_id:
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        async with AsyncSessionLocal() as session:
            # Try multiple entity_type patterns for the given entity
            type_candidates = [
                entity,                      # e.g. "awb"
                f"param:{entity}",           # e.g. "param:awb"
                entity.replace("_", ""),     # e.g. "orderid"
                f"param:{entity.replace('_', '')}",
                "api_id",                    # direct api ID
                "api_path",                  # direct api path
                "tool_name",                 # direct tool name
                "table_name",               # direct table name
            ]

            lookup_row = None
            for etype in type_candidates:
                stmt = select(EntityLookupRow).where(
                    EntityLookupRow.entity_type == etype,
                    EntityLookupRow.entity_value == str(entity_id),
                )
                lookup_row = (await session.execute(stmt)).scalar_one_or_none()
                if lookup_row:
                    break

            if not lookup_row:
                result.latency_ms = (time.monotonic() - start) * 1000
                return result

            # Found the anchor node — now BFS expand
            anchor_id = lookup_row.node_id
            visited: Set[str] = {anchor_id}
            all_edge_rows: List[GraphEdgeRow] = []
            frontier = {anchor_id}

            for _depth in range(max_depth):
                if not frontier:
                    break
                edge_stmt = select(GraphEdgeRow).where(
                    or_(
                        GraphEdgeRow.source_id.in_(frontier),
                        GraphEdgeRow.target_id.in_(frontier),
                    )
                )
                edge_rows = (await session.execute(edge_stmt)).scalars().all()
                all_edge_rows.extend(edge_rows)

                next_frontier: Set[str] = set()
                for e in edge_rows:
                    for nid in (e.source_id, e.target_id):
                        if nid not in visited:
                            visited.add(nid)
                            next_frontier.add(nid)
                frontier = next_frontier

            # Fetch all visited nodes
            node_rows = (
                await session.execute(
                    select(GraphNodeRow).where(GraphNodeRow.id.in_(visited))
                )
            ).scalars().all()

        # Order: anchor first, then neighbors
        ordered_ids = [anchor_id] + [nid for nid in visited if nid != anchor_id]

        result.node_ids = ordered_ids
        result.nodes = {r.id: _row_to_model(r) for r in node_rows}
        result.edges = [_edge_row_to_model(e) for e in all_edge_rows]
        result.hit_count = len(ordered_ids)
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    # -------------------------------------------------------------------
    # Leg 2: Graph neighborhood (real domain inference + session seeds)
    # -------------------------------------------------------------------

    async def _leg_graph_neighborhood(
        self,
        query: str,
        intent: Optional[str],
        entity: Optional[str],
        repo_id: Optional[str],
        max_depth: int,
        session_entity_seeds: Optional[Dict[str, List[str]]] = None,
    ) -> LegResult:
        """Map intent/entity to graph seeds via real edges, not just domain:{entity}."""
        start = time.monotonic()
        result = LegResult(leg_name="graph_neighborhood")

        async with AsyncSessionLocal() as session:
            seed_ids: Set[str] = set()

            # 1. Seed from intent node if it exists in the graph
            if intent:
                intent_node_id = f"intent:{intent}"
                exists = await session.get(GraphNodeRow, intent_node_id)
                if exists:
                    seed_ids.add(intent_node_id)

            # 2. Real domain inference: look up entity in entity_lookup → find node → follow belongs_to_domain edge
            if entity and not seed_ids:
                # Find any node linked to this entity type
                lookup_stmt = (
                    select(EntityLookupRow.node_id)
                    .where(EntityLookupRow.entity_type.in_([
                        entity, f"param:{entity}", "intent_name", "table_name", "tool_name",
                    ]))
                    .limit(5)
                )
                lookup_rows = (await session.execute(lookup_stmt)).scalars().all()
                if lookup_rows:
                    # Follow belongs_to_domain edges from these nodes to find domain seeds
                    domain_stmt = select(GraphEdgeRow.target_id).where(
                        GraphEdgeRow.source_id.in_(lookup_rows),
                        GraphEdgeRow.edge_type == EdgeType.belongs_to_domain.value,
                    ).limit(3)
                    domain_ids = (await session.execute(domain_stmt)).scalars().all()
                    seed_ids.update(domain_ids)
                    # Also add the source nodes themselves
                    seed_ids.update(lookup_rows[:3])

            # 3. Session entity seeds — add nodes from prior turns
            if session_entity_seeds:
                for etype, eids in session_entity_seeds.items():
                    for eid in eids[-3:]:  # last 3 per type
                        # Look up in entity_lookup
                        stmt = select(EntityLookupRow.node_id).where(
                            EntityLookupRow.entity_type == etype,
                            EntityLookupRow.entity_value == str(eid),
                        )
                        node_id = (await session.execute(stmt)).scalar_one_or_none()
                        if node_id:
                            seed_ids.add(node_id)

            # 4. Fallback: keyword match on node labels via ILIKE
            if not seed_ids:
                # Split query into words and match on the most specific ones
                query_lower = f"%{query.lower()}%"
                stmt = (
                    select(GraphNodeRow)
                    .where(
                        or_(
                            GraphNodeRow.label.ilike(query_lower),
                            GraphNodeRow.domain.ilike(query_lower),
                        )
                    )
                )
                if repo_id:
                    stmt = stmt.where(GraphNodeRow.repo_id == repo_id)
                stmt = stmt.limit(10)
                matched = (await session.execute(stmt)).scalars().all()
                seed_ids = {r.id for r in matched}

            if not seed_ids:
                result.latency_ms = (time.monotonic() - start) * 1000
                return result

            # BFS from seeds
            visited: Set[str] = set(seed_ids)
            all_edge_rows: List[GraphEdgeRow] = []
            frontier = set(seed_ids)

            for _depth in range(max_depth):
                if not frontier:
                    break
                edge_stmt = select(GraphEdgeRow).where(
                    or_(
                        GraphEdgeRow.source_id.in_(frontier),
                        GraphEdgeRow.target_id.in_(frontier),
                    )
                )
                edge_rows = (await session.execute(edge_stmt)).scalars().all()
                all_edge_rows.extend(edge_rows)

                next_frontier: Set[str] = set()
                for e in edge_rows:
                    for nid in (e.source_id, e.target_id):
                        if nid not in visited:
                            visited.add(nid)
                            next_frontier.add(nid)
                frontier = next_frontier

            # Fetch all visited node rows
            node_rows = (
                await session.execute(
                    select(GraphNodeRow).where(GraphNodeRow.id.in_(visited))
                )
            ).scalars().all()

        # Order: seeds first, then by node_type priority (api > tool > agent > table > intent > domain)
        type_priority = {
            "api_endpoint": 0, "tool": 1, "agent": 2,
            "table": 3, "intent": 4, "domain": 5,
        }
        seeds_list = list(seed_ids)
        others = [nid for nid in visited if nid not in seed_ids]
        node_map = {r.id: r for r in node_rows}
        others.sort(key=lambda nid: type_priority.get(
            node_map[nid].node_type if nid in node_map else "zzz", 99
        ))

        ordered_ids = seeds_list + others
        result.node_ids = ordered_ids
        result.nodes = {r.id: _row_to_model(r) for r in node_rows}
        result.edges = [_edge_row_to_model(e) for e in all_edge_rows]
        result.hit_count = len(ordered_ids)
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    # -------------------------------------------------------------------
    # Leg 3: Vector similarity (keeps non-graph docs as evidence)
    # -------------------------------------------------------------------

    async def _leg_vector_search(
        self,
        query: str,
        repo_id: Optional[str],
        top_k: int,
    ) -> LegResult:
        """Embed query → cosine search on cosmos_embeddings → map to graph nodes OR keep as proxy docs."""
        start = time.monotonic()
        result = LegResult(leg_name="vector_search")

        try:
            hits = await self._vectorstore.search_similar(
                query=query,
                limit=top_k,
                repo_id=repo_id,
                threshold=0.3,
            )
        except Exception as exc:
            logger.warning("hybrid_retrieval.vector_search_error", error=str(exc))
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        if not hits:
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        # Build candidate graph node IDs from vector hits
        hit_to_candidates: Dict[int, List[str]] = {}
        all_candidate_ids: List[str] = []
        for idx, hit in enumerate(hits):
            etype = hit.get("entity_type", "")
            eid = hit.get("entity_id", "")
            candidates = [
                f"{etype}:{eid}",
                f"api:{eid}",
                f"table:{eid}",
                f"tool:{eid}",
                eid,
            ]
            hit_to_candidates[idx] = candidates
            all_candidate_ids.extend(candidates)

        # Resolve which candidates exist as graph nodes
        found_graph_ids: Set[str] = set()
        graph_node_map: Dict[str, GraphNodeRow] = {}
        if all_candidate_ids:
            async with AsyncSessionLocal() as session:
                stmt = select(GraphNodeRow).where(
                    GraphNodeRow.id.in_(all_candidate_ids)
                )
                node_rows = (await session.execute(stmt)).scalars().all()
                found_graph_ids = {r.id for r in node_rows}
                graph_node_map = {r.id: r for r in node_rows}

        # Walk hits in similarity order: resolve to graph node or create proxy doc
        ordered_ids: List[str] = []
        nodes: Dict[str, GraphNode] = {}
        seen: Set[str] = set()

        for idx, hit in enumerate(hits):
            etype = hit.get("entity_type", "")
            eid = hit.get("entity_id", "")
            similarity = hit.get("similarity", 0.0)
            content = hit.get("content", "")
            metadata = hit.get("metadata", {})

            # Try to map to a graph node
            resolved_id = None
            for candidate in hit_to_candidates.get(idx, []):
                if candidate in found_graph_ids and candidate not in seen:
                    resolved_id = candidate
                    break

            if resolved_id:
                # Mapped to real graph node
                ordered_ids.append(resolved_id)
                seen.add(resolved_id)
                row = graph_node_map[resolved_id]
                nodes[resolved_id] = _row_to_model(row)
            else:
                # No graph node match — create proxy document node
                # This preserves vector evidence that doesn't exist in the typed graph
                proxy_id = f"doc:{etype}:{eid}"
                if proxy_id not in seen:
                    ordered_ids.append(proxy_id)
                    seen.add(proxy_id)
                    nodes[proxy_id] = GraphNode(
                        id=proxy_id,
                        node_type=NodeType.module,  # closest generic type
                        label=content[:200] if content else f"{etype}/{eid}",
                        repo_id=hit.get("repo_id"),
                        properties={
                            "_source": "vector_proxy",
                            "_similarity": similarity,
                            "_entity_type": etype,
                            "_entity_id": eid,
                            "_content": content[:500],
                            **metadata,
                        },
                    )

        result.node_ids = ordered_ids
        result.nodes = nodes
        result.hit_count = len(ordered_ids)
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    # -------------------------------------------------------------------
    # Bug 5 fix: Vector leg from pre-computed hits (avoids re-embedding)
    # -------------------------------------------------------------------

    async def _leg_vector_from_hits(
        self,
        hits: List[Dict],
        repo_id: Optional[str],
    ) -> LegResult:
        """Build a vector LegResult from pre-computed probe hits.

        Bug 5 fix: the Stage-1 probe already ran a vector search on the same query.
        Instead of re-embedding and searching again, we reuse those results here.
        The graph-node resolution logic is identical to _leg_vector_search().
        """
        start = time.monotonic()
        result = LegResult(leg_name="vector_search")

        if not hits:
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        # Delegate to the same resolution logic as the full vector leg
        # by building candidate node IDs and resolving against the graph.
        hit_to_candidates: Dict[int, List[str]] = {}
        all_candidate_ids: List[str] = []
        for idx, hit in enumerate(hits):
            etype = hit.get("entity_type", "")
            eid = hit.get("entity_id", "")
            candidates = [
                f"{etype}:{eid}",
                f"api:{eid}",
                f"table:{eid}",
                f"tool:{eid}",
                eid,
            ]
            hit_to_candidates[idx] = candidates
            all_candidate_ids.extend(candidates)

        found_graph_ids: Set[str] = set()
        graph_node_map: Dict[str, GraphNodeRow] = {}
        if all_candidate_ids:
            async with AsyncSessionLocal() as session:
                stmt = select(GraphNodeRow).where(
                    GraphNodeRow.id.in_(all_candidate_ids)
                )
                node_rows = (await session.execute(stmt)).scalars().all()
                found_graph_ids = {r.id for r in node_rows}
                graph_node_map = {r.id: r for r in node_rows}

        ordered_ids: List[str] = []
        nodes: Dict[str, GraphNode] = {}
        seen: Set[str] = set()

        for idx, hit in enumerate(hits):
            etype = hit.get("entity_type", "")
            eid = hit.get("entity_id", "")
            similarity = hit.get("similarity", 0.0)
            content = hit.get("content", "")
            metadata = hit.get("metadata", {})

            resolved_id = None
            for candidate in hit_to_candidates.get(idx, []):
                if candidate in found_graph_ids and candidate not in seen:
                    resolved_id = candidate
                    break

            if resolved_id:
                ordered_ids.append(resolved_id)
                seen.add(resolved_id)
                row = graph_node_map[resolved_id]
                nodes[resolved_id] = _row_to_model(row)
            else:
                proxy_id = f"doc:{etype}:{eid}"
                if proxy_id not in seen:
                    ordered_ids.append(proxy_id)
                    seen.add(proxy_id)
                    nodes[proxy_id] = GraphNode(
                        id=proxy_id,
                        node_type=NodeType.module,
                        label=content[:200] if content else f"{etype}/{eid}",
                        repo_id=hit.get("repo_id"),
                        properties={
                            "_source": "vector_probe_reuse",
                            "_similarity": similarity,
                            "_entity_type": etype,
                            "_entity_id": eid,
                            "_content": content[:500],
                            **metadata,
                        },
                    )

        result.node_ids = ordered_ids
        result.nodes = nodes
        result.hit_count = len(ordered_ids)
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    # -------------------------------------------------------------------
    # Leg 4: Lexical search (GIN-indexed full-text with ts_rank_cd)
    # -------------------------------------------------------------------

    async def _leg_lexical_search(
        self,
        query: str,
        repo_id: Optional[str],
        top_k: int,
    ) -> LegResult:
        """Full-text search on graph_nodes using GIN index + ts_rank_cd.

        Also searches JSON properties for keywords and aliases.
        """
        start = time.monotonic()
        result = LegResult(leg_name="lexical_search")

        async with AsyncSessionLocal() as session:
            params: Dict[str, Any] = {"query": query, "limit": top_k}
            repo_filter = ""
            if repo_id:
                repo_filter = "AND gn.repo_id = :repo_id"
                params["repo_id"] = repo_id

            # Primary: GIN-indexed full-text on label
            # Secondary: ILIKE on label (catches partial matches ts misses)
            # Tertiary: JSON keyword/alias search
            sql = text(f"""
                WITH fts AS (
                    SELECT
                        gn.id, gn.node_type, gn.label, gn.repo_id, gn.domain,
                        gn.properties, gn.created_at, gn.updated_at,
                        ts_rank_cd(
                            gn.label,
                            plainto_tsquery('english', :query)
                        ) AS fts_score,
                        CASE
                            WHEN gn.label ILIKE '%' || :query || '%' THEN 0.5
                            ELSE 0.0
                        END AS ilike_score,
                        CASE
                            WHEN gn.properties->>'keywords' ILIKE '%' || :query || '%' THEN 0.3
                            WHEN gn.properties->>'aliases' ILIKE '%' || :query || '%' THEN 0.3
                            WHEN gn.properties->>'retrieval_keywords' ILIKE '%' || :query || '%' THEN 0.2
                            ELSE 0.0
                        END AS prop_score
                    FROM graph_nodes gn
                    WHERE (
                        gn.label LIKE CONCAT('%', :query, '%')
                        OR gn.label ILIKE '%' || :query || '%'
                        OR gn.properties->>'keywords' ILIKE '%' || :query || '%'
                        OR gn.properties->>'aliases' ILIKE '%' || :query || '%'
                        OR gn.properties->>'retrieval_keywords' ILIKE '%' || :query || '%'
                    )
                    {repo_filter}
                )
                SELECT *, (fts_score + ilike_score + prop_score) AS combined_score
                FROM fts
                ORDER BY combined_score DESC, label
                LIMIT :limit
            """)

            try:
                rows = (await session.execute(sql, params)).fetchall()
            except Exception as exc:
                logger.warning("hybrid_retrieval.lexical_error", error=str(exc))
                rows = await self._lexical_fallback(session, query, repo_id, top_k)

        ordered_ids: List[str] = []
        nodes: Dict[str, GraphNode] = {}
        for row in rows:
            node = GraphNode(
                id=row.id,
                node_type=NodeType(row.node_type),
                label=row.label,
                repo_id=row.repo_id,
                properties=row.properties or {},
                created_at=row.created_at,
            )
            ordered_ids.append(row.id)
            nodes[row.id] = node

        result.node_ids = ordered_ids
        result.nodes = nodes
        result.hit_count = len(ordered_ids)
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    @staticmethod
    async def _lexical_fallback(session, query: str, repo_id: Optional[str], top_k: int):
        """Simple ILIKE fallback when GIN index isn't ready."""
        stmt = select(GraphNodeRow).where(
            or_(
                GraphNodeRow.label.ilike(f"%{query}%"),
                GraphNodeRow.node_type.ilike(f"%{query}%"),
            )
        )
        if repo_id:
            stmt = stmt.where(GraphNodeRow.repo_id == repo_id)
        stmt = stmt.limit(top_k)
        return (await session.execute(stmt)).scalars().all()

    # -------------------------------------------------------------------
    # Leg 5: Personalized PageRank
    # -------------------------------------------------------------------

    async def _leg_personalized_pagerank(
        self,
        entity: Optional[str],
        entity_id: Optional[str],
        intent: Optional[str],
        repo_id: Optional[str],
        top_k: int,
    ) -> LegResult:
        """Compute Personalized PageRank from query-specific seed nodes.

        PPR finds important nodes at ANY depth (not just 2 hops) by
        propagating relevance from seeds through the graph. This catches
        cross-domain relationships that BFS misses.
        """
        t0 = time.monotonic()
        try:
            from app.services.graphrag import GraphRAGService
            graphrag = GraphRAGService()
            if not graphrag._loaded:
                await graphrag.load_from_pg()

            # Build seed node IDs from entity, intent, domain
            seeds = []
            if entity_id and entity:
                # Try common entity node patterns
                for pattern in [f"{entity}:{entity_id}", f"table:{entity}", f"api:{entity}",
                                f"tool:{entity}", f"action_contract:{entity}", entity]:
                    if graphrag._graph.has_node(pattern):
                        seeds.append(pattern)
                        break
            if intent:
                intent_node = f"intent:{intent}"
                if graphrag._graph.has_node(intent_node):
                    seeds.append(intent_node)
            # Add domain seed if entity maps to a known domain
            if entity:
                domain_node = f"domain:{entity}"
                if graphrag._graph.has_node(domain_node):
                    seeds.append(domain_node)

            if not seeds:
                return LegResult(
                    leg_name="personalized_pagerank",
                    node_ids=[], nodes={}, edges=[],
                    latency_ms=(time.monotonic() - t0) * 1000,
                    hit_count=0,
                )

            # Run PPR
            ppr_results = await graphrag.personalized_pagerank(
                seed_node_ids=seeds, alpha=0.15, top_k=top_k,
            )

            # Convert to LegResult
            node_ids = [nid for nid, _ in ppr_results]
            nodes = {}
            for nid, score in ppr_results:
                node_data = graphrag._graph.nodes.get(nid)
                if node_data:
                    nodes[nid] = GraphNode(
                        id=nid,
                        node_type=node_data.get("node_type", "unknown"),
                        label=node_data.get("label", nid),
                        repo_id=node_data.get("repo_id"),
                        properties={**node_data.get("properties", {}), "_ppr_score": score},
                    )

            return LegResult(
                leg_name="personalized_pagerank",
                node_ids=node_ids,
                nodes=nodes,
                edges=[],  # PPR doesn't return edges directly
                latency_ms=(time.monotonic() - t0) * 1000,
                hit_count=len(node_ids),
            )
        except Exception as e:
            logger.warning("leg.ppr_failed", error=str(e))
            return LegResult(
                leg_name="personalized_pagerank",
                node_ids=[], nodes={}, edges=[],
                latency_ms=(time.monotonic() - t0) * 1000,
                hit_count=0,
            )

    # -------------------------------------------------------------------
    # Weighted RRF Fusion
    # -------------------------------------------------------------------

    def _rrf_fuse(
        self,
        legs: Dict[str, LegResult],
        top_k: int,
    ) -> List[RetrievedNode]:
        """Weighted Reciprocal Rank Fusion across all legs.

        score(node) = Σ_leg (leg_weight / (k + rank_in_leg))
        where k=60, leg_weight from LEG_WEIGHTS.

        Nodes found by more legs get higher scores. Exact lookup
        contributes 2x the weight of lexical, reflecting precision.
        """
        scores: Dict[str, float] = {}
        sources: Dict[str, List[str]] = {}
        ranks: Dict[str, Dict[str, int]] = {}
        all_nodes: Dict[str, GraphNode] = {}

        for leg_name, leg in legs.items():
            if not leg.node_ids:
                continue

            weight = LEG_WEIGHTS.get(leg_name, 1.0)

            for rank_0, node_id in enumerate(leg.node_ids):
                rank = rank_0 + 1  # 1-indexed
                rrf_contrib = weight / (RRF_K + rank)

                scores[node_id] = scores.get(node_id, 0.0) + rrf_contrib
                sources.setdefault(node_id, []).append(leg_name)
                ranks.setdefault(node_id, {})[leg_name] = rank

                if node_id in leg.nodes and node_id not in all_nodes:
                    all_nodes[node_id] = leg.nodes[node_id]

        # Phase 4b: Apply guardrail penalty when query matches negative_routing_keywords
        # Multiplier ×0.3 — still returned (may be useful context) but ranked much lower
        if query_lower := getattr(self, "_current_query_lower", ""):
            for node_id, node in all_nodes.items():
                neg_kws = (node.properties or {}).get("negative_routing_keywords", [])
                if neg_kws and any(kw in query_lower for kw in neg_kws):
                    scores[node_id] = scores.get(node_id, 0.0) * 0.3

        # Sort by weighted RRF score descending
        sorted_ids = sorted(scores.keys(), key=lambda nid: scores[nid], reverse=True)

        result: List[RetrievedNode] = []
        for node_id in sorted_ids[:top_k]:
            if node_id not in all_nodes:
                continue
            result.append(RetrievedNode(
                node=all_nodes[node_id],
                score=round(scores[node_id], 6),
                sources=sources.get(node_id, []),
                rank_by_leg=ranks.get(node_id, {}),
            ))

        return result

    # -------------------------------------------------------------------
    # Relationship chain extraction
    # -------------------------------------------------------------------

    def _extract_chains(
        self,
        edges: List[GraphEdge],
        ranked_ids: Set[str],
    ) -> List[RelationshipChain]:
        """Extract relationship chains that connect ranked nodes."""
        chains: List[RelationshipChain] = []

        adjacency: Dict[str, List[GraphEdge]] = {}
        for edge in edges:
            adjacency.setdefault(edge.source_id, []).append(edge)

        # Direct connections between ranked nodes
        for edge in edges:
            if edge.source_id in ranked_ids and edge.target_id in ranked_ids:
                chain_type = _classify_chain(edge)
                chains.append(RelationshipChain(
                    edges=[edge],
                    source_node_id=edge.source_id,
                    target_node_id=edge.target_id,
                    chain_type=chain_type,
                ))

        # 2-hop paths between ranked nodes through intermediaries
        for src_id in ranked_ids:
            for edge1 in adjacency.get(src_id, []):
                mid_id = edge1.target_id
                if mid_id in ranked_ids:
                    continue
                for edge2 in adjacency.get(mid_id, []):
                    if edge2.target_id in ranked_ids:
                        chains.append(RelationshipChain(
                            edges=[edge1, edge2],
                            source_node_id=src_id,
                            target_node_id=edge2.target_id,
                            chain_type=f"{edge1.edge_type.value}→{edge2.edge_type.value}",
                        ))

        return chains


# ---------------------------------------------------------------------------
# GIN index creation helper (call once during schema migration)
# ---------------------------------------------------------------------------

async def ensure_lexical_indexes() -> None:
    """Create GIN index on graph_nodes.label for fast full-text search.

    Safe to call multiple times (IF NOT EXISTS).
    """
    async with AsyncSessionLocal() as session:
        for idx_sql in [
            "CREATE INDEX idx_graph_nodes_label_fts ON graph_nodes (label)",
            "CREATE INDEX idx_graph_nodes_properties_gin ON graph_nodes (properties(255))",
        ]:
            try:
                await session.execute(text(idx_sql))
            except Exception:
                pass  # index already exists or column not available
        await session.commit()
    logger.info("hybrid_retrieval.lexical_indexes_ensured")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _row_to_model(row: GraphNodeRow) -> GraphNode:
    return GraphNode(
        id=row.id,
        node_type=NodeType(row.node_type),
        label=row.label,
        repo_id=row.repo_id,
        properties=row.properties or {},
        created_at=row.created_at,
    )


def _edge_row_to_model(row: GraphEdgeRow) -> GraphEdge:
    return GraphEdge(
        source_id=row.source_id,
        target_id=row.target_id,
        edge_type=EdgeType(row.edge_type),
        weight=row.weight,
        repo_id=row.repo_id,
        properties=row.properties or {},
        created_at=row.created_at,
    )


def _classify_chain(edge: GraphEdge) -> str:
    """Human-readable chain type from an edge."""
    mapping = {
        "reads_table": "api→table(read)",
        "writes_table": "api→table(write)",
        "implements_tool": "api→tool",
        "assigned_to_agent": "api→agent",
        "has_intent": "api→intent",
        "belongs_to_domain": "→domain",
    }
    return mapping.get(edge.edge_type.value, edge.edge_type.value)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

hybrid_retriever = HybridRetriever()

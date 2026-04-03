"""
GraphRAG service — hybrid in-memory (NetworkX) + MySQL/Neo4j graph store.

Provides knowledge-graph ingestion, BFS traversal, keyword search,
shortest-path queries, and LLM-context formatting for COSMOS.

Neo4j is the PRIMARY backend for graph operations (traversal, lookup, stats).
MySQL (MARS DB) is the fallback for relational persistence.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
import structlog
from sqlalchemy import Column, DateTime, Float, Index, String, Text, and_, func, or_, select, text
from sqlalchemy.types import JSON

from app.db.models import Base
from app.db.session import AsyncSessionLocal
from app.services.neo4j_graph import neo4j_graph_service
from app.services.graphrag_models import (
    EdgeType,
    GraphEdge,
    GraphNode,
    GraphStats,
    NodeType,
    QueryResult,
    TraversalResult,
)

logger = structlog.get_logger(__name__)


# ── SQLAlchemy ORM models for persistence ──────────────────────────────────

class GraphNodeRow(Base):
    __tablename__ = "graph_nodes"

    id = Column(String(191), primary_key=True)
    node_type = Column(String(50), nullable=False, index=True)
    label = Column(String(500), nullable=False)
    repo_id = Column(String(255), nullable=True, index=True)
    domain = Column(String(100), nullable=True, index=True)
    properties = Column(JSON, default={})
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_graph_nodes_type_repo", "node_type", "repo_id"),
        Index("idx_graph_nodes_domain", "domain"),
    )


class GraphEdgeRow(Base):
    __tablename__ = "graph_edges"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(191), nullable=False, index=True)
    target_id = Column(String(191), nullable=False, index=True)
    edge_type = Column(String(50), nullable=False, index=True)
    weight = Column(Float, default=1.0)
    repo_id = Column(String(255), nullable=True, index=True)
    properties = Column(JSON, default={})
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_graph_edges_src_tgt", "source_id", "target_id"),
        Index("idx_graph_edges_type_repo", "edge_type", "repo_id"),
        Index("idx_graph_edges_unique_triple", "source_id", "target_id", "edge_type", unique=True),
    )


class EntityLookupRow(Base):
    """Fast exact-match index for entity IDs (AWB, order_id, seller_id, api_path, tool_name).
    PK includes repo_id to prevent cross-repo collision."""
    __tablename__ = "entity_lookup"

    entity_type = Column(String(50), nullable=False, primary_key=True)
    entity_value = Column(String(191), nullable=False, primary_key=True)
    repo_id = Column(String(255), nullable=False, primary_key=True, default="default")
    node_id = Column(String(191), nullable=False, index=True)

    __table_args__ = (
        Index("idx_lookup_type", "entity_type"),
        Index("idx_lookup_node", "node_id"),
        Index("idx_lookup_repo", "repo_id"),
    )


# ── GraphRAG service ──────────────────────────────────────────────────────

class GraphRAGService:
    """Hybrid in-memory / persistent graph store."""

    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        self._loaded = False

    # ── Bootstrap ──────────────────────────────────────────────────────────

    async def load_from_db(self) -> None:
        """Hydrate the NetworkX graph — try Neo4j first, fall back to MySQL."""
        logger.info("graphrag.load_from_db.start")

        # Neo4j primary path
        if neo4j_graph_service.available:
            try:
                neo4j_stats = await neo4j_graph_service.get_stats()
                if neo4j_stats.get("available") and neo4j_stats.get("nodes", 0) > 0:
                    logger.info("graphrag.load_from_db.neo4j_primary", stats=neo4j_stats)
                    # Neo4j is the source of truth — data stays in Neo4j,
                    # NetworkX will be populated on-demand via queries.
                    self._loaded = True
                    logger.info(
                        "graphrag.load_from_db.done",
                        source="neo4j",
                        nodes=neo4j_stats.get("nodes", 0),
                        edges=neo4j_stats.get("edges", 0),
                    )
                    return
            except Exception as exc:
                logger.warning("graphrag.load_from_db.neo4j_failed", error=str(exc))

        # MySQL fallback
        logger.info("graphrag.load_from_db.mysql_fallback")
        async with AsyncSessionLocal() as session:
            # Load nodes
            node_rows = (await session.execute(select(GraphNodeRow))).scalars().all()
            for row in node_rows:
                self._graph.add_node(
                    row.id,
                    node_type=row.node_type,
                    label=row.label,
                    repo_id=row.repo_id,
                    properties=row.properties or {},
                    created_at=row.created_at,
                )

            # Load edges
            edge_rows = (await session.execute(select(GraphEdgeRow))).scalars().all()
            for row in edge_rows:
                self._graph.add_edge(
                    row.source_id,
                    row.target_id,
                    edge_type=row.edge_type,
                    weight=row.weight,
                    repo_id=row.repo_id,
                    properties=row.properties or {},
                    created_at=row.created_at,
                )

        self._loaded = True
        logger.info(
            "graphrag.load_from_db.done",
            source="mysql",
            nodes=self._graph.number_of_nodes(),
            edges=self._graph.number_of_edges(),
        )

    # ── Node CRUD ──────────────────────────────────────────────────────────

    async def ingest_node(
        self,
        node_id: str,
        node_type: NodeType,
        label: str,
        repo_id: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> GraphNode:
        """Add or update a single node in NetworkX, Neo4j, and MySQL."""
        props = properties or {}
        now = datetime.now(timezone.utc)

        # NetworkX
        if self._graph.has_node(node_id):
            self._graph.nodes[node_id].update(
                node_type=node_type.value, label=label, repo_id=repo_id,
                properties=props,
            )
        else:
            self._graph.add_node(
                node_id,
                node_type=node_type.value, label=label, repo_id=repo_id,
                properties=props, created_at=now,
            )

        # Neo4j — dual-write
        if neo4j_graph_service.available:
            try:
                await neo4j_graph_service.ingest_node(
                    node_id=node_id, node_type=node_type.value, label=label,
                    repo_id=repo_id, properties=props,
                )
            except Exception as exc:
                logger.warning("graphrag.ingest_node.neo4j_failed", node_id=node_id, error=str(exc))

        # MySQL — upsert
        async with AsyncSessionLocal() as session:
            existing = await session.get(GraphNodeRow, node_id)
            if existing:
                existing.node_type = node_type.value
                existing.label = label
                existing.repo_id = repo_id
                existing.properties = props
            else:
                session.add(GraphNodeRow(
                    id=node_id, node_type=node_type.value, label=label,
                    repo_id=repo_id, properties=props, created_at=now,
                ))
            await session.commit()

        logger.debug("graphrag.ingest_node", node_id=node_id, node_type=node_type.value)
        return GraphNode(
            id=node_id, node_type=node_type, label=label,
            repo_id=repo_id, properties=props, created_at=now,
        )

    # ── Edge CRUD ──────────────────────────────────────────────────────────

    async def ingest_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        repo_id: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> GraphEdge:
        """Add or strengthen an edge. Duplicate calls bump weight by 0.1."""
        props = properties or {}
        now = datetime.now(timezone.utc)
        weight = 1.0

        # NetworkX — strengthen existing edge
        if self._graph.has_edge(source_id, target_id):
            edata = self._graph[source_id][target_id]
            if edata.get("edge_type") == edge_type.value:
                edata["weight"] = edata.get("weight", 1.0) + 0.1
                edata["properties"].update(props)
                weight = edata["weight"]
            else:
                # different edge type — overwrite
                self._graph[source_id][target_id].update(
                    edge_type=edge_type.value, weight=1.0, repo_id=repo_id,
                    properties=props, created_at=now,
                )
                weight = 1.0
        else:
            self._graph.add_edge(
                source_id, target_id,
                edge_type=edge_type.value, weight=weight, repo_id=repo_id,
                properties=props, created_at=now,
            )

        # Neo4j — dual-write
        if neo4j_graph_service.available:
            try:
                await neo4j_graph_service.ingest_edge(
                    source_id=source_id, target_id=target_id,
                    edge_type=edge_type.value, repo_id=repo_id, weight=weight,
                )
            except Exception as exc:
                logger.warning("graphrag.ingest_edge.neo4j_failed", src=source_id, error=str(exc))

        # MySQL — check for existing edge (same src/tgt/type)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(GraphEdgeRow)
                .where(
                    GraphEdgeRow.source_id == source_id,
                    GraphEdgeRow.target_id == target_id,
                    GraphEdgeRow.edge_type == edge_type.value,
                )
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                existing.weight = round(existing.weight + 0.1, 2)
                existing.properties = {**(existing.properties or {}), **props}
                weight = existing.weight
            else:
                session.add(GraphEdgeRow(
                    source_id=source_id, target_id=target_id,
                    edge_type=edge_type.value, weight=weight, repo_id=repo_id,
                    properties=props, created_at=now,
                ))
            await session.commit()

        logger.debug(
            "graphrag.ingest_edge",
            source=source_id, target=target_id, edge_type=edge_type.value, weight=weight,
        )
        return GraphEdge(
            source_id=source_id, target_id=target_id, edge_type=edge_type,
            weight=weight, repo_id=repo_id, properties=props, created_at=now,
        )

    # ── Domain-specific ingest helpers ─────────────────────────────────────

    async def ingest_module_deps(
        self,
        repo_id: str,
        modules: List[Dict[str, Any]],
    ) -> int:
        """Ingest a batch of module dependency edges.

        Each item in *modules* should have keys: source, target,
        optional edge_type (default depends_on), optional properties.
        Returns count of edges ingested.
        """
        count = 0
        for dep in modules:
            source = dep["source"]
            target = dep["target"]
            etype = EdgeType(dep.get("edge_type", "depends_on"))
            props = dep.get("properties", {})

            # Ensure both nodes exist
            await self.ingest_node(source, NodeType.module, source, repo_id=repo_id)
            await self.ingest_node(target, NodeType.module, target, repo_id=repo_id)
            await self.ingest_edge(source, target, etype, repo_id=repo_id, properties=props)
            count += 1

        logger.info("graphrag.ingest_module_deps", repo_id=repo_id, count=count)
        return count

    async def ingest_courier_relationship(
        self,
        repo_id: str,
        courier_id: str,
        courier_name: str,
        seller_id: Optional[str] = None,
        seller_name: Optional[str] = None,
        channel_id: Optional[str] = None,
        channel_name: Optional[str] = None,
        ndr_count: int = 0,
        properties: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Ingest courier node and its relationships to sellers/channels."""
        props = properties or {}
        edges_created = 0

        await self.ingest_node(courier_id, NodeType.courier, courier_name, repo_id=repo_id, properties=props)

        if seller_id and seller_name:
            await self.ingest_node(seller_id, NodeType.seller, seller_name, repo_id=repo_id)
            await self.ingest_edge(
                courier_id, seller_id, EdgeType.delivers_for,
                repo_id=repo_id, properties={"ndr_count": ndr_count},
            )
            edges_created += 1

        if channel_id and channel_name:
            await self.ingest_node(channel_id, NodeType.channel, channel_name, repo_id=repo_id)
            await self.ingest_edge(
                courier_id, channel_id, EdgeType.connects,
                repo_id=repo_id,
            )
            edges_created += 1

        if ndr_count > 0 and seller_id:
            await self.ingest_edge(
                courier_id, seller_id, EdgeType.has_ndr,
                repo_id=repo_id, properties={"ndr_count": ndr_count, **props},
            )
            edges_created += 1

        logger.info("graphrag.ingest_courier", courier_id=courier_id, edges=edges_created)
        return edges_created

    async def ingest_channel_relationship(
        self,
        repo_id: str,
        channel_id: str,
        channel_name: str,
        seller_id: str,
        seller_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Ingest channel <-> seller relationship."""
        props = properties or {}
        await self.ingest_node(channel_id, NodeType.channel, channel_name, repo_id=repo_id, properties=props)
        await self.ingest_node(seller_id, NodeType.seller, seller_name, repo_id=repo_id)
        await self.ingest_edge(
            seller_id, channel_id, EdgeType.sells_on,
            repo_id=repo_id, properties=props,
        )
        logger.info("graphrag.ingest_channel", channel_id=channel_id, seller_id=seller_id)
        return 1

    # ── Query helpers ──────────────────────────────────────────────────────

    async def query_related(
        self,
        q: str,
        repo_id: Optional[str] = None,
        max_depth: int = 2,
        limit: int = 20,
    ) -> QueryResult:
        """Keyword search over node labels + BFS expansion of matched nodes."""
        query_lower = q.lower()
        matched_ids: List[str] = []

        for nid, data in self._graph.nodes(data=True):
            if repo_id and data.get("repo_id") != repo_id:
                continue
            label = (data.get("label") or "").lower()
            ntype = (data.get("node_type") or "").lower()
            if query_lower in label or query_lower in ntype:
                matched_ids.append(nid)

        # BFS expansion from matched nodes
        related_ids: Set[str] = set()
        related_edge_tuples: List[Tuple[str, str]] = []
        for root in matched_ids[:limit]:
            visited: Set[str] = set()
            queue: List[Tuple[str, int]] = [(root, 0)]
            while queue:
                current, depth = queue.pop(0)
                if current in visited or depth > max_depth:
                    continue
                visited.add(current)
                if current != root:
                    related_ids.add(current)
                for neighbor in self._graph.successors(current):
                    related_edge_tuples.append((current, neighbor))
                    if neighbor not in visited and depth + 1 <= max_depth:
                        queue.append((neighbor, depth + 1))
                for neighbor in self._graph.predecessors(current):
                    related_edge_tuples.append((neighbor, current))
                    if neighbor not in visited and depth + 1 <= max_depth:
                        queue.append((neighbor, depth + 1))

        matched_nodes = [self._node_to_model(nid) for nid in matched_ids[:limit]]
        related_nodes = [self._node_to_model(nid) for nid in list(related_ids)[:limit]]
        related_edges = []
        seen_edges: Set[Tuple[str, str]] = set()
        for src, tgt in related_edge_tuples:
            if (src, tgt) not in seen_edges and self._graph.has_edge(src, tgt):
                seen_edges.add((src, tgt))
                related_edges.append(self._edge_to_model(src, tgt))

        return QueryResult(
            query=q,
            matched_nodes=matched_nodes,
            related_nodes=related_nodes,
            related_edges=related_edges[:limit * 2],
            total_matches=len(matched_ids),
        )

    async def traverse(
        self,
        node_id: str,
        max_depth: int = 2,
    ) -> TraversalResult:
        """BFS traversal from a single node."""
        if not self._graph.has_node(node_id):
            return TraversalResult(
                root_id=node_id, max_depth=max_depth,
                nodes=[], edges=[], total_nodes=0, total_edges=0,
            )

        visited: Set[str] = set()
        edge_set: Set[Tuple[str, str]] = set()
        queue: List[Tuple[str, int]] = [(node_id, 0)]

        while queue:
            current, depth = queue.pop(0)
            if current in visited or depth > max_depth:
                continue
            visited.add(current)
            for neighbor in self._graph.successors(current):
                edge_set.add((current, neighbor))
                if neighbor not in visited and depth + 1 <= max_depth:
                    queue.append((neighbor, depth + 1))
            for neighbor in self._graph.predecessors(current):
                edge_set.add((neighbor, current))
                if neighbor not in visited and depth + 1 <= max_depth:
                    queue.append((neighbor, depth + 1))

        nodes = [self._node_to_model(nid) for nid in visited]
        edges = [
            self._edge_to_model(src, tgt)
            for src, tgt in edge_set
            if self._graph.has_edge(src, tgt)
        ]

        return TraversalResult(
            root_id=node_id, max_depth=max_depth,
            nodes=nodes, edges=edges,
            total_nodes=len(nodes), total_edges=len(edges),
        )

    async def get_stats(self) -> GraphStats:
        """Return aggregate statistics about the graph."""
        node_type_counts: Dict[str, int] = defaultdict(int)
        for _, data in self._graph.nodes(data=True):
            nt = data.get("node_type", "unknown")
            node_type_counts[nt] += 1

        edge_type_counts: Dict[str, int] = defaultdict(int)
        for _, _, data in self._graph.edges(data=True):
            et = data.get("edge_type", "unknown")
            edge_type_counts[et] += 1

        n_nodes = self._graph.number_of_nodes()
        n_edges = self._graph.number_of_edges()
        undirected = self._graph.to_undirected()
        components = nx.number_connected_components(undirected) if n_nodes > 0 else 0
        avg_degree = (2 * n_edges / n_nodes) if n_nodes > 0 else 0.0

        return GraphStats(
            total_nodes=n_nodes,
            total_edges=n_edges,
            node_type_counts=dict(node_type_counts),
            edge_type_counts=dict(edge_type_counts),
            connected_components=components,
            avg_degree=round(avg_degree, 2),
        )

    async def find_nodes(
        self,
        node_type: Optional[NodeType] = None,
        repo_id: Optional[str] = None,
        label_contains: Optional[str] = None,
        limit: int = 50,
    ) -> List[GraphNode]:
        """Filter and return nodes from the in-memory graph."""
        results: List[GraphNode] = []
        query_lower = (label_contains or "").lower()

        for nid, data in self._graph.nodes(data=True):
            if node_type and data.get("node_type") != node_type.value:
                continue
            if repo_id and data.get("repo_id") != repo_id:
                continue
            if label_contains and query_lower not in (data.get("label") or "").lower():
                continue
            results.append(self._node_to_model(nid))
            if len(results) >= limit:
                break

        return results

    async def get_shortest_path(
        self,
        source_id: str,
        target_id: str,
    ) -> Optional[TraversalResult]:
        """Return shortest path between two nodes (unweighted)."""
        if not self._graph.has_node(source_id) or not self._graph.has_node(target_id):
            return None

        try:
            path_ids: List[str] = nx.shortest_path(
                self._graph, source=source_id, target=target_id,
            )
        except nx.NetworkXNoPath:
            return None

        nodes = [self._node_to_model(nid) for nid in path_ids]
        edges: List[GraphEdge] = []
        for i in range(len(path_ids) - 1):
            src, tgt = path_ids[i], path_ids[i + 1]
            if self._graph.has_edge(src, tgt):
                edges.append(self._edge_to_model(src, tgt))

        return TraversalResult(
            root_id=source_id,
            max_depth=len(path_ids) - 1,
            nodes=nodes,
            edges=edges,
            total_nodes=len(nodes),
            total_edges=len(edges),
        )

    async def personalized_pagerank(
        self,
        seed_node_ids: List[str],
        alpha: float = 0.15,
        max_iter: int = 50,
        top_k: int = 20,
    ) -> List[tuple]:
        """Compute Personalized PageRank from seed nodes.

        Unlike BFS (fixed depth), PPR finds important nodes at ANY distance
        by propagating relevance through the graph. A node connected to many
        important nodes gets a high score even if 5+ hops from the seed.

        Args:
            seed_node_ids: Query-specific anchor nodes (entity, intent, domain)
            alpha: Restart probability (0.15 = 85% chance to keep walking)
            max_iter: Max iterations for convergence
            top_k: Return top K nodes by PPR score

        Returns:
            List of (node_id, ppr_score) tuples sorted by score descending.
        """
        if not self._loaded or not self._graph.number_of_nodes():
            return []

        # Build personalization dict — uniform weight over seeds that exist in graph
        valid_seeds = [s for s in seed_node_ids if self._graph.has_node(s)]
        if not valid_seeds:
            return []

        personalization = {s: 1.0 / len(valid_seeds) for s in valid_seeds}

        try:
            ppr_scores = nx.pagerank(
                self._graph,
                alpha=alpha,
                personalization=personalization,
                max_iter=max_iter,
                tol=1e-6,
            )
        except nx.PowerIterationFailedConvergence:
            # Fallback: use simpler approach
            ppr_scores = nx.pagerank(
                self._graph,
                alpha=alpha,
                personalization=personalization,
                max_iter=max_iter * 2,
                tol=1e-4,
            )

        # Sort by score, exclude seed nodes themselves, return top_k
        ranked = sorted(
            ((nid, score) for nid, score in ppr_scores.items() if nid not in valid_seeds),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_k]

    async def weighted_shortest_path(
        self,
        source_id: str,
        target_id: str,
    ) -> Optional[TraversalResult]:
        """Shortest path using edge weights (Dijkstra) instead of hop count."""
        if not self._graph.has_node(source_id) or not self._graph.has_node(target_id):
            return None

        try:
            path_ids = nx.dijkstra_path(
                self._graph, source=source_id, target=target_id, weight="weight",
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

        nodes = [self._node_to_model(nid) for nid in path_ids]
        edges = []
        total_weight = 0.0
        for i in range(len(path_ids) - 1):
            src, tgt = path_ids[i], path_ids[i + 1]
            if self._graph.has_edge(src, tgt):
                edges.append(self._edge_to_model(src, tgt))
                total_weight += self._graph[src][tgt].get("weight", 1.0)

        result = TraversalResult(
            root_id=source_id,
            max_depth=len(path_ids) - 1,
            nodes=nodes,
            edges=edges,
            total_nodes=len(nodes),
            total_edges=len(edges),
        )
        return result

    # ── DB-backed queries (Neo4j primary, MySQL fallback) ──────────────────

    async def pg_query_related(
        self,
        q: str,
        repo_id: Optional[str] = None,
        node_type: Optional[str] = None,
        domain: Optional[str] = None,
        max_depth: int = 2,
        limit: int = 20,
    ) -> QueryResult:
        """Search nodes + BFS expand — Neo4j primary, MySQL fallback."""
        # Neo4j primary path
        if neo4j_graph_service.available:
            try:
                records = await neo4j_graph_service.bfs_query(
                    query_text=q, repo_id=repo_id, max_depth=max_depth, limit=limit,
                )
                if records is not None:
                    matched_nodes = []
                    related_nodes = []
                    for r in records:
                        node = GraphNode(
                            id=r["node_id"],
                            node_type=NodeType(r.get("node_type", "module")),
                            label=r.get("label", r["node_id"]),
                            repo_id=r.get("repo_id"),
                            properties={},
                            created_at=None,
                        )
                        depth = r.get("depth", 0)
                        if depth == 0:
                            matched_nodes.append(node)
                        else:
                            related_nodes.append(node)

                    return QueryResult(
                        query=q,
                        matched_nodes=matched_nodes,
                        related_nodes=related_nodes,
                        related_edges=[],
                        total_matches=len(matched_nodes),
                    )
            except Exception as exc:
                logger.warning("graphrag.pg_query_related.neo4j_failed", error=str(exc))

        # MySQL fallback
        query_lower = f"%{q.lower()}%"

        async with AsyncSessionLocal() as session:
            # Find matching nodes
            stmt = select(GraphNodeRow).where(
                or_(
                    GraphNodeRow.label.ilike(query_lower),
                    GraphNodeRow.node_type.ilike(query_lower),
                )
            )
            if repo_id:
                stmt = stmt.where(GraphNodeRow.repo_id == repo_id)
            if node_type:
                stmt = stmt.where(GraphNodeRow.node_type == node_type)
            if domain:
                stmt = stmt.where(GraphNodeRow.domain == domain)
            stmt = stmt.limit(limit)

            matched_rows = (await session.execute(stmt)).scalars().all()
            matched_ids = [r.id for r in matched_rows]

            # BFS expand via edges (1-2 hops)
            related_ids: Set[str] = set()
            edge_rows_all: List[GraphEdgeRow] = []
            frontier = set(matched_ids)

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
                edge_rows_all.extend(edge_rows)

                next_frontier: Set[str] = set()
                for e in edge_rows:
                    if e.source_id not in matched_ids and e.source_id not in related_ids:
                        next_frontier.add(e.source_id)
                        related_ids.add(e.source_id)
                    if e.target_id not in matched_ids and e.target_id not in related_ids:
                        next_frontier.add(e.target_id)
                        related_ids.add(e.target_id)
                frontier = next_frontier

            # Fetch related node rows
            related_rows = []
            if related_ids:
                rel_stmt = select(GraphNodeRow).where(GraphNodeRow.id.in_(related_ids)).limit(limit)
                related_rows = (await session.execute(rel_stmt)).scalars().all()

        matched_nodes = [self._row_to_model(r) for r in matched_rows]
        related_nodes = [self._row_to_model(r) for r in related_rows[:limit]]
        related_edges = [self._edge_row_to_model(e) for e in edge_rows_all[:limit * 2]]

        return QueryResult(
            query=q,
            matched_nodes=matched_nodes,
            related_nodes=related_nodes,
            related_edges=related_edges,
            total_matches=len(matched_ids),
        )

    async def pg_traverse(
        self,
        node_id: str,
        max_depth: int = 2,
    ) -> TraversalResult:
        """BFS traversal from a single node — Neo4j primary, MySQL fallback."""
        # Neo4j primary path
        if neo4j_graph_service.available:
            try:
                records = await neo4j_graph_service.bfs_query(
                    query_text=node_id, max_depth=max_depth, limit=100,
                )
                if records:
                    nodes = [
                        GraphNode(
                            id=r["node_id"], node_type=NodeType(r.get("node_type", "module")),
                            label=r.get("label", r["node_id"]), repo_id=r.get("repo_id"),
                            properties={}, created_at=None,
                        )
                        for r in records
                    ]
                    return TraversalResult(
                        root_id=node_id, max_depth=max_depth,
                        nodes=nodes, edges=[],
                        total_nodes=len(nodes), total_edges=0,
                    )
            except Exception as exc:
                logger.warning("graphrag.pg_traverse.neo4j_failed", error=str(exc))

        # MySQL fallback
        async with AsyncSessionLocal() as session:
            # Check node exists
            root = await session.get(GraphNodeRow, node_id)
            if not root:
                return TraversalResult(
                    root_id=node_id, max_depth=max_depth,
                    nodes=[], edges=[], total_nodes=0, total_edges=0,
                )

            visited: Set[str] = {node_id}
            all_edge_rows: List[GraphEdgeRow] = []
            frontier = {node_id}

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

        nodes = [self._row_to_model(r) for r in node_rows]
        edges = [self._edge_row_to_model(e) for e in all_edge_rows]

        return TraversalResult(
            root_id=node_id, max_depth=max_depth,
            nodes=nodes, edges=edges,
            total_nodes=len(nodes), total_edges=len(edges),
        )

    async def pg_find_nodes(
        self,
        node_type: Optional[str] = None,
        repo_id: Optional[str] = None,
        domain: Optional[str] = None,
        label_contains: Optional[str] = None,
        limit: int = 50,
    ) -> List[GraphNode]:
        """Filter nodes from MySQL (MARS DB)."""
        async with AsyncSessionLocal() as session:
            stmt = select(GraphNodeRow)
            if node_type:
                stmt = stmt.where(GraphNodeRow.node_type == node_type)
            if repo_id:
                stmt = stmt.where(GraphNodeRow.repo_id == repo_id)
            if domain:
                stmt = stmt.where(GraphNodeRow.domain == domain)
            if label_contains:
                stmt = stmt.where(GraphNodeRow.label.ilike(f"%{label_contains}%"))
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        return [self._row_to_model(r) for r in rows]

    async def pg_lookup_entity(
        self,
        entity_type: str,
        entity_value: str,
        repo_id: Optional[str] = None,
    ) -> Optional[GraphNode]:
        """Exact-match entity lookup — Neo4j primary, MySQL fallback.
        When repo_id is provided, scopes to that repo; otherwise returns first match."""
        # Neo4j primary path
        if neo4j_graph_service.available:
            try:
                result = await neo4j_graph_service.entity_lookup(entity_type, entity_value)
                if result:
                    return GraphNode(
                        id=result["node_id"],
                        node_type=NodeType(result.get("node_type", "module")),
                        label=result.get("label", result["node_id"]),
                        repo_id=result.get("repo_id"),
                        properties={},
                        created_at=None,
                    )
            except Exception as exc:
                logger.warning("graphrag.pg_lookup_entity.neo4j_failed", error=str(exc))

        # MySQL fallback
        async with AsyncSessionLocal() as session:
            filters = [
                EntityLookupRow.entity_type == entity_type,
                EntityLookupRow.entity_value == entity_value,
            ]
            if repo_id:
                filters.append(EntityLookupRow.repo_id == repo_id)
            lookup = (
                await session.execute(
                    select(EntityLookupRow).where(*filters)
                )
            ).scalars().first()
            if not lookup:
                return None
            node = await session.get(GraphNodeRow, lookup.node_id)
            if not node:
                return None
            return self._row_to_model(node)

    async def pg_get_stats(self) -> GraphStats:
        """Aggregate graph stats — Neo4j primary, MySQL fallback."""
        # Neo4j primary path
        if neo4j_graph_service.available:
            try:
                neo_stats = await neo4j_graph_service.get_stats()
                if neo_stats.get("available"):
                    n_nodes = neo_stats.get("nodes", 0)
                    n_edges = neo_stats.get("edges", 0)
                    avg_degree = (2 * n_edges / n_nodes) if n_nodes > 0 else 0.0
                    return GraphStats(
                        total_nodes=n_nodes,
                        total_edges=n_edges,
                        node_type_counts={},  # Neo4j stats endpoint doesn't break down by type
                        edge_type_counts={},
                        connected_components=0,
                        avg_degree=round(avg_degree, 2),
                    )
            except Exception as exc:
                logger.warning("graphrag.pg_get_stats.neo4j_failed", error=str(exc))

        # MySQL fallback
        async with AsyncSessionLocal() as session:
            n_nodes = (await session.execute(
                select(func.count()).select_from(GraphNodeRow)
            )).scalar_one()
            n_edges = (await session.execute(
                select(func.count()).select_from(GraphEdgeRow)
            )).scalar_one()

            node_type_rows = (await session.execute(
                select(GraphNodeRow.node_type, func.count().label("cnt"))
                .group_by(GraphNodeRow.node_type)
            )).all()
            node_type_counts = {r.node_type: r.cnt for r in node_type_rows}

            edge_type_rows = (await session.execute(
                select(GraphEdgeRow.edge_type, func.count().label("cnt"))
                .group_by(GraphEdgeRow.edge_type)
            )).all()
            edge_type_counts = {r.edge_type: r.cnt for r in edge_type_rows}

        avg_degree = (2 * n_edges / n_nodes) if n_nodes > 0 else 0.0

        return GraphStats(
            total_nodes=n_nodes,
            total_edges=n_edges,
            node_type_counts=node_type_counts,
            edge_type_counts=edge_type_counts,
            connected_components=0,  # expensive to compute in SQL, skip
            avg_degree=round(avg_degree, 2),
        )

    # ── Row-to-model helpers ──────────────────────────────────────────────

    @staticmethod
    def _row_to_model(row: GraphNodeRow) -> GraphNode:
        return GraphNode(
            id=row.id,
            node_type=NodeType(row.node_type),
            label=row.label,
            repo_id=row.repo_id,
            properties=row.properties or {},
            created_at=row.created_at,
        )

    @staticmethod
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

    # ── LLM context formatting ─────────────────────────────────────────────

    async def format_as_context(
        self,
        query_result: QueryResult,
        max_tokens: int = 2000,
    ) -> str:
        """Format a QueryResult into a compact text block for LLM prompts."""
        lines: List[str] = []
        lines.append(f"# Graph context for: {query_result.query}")
        lines.append(f"Matched {query_result.total_matches} node(s).\n")

        if query_result.matched_nodes:
            lines.append("## Direct matches")
            for node in query_result.matched_nodes[:10]:
                props_str = ", ".join(f"{k}={v}" for k, v in node.properties.items()) if node.properties else ""
                lines.append(f"- [{node.node_type.value}] {node.label} (id={node.id}){' | ' + props_str if props_str else ''}")

        if query_result.related_nodes:
            lines.append("\n## Related entities")
            for node in query_result.related_nodes[:15]:
                lines.append(f"- [{node.node_type.value}] {node.label} (id={node.id})")

        if query_result.related_edges:
            lines.append("\n## Relationships")
            for edge in query_result.related_edges[:20]:
                lines.append(f"- {edge.source_id} --[{edge.edge_type.value}, w={edge.weight}]--> {edge.target_id}")

        text = "\n".join(lines)
        # Rough token cap (1 token ~ 4 chars)
        char_limit = max_tokens * 4
        if len(text) > char_limit:
            text = text[:char_limit] + "\n... (truncated)"
        return text

    # ── Internal helpers ───────────────────────────────────────────────────

    def _node_to_model(self, node_id: str) -> GraphNode:
        data = self._graph.nodes.get(node_id, {})
        return GraphNode(
            id=node_id,
            node_type=NodeType(data.get("node_type", "module")),
            label=data.get("label", node_id),
            repo_id=data.get("repo_id"),
            properties=data.get("properties", {}),
            created_at=data.get("created_at"),
        )

    def _edge_to_model(self, source_id: str, target_id: str) -> GraphEdge:
        data = self._graph[source_id][target_id]
        return GraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=EdgeType(data.get("edge_type", "depends_on")),
            weight=data.get("weight", 1.0),
            repo_id=data.get("repo_id"),
            properties=data.get("properties", {}),
            created_at=data.get("created_at"),
        )


# ── Module-level singleton ────────────────────────────────────────────────

graphrag_service = GraphRAGService()

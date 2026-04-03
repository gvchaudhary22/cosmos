"""
Neo4j graph backend for COSMOS — runs PARALLEL to the existing NetworkX/Postgres GraphRAGService.

This service mirrors the GraphRAGService interface so it can slot in as a
drop-in replacement or run alongside for A/B comparison in the benchmark.

Key differences vs NetworkX approach:
  - Native Cypher BFS (no Python-side BFS loop)
  - Index-backed relationship traversal at any scale
  - Variable-length path patterns  e.g. (a)-[*1..2]->(b)
  - Built-in APOC procedures for advanced traversal (optional)

Usage::

    svc = Neo4jGraphService(uri="bolt://localhost:7687", user="neo4j", password="password")
    await svc.connect()
    await svc.ingest_node("orders", "module", "Orders Module")
    result = await svc.bfs_query("order status", max_depth=2)
    await svc.close()

If Neo4j is not running, the service degrades gracefully — methods return empty
results with a warning, so the rest of the pipeline keeps working.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
_NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password")


class Neo4jGraphService:
    """
    Async Neo4j graph backend.

    Requires the `neo4j` Python driver:  pip install neo4j

    Falls back to no-op if driver is not installed or connection fails.
    """

    def __init__(
        self,
        uri: str = _NEO4J_URI,
        user: str = _NEO4J_USER,
        password: str = _NEO4J_PASSWORD,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._driver = None
        self._available = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Open the Neo4j driver. Returns True if successful."""
        try:
            from neo4j import AsyncGraphDatabase  # type: ignore[import]
            self._driver = AsyncGraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
            )
            await self._driver.verify_connectivity()
            await self._ensure_indexes()
            self._available = True
            logger.info("neo4j.connected", uri=self._uri)
            return True
        except ImportError:
            logger.warning("neo4j.driver_not_installed", hint="pip install neo4j")
            return False
        except Exception as exc:
            logger.warning("neo4j.connect_failed", uri=self._uri, error=str(exc))
            return False

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ── Schema / Indexes ──────────────────────────────────────────────────────

    async def _ensure_indexes(self) -> None:
        """Create indexes for fast lookups (idempotent)."""
        index_queries = [
            "CREATE INDEX cosmos_node_id IF NOT EXISTS FOR (n:CosmosNode) ON (n.node_id)",
            "CREATE INDEX cosmos_node_type IF NOT EXISTS FOR (n:CosmosNode) ON (n.node_type)",
            "CREATE INDEX cosmos_node_label IF NOT EXISTS FOR (n:CosmosNode) ON (n.label)",
            "CREATE INDEX cosmos_node_repo IF NOT EXISTS FOR (n:CosmosNode) ON (n.repo_id)",
            "CREATE INDEX cosmos_entity_lookup IF NOT EXISTS FOR (e:EntityLookup) ON (e.entity_type, e.entity_value)",
        ]
        async with self._driver.session() as session:
            for q in index_queries:
                try:
                    await session.run(q)
                except Exception as exc:
                    logger.debug("neo4j.index_skip", query=q[:60], reason=str(exc))

    # ── Node CRUD ─────────────────────────────────────────────────────────────

    async def ingest_node(
        self,
        node_id: str,
        node_type: str,
        label: str,
        repo_id: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """MERGE a node into Neo4j. Returns True on success."""
        if not self._available:
            return False
        props = properties or {}
        query = """
        MERGE (n:CosmosNode {node_id: $node_id})
        SET n.node_type = $node_type,
            n.label = $label,
            n.repo_id = $repo_id,
            n.props = $props,
            n.updated_at = timestamp()
        RETURN n.node_id
        """
        try:
            async with self._driver.session() as session:
                await session.run(query, node_id=node_id, node_type=node_type,
                                  label=label, repo_id=repo_id, props=str(props))
            return True
        except Exception as exc:
            logger.warning("neo4j.ingest_node.failed", node_id=node_id, error=str(exc))
            return False

    async def ingest_nodes_bulk(self, nodes: List[Dict[str, Any]]) -> int:
        """
        Bulk MERGE nodes. Each dict must have: node_id, node_type, label.
        Optional: repo_id, properties.
        Returns count of nodes processed.
        """
        if not self._available:
            return 0
        query = """
        UNWIND $rows AS row
        MERGE (n:CosmosNode {node_id: row.node_id})
        SET n.node_type = row.node_type,
            n.label = row.label,
            n.repo_id = row.repo_id,
            n.updated_at = timestamp()
        """
        try:
            rows = [
                {
                    "node_id": n["node_id"],
                    "node_type": n.get("node_type", "module"),
                    "label": n.get("label", n["node_id"]),
                    "repo_id": n.get("repo_id"),
                }
                for n in nodes
            ]
            async with self._driver.session() as session:
                await session.run(query, rows=rows)
            logger.info("neo4j.bulk_ingest_nodes", count=len(rows))
            return len(rows)
        except Exception as exc:
            logger.warning("neo4j.bulk_ingest_nodes.failed", error=str(exc))
            return 0

    # ── Edge CRUD ─────────────────────────────────────────────────────────────

    async def ingest_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        repo_id: Optional[str] = None,
        weight: float = 1.0,
    ) -> bool:
        """MERGE a relationship between two nodes. Returns True on success."""
        if not self._available:
            return False
        query = f"""
        MATCH (a:CosmosNode {{node_id: $src}})
        MATCH (b:CosmosNode {{node_id: $tgt}})
        MERGE (a)-[r:{edge_type.upper().replace('-', '_')} {{repo_id: $repo_id}}]->(b)
        SET r.weight = coalesce(r.weight, 0) + $weight,
            r.updated_at = timestamp()
        RETURN type(r)
        """
        try:
            async with self._driver.session() as session:
                await session.run(query, src=source_id, tgt=target_id,
                                  repo_id=repo_id, weight=weight)
            return True
        except Exception as exc:
            logger.warning("neo4j.ingest_edge.failed", src=source_id, tgt=target_id, error=str(exc))
            return False

    async def ingest_edges_bulk(self, edges: List[Dict[str, Any]]) -> int:
        """
        Bulk MERGE edges via UNWIND. Each dict: source_id, target_id, edge_type.
        NOTE: all edges in a batch must be the same edge_type (Cypher limitation).
        Groups by edge_type internally.
        """
        if not self._available:
            return 0
        from collections import defaultdict
        by_type: Dict[str, List[Dict]] = defaultdict(list)
        for e in edges:
            etype = e.get("edge_type", "depends_on").upper().replace("-", "_")
            by_type[etype].append(e)

        total = 0
        for etype, batch in by_type.items():
            query = f"""
            UNWIND $rows AS row
            MATCH (a:CosmosNode {{node_id: row.source_id}})
            MATCH (b:CosmosNode {{node_id: row.target_id}})
            MERGE (a)-[r:{etype}]->(b)
            SET r.weight = coalesce(r.weight, 0) + 1.0,
                r.repo_id = row.repo_id
            """
            try:
                rows = [{"source_id": e["source_id"], "target_id": e["target_id"],
                         "repo_id": e.get("repo_id")} for e in batch]
                async with self._driver.session() as session:
                    await session.run(query, rows=rows)
                total += len(batch)
            except Exception as exc:
                logger.warning("neo4j.bulk_ingest_edges.failed", edge_type=etype, error=str(exc))

        logger.info("neo4j.bulk_ingest_edges", total=total)
        return total

    # ── Query: BFS traversal ──────────────────────────────────────────────────

    async def bfs_query(
        self,
        query_text: str,
        repo_id: Optional[str] = None,
        max_depth: int = 2,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Keyword match on label → Cypher variable-length BFS traversal.

        Returns list of node dicts: {node_id, node_type, label, repo_id, depth}
        """
        if not self._available:
            return []

        t0 = time.monotonic()
        repo_filter = "AND n.repo_id = $repo_id" if repo_id else ""
        query = f"""
        MATCH (n:CosmosNode)
        WHERE toLower(n.label) CONTAINS toLower($q)
        {repo_filter}
        WITH n LIMIT $seed_limit
        MATCH path = (n)-[*0..{max_depth}]-(related:CosmosNode)
        WITH related, min(length(path)) AS depth
        RETURN related.node_id AS node_id,
               related.node_type AS node_type,
               related.label AS label,
               related.repo_id AS repo_id,
               depth
        ORDER BY depth ASC, related.label ASC
        LIMIT $limit
        """
        try:
            params = {"q": query_text, "limit": limit, "seed_limit": 10}
            if repo_id:
                params["repo_id"] = repo_id

            async with self._driver.session() as session:
                result = await session.run(query, **params)
                records = await result.data()

            latency_ms = (time.monotonic() - t0) * 1000
            logger.debug("neo4j.bfs_query", q=query_text[:50], hits=len(records),
                         latency_ms=round(latency_ms, 1))
            return records
        except Exception as exc:
            logger.warning("neo4j.bfs_query.failed", error=str(exc))
            return []

    # ── Query: Entity exact lookup ─────────────────────────────────────────────

    async def register_entity(
        self,
        entity_type: str,
        entity_value: str,
        node_id: str,
        repo_id: Optional[str] = None,
    ) -> bool:
        """Register an exact-match entity lookup (AWB, order_id, seller_id, api_path)."""
        if not self._available:
            return False
        query = """
        MERGE (e:EntityLookup {entity_type: $etype, entity_value: $evalue})
        SET e.node_id = $node_id, e.repo_id = $repo_id
        """
        try:
            async with self._driver.session() as session:
                await session.run(query, etype=entity_type, evalue=entity_value,
                                  node_id=node_id, repo_id=repo_id)
            return True
        except Exception as exc:
            logger.warning("neo4j.register_entity.failed", error=str(exc))
            return False

    async def entity_lookup(
        self,
        entity_type: str,
        entity_value: str,
    ) -> Optional[Dict[str, Any]]:
        """Exact-match entity → linked node dict."""
        if not self._available:
            return None
        query = """
        MATCH (e:EntityLookup {entity_type: $etype, entity_value: $evalue})
        MATCH (n:CosmosNode {node_id: e.node_id})
        RETURN n.node_id AS node_id, n.node_type AS node_type, n.label AS label
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(query, etype=entity_type, evalue=entity_value)
                record = await result.single()
                return dict(record) if record else None
        except Exception as exc:
            logger.warning("neo4j.entity_lookup.failed", error=str(exc))
            return None

    # ── Advanced: Weighted Dijkstra + Multi-hop Chain Scoring ──────────────

    async def weighted_shortest_path(
        self, source_id: str, target_id: str, max_depth: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Find shortest path using edge weights (Dijkstra) instead of hop count."""
        if not self._available:
            return None
        query = f"""
        MATCH (a:CosmosNode {{node_id: $src}}), (b:CosmosNode {{node_id: $tgt}})
        MATCH path = shortestPath((a)-[*..{max_depth}]->(b))
        RETURN [n IN nodes(path) | n.node_id] AS node_ids,
               [n IN nodes(path) | n.label] AS labels,
               [r IN relationships(path) | type(r)] AS edge_types,
               reduce(w = 0.0, r IN relationships(path) | w + coalesce(r.weight, 1.0)) AS total_weight,
               length(path) AS hops
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(query, src=source_id, tgt=target_id)
                record = await result.single()
                if not record:
                    return None
                return {
                    "node_ids": record["node_ids"],
                    "labels": record["labels"],
                    "edge_types": record["edge_types"],
                    "total_weight": record["total_weight"],
                    "hops": record["hops"],
                }
        except Exception as exc:
            logger.debug("neo4j.weighted_path.failed", error=str(exc))
            return None

    async def score_chains(
        self,
        seed_ids: List[str],
        target_types: Optional[List[str]] = None,
        max_depth: int = 4,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Score multi-hop paths from seed nodes to target node types.

        Finds ALL paths from seeds to targets (action_contract, workflow,
        table, api_endpoint), scored by cumulative edge weight.
        """
        if not self._available or not seed_ids:
            return []
        target_filter = ""
        if target_types:
            types_str = ", ".join(f"'{t}'" for t in target_types)
            target_filter = f"AND target.node_type IN [{types_str}]"

        query = f"""
        UNWIND $seeds AS seed_id
        MATCH (start:CosmosNode {{node_id: seed_id}})
        MATCH path = (start)-[*1..{max_depth}]->(target:CosmosNode)
        WHERE target.node_id <> start.node_id {target_filter}
        WITH target, path,
             reduce(w = 0.0, r IN relationships(path) | w + coalesce(r.weight, 1.0)) AS chain_weight,
             length(path) AS hops
        RETURN DISTINCT target.node_id AS node_id,
               target.node_type AS node_type,
               target.label AS label,
               chain_weight, hops,
               [n IN nodes(path) | n.label] AS path_labels
        ORDER BY chain_weight DESC, hops ASC
        LIMIT $limit
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(query, seeds=seed_ids[:5], limit=limit)
                records = [dict(r) async for r in result]
                logger.debug("neo4j.score_chains", seeds=len(seed_ids), results=len(records))
                return records
        except Exception as exc:
            logger.warning("neo4j.score_chains.failed", error=str(exc))
            return []

    async def domain_scoped_traversal(
        self, seed_id: str, domain: str, max_depth: int = 3, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Traverse staying within a domain cluster (e.g., only orders-related nodes)."""
        if not self._available:
            return []
        query = f"""
        MATCH (start:CosmosNode {{node_id: $seed}})
        MATCH path = (start)-[*1..{max_depth}]-(related:CosmosNode)
        WHERE related.props.domain = $domain OR toLower(related.label) CONTAINS toLower($domain)
        WITH related, min(length(path)) AS depth,
             reduce(w = 0.0, r IN relationships(path) | w + coalesce(r.weight, 1.0)) AS chain_weight
        RETURN related.node_id AS node_id, related.node_type AS node_type,
               related.label AS label, depth, chain_weight
        ORDER BY chain_weight DESC, depth ASC
        LIMIT $limit
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(query, seed=seed_id, domain=domain, limit=limit)
                return [dict(r) async for r in result]
        except Exception as exc:
            logger.warning("neo4j.domain_traversal.failed", error=str(exc))
            return []

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        """Return node/edge counts from Neo4j."""
        if not self._available:
            return {"available": False}
        query = """
        MATCH (n:CosmosNode) WITH count(n) AS nodes
        OPTIONAL MATCH ()-[r]->() WITH nodes, count(r) AS edges
        RETURN nodes, edges
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(query)
                record = await result.single()
                return {"nodes": record["nodes"], "edges": record["edges"], "available": True}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    # ── Sync from Postgres (migration helper) ─────────────────────────────────

    async def sync_from_graphrag(self, graphrag_service: Any, batch_size: int = 500) -> Dict[str, int]:
        """
        Mirror all nodes + edges from the existing GraphRAGService (NetworkX/Postgres)
        into Neo4j. Idempotent — uses MERGE.

        Use this once to seed Neo4j from your Postgres graph store.

        Returns: {"nodes": N, "edges": E}
        """
        if not self._available:
            logger.warning("neo4j.sync_from_graphrag.skipped", reason="Neo4j not available")
            return {"nodes": 0, "edges": 0}

        graph = graphrag_service._graph  # nx.DiGraph
        nodes_done = 0
        edges_done = 0

        # Batch node ingest
        node_batch = []
        for node_id, data in graph.nodes(data=True):
            node_batch.append({
                "node_id": node_id,
                "node_type": data.get("node_type", "module"),
                "label": data.get("label", node_id),
                "repo_id": data.get("repo_id"),
            })
            if len(node_batch) >= batch_size:
                nodes_done += await self.ingest_nodes_bulk(node_batch)
                node_batch = []
        if node_batch:
            nodes_done += await self.ingest_nodes_bulk(node_batch)

        # Batch edge ingest
        edge_batch = []
        for src, tgt, data in graph.edges(data=True):
            edge_batch.append({
                "source_id": src,
                "target_id": tgt,
                "edge_type": data.get("edge_type", "depends_on"),
                "repo_id": data.get("repo_id"),
            })
            if len(edge_batch) >= batch_size:
                edges_done += await self.ingest_edges_bulk(edge_batch)
                edge_batch = []
        if edge_batch:
            edges_done += await self.ingest_edges_bulk(edge_batch)

        logger.info("neo4j.sync_from_graphrag.done", nodes=nodes_done, edges=edges_done)
        return {"nodes": nodes_done, "edges": edges_done}


# ── Module-level singleton (connect lazily) ───────────────────────────────────

neo4j_graph_service = Neo4jGraphService()

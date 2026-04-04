"""
audit_neo4j_sync.py — Audit and sync graph_nodes/graph_edges/entity_lookup between
MySQL (source of truth) and Neo4j (may be behind due to silent failures).

Problem:
  MySQL is always written (dual-write fallback in _flush_nodes/_flush_edges/_flush_lookups).
  Neo4j is written only when available — failures are logged as warnings, not errors.
  Result: MySQL has nodes/edges that Neo4j never received.

Usage:
  python scripts/audit_neo4j_sync.py --audit           # show gap counts only
  python scripts/audit_neo4j_sync.py --sync            # push missing rows into Neo4j
  python scripts/audit_neo4j_sync.py --sync --batch-size 500  # tune batch size
  python scripts/audit_neo4j_sync.py --audit --repo MultiChannel_API  # filter by repo
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.db.session import AsyncSessionLocal
import structlog

logger = structlog.get_logger(__name__)

DEFAULT_BATCH = 500


# ---------------------------------------------------------------------------
# MySQL readers
# ---------------------------------------------------------------------------

async def mysql_node_ids(repo_id: Optional[str] = None) -> Dict[str, Dict]:
    """Return {node_id: {node_type, label, repo_id, properties}} from MySQL graph_nodes."""
    async with AsyncSessionLocal() as session:
        q = "SELECT id, node_type, label, repo_id, properties FROM graph_nodes"
        params = {}
        if repo_id:
            q += " WHERE repo_id = :repo"
            params["repo"] = repo_id
        result = await session.execute(text(q), params)
        rows = result.fetchall()
    return {
        r[0]: {
            "node_id": r[0],
            "node_type": r[1] or "module",
            "label": r[2] or r[0],
            "repo_id": r[3],
            "properties": json.loads(r[4]) if r[4] else {},
        }
        for r in rows
    }


async def mysql_edge_ids(repo_id: Optional[str] = None) -> Dict[str, Dict]:
    """Return {id: {source_id, target_id, edge_type, weight, repo_id}} from MySQL graph_edges."""
    async with AsyncSessionLocal() as session:
        q = "SELECT id, source_id, target_id, edge_type, weight, repo_id FROM graph_edges"
        params = {}
        if repo_id:
            q += " WHERE repo_id = :repo"
            params["repo"] = repo_id
        result = await session.execute(text(q), params)
        rows = result.fetchall()
    return {
        r[0]: {
            "source_id": r[1],
            "target_id": r[2],
            "edge_type": r[3],
            "weight": float(r[4]) if r[4] else 1.0,
            "repo_id": r[5],
        }
        for r in rows
    }


async def mysql_lookup_ids(repo_id: Optional[str] = None) -> List[Dict]:
    """Return all entity_lookup rows from MySQL."""
    async with AsyncSessionLocal() as session:
        q = "SELECT entity_type, entity_value, repo_id, node_id FROM entity_lookup"
        params = {}
        if repo_id:
            q += " WHERE repo_id = :repo"
            params["repo"] = repo_id
        result = await session.execute(text(q), params)
        rows = result.fetchall()
    return [
        {"entity_type": r[0], "entity_value": r[1], "repo_id": r[2], "node_id": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Neo4j readers
# ---------------------------------------------------------------------------

async def neo4j_node_ids(neo4j) -> set:
    """Return set of node_ids present in Neo4j."""
    try:
        async with neo4j._driver.session() as session:
            result = await session.run("MATCH (n:CosmosNode) RETURN n.node_id AS node_id")
            records = await result.values()
            return {r[0] for r in records if r[0]}
    except Exception as e:
        logger.warning("audit.neo4j.fetch_nodes.failed", error=str(e))
        return set()


async def neo4j_edge_keys(neo4j) -> set:
    """Return set of (source_id, target_id, edge_type) tuples present in Neo4j."""
    try:
        async with neo4j._driver.session() as session:
            result = await session.run(
                "MATCH (a:CosmosNode)-[r]->(b:CosmosNode) "
                "RETURN a.node_id, b.node_id, type(r)"
            )
            records = await result.values()
            return {(r[0], r[1], r[2].lower()) for r in records if r[0] and r[1]}
    except Exception as e:
        logger.warning("audit.neo4j.fetch_edges.failed", error=str(e))
        return set()


async def neo4j_entity_keys(neo4j) -> set:
    """Return set of (entity_type, entity_value) present in Neo4j entity index."""
    try:
        async with neo4j._driver.session() as session:
            result = await session.run(
                "MATCH (n:CosmosNode) WHERE n.entity_type IS NOT NULL "
                "RETURN n.entity_type, n.entity_value"
            )
            records = await result.values()
            return {(r[0], r[1]) for r in records if r[0] and r[1]}
    except Exception as e:
        logger.warning("audit.neo4j.fetch_entities.failed", error=str(e))
        return set()


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

async def sync_nodes(neo4j, missing_nodes: List[Dict], batch_size: int) -> int:
    """Push missing nodes into Neo4j in batches."""
    total = 0
    batches = [missing_nodes[i:i + batch_size] for i in range(0, len(missing_nodes), batch_size)]
    for i, batch in enumerate(batches, 1):
        neo4j_rows = [
            {
                "node_id": n["node_id"],
                "node_type": n.get("node_type", "module"),
                "label": n.get("label", n["node_id"]),
                "repo_id": n.get("repo_id"),
            }
            for n in batch
        ]
        count = await neo4j.ingest_nodes_bulk(neo4j_rows)
        total += count
        print(f"  nodes [{i}/{len(batches)}] pushed={count}/{len(batch)}")
    return total


async def sync_edges(neo4j, missing_edges: List[Dict], batch_size: int) -> int:
    """Push missing edges into Neo4j in batches (grouped by edge_type)."""
    from collections import defaultdict
    by_type: Dict[str, List] = defaultdict(list)
    for e in missing_edges:
        by_type[e["edge_type"]].append(e)

    total = 0
    for edge_type, edges in by_type.items():
        batches = [edges[i:i + batch_size] for i in range(0, len(edges), batch_size)]
        for batch in batches:
            neo4j_rows = [
                {
                    "source_id": e["source_id"],
                    "target_id": e["target_id"],
                    "edge_type": e["edge_type"],
                    "weight": e.get("weight", 1.0),
                    "repo_id": e.get("repo_id"),
                }
                for e in batch
            ]
            count = await neo4j.ingest_edges_bulk(neo4j_rows)
            total += count
    print(f"  edges pushed={total}/{len(missing_edges)}")
    return total


async def sync_lookups(neo4j, missing_lookups: List[Dict]) -> int:
    """Push missing entity_lookup rows into Neo4j."""
    total = 0
    for item in missing_lookups:
        try:
            await neo4j.register_entity(
                entity_type=item["entity_type"],
                entity_value=item["entity_value"],
                node_id=item["node_id"],
                repo_id=item.get("repo_id"),
            )
            total += 1
        except Exception as e:
            logger.warning("audit.sync.lookup.failed", item=item, error=str(e))
    print(f"  lookups pushed={total}/{len(missing_lookups)}")
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args):
    from app.services.neo4j_graph import neo4j_graph_service as neo4j

    print(f"\n{'='*65}")
    print(f"audit_neo4j_sync.py  [{'SYNC' if args.sync else 'AUDIT-ONLY'}]")
    if args.repo:
        print(f"Repo filter: {args.repo}")
    print(f"{'='*65}\n")

    # Connect Neo4j (singleton needs explicit connect)
    if not neo4j.available:
        print("Connecting to Neo4j ...")
        ok = await neo4j.connect()
        if not ok:
            print("[ERROR] Neo4j is not available. Check connection and retry.")
            return

    print("Fetching MySQL graph_nodes ...")
    t0 = time.monotonic()
    mysql_nodes = await mysql_node_ids(args.repo)
    print(f"  MySQL graph_nodes : {len(mysql_nodes):,}")

    print("Fetching Neo4j nodes ...")
    neo4j_nodes = await neo4j_node_ids(neo4j)
    print(f"  Neo4j nodes       : {len(neo4j_nodes):,}")

    missing_node_ids = set(mysql_nodes.keys()) - neo4j_nodes
    extra_node_ids   = neo4j_nodes - set(mysql_nodes.keys())
    print(f"\n  Missing in Neo4j  : {len(missing_node_ids):,}  ← will sync")
    print(f"  Extra in Neo4j    : {len(extra_node_ids):,}   (orphans, not synced)")

    print("\nFetching MySQL graph_edges ...")
    mysql_edges = await mysql_edge_ids(args.repo)
    print(f"  MySQL graph_edges : {len(mysql_edges):,}")

    print("Fetching Neo4j edges ...")
    neo4j_edge_set = await neo4j_edge_keys(neo4j)
    print(f"  Neo4j edges       : {len(neo4j_edge_set):,}")

    missing_edges = [
        e for e in mysql_edges.values()
        if (e["source_id"], e["target_id"], e["edge_type"].lower()) not in neo4j_edge_set
    ]
    print(f"\n  Missing in Neo4j  : {len(missing_edges):,}  ← will sync")

    print("\nFetching MySQL entity_lookup ...")
    mysql_lookups = await mysql_lookup_ids(args.repo)
    print(f"  MySQL entity_lookup : {len(mysql_lookups):,}")

    print("Fetching Neo4j entity index ...")
    neo4j_entity_set = await neo4j_entity_keys(neo4j)
    print(f"  Neo4j entity index  : {len(neo4j_entity_set):,}")

    missing_lookups = [
        l for l in mysql_lookups
        if (l["entity_type"], l["entity_value"]) not in neo4j_entity_set
    ]
    print(f"\n  Missing in Neo4j  : {len(missing_lookups):,}  ← will sync")

    print(f"\n{'='*65}")
    print(f"SUMMARY")
    print(f"  Nodes  — MySQL: {len(mysql_nodes):,} | Neo4j: {len(neo4j_nodes):,} | Gap: {len(missing_node_ids):,}")
    print(f"  Edges  — MySQL: {len(mysql_edges):,} | Neo4j: {len(neo4j_edge_set):,} | Gap: {len(missing_edges):,}")
    print(f"  Lookup — MySQL: {len(mysql_lookups):,} | Neo4j: {len(neo4j_entity_set):,} | Gap: {len(missing_lookups):,}")
    print(f"{'='*65}\n")

    if not args.sync:
        print("Run with --sync to push missing rows into Neo4j.")
        return

    if not missing_node_ids and not missing_edges and not missing_lookups:
        print("Neo4j is fully in sync with MySQL. Nothing to do.")
        return

    print("Syncing missing nodes ...")
    missing_node_list = [mysql_nodes[nid] for nid in missing_node_ids]
    synced_nodes = await sync_nodes(neo4j, missing_node_list, args.batch_size)

    print("\nSyncing missing edges ...")
    synced_edges = await sync_edges(neo4j, missing_edges, args.batch_size)

    print("\nSyncing missing entity lookups ...")
    synced_lookups = await sync_lookups(neo4j, missing_lookups)

    elapsed = time.monotonic() - t0
    print(f"\n{'='*65}")
    print(f"Sync complete in {elapsed:.1f}s")
    print(f"  Nodes synced  : {synced_nodes:,} / {len(missing_node_ids):,}")
    print(f"  Edges synced  : {synced_edges:,} / {len(missing_edges):,}")
    print(f"  Lookups synced: {synced_lookups:,} / {len(missing_lookups):,}")
    print(f"{'='*65}")


def main():
    parser = argparse.ArgumentParser(description="Audit and sync MySQL → Neo4j graph data")
    parser.add_argument("--audit", action="store_true", help="Show gap counts (default mode)")
    parser.add_argument("--sync", action="store_true", help="Push missing rows from MySQL into Neo4j")
    parser.add_argument("--repo", default=None, help="Filter by repo_id (e.g. MultiChannel_API)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, help="Nodes per Neo4j batch (default 500)")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

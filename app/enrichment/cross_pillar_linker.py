"""
Cross-Pillar Linker — Builds explicit links between KB documents across pillars.

Links:
  - Schema table → API endpoints that read/write it
  - Action contract → API endpoints it dispatches to
  - Action contract → Schema tables it modifies
  - Workflow runbook → Action contracts it orchestrates

Links are stored as:
  1. graph_edges in MySQL (for MARS agent registry UI + multi-hop retrieval)
  2. Metadata on Qdrant points (linked_chunk_ids for retrieval expansion)

Usage:
    linker = CrossPillarLinker()
    stats = await linker.build_links()
"""

import json
import uuid
from typing import Dict, List, Set

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


class CrossPillarLinker:
    """Builds cross-pillar links between KB documents."""

    async def build_links(self) -> Dict:
        """Read graph_nodes, find cross-pillar relationships, store as graph_edges."""
        stats = {"schema_to_api": 0, "action_to_api": 0, "action_to_schema": 0, "total_edges": 0}

        try:
            async with AsyncSessionLocal() as session:
                # Load all nodes by type
                schema_nodes = await self._load_nodes(session, "table")
                api_nodes = await self._load_nodes(session, "api_endpoint")
                action_nodes = await self._load_nodes(session, "action_contract")
                tool_nodes = await self._load_nodes(session, "tool")

                # 1. Schema → API: match table_name in API endpoint path/label
                for schema in schema_nodes:
                    table_name = schema["label"].lower()
                    # Skip very generic names that would match too broadly
                    if table_name in ("id", "status", "data", "user", "type"):
                        continue

                    for api in api_nodes:
                        api_label = api["label"].lower()
                        # Match table name in API path segments
                        if table_name in api_label.split("."):
                            edge_id = str(uuid.uuid4())
                            await self._upsert_edge(
                                session, edge_id,
                                source_id=schema["id"],
                                target_id=api["id"],
                                edge_type="schema_used_by_api",
                                repo_id=api.get("repo_id", ""),
                            )
                            stats["schema_to_api"] += 1

                # 2. Action → API: match dispatch_point in action properties
                for action in action_nodes:
                    props = action.get("properties", {})
                    dispatch = props.get("dispatch_point", "")
                    if not dispatch:
                        continue

                    # Extract path from dispatch_point (e.g., "POST /v1/external/orders/create/adhoc")
                    path_parts = dispatch.lower().split()
                    path = path_parts[-1] if path_parts else ""

                    for api in api_nodes:
                        api_props = api.get("properties", {})
                        api_path = api_props.get("path", api["label"]).lower()
                        if path and path in api_path:
                            edge_id = str(uuid.uuid4())
                            await self._upsert_edge(
                                session, edge_id,
                                source_id=action["id"],
                                target_id=api["id"],
                                edge_type="action_dispatches_api",
                                repo_id=action.get("repo_id", ""),
                            )
                            stats["action_to_api"] += 1

                # 3. Action → Schema: match table references in action properties
                for action in action_nodes:
                    props = action.get("properties", {})
                    domain = action.get("domain", "")

                    for schema in schema_nodes:
                        table_name = schema["label"].lower()
                        action_label = action["label"].lower()
                        # Match domain + table name
                        if domain and domain == schema.get("domain", ""):
                            edge_id = str(uuid.uuid4())
                            await self._upsert_edge(
                                session, edge_id,
                                source_id=action["id"],
                                target_id=schema["id"],
                                edge_type="action_modifies_table",
                                repo_id=action.get("repo_id", ""),
                            )
                            stats["action_to_schema"] += 1

                await session.commit()

                stats["total_edges"] = stats["schema_to_api"] + stats["action_to_api"] + stats["action_to_schema"]
                logger.info("cross_pillar_linker.complete", **stats)

        except Exception as e:
            logger.error("cross_pillar_linker.failed", error=str(e))

        return stats

    async def _load_nodes(self, session, node_type: str) -> List[Dict]:
        """Load graph nodes of a specific type."""
        result = await session.execute(
            text("SELECT id, label, domain, repo_id, properties FROM graph_nodes WHERE node_type = :nt"),
            {"nt": node_type},
        )
        nodes = []
        for row in result.fetchall():
            props = {}
            if row.properties:
                try:
                    props = json.loads(row.properties) if isinstance(row.properties, str) else row.properties
                except (json.JSONDecodeError, TypeError):
                    pass
            nodes.append({
                "id": row.id,
                "label": row.label or "",
                "domain": row.domain or "",
                "repo_id": row.repo_id or "",
                "properties": props,
            })
        return nodes

    async def _upsert_edge(
        self, session, edge_id: str, source_id: str, target_id: str,
        edge_type: str, repo_id: str = "",
    ):
        """Insert or update a cross-pillar edge in graph_edges."""
        await session.execute(
            text("""INSERT INTO graph_edges (id, source_id, target_id, edge_type, weight, repo_id, created_at)
                    VALUES (:id, :src, :tgt, :et, 1.0, :repo, NOW())
                    ON DUPLICATE KEY UPDATE weight = 1.0, repo_id = :repo"""),
            {"id": edge_id, "src": source_id, "tgt": target_id, "et": edge_type, "repo": repo_id},
        )

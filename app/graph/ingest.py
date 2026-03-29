"""
Canonical KB ingestion pipeline for COSMOS GraphRAG.

Reads Shiprocket knowledge-base YAML files (pillar_3_api_mcp_tools and
pillar_1_schema) and populates graph_nodes, graph_edges, and entity_lookup
tables in PostgreSQL using bulk upserts.

Usage:
    pipeline = CanonicalIngestionPipeline(kb_path="mars/knowledge_base/")
    report   = await pipeline.ingest_all()
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import AsyncSessionLocal
from app.services.graphrag import (
    EntityLookupRow,
    GraphEdgeRow,
    GraphNodeRow,
)
from app.services.graphrag_models import EdgeType, NodeType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# IngestReport
# ---------------------------------------------------------------------------

@dataclass
class IngestReport:
    """Tracks counts of objects created/updated per type during one pipeline run."""

    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    lookups_upserted: int = 0

    # per node-type breakdown
    node_type_counts: Dict[str, int] = field(default_factory=dict)
    # per edge-type breakdown
    edge_type_counts: Dict[str, int] = field(default_factory=dict)

    repos_processed: List[str] = field(default_factory=list)
    apis_processed: int = 0
    tables_processed: int = 0
    intents_processed: int = 0
    errors: List[str] = field(default_factory=list)

    def bump_node(self, node_type: str, is_new: bool) -> None:
        if is_new:
            self.nodes_created += 1
        else:
            self.nodes_updated += 1
        self.node_type_counts[node_type] = self.node_type_counts.get(node_type, 0) + 1

    def bump_edge(self, edge_type: str, is_new: bool) -> None:
        if is_new:
            self.edges_created += 1
        else:
            self.edges_updated += 1
        self.edge_type_counts[edge_type] = self.edge_type_counts.get(edge_type, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes_created": self.nodes_created,
            "nodes_updated": self.nodes_updated,
            "edges_created": self.edges_created,
            "edges_updated": self.edges_updated,
            "lookups_upserted": self.lookups_upserted,
            "node_type_counts": self.node_type_counts,
            "edge_type_counts": self.edge_type_counts,
            "repos_processed": self.repos_processed,
            "apis_processed": self.apis_processed,
            "tables_processed": self.tables_processed,
            "intents_processed": self.intents_processed,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# CanonicalIngestionPipeline
# ---------------------------------------------------------------------------

class CanonicalIngestionPipeline:
    """
    Full KB ingestion pipeline.

    Traverses knowledge_base/shiprocket/{repo}/pillar_* directories,
    builds typed graph nodes/edges/lookups, and bulk-upserts them into
    PostgreSQL.
    """

    def __init__(self, kb_path: str = "mars/knowledge_base/") -> None:
        self._kb_path = Path(kb_path)
        self._report = IngestReport()
        # In-flight batch buffers
        self._node_batch: List[Dict[str, Any]] = []
        self._edge_batch: List[Dict[str, Any]] = []
        self._lookup_batch: List[Dict[str, Any]] = []
        # Track node/edge existence within this run to avoid redundant DB lookups
        self._seen_node_ids: set[str] = set()
        self._seen_edge_triples: set[Tuple[str, str, str]] = set()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def ingest_all(self) -> IngestReport:
        """Ingest the entire knowledge base and return a summary report."""
        self._report = IngestReport()
        shiprocket_root = self._kb_path / "shiprocket"

        if not shiprocket_root.exists():
            logger.warning("kb.ingest.root_missing", path=str(shiprocket_root))
            self._report.errors.append(f"KB root not found: {shiprocket_root}")
            return self._report

        repo_dirs = [d for d in shiprocket_root.iterdir() if d.is_dir()]
        logger.info("kb.ingest.start", repos=len(repo_dirs), kb_path=str(self._kb_path))

        for repo_dir in sorted(repo_dirs):
            try:
                await self._ingest_repo(str(repo_dir), repo_dir.name)
            except Exception as exc:  # pylint: disable=broad-except
                msg = f"repo={repo_dir.name} error={exc}"
                logger.error("kb.ingest.repo_error", repo=repo_dir.name, error=str(exc))
                self._report.errors.append(msg)

        # Flush any remaining batches
        await self._flush_batches()

        logger.info("kb.ingest.done", **self._report.to_dict())
        return self._report

    # -----------------------------------------------------------------------
    # Repo-level ingestion
    # -----------------------------------------------------------------------

    async def _ingest_repo(self, repo_path: str, repo_name: str) -> None:
        """Process all pillars inside one repository directory."""
        logger.info("kb.ingest.repo", repo=repo_name)
        self._report.repos_processed.append(repo_name)
        base = Path(repo_path)

        # ── Pillar 3: API / MCP tools ──────────────────────────────────────
        pillar3 = base / "pillar_3_api_mcp_tools"
        if pillar3.exists():
            # Intent taxonomy
            taxonomy_path = pillar3 / "intent_taxonomy.yaml"
            if taxonomy_path.exists():
                await self._ingest_intent_taxonomy(str(taxonomy_path), repo_name)

            # Individual API directories
            apis_dir = pillar3 / "apis"
            if apis_dir.exists():
                for api_dir in sorted(apis_dir.iterdir()):
                    if api_dir.is_dir():
                        try:
                            await self._ingest_api(str(api_dir), repo_name)
                            self._report.apis_processed += 1
                        except Exception as exc:  # pylint: disable=broad-except
                            msg = f"repo={repo_name} api={api_dir.name} error={exc}"
                            logger.warning("kb.ingest.api_error", api=api_dir.name, error=str(exc))
                            self._report.errors.append(msg)

        # ── Pillar 1: DB schema ────────────────────────────────────────────
        pillar1 = base / "pillar_1_schema"
        if pillar1.exists():
            tables_dir = pillar1 / "tables"
            if tables_dir.exists():
                for table_dir in sorted(tables_dir.iterdir()):
                    if table_dir.is_dir():
                        try:
                            await self._ingest_table(str(table_dir), repo_name)
                            self._report.tables_processed += 1
                        except Exception as exc:  # pylint: disable=broad-except
                            msg = f"repo={repo_name} table={table_dir.name} error={exc}"
                            logger.warning("kb.ingest.table_error", table=table_dir.name, error=str(exc))
                            self._report.errors.append(msg)

        # ── Pillar 1 extras: connections.yaml → DB connection nodes ─────────
        connections_yaml = pillar1 / "connections.yaml" if pillar1 and pillar1.exists() else None
        if connections_yaml and connections_yaml.exists():
            try:
                conn_data = self._read_yaml(str(connections_yaml))
                if isinstance(conn_data, dict):
                    for conn_name, conn_info in conn_data.get("connections", {}).items():
                        if not isinstance(conn_info, dict):
                            continue
                        conn_node_id = f"db_conn:{repo_name}:{conn_name}"
                        await self._upsert_node(
                            node_id=conn_node_id,
                            node_type=NodeType.module.value,
                            label=f"DB:{conn_name}",
                            repo_id=repo_name,
                            domain="infrastructure",
                            properties={
                                "connection_name": conn_name,
                                "purpose": conn_info.get("purpose", ""),
                                "driver": conn_info.get("driver", ""),
                                "tables": conn_info.get("tables", []),
                            },
                        )
                        # Link tables to their DB connection
                        for tbl in conn_info.get("tables", []):
                            if isinstance(tbl, str):
                                await self._upsert_edge(
                                    source_id=f"table:{tbl}",
                                    target_id=conn_node_id,
                                    edge_type=EdgeType.belongs_to_domain.value,
                                    repo_id=repo_name,
                                    properties={"connection": conn_name},
                                )
            except Exception as exc:
                logger.warning("kb.ingest.connections_error", error=str(exc))

        # ── Pillar 5: Module docs → module nodes + edges ───────────────────
        pillar5 = base / "pillar_5_module_docs" / "modules"
        if pillar5.exists():
            for mod_dir in sorted(pillar5.iterdir()):
                if mod_dir.is_dir():
                    try:
                        await self._ingest_module(str(mod_dir), repo_name)
                    except Exception as exc:
                        msg = f"repo={repo_name} module={mod_dir.name} error={exc}"
                        logger.warning("kb.ingest.module_error", module=mod_dir.name, error=str(exc))
                        self._report.errors.append(msg)

        # Flush after each repo to keep batch sizes bounded
        await self._flush_batches()

    # -----------------------------------------------------------------------
    # Intent taxonomy
    # -----------------------------------------------------------------------

    async def _ingest_intent_taxonomy(self, taxonomy_path: str, repo_name: str) -> None:
        """Parse intent_taxonomy.yaml and create intent nodes."""
        data = self._read_yaml(taxonomy_path)
        intents: List[Dict[str, Any]] = data.get("intents", [])
        if not intents:
            logger.warning("kb.ingest.taxonomy_empty", path=taxonomy_path)
            return

        for entry in intents:
            intent_name = entry.get("intent")
            if not intent_name:
                continue

            node_id = f"intent:{intent_name}"
            props: Dict[str, Any] = {
                "api_count": entry.get("api_count", 0),
                "api_ids_sample": entry.get("api_ids_sample", []),
                "source_file": taxonomy_path,
            }
            is_new = await self._upsert_node(
                node_id=node_id,
                node_type=NodeType.intent.value,
                label=intent_name,
                repo_id=repo_name,
                domain=None,
                properties=props,
            )
            self._report.bump_node(NodeType.intent.value, is_new)
            await self._upsert_lookup("intent_name", intent_name, node_id, repo_name)
            self._report.intents_processed += 1

        logger.debug("kb.ingest.taxonomy_done", repo=repo_name, count=len(intents))

    # -----------------------------------------------------------------------
    # API folder ingestion
    # -----------------------------------------------------------------------

    async def _ingest_api(self, api_dir: str, repo_name: str) -> None:
        """
        Parse one API folder and create:
          - API node (from high.yaml overview section)
          - Tool node  (from high.yaml tool_agent_tags section)
          - Agent node (from high.yaml tool_agent_tags section)
          - Table nodes (from high.yaml db_mapping or request_schema section)
          - Entity lookups (from high.yaml examples section)
          - Edges: API→tool, API→agent, API→intent, API→table, API→domain

        Reads from high.yaml (merged file) or falls back to individual files.
        """
        api_path = Path(api_dir)

        # Load from high.yaml (preferred) or individual files (fallback)
        high = self._read_yaml(str(api_path / "high.yaml"))
        if not high:
            high = {}

        # ── 1. API node from overview section ──────────────────────────────
        # Try high.yaml sections first, fall back to individual files
        index = high.get("index", {}) or self._read_yaml(str(api_path / "index.yaml"))
        overview = high.get("overview", {}) or self._read_yaml(str(api_path / "overview.yaml"))

        api_id = (index.get("api_id") if isinstance(index, dict) else None) or api_path.name
        node_id = f"api:{api_id}"

        summary = index.get("summary", {}) if isinstance(index, dict) else {}
        safety = index.get("safety", {}) if isinstance(index, dict) else {}

        props: Dict[str, Any] = {
            "method": summary.get("method", ""),
            "path": summary.get("path", ""),
            "domain": summary.get("domain", ""),
            "candidate_tool": summary.get("candidate_tool", ""),
            "primary_agent": summary.get("primary_agent", ""),
            "read_write_type": safety.get("read_write_type", ""),
            "idempotent": safety.get("idempotent", None),
            "blast_radius": safety.get("blast_radius", ""),
            "pii_fields": safety.get("pii_fields", []),
        }
        domain_name: Optional[str] = summary.get("domain") or None

        # ── 2. overview section → enrich label ─────────────────────────────
        if isinstance(overview, dict) and overview.get("_status") == "stub":
            overview = self._read_yaml(str(api_path / "overview.yaml"))
        api_data = overview.get("api", {}) if isinstance(overview, dict) else {}
        classification = overview.get("classification", {}) if isinstance(overview, dict) else {}
        retrieval_hints = overview.get("retrieval_hints", {}) if isinstance(overview, dict) else {}

        canonical_summary: str = retrieval_hints.get("canonical_summary", "")
        label: str = canonical_summary or props.get("path") or api_id

        if api_data.get("method"):
            props["method"] = api_data["method"]
        if api_data.get("path"):
            props["path"] = api_data["path"]
        if classification.get("domain"):
            domain_name = classification["domain"]
            props["domain"] = domain_name

        props["keywords"] = retrieval_hints.get("keywords", [])
        props["aliases"] = retrieval_hints.get("aliases", [])

        # Upsert API node
        is_new = await self._upsert_node(
            node_id=node_id,
            node_type=NodeType.api_endpoint.value,
            label=label,
            repo_id=repo_name,
            domain=domain_name,
            properties=props,
        )
        self._report.bump_node(NodeType.api_endpoint.value, is_new)

        # Entity lookups for API path, api_id
        api_path_val: str = props.get("path", "")
        if api_path_val:
            await self._upsert_lookup("api_path", api_path_val, node_id, repo_name)
        await self._upsert_lookup("api_id", api_id, node_id, repo_name)

        # ── 3. domain node + edge ──────────────────────────────────────────
        if domain_name:
            domain_node_id = f"domain:{domain_name}"
            dom_new = await self._upsert_node(
                node_id=domain_node_id,
                node_type=NodeType.domain.value,
                label=domain_name,
                repo_id=repo_name,
                domain=domain_name,
                properties={},
            )
            self._report.bump_node(NodeType.domain.value, dom_new)

            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=domain_node_id,
                edge_type=EdgeType.belongs_to_domain.value,
                repo_id=repo_name,
                properties={},
            )
            self._report.bump_edge(EdgeType.belongs_to_domain.value, edge_new)

        # ── 4. tool_agent_tags → tool + agent nodes + edges ─────────────────
        tags = high.get("tool_agent_tags", {}) or self._read_yaml(str(api_path / "tool_agent_tags.yaml"))
        if isinstance(tags, dict) and tags.get("_status") == "stub":
            tags = self._read_yaml(str(api_path / "tool_agent_tags.yaml"))
        tool_assignment = tags.get("tool_assignment", {})
        agent_assignment = tags.get("agent_assignment", {})
        intent_tags = tags.get("intent_tags", {})
        retrieval_keywords: List[str] = tags.get("retrieval_keywords", [])

        if retrieval_keywords:
            props["retrieval_keywords"] = retrieval_keywords
            # Re-upsert API node with enriched properties (no bump — same node)
            await self._upsert_node(
                node_id=node_id,
                node_type=NodeType.api_endpoint.value,
                label=label,
                repo_id=repo_name,
                domain=domain_name,
                properties=props,
            )

        tool_candidate: Optional[str] = tool_assignment.get("tool_candidate")
        if tool_candidate:
            tool_node_id = f"tool:{tool_candidate}"
            tool_props: Dict[str, Any] = {
                "tool_group": tool_assignment.get("tool_group", ""),
                "read_write_type": tool_assignment.get("read_write_type", ""),
                "risk_level": tool_assignment.get("risk_level", ""),
                "approval_mode": tool_assignment.get("approval_mode", ""),
            }
            tool_new = await self._upsert_node(
                node_id=tool_node_id,
                node_type=NodeType.tool.value,
                label=tool_candidate,
                repo_id=repo_name,
                domain=domain_name,
                properties=tool_props,
            )
            self._report.bump_node(NodeType.tool.value, tool_new)

            # API → tool edge
            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=tool_node_id,
                edge_type=EdgeType.implements_tool.value,
                repo_id=repo_name,
                properties={},
            )
            self._report.bump_edge(EdgeType.implements_tool.value, edge_new)

            # Tool entity lookup
            await self._upsert_lookup("tool_name", tool_candidate, node_id, repo_name)

        # Agent nodes + edges
        owner_agent: Optional[str] = agent_assignment.get("owner")
        secondary_raw = agent_assignment.get("secondary", [])
        secondary_agents: List[str] = (
            secondary_raw if isinstance(secondary_raw, list)
            else [secondary_raw] if secondary_raw else []
        )

        for agent_name in filter(None, [owner_agent] + secondary_agents):
            agent_node_id = f"agent:{agent_name}"
            agent_new = await self._upsert_node(
                node_id=agent_node_id,
                node_type=NodeType.agent.value,
                label=agent_name,
                repo_id=repo_name,
                domain=domain_name,
                properties={"role": "owner" if agent_name == owner_agent else "secondary"},
            )
            self._report.bump_node(NodeType.agent.value, agent_new)

            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=agent_node_id,
                edge_type=EdgeType.assigned_to_agent.value,
                repo_id=repo_name,
                properties={"role": "owner" if agent_name == owner_agent else "secondary"},
            )
            self._report.bump_edge(EdgeType.assigned_to_agent.value, edge_new)

        # Intent edges
        primary_intent: Optional[str] = intent_tags.get("primary")
        secondary_intents: List[str] = intent_tags.get("secondary", [])
        if isinstance(secondary_intents, str):
            secondary_intents = [secondary_intents]

        for intent_name in filter(None, [primary_intent] + secondary_intents):
            intent_node_id = f"intent:{intent_name}"
            # Ensure intent node exists (may have been created from taxonomy)
            if intent_node_id not in self._seen_node_ids:
                int_new = await self._upsert_node(
                    node_id=intent_node_id,
                    node_type=NodeType.intent.value,
                    label=intent_name,
                    repo_id=repo_name,
                    domain=domain_name,
                    properties={},
                )
                self._report.bump_node(NodeType.intent.value, int_new)

            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=intent_node_id,
                edge_type=EdgeType.has_intent.value,
                repo_id=repo_name,
                properties={"role": "primary" if intent_name == primary_intent else "secondary"},
            )
            self._report.bump_edge(EdgeType.has_intent.value, edge_new)

        # ── 5. db_mapping → table nodes + reads/writes edges ────────────────
        db_mapping = high.get("db_mapping", {}) or self._read_yaml(str(api_path / "db_mapping.yaml"))
        if isinstance(db_mapping, dict) and db_mapping.get("_status") == "stub":
            db_mapping = self._read_yaml(str(api_path / "db_mapping.yaml"))
        primary_table = db_mapping.get("primary_table", {})
        related_tables: List[Dict[str, Any]] = db_mapping.get("related_tables", [])

        all_table_entries: List[Dict[str, Any]] = []
        if primary_table and primary_table.get("name"):
            all_table_entries.append(primary_table)
        all_table_entries.extend(t for t in related_tables if t.get("name"))

        for tbl in all_table_entries:
            tbl_name: str = tbl["name"]
            tbl_role: str = tbl.get("role", "read")
            tbl_node_id = f"table:{tbl_name}"

            tbl_new = await self._upsert_node(
                node_id=tbl_node_id,
                node_type=NodeType.table.value,
                label=tbl_name,
                repo_id=repo_name,
                domain=domain_name,
                properties={"role": tbl_role},
            )
            self._report.bump_node(NodeType.table.value, tbl_new)

            # Determine edge type based on role
            write_keywords = {"write", "writes", "insert", "update", "delete", "create"}
            role_lower = tbl_role.lower()
            if any(kw in role_lower for kw in write_keywords):
                etype = EdgeType.writes_table.value
            else:
                etype = EdgeType.reads_table.value

            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=tbl_node_id,
                edge_type=etype,
                repo_id=repo_name,
                properties={"role": tbl_role},
            )
            self._report.bump_edge(etype, edge_new)

        # ── 6. examples → entity_lookup from param_extraction_pairs ─────────
        examples = high.get("examples", {}) or self._read_yaml(str(api_path / "examples.yaml"))
        if isinstance(examples, dict) and examples.get("_status") == "stub":
            examples = self._read_yaml(str(api_path / "examples.yaml"))
        pairs: List[Dict[str, Any]] = examples.get("param_extraction_pairs", [])

        for pair in pairs:
            params: Dict[str, Any] = pair.get("params", {})
            for param_key, param_val in params.items():
                if param_val and isinstance(param_val, (str, int, float)):
                    await self._upsert_lookup(
                        entity_type=f"param:{param_key}",
                        entity_value=str(param_val),
                        node_id=node_id,
                        repo_id=repo_name,
                    )

        logger.debug("kb.ingest.api_done", api_id=api_id, repo=repo_name)

    # -----------------------------------------------------------------------
    # Table folder ingestion
    # -----------------------------------------------------------------------

    async def _ingest_table(self, table_dir: str, repo_name: str) -> None:
        """
        Parse one pillar_1_schema table directory:
          - high.yaml → _meta + columns sections (preferred)
          - Falls back to _meta.yaml + columns.yaml
          - domain edge
        """
        tbl_path = Path(table_dir)
        table_name = tbl_path.name
        node_id = f"table:{table_name}"

        # Load from high.yaml (preferred) or individual files
        high = self._read_yaml(str(tbl_path / "high.yaml"))
        if not high:
            high = {}

        # ── _meta section ───────────────────────────────────────────────────
        meta = high.get("_meta", {})
        if isinstance(meta, dict) and meta.get("_status") == "stub":
            meta = self._read_yaml(str(tbl_path / "_meta.yaml"))
        if not meta or not isinstance(meta, dict):
            meta = self._read_yaml(str(tbl_path / "_meta.yaml"))
        domain_name: Optional[str] = meta.get("domain") or None
        description: str = meta.get("description", "")
        canonical_table: str = meta.get("canonical_table", table_name)
        label = canonical_table or table_name

        # ── columns section ─────────────────────────────────────────────────
        col_data = high.get("columns", {})
        if isinstance(col_data, dict) and col_data.get("_status") == "stub":
            col_data = self._read_yaml(str(tbl_path / "columns.yaml"))
        if not col_data or not isinstance(col_data, dict):
            col_data = self._read_yaml(str(tbl_path / "columns.yaml"))
        columns: List[Dict[str, Any]] = col_data.get("columns", []) if isinstance(col_data, dict) else []

        props: Dict[str, Any] = {
            "description": description,
            "columns": columns,
        }
        if domain_name:
            props["domain"] = domain_name

        is_new = await self._upsert_node(
            node_id=node_id,
            node_type=NodeType.table.value,
            label=label,
            repo_id=repo_name,
            domain=domain_name,
            properties=props,
        )
        self._report.bump_node(NodeType.table.value, is_new)

        # Lookup by table name
        await self._upsert_lookup("table_name", table_name, node_id, repo_name)
        if canonical_table != table_name:
            await self._upsert_lookup("table_name", canonical_table, node_id, repo_name)

        # ── domain node + edge ──────────────────────────────────────────────
        if domain_name:
            domain_node_id = f"domain:{domain_name}"
            dom_new = await self._upsert_node(
                node_id=domain_node_id,
                node_type=NodeType.domain.value,
                label=domain_name,
                repo_id=repo_name,
                domain=domain_name,
                properties={},
            )
            self._report.bump_node(NodeType.domain.value, dom_new)

            edge_new = await self._upsert_edge(
                source_id=node_id,
                target_id=domain_node_id,
                edge_type=EdgeType.belongs_to_domain.value,
                repo_id=repo_name,
                properties={},
            )
            self._report.bump_edge(EdgeType.belongs_to_domain.value, edge_new)

        logger.debug("kb.ingest.table_done", table=table_name, repo=repo_name)

    # -----------------------------------------------------------------------
    # Module folder ingestion (Pillar 5)
    # -----------------------------------------------------------------------

    async def _ingest_module(self, module_dir: str, repo_name: str) -> None:
        """Ingest a Pillar 5 module into the graph (M1 fix: deep ingestion).

        Creates module node with rich properties and edges to:
        - Domain (belongs_to_domain)
        - Tables mentioned in index.yaml (reads_table / writes_table)
        - APIs mentioned in index.yaml (has_api)
        - Related modules via cross-links
        """
        mod_path = Path(module_dir)
        module_name = mod_path.name
        node_id = f"module:{repo_name}:{module_name}"

        # Read index.yaml for metadata + cross-references
        index_data = self._read_yaml(str(mod_path / "index.yaml"))
        if not isinstance(index_data, dict):
            index_data = {}

        props: Dict[str, Any] = {
            "repo": repo_name,
            "module": module_name,
            "quality_score": index_data.get("quality_score", 0),
            "training_ready": index_data.get("training_ready", False),
        }

        # Extract rich metadata from index.yaml
        top_entities = index_data.get("top_entities", {})
        if isinstance(top_entities, dict):
            props["controllers"] = top_entities.get("controllers", 0)
            props["api_routes"] = top_entities.get("api_routes", 0)
            props["db_tables"] = top_entities.get("db_tables", 0)

        # Extract file list, features, dependencies from other YAML files
        for extra_file in ["dependencies.yaml", "CLAUDE.yaml"]:
            extra = self._read_yaml(str(mod_path / extra_file))
            if isinstance(extra, dict) and extra.get("content"):
                # Store a snippet for graph node properties
                content = extra.get("content", "")
                if isinstance(content, str) and len(content) > 50:
                    props[f"_{extra_file.replace('.yaml', '')}_preview"] = content[:500]

        is_new = await self._upsert_node(
            node_id=node_id,
            node_type=NodeType.module.value,
            label=module_name,
            repo_id=repo_name,
            domain=module_name,
            properties=props,
        )
        self._report.bump_node(NodeType.module.value, is_new)

        # Lookups
        await self._upsert_lookup("module_name", module_name, node_id, repo_name)

        # Link module → domain
        domain_node_id = f"domain:{module_name}"
        if domain_node_id not in self._seen_node_ids:
            await self._upsert_node(
                node_id=domain_node_id,
                node_type=NodeType.domain.value,
                label=module_name,
                repo_id=repo_name,
                domain=module_name,
                properties={},
            )
        await self._upsert_edge(
            source_id=node_id,
            target_id=domain_node_id,
            edge_type=EdgeType.belongs_to_domain.value,
            repo_id=repo_name,
            properties={},
        )

        # Link module → tables (from index.yaml pillar1_links or cross_links.yaml)
        cross_links_path = mod_path.parent.parent / "cross_links.yaml"
        cross_links = self._read_yaml(str(cross_links_path))
        if isinstance(cross_links, dict):
            module_tables = cross_links.get("module_to_tables", {}).get(module_name, [])
            for table_ref in module_tables:
                # table_ref like "pillar_1_schema/tables/orders"
                table_name = table_ref.split("/")[-1] if "/" in str(table_ref) else str(table_ref)
                table_node_id = f"table:{table_name}"
                await self._upsert_edge(
                    source_id=node_id,
                    target_id=table_node_id,
                    edge_type=EdgeType.reads_table.value,
                    repo_id=repo_name,
                    properties={"source": "pillar5_cross_link"},
                )

        logger.debug("kb.ingest.module_done", module=module_name, repo=repo_name)

    # -----------------------------------------------------------------------
    # Primitive upsert helpers
    # -----------------------------------------------------------------------

    async def _upsert_node(
        self,
        node_id: str,
        node_type: str,
        label: str,
        repo_id: Optional[str],
        domain: Optional[str],
        properties: Dict[str, Any],
    ) -> bool:
        """
        Buffer a node for bulk upsert.

        Returns True if this is the first time we've seen this node_id in
        the current run (approximation — does not query DB), False otherwise.
        """
        is_new = node_id not in self._seen_node_ids
        self._seen_node_ids.add(node_id)

        now = datetime.now(timezone.utc)
        self._node_batch.append({
            "id": node_id,
            "node_type": node_type,
            "label": label,
            "repo_id": repo_id,
            "domain": domain,
            "properties": properties,
            "created_at": now,
            "updated_at": now,
        })

        # Flush when batch gets large
        if len(self._node_batch) >= 200:
            await self._flush_nodes()

        return is_new

    async def _upsert_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        repo_id: Optional[str],
        properties: Dict[str, Any],
    ) -> bool:
        """
        Buffer an edge for bulk upsert.

        Returns True if this (source, target, edge_type) triple is new
        within the current run.
        """
        triple_key = (source_id, target_id, edge_type)
        is_new = triple_key not in self._seen_edge_triples
        self._seen_edge_triples.add(triple_key)  # type: ignore[attr-defined]

        now = datetime.now(timezone.utc)
        self._edge_batch.append({
            "id": str(uuid.uuid4()),
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "weight": 1.0,
            "repo_id": repo_id,
            "properties": properties,
            "created_at": now,
        })

        if len(self._edge_batch) >= 200:
            await self._flush_edges()

        return is_new

    async def _upsert_lookup(
        self,
        entity_type: str,
        entity_value: str,
        node_id: str,
        repo_id: Optional[str],
    ) -> None:
        """Buffer an entity_lookup row for bulk upsert."""
        self._lookup_batch.append({
            "entity_type": entity_type,
            "entity_value": entity_value,
            "node_id": node_id,
            "repo_id": repo_id,
        })
        self._report.lookups_upserted += 1

        if len(self._lookup_batch) >= 500:
            await self._flush_lookups()

    # -----------------------------------------------------------------------
    # Batch flush helpers
    # -----------------------------------------------------------------------

    async def _flush_batches(self) -> None:
        """Flush all pending node / edge / lookup batches to PostgreSQL."""
        await self._flush_nodes()
        await self._flush_edges()
        await self._flush_lookups()

    async def _flush_nodes(self) -> None:
        if not self._node_batch:
            return
        batch = self._node_batch[:]
        self._node_batch.clear()

        async with AsyncSessionLocal() as session:
            stmt = pg_insert(GraphNodeRow).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "node_type": stmt.excluded.node_type,
                    "label": stmt.excluded.label,
                    "repo_id": stmt.excluded.repo_id,
                    "domain": stmt.excluded.domain,
                    "properties": stmt.excluded.properties,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

        logger.debug("kb.flush.nodes", count=len(batch))

    async def _flush_edges(self) -> None:
        if not self._edge_batch:
            return
        batch = self._edge_batch[:]
        self._edge_batch.clear()

        async with AsyncSessionLocal() as session:
            stmt = pg_insert(GraphEdgeRow).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="idx_graph_edges_unique_triple",
                set_={
                    "weight": stmt.excluded.weight,
                    "repo_id": stmt.excluded.repo_id,
                    "properties": stmt.excluded.properties,
                },
            )
            await session.execute(stmt)
            await session.commit()

        logger.debug("kb.flush.edges", count=len(batch))

    async def _flush_lookups(self) -> None:
        if not self._lookup_batch:
            return
        batch = self._lookup_batch[:]
        self._lookup_batch.clear()

        async with AsyncSessionLocal() as session:
            stmt = pg_insert(EntityLookupRow).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["entity_type", "entity_value"],
                set_={
                    "node_id": stmt.excluded.node_id,
                    "repo_id": stmt.excluded.repo_id,
                },
            )
            await session.execute(stmt)
            await session.commit()

        logger.debug("kb.flush.lookups", count=len(batch))

    # -----------------------------------------------------------------------
    # YAML reader
    # -----------------------------------------------------------------------

    @staticmethod
    def _read_yaml(filepath: str) -> Dict[str, Any]:
        """
        Safe YAML reader.  Returns an empty dict on any error (missing file,
        parse error, non-dict root).
        """
        p = Path(filepath)
        if not p.exists():
            return {}
        try:
            with p.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if not isinstance(data, dict):
                logger.warning("kb.yaml.non_dict", path=filepath, type=type(data).__name__)
                return {}
            return data
        except yaml.YAMLError as exc:
            logger.warning("kb.yaml.parse_error", path=filepath, error=str(exc))
            return {}
        except OSError as exc:
            logger.warning("kb.yaml.read_error", path=filepath, error=str(exc))
            return {}

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Query live counts from graph_nodes, graph_edges, entity_lookup."""
        async with AsyncSessionLocal() as session:
            node_count = (
                await session.execute(select(func.count()).select_from(GraphNodeRow))
            ).scalar_one()
            edge_count = (
                await session.execute(select(func.count()).select_from(GraphEdgeRow))
            ).scalar_one()
            lookup_count = (
                await session.execute(select(func.count()).select_from(EntityLookupRow))
            ).scalar_one()

            # Per node-type breakdown
            node_type_rows = (
                await session.execute(
                    select(GraphNodeRow.node_type, func.count().label("cnt"))
                    .group_by(GraphNodeRow.node_type)
                )
            ).all()
            node_type_counts = {row.node_type: row.cnt for row in node_type_rows}

            # Per edge-type breakdown
            edge_type_rows = (
                await session.execute(
                    select(GraphEdgeRow.edge_type, func.count().label("cnt"))
                    .group_by(GraphEdgeRow.edge_type)
                )
            ).all()
            edge_type_counts = {row.edge_type: row.cnt for row in edge_type_rows}

        return {
            "total_nodes": node_count,
            "total_edges": edge_count,
            "total_lookups": lookup_count,
            "node_type_counts": node_type_counts,
            "edge_type_counts": edge_type_counts,
        }


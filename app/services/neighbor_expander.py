"""
NeighborExpander — sibling chunk expansion for COSMOS retrieval.

Given a list of top-ranked nodes (each with metadata.parent_doc_id and
metadata.chunk_index), fetches adjacent chunks from cosmos_embeddings to
provide surrounding evidence without re-ranking.

Phase 2 addition: enables neighbor chunk expansion in Wave 2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)


@dataclass
class NeighborChunk:
    """A sibling chunk fetched from cosmos_embeddings."""

    entity_type: str
    entity_id: str
    content: str
    chunk_type: str
    chunk_index: int
    parent_doc_id: str
    section: str
    pillar: str = ""


@dataclass
class NeighborExpansionResult:
    """Result of neighbor chunk expansion for a set of top-ranked nodes."""

    neighbor_chunks: List[NeighborChunk] = field(default_factory=list)
    parents_expanded: int = 0
    latency_ms: float = 0.0


class NeighborExpander:
    """
    Fetches sibling chunks adjacent to top-ranked results.

    For each of the top-N ranked nodes that has a parent_doc_id and chunk_index
    in its properties/metadata, queries cosmos_embeddings for chunks at
    chunk_index±window. This provides surrounding context (e.g., the example
    just before or the schema just after an api_overview) without an extra
    embedding call.

    SQL:
        SELECT entity_type, entity_id, content, metadata
        FROM cosmos_embeddings
        WHERE metadata->>'parent_doc_id' = :parent_doc_id
          AND (metadata->>'chunk_index')::int BETWEEN :lo AND :hi
        ORDER BY (metadata->>'chunk_index')::int
    """

    def __init__(self, window: int = 1, max_parents: int = 5) -> None:
        """
        Args:
            window: How many chunks before/after to fetch (default 1 = ±1).
            max_parents: Max distinct parent_doc_ids to expand (default 5).
        """
        self._window = window
        self._max_parents = max_parents

    async def expand(
        self,
        ranked_nodes: List[Any],
        exclude_entity_ids: Optional[Set[str]] = None,
        vector_hits: Optional[List[Dict[str, Any]]] = None,
    ) -> NeighborExpansionResult:
        """
        Expand top-ranked nodes into sibling chunks.

        Most graph nodes (api_endpoint, module, table) do NOT carry parent_doc_id
        or chunk_index — those are vector-layer metadata properties. Only vector
        proxy nodes and raw vector hit dicts have them.

        This method therefore sources parent_doc_id/chunk_index from two places
        in priority order:
          1. vector_hits — raw cosmos_embeddings rows passed from the probe stage
             (these always have full metadata JSONB, including parent_doc_id and
             chunk_index for any chunk written with Phase 1/2 metadata).
          2. ranked_nodes where _source == "vector_proxy" — proxy graph nodes
             created from unresolved vector hits (metadata is spread into properties).
        Plain graph nodes (api_endpoint, module, table) are skipped silently.

        Args:
            ranked_nodes: Ranked results from HybridRetriever (RetrievedNode list).
            exclude_entity_ids: entity_ids already included — skip them.
            vector_hits: Raw cosmos_embeddings dicts from Stage 1 probe (preferred
                         source — always have full metadata). Pass
                         probe_vector.data["chunks"] here.

        Returns:
            NeighborExpansionResult with sibling chunks sorted by parent+chunk_index.
        """
        t0 = time.monotonic()
        result = NeighborExpansionResult()
        exclude: Set[str] = exclude_entity_ids or set()

        parents_to_expand: Dict[str, int] = {}  # parent_doc_id → center chunk_index

        # ── Priority 1: raw vector hits (best source — full metadata always present)
        for hit in (vector_hits or []):
            if not isinstance(hit, dict):
                continue
            meta: Dict[str, Any] = hit.get("metadata") or {}
            parent_doc_id = meta.get("parent_doc_id")
            chunk_idx_raw = meta.get("chunk_index")
            if not parent_doc_id or chunk_idx_raw is None:
                continue
            try:
                chunk_idx = int(chunk_idx_raw)
            except (ValueError, TypeError):
                continue
            if parent_doc_id not in parents_to_expand:
                parents_to_expand[parent_doc_id] = chunk_idx
                if len(parents_to_expand) >= self._max_parents:
                    break

        # ── Priority 2: vector proxy nodes from ranked results (unresolved hits)
        for rn in ranked_nodes:
            if len(parents_to_expand) >= self._max_parents:
                break
            props = _extract_props(rn)
            # Only expand proxy nodes — real graph nodes don't carry chunk metadata
            if props.get("_source") != "vector_proxy":
                continue
            parent_doc_id = props.get("parent_doc_id")
            chunk_idx_raw = props.get("chunk_index")
            if not parent_doc_id or chunk_idx_raw is None:
                continue
            try:
                chunk_idx = int(chunk_idx_raw)
            except (ValueError, TypeError):
                continue
            if parent_doc_id not in parents_to_expand:
                parents_to_expand[parent_doc_id] = chunk_idx

        if not parents_to_expand:
            result.latency_ms = (time.monotonic() - t0) * 1000
            return result

        # ── Query cosmos_embeddings for each parent's sibling range ──────────
        try:
            async with AsyncSessionLocal() as session:
                for parent_doc_id, chunk_idx in parents_to_expand.items():
                    lo = max(0, chunk_idx - self._window)
                    hi = chunk_idx + self._window

                    rows = await session.execute(
                        text("""
                            SELECT entity_type, entity_id, content, metadata
                            FROM cosmos_embeddings
                            WHERE metadata->>'parent_doc_id' = :parent_doc_id
                              AND (metadata->>'chunk_index')::int BETWEEN :lo AND :hi
                            ORDER BY (metadata->>'chunk_index')::int
                            LIMIT 10
                        """),
                        {"parent_doc_id": parent_doc_id, "lo": lo, "hi": hi},
                    )

                    for row in rows.fetchall():
                        eid = row[1] or ""
                        if eid in exclude:
                            continue
                        meta: Dict[str, Any] = row[3] or {}
                        row_chunk_idx = meta.get("chunk_index", chunk_idx)

                        # Skip the center chunk (already in ranked results)
                        try:
                            if int(row_chunk_idx) == chunk_idx:
                                continue
                        except (ValueError, TypeError):
                            pass

                        result.neighbor_chunks.append(NeighborChunk(
                            entity_type=row[0] or "",
                            entity_id=eid,
                            content=row[2] or "",
                            chunk_type=meta.get("chunk_type", ""),
                            chunk_index=int(row_chunk_idx) if row_chunk_idx is not None else 0,
                            parent_doc_id=parent_doc_id,
                            section=meta.get("section", ""),
                            pillar=meta.get("pillar", ""),
                        ))

                    result.parents_expanded += 1

        except Exception as exc:
            logger.warning("neighbor_expander.db_error", error=str(exc))

        result.latency_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "neighbor_expander.done",
            parents=result.parents_expanded,
            chunks=len(result.neighbor_chunks),
            latency_ms=round(result.latency_ms, 1),
        )
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_props(rn: Any) -> Dict[str, Any]:
    """Extract metadata properties dict from a ranked node (RetrievedNode or dict)."""
    # RetrievedNode from graph/retrieval.py
    if hasattr(rn, "node") and hasattr(rn.node, "properties"):
        return rn.node.properties or {}
    # Dict with nested metadata
    if isinstance(rn, dict):
        # Direct properties key
        if "properties" in rn:
            return rn["properties"] or {}
        # metadata key (vector search results)
        if "metadata" in rn:
            return rn["metadata"] or {}
        return rn
    return {}

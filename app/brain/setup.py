"""
Brain setup — Factory to create and wire brain components.
"""

import structlog

from app.brain.graph import QueryGraph
from app.brain.indexer import KnowledgeIndexer
from app.brain.pipeline import KBUpdatePipeline
from app.brain.router import IntelligentRouter
from app.graph.ingest import CanonicalIngestionPipeline

logger = structlog.get_logger()


def create_brain(
    kb_path: str, llm_client=None, mcapi_client=None
) -> dict:
    """Create and initialize the RAG brain with 3-tier routing.

    Returns dict with:
    - indexer: KnowledgeIndexer (indexed)
    - router: IntelligentRouter (3-tier: decision tree + tool-use + reasoning)
    - graph: QueryGraph (for Tier 3 full reasoning)
    - pipeline: KBUpdatePipeline (ready for updates)
    - graph_ingest: CanonicalIngestionPipeline (KB → typed graph)
    - document_count: int
    - routing_stats: dict
    """
    logger.info("Initializing RAG brain", kb_path=kb_path)

    indexer = KnowledgeIndexer(kb_path)
    count = indexer.index_all()

    logger.info(
        "Knowledge base indexed",
        document_count=count,
        stats=indexer.get_stats(),
    )

    # Build 3-tier router on top of indexer
    router = IntelligentRouter(indexer)
    routing_stats = router.build()

    logger.info(
        "3-tier router built",
        tier1_entries=routing_stats.get("tier1_entries", 0),
        tier2_tool_count=routing_stats.get("tier2_tool_count", 0),
    )

    graph = QueryGraph(indexer, llm_client, mcapi_client)
    pipeline = KBUpdatePipeline(indexer, kb_path)
    pipeline.snapshot_hashes()

    # Canonical graph ingestion pipeline (KB YAMLs → typed Postgres graph)
    graph_ingest = CanonicalIngestionPipeline(kb_path=kb_path)

    return {
        "indexer": indexer,
        "router": router,
        "graph": graph,
        "pipeline": pipeline,
        "graph_ingest": graph_ingest,
        "document_count": count,
        "routing_stats": routing_stats,
    }

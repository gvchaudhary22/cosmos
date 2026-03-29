"""
COSMOS RAG Brain — Knowledge Base Indexer + LangGraph Query Processor.

Indexes 5,620+ API endpoints and 677 DB tables from the MARS knowledge_base
into an in-memory TF-IDF vector store, then retrieves the top-K relevant
documents for each user query. This replaces loading all tool definitions
into the LLM context, saving ~80% tokens.

Components:
  - KnowledgeIndexer: TF-IDF based in-memory vector search over YAML files
  - QueryGraph: LangGraph-style state machine for query processing
  - KBUpdatePipeline: Auto-update pipeline for knowledge base changes
  - create_brain: Factory to wire all components together
"""

from app.brain.indexer import KBDocument, KnowledgeIndexer
from app.brain.graph import GraphPhase, QueryGraph, QueryState
from app.brain.hierarchy import HierarchicalIndex, HierarchyNode
from app.brain.pipeline import IndexUpdate, KBUpdatePipeline
from app.brain.router import IntelligentRouter, RouteResult, RoutingTier
from app.brain.cache import CacheEntry, SemanticCache
from app.brain.grel import (
    ApprovalStatus,
    GatheredData,
    GRELEngine,
    GRELPhase,
    GRELResult,
    LearningInsight,
    LearningType,
    SynthesisResult,
)
from app.brain.wiring import KBScanScheduler, wire_brain
from app.brain.setup import create_brain

__all__ = [
    "KBDocument",
    "KnowledgeIndexer",
    "GraphPhase",
    "QueryGraph",
    "QueryState",
    "HierarchicalIndex",
    "HierarchyNode",
    "IndexUpdate",
    "KBUpdatePipeline",
    "IntelligentRouter",
    "RouteResult",
    "RoutingTier",
    "CacheEntry",
    "SemanticCache",
    "ApprovalStatus",
    "GatheredData",
    "GRELEngine",
    "GRELPhase",
    "GRELResult",
    "LearningInsight",
    "LearningType",
    "SynthesisResult",
    "KBScanScheduler",
    "wire_brain",
    "create_brain",
]

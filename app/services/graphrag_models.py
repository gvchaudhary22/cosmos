"""
Pydantic models for the GraphRAG service.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class NodeType(str, Enum):
    # Code structure
    module = "module"
    function = "function"
    file = "file"
    # Logistics domain
    courier = "courier"
    seller = "seller"
    channel = "channel"
    # KB-derived typed nodes
    api_endpoint = "api_endpoint"
    table = "table"
    domain = "domain"
    intent = "intent"
    tool = "tool"
    agent = "agent"
    # Phase 3: Pillar 4 page/role nodes
    page = "page"
    role = "role"
    # Phase 5c: Pillar 3 request-schema field nodes
    schema_field = "schema_field"
    # Pillar 6: Action contracts
    action_contract = "action_contract"
    # Pillar 7: Workflow runbooks
    workflow = "workflow"
    # Pillar 9/10/11: Agent, Skill, Tool definitions
    skill = "skill"


class EdgeType(str, Enum):
    # Code structure
    depends_on = "depends_on"
    imports = "imports"
    calls = "calls"
    # Logistics domain
    delivers_for = "delivers_for"
    sells_on = "sells_on"
    has_ndr = "has_ndr"
    connects = "connects"
    # KB-derived typed edges
    belongs_to_domain = "belongs_to_domain"
    implements_tool = "implements_tool"
    assigned_to_agent = "assigned_to_agent"
    has_intent = "has_intent"
    reads_table = "reads_table"
    writes_table = "writes_table"
    cross_repo_shares = "cross_repo_shares"
    has_api = "has_api"
    # Phase 3: Pillar 4 page graph edges
    has_action = "has_action"       # page → api_endpoint (from api_bindings.yaml)
    requires_role = "requires_role"  # page → role (from role_permissions.yaml)
    # Phase 5c: API → schema field edges
    has_field = "has_field"          # api_endpoint → schema_field (from request_schema.yaml)
    # Pillar 6/7: Action and workflow edges
    uses_action = "uses_action"      # workflow → action_contract
    has_precondition = "has_precondition"  # action_contract → table (precondition check)
    dispatches_job = "dispatches_job"      # action_contract → action_contract (async follow-up)
    calls_api = "calls_api"               # action_contract → api_endpoint
    # Pillar 9/10/11: Agent, Skill, Tool edges
    agent_has_skill = "agent_has_skill"   # agent → skill
    skill_calls_tool = "skill_calls_tool" # skill → tool


# ── Universal identity context ─────────────────────────────────────────────

class MarsContext(BaseModel):
    """Universal identity block attached to every GraphRAG request."""
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    repo_id: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    action: Optional[str] = None
    user_id: Optional[str] = None
    callback_url: Optional[str] = None
    session_id: Optional[str] = None


# ── Core graph objects ─────────────────────────────────────────────────────

class GraphNode(BaseModel):
    id: str
    node_type: NodeType
    label: str
    repo_id: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class GraphEdge(BaseModel):
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    repo_id: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Query / traversal results ──────────────────────────────────────────────

class TraversalResult(BaseModel):
    root_id: str
    max_depth: int
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    total_nodes: int
    total_edges: int


class QueryResult(BaseModel):
    query: str
    matched_nodes: List[GraphNode]
    related_nodes: List[GraphNode]
    related_edges: List[GraphEdge]
    total_matches: int


class GraphStats(BaseModel):
    total_nodes: int
    total_edges: int
    node_type_counts: Dict[str, int]
    edge_type_counts: Dict[str, int]
    connected_components: int
    avg_degree: float


# ── Ingest request bodies ─────────────────────────────────────────────────

class IngestModuleDepsRequest(BaseModel):
    """Ingest a batch of module-level dependency edges."""
    repo_id: str
    modules: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "List of dicts with keys: source (str), target (str), "
            "edge_type (str, default depends_on), properties (dict, optional)"
        ),
    )
    context: Optional[MarsContext] = None


class IngestCourierRequest(BaseModel):
    """Ingest a courier <-> seller / channel relationship."""
    repo_id: str
    courier_id: str
    courier_name: str
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    ndr_count: int = 0
    properties: Dict[str, Any] = Field(default_factory=dict)
    context: Optional[MarsContext] = None


class IngestChannelRequest(BaseModel):
    """Ingest a channel <-> seller relationship."""
    repo_id: str
    channel_id: str
    channel_name: str
    seller_id: str
    seller_name: str
    properties: Dict[str, Any] = Field(default_factory=dict)
    context: Optional[MarsContext] = None


# ── Generic query request / response ──────────────────────────────────────

class GraphQueryRequest(BaseModel):
    q: str
    repo_id: Optional[str] = None
    node_type: Optional[NodeType] = None
    max_depth: int = 2
    limit: int = 20
    context: Optional[MarsContext] = None


class GraphQueryResponse(BaseModel):
    query: str
    results: QueryResult
    context_text: Optional[str] = None


# ── Hybrid retrieval request / response ───────────────────────────────────

class HybridRetrieveRequest(BaseModel):
    """Request body for the unified hybrid retrieval endpoint."""
    query: str = Field(..., min_length=1, description="Natural language query")
    intent: Optional[str] = Field(None, description="Classified intent (e.g. tracking, billing)")
    entity: Optional[str] = Field(None, description="Entity type (e.g. awb, order_id, seller_id)")
    entity_id: Optional[str] = Field(None, description="Entity value (e.g. 12345)")
    repo_id: Optional[str] = Field(None, description="Filter by repository")
    max_depth: int = Field(2, ge=0, le=5, description="BFS expansion depth")
    top_k: int = Field(10, ge=1, le=50, description="Max results to return")
    max_context_tokens: int = Field(4000, ge=500, le=16000, description="Token budget for context assembly")
    context: Optional[MarsContext] = None


class RetrievedNodeResponse(BaseModel):
    """A single retrieved node with score and provenance."""
    id: str
    node_type: str
    label: str
    score: float
    sources: List[str]
    rank_by_leg: Dict[str, int]
    repo_id: Optional[str] = None
    domain: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)


class LegDiagnostics(BaseModel):
    """Per-leg retrieval diagnostics."""
    leg_name: str
    hit_count: int
    latency_ms: float


class HybridRetrieveResponse(BaseModel):
    """Response from the unified hybrid retrieval endpoint."""
    query: str
    intent: Optional[str] = None
    entity: Optional[str] = None
    entity_id: Optional[str] = None

    ranked_nodes: List[RetrievedNodeResponse]
    context_text: str
    context_token_estimate: int

    # Diagnostics
    leg_diagnostics: List[LegDiagnostics]
    total_latency_ms: float
    fusion_method: str = "rrf_k60"
    total_results: int

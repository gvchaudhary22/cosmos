"""
Vector store API endpoints for COSMOS.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from app.services.vectorstore import VectorStoreService

router = APIRouter(tags=["vectorstore"])

_service = VectorStoreService()


# --- Request / Response Models ---


class EmbedRequest(BaseModel):
    entity_type: str = Field(..., description="Type of entity (e.g. 'ticket', 'faq', 'knowledge')")
    entity_id: str = Field(..., description="Unique identifier for the entity")
    content: str = Field(..., min_length=1, description="Text content to embed")
    repo_id: Optional[str] = Field(None, description="Repository / tenant identifier")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata to store")


class EmbedResponse(BaseModel):
    id: str
    message: str = "Embedding stored"


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query text")
    limit: int = Field(5, ge=1, le=100, description="Max results to return")
    entity_type: Optional[str] = Field(None, description="Filter by entity type")
    repo_id: Optional[str] = Field(None, description="Filter by repo id")
    threshold: float = Field(0.0, ge=0.0, le=1.0, description="Minimum similarity threshold")


class SearchResult(BaseModel):
    id: str
    repo_id: Optional[str] = None
    entity_type: str
    entity_id: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    similarity: float


class BatchItem(BaseModel):
    entity_type: str
    entity_id: str
    content: str
    metadata: Optional[Dict[str, Any]] = None


class BatchRequest(BaseModel):
    items: List[BatchItem] = Field(..., min_length=1, max_length=500)
    repo_id: Optional[str] = None


class BatchResponse(BaseModel):
    ids: List[str]
    count: int


class StatsResponse(BaseModel):
    total_embeddings: int
    by_entity_type: Dict[str, int] = {}
    by_repo: Dict[str, int] = {}
    by_trust_tier: Dict[str, int] = {}
    by_embedding_model: Dict[str, int] = {}
    active_embedding_dim: int = 1536
    active_embedding_model: str = "text-embedding-3-small"
    backend: str = "aigateway"
    env: str = "development"
    latest_embedding_at: Optional[str] = None
    model_selection_policy: Optional[Dict] = None
    # Backward compatibility
    embedding_dim: Optional[int] = None
    model: Optional[str] = None


class DeleteResponse(BaseModel):
    deleted_count: int
    message: str


# --- Endpoints ---


@router.post("/embed", response_model=EmbedResponse)
async def embed_and_store(request: EmbedRequest):
    """Embed text and store the embedding in the vector store."""
    try:
        row_id = await _service.store_embedding(
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            content=request.content,
            repo_id=request.repo_id,
            metadata=request.metadata,
        )
        return EmbedResponse(id=row_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to store embedding: {exc}")


@router.post("/search", response_model=List[SearchResult])
async def search_similar(request: SearchRequest):
    """Search for similar embeddings using cosine similarity."""
    try:
        results = await _service.search_similar(
            query=request.query,
            limit=request.limit,
            entity_type=request.entity_type,
            repo_id=request.repo_id,
            threshold=request.threshold,
        )
        return [SearchResult(**r) for r in results]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}")


@router.post("/batch", response_model=BatchResponse)
async def batch_embed(request: BatchRequest):
    """Batch embed and store multiple texts."""
    try:
        items = [item.model_dump() for item in request.items]
        ids = await _service.batch_embed(items=items, repo_id=request.repo_id)
        return BatchResponse(ids=ids, count=len(ids))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch embed failed: {exc}")


@router.delete("/{entity_type}/{entity_id}", response_model=DeleteResponse)
async def delete_embeddings(entity_type: str, entity_id: str):
    """Delete all embeddings for a given entity."""
    try:
        deleted = await _service.delete_by_entity(entity_type=entity_type, entity_id=entity_id)
        return DeleteResponse(
            deleted_count=deleted,
            message=f"Deleted {deleted} embedding(s) for {entity_type}/{entity_id}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get vector store statistics."""
    try:
        stats = await _service.get_stats()
        return StatsResponse(**stats)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {exc}")

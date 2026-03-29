"""
Knowledge base API endpoints for COSMOS.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional

from app.db.session import get_db
from app.learning.knowledge import KnowledgeManager

router = APIRouter()


class KnowledgeAddRequest(BaseModel):
    category: str
    question: str
    answer: str
    source: str
    confidence: float = 1.0


class KnowledgeAddResponse(BaseModel):
    id: str
    message: str = "Knowledge entry added"


class KnowledgeSearchResult(BaseModel):
    id: str
    category: Optional[str] = None
    question: str
    answer: str
    source: Optional[str] = None
    confidence: float = 0.0
    similarity: float = 0.0


class KnowledgeCorrectRequest(BaseModel):
    record_id: str
    corrected_answer: str


class KnowledgeStatsResponse(BaseModel):
    total_entries: int
    by_category: dict = Field(default_factory=dict)
    last_updated: Optional[str] = None
    top_used: list = Field(default_factory=list)
    coverage_gaps: List[str] = Field(default_factory=list)


@router.post("", response_model=KnowledgeAddResponse)
async def add_knowledge(request: KnowledgeAddRequest, db=Depends(get_db)):
    """Add a knowledge entry."""
    manager = KnowledgeManager(db)
    try:
        entry_id = await manager.add_knowledge(
            category=request.category,
            question=request.question,
            answer=request.answer,
            source=request.source,
            confidence=request.confidence,
        )
        return KnowledgeAddResponse(id=entry_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/search", response_model=List[KnowledgeSearchResult])
async def search_knowledge(
    query: str = Query(..., min_length=1),
    category: Optional[str] = None,
    limit: int = 5,
    db=Depends(get_db),
):
    """Search knowledge base by text similarity."""
    manager = KnowledgeManager(db)
    return await manager.search_knowledge(query=query, category=category, limit=limit)


@router.post("/correct")
async def correct_answer(request: KnowledgeCorrectRequest, db=Depends(get_db)):
    """Correct an answer — creates or updates a knowledge entry."""
    manager = KnowledgeManager(db)
    try:
        await manager.update_from_feedback(
            record_id=request.record_id,
            corrected_answer=request.corrected_answer,
        )
        return {"message": "Knowledge updated from correction"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stats", response_model=KnowledgeStatsResponse)
async def get_knowledge_stats(db=Depends(get_db)):
    """Get knowledge base statistics."""
    manager = KnowledgeManager(db)
    result = await manager.get_knowledge_stats()
    return KnowledgeStatsResponse(**result)

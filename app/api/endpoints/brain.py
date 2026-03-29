"""
Brain API endpoints — RAG knowledge base search, query processing, and updates.

Routes:
  POST /cosmos/api/v1/brain/query     — Process query through RAG brain
  GET  /cosmos/api/v1/brain/search    — Search knowledge base directly
  GET  /cosmos/api/v1/brain/document/{doc_id} — Get specific KB document
  GET  /cosmos/api/v1/brain/stats     — Index stats
  POST /cosmos/api/v1/brain/reindex   — Trigger full re-index
  POST /cosmos/api/v1/brain/webhook   — GitHub webhook for auto-update
  POST /cosmos/api/v1/brain/learn     — Submit learning feedback
  GET  /cosmos/api/v1/brain/updates   — Recent update history
  POST /cosmos/api/v1/brain/scan      — Scan for file changes and process
"""

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

logger = structlog.get_logger()

router = APIRouter(tags=["brain"])


# --- Request/Response models ---


class BrainQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    session_id: str = ""
    user_role: str = "agent"
    company_id: str = ""


class BrainLearnRequest(BaseModel):
    doc_id: str = Field(..., min_length=1)
    correct_query: str = Field(..., min_length=1)
    correct_params: Dict[str, Any] = Field(default_factory=dict)
    feedback_score: int = Field(default=5, ge=1, le=10)


# --- Helpers ---


def _get_brain(request: Request):
    """Get brain from app state, raise 503 if not initialized."""
    brain = getattr(request.app.state, "brain", None)
    if brain is None:
        raise HTTPException(
            status_code=503,
            detail="Brain not initialized. Knowledge base path may not exist.",
        )
    return brain


def _doc_to_dict(doc) -> dict:
    """Convert KBDocument to JSON-safe dict (excluding embedding)."""
    return {
        "doc_id": doc.doc_id,
        "doc_type": doc.doc_type,
        "repo": doc.repo,
        "domain": doc.domain,
        "summary": doc.summary,
        "intent_tags": doc.intent_tags,
        "keywords": doc.keywords,
        "aliases": doc.aliases,
        "example_queries": doc.example_queries,
        "tool_candidate": doc.tool_candidate,
        "primary_agent": doc.primary_agent,
        "read_write_type": doc.read_write_type,
        "risk_level": doc.risk_level,
        "approval_mode": doc.approval_mode,
        "method": doc.method,
        "path": doc.path,
        "param_examples": doc.param_examples,
        "negative_examples": doc.negative_examples,
        "training_ready": doc.training_ready,
        "confidence": doc.confidence,
    }


# --- Endpoints ---


@router.post("/query")
async def brain_query(req: BrainQueryRequest, request: Request):
    """Process query through RAG brain graph."""
    brain = _get_brain(request)
    graph = brain["graph"]

    state = await graph.process(
        query=req.query,
        session_id=req.session_id,
        user_role=req.user_role,
        company_id=req.company_id,
    )

    return {
        "query": state.query,
        "response": state.response,
        "phase": state.phase.value,
        "phases_completed": state.phases_completed,
        "selected_tool": state.selected_tool,
        "tool_confidence": state.tool_confidence,
        "param_confidence": state.param_confidence,
        "final_confidence": state.final_confidence,
        "extracted_params": state.extracted_params,
        "validation_passed": state.validation_passed,
        "validation_errors": state.validation_errors,
        "execution_success": state.execution_success,
        "retrieved_docs_count": len(state.retrieved_docs),
        "errors": state.errors,
    }


@router.get("/search")
async def brain_search(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(default=5, ge=1, le=50),
    doc_type: Optional[str] = Query(default=None, description="api or table"),
    domain: Optional[str] = Query(default=None),
    repo: Optional[str] = Query(default=None),
):
    """Search knowledge base directly."""
    brain = _get_brain(request)
    indexer = brain["indexer"]

    filters = {}
    if doc_type:
        filters["doc_type"] = doc_type
    if domain:
        filters["domain"] = domain
    if repo:
        filters["repo"] = repo

    results = indexer.search(q, top_k=top_k, filters=filters or None)

    return {
        "query": q,
        "count": len(results),
        "results": [
            {"document": _doc_to_dict(doc), "score": round(score, 4)}
            for doc, score in results
        ],
    }


@router.get("/document/{doc_id:path}")
async def brain_document(doc_id: str, request: Request):
    """Get specific KB document by ID."""
    brain = _get_brain(request)
    indexer = brain["indexer"]

    doc = indexer.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")

    return {"document": _doc_to_dict(doc)}


@router.get("/stats")
async def brain_stats(request: Request):
    """Index stats: total docs, per-repo, per-domain."""
    brain = _get_brain(request)
    indexer = brain["indexer"]
    pipeline = brain["pipeline"]

    return {
        "indexer": indexer.get_stats(),
        "pipeline": pipeline.get_stats(),
    }


@router.post("/reindex")
async def brain_reindex(request: Request):
    """Trigger full re-index of knowledge base."""
    brain = _get_brain(request)
    pipeline = brain["pipeline"]

    result = await pipeline.full_reindex()

    # Update the document_count in brain state
    brain["document_count"] = brain["indexer"].document_count

    logger.info("Brain re-indexed", result=result)
    return {"status": "reindexed", "result": result}


@router.post("/webhook")
async def brain_webhook(request: Request):
    """GitHub webhook for auto-update of knowledge base."""
    brain = _get_brain(request)
    pipeline = brain["pipeline"]

    payload = await request.json()
    updates = await pipeline.handle_github_webhook(payload)

    return {
        "status": "processed",
        "updates": len(updates),
        "details": [
            {
                "update_id": u.update_id,
                "doc_id": u.doc_id,
                "status": u.status,
            }
            for u in updates
        ],
    }


@router.post("/learn")
async def brain_learn(req: BrainLearnRequest, request: Request):
    """Submit learning feedback for KB update."""
    brain = _get_brain(request)
    pipeline = brain["pipeline"]

    feedback = {
        "doc_id": req.doc_id,
        "correct_query": req.correct_query,
        "correct_params": req.correct_params,
        "feedback_score": req.feedback_score,
    }

    update = await pipeline.handle_learning_feedback(feedback)
    if update is None:
        raise HTTPException(status_code=400, detail="Invalid feedback: missing doc_id")

    return {
        "status": update.status,
        "update_id": update.update_id,
        "doc_id": update.doc_id,
        "error": update.error,
    }


@router.get("/updates")
async def brain_updates(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    """Recent update history."""
    brain = _get_brain(request)
    pipeline = brain["pipeline"]

    return {
        "updates": pipeline.get_update_history(limit=limit),
    }


@router.post("/scan")
async def brain_scan(request: Request):
    """Scan for file changes and process them."""
    brain = _get_brain(request)
    pipeline = brain["pipeline"]

    changes = pipeline.scan_for_changes()
    if not changes:
        return {"status": "no_changes", "changes": 0, "updates": []}

    updates = await pipeline.process_changes(changes)

    # Update hashes after processing
    pipeline.snapshot_hashes()

    return {
        "status": "processed",
        "changes": len(changes),
        "updates": [
            {
                "update_id": u.update_id,
                "doc_id": u.doc_id,
                "update_type": u.update_type,
                "status": u.status,
            }
            for u in updates
        ],
    }

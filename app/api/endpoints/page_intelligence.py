"""
Page & Role Intelligence API endpoints (Pillar 4).

Provides REST access to page metadata, field traces, role permissions,
cross-repo mappings, and page search powered by the
PageIntelligenceService.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

router = APIRouter()


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class PageSearchRequest(BaseModel):
    query: str
    role: Optional[str] = None
    top_k: int = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_service(request: Request):
    """Retrieve PageIntelligenceService from app state."""
    svc = getattr(request.app.state, "page_intelligence", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Page Intelligence service is not available",
        )
    return svc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", tags=["pages"])
async def list_pages(
    request: Request,
    repo: Optional[str] = None,
    domain: Optional[str] = None,
    role: Optional[str] = None,
    page_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all indexed pages with optional filters."""
    svc = _get_service(request)
    results = []
    for doc in svc.pages.values():
        if repo and doc.repo != repo:
            continue
        if domain and doc.domain != domain:
            continue
        if page_type and doc.page_type != page_type:
            continue
        if role:
            if doc.roles_required and role not in doc.roles_required:
                if role not in doc.role_permissions:
                    continue
        results.append({
            "page_id": doc.page_id,
            "route": doc.route,
            "repo": doc.repo,
            "domain": doc.domain,
            "page_type": doc.page_type,
            "component": doc.component,
            "module": doc.module,
            "framework": doc.framework,
            "roles_required": doc.roles_required,
            "field_count": len(doc.fields),
            "action_count": len(doc.actions),
            "training_ready": doc.training_ready,
            "confidence": doc.confidence,
        })
    return results


@router.get("/stats", tags=["pages"])
async def get_stats(request: Request) -> Dict[str, Any]:
    """Return Pillar 4 indexing stats."""
    svc = _get_service(request)
    return svc.get_stats()


@router.post("/search", tags=["pages"])
async def search_pages(
    request: Request, body: PageSearchRequest
) -> List[Dict[str, Any]]:
    """Search pages by query text with optional role filter."""
    svc = _get_service(request)
    return await svc.search_pages(
        query=body.query, role=body.role, top_k=body.top_k
    )


@router.get("/{page_id}", tags=["pages"])
async def get_page(request: Request, page_id: str) -> Dict[str, Any]:
    """Get full page intelligence for a specific page."""
    svc = _get_service(request)
    result = await svc.get_page(page_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return result


@router.get("/{page_id}/fields", tags=["pages"])
async def get_page_fields(request: Request, page_id: str) -> Dict[str, Any]:
    """Get fields for a page."""
    svc = _get_service(request)
    doc = svc.pages.get(page_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return {"page_id": page_id, "fields": doc.fields}


@router.get("/{page_id}/actions", tags=["pages"])
async def get_page_actions(request: Request, page_id: str) -> Dict[str, Any]:
    """Get actions for a page."""
    svc = _get_service(request)
    doc = svc.pages.get(page_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return {"page_id": page_id, "actions": doc.actions}


@router.get("/{page_id}/apis", tags=["pages"])
async def get_page_apis(request: Request, page_id: str) -> Dict[str, Any]:
    """Get API bindings for a page."""
    svc = _get_service(request)
    apis = await svc.get_page_apis(page_id)
    if not apis and page_id not in svc.pages:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return {"page_id": page_id, "api_bindings": apis}


@router.get("/{page_id}/permissions/{role}", tags=["pages"])
async def get_page_role_permissions(
    request: Request, page_id: str, role: str
) -> Dict[str, Any]:
    """Get role permissions for a specific page."""
    svc = _get_service(request)
    if page_id not in svc.pages:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    return await svc.get_role_permissions(role, page_id=page_id)


@router.get("/field-trace", tags=["pages"])
async def get_field_trace(
    request: Request,
    field_name: str = Query(..., description="Field name to trace"),
    page_id: Optional[str] = Query(None, description="Optional page to scope the trace"),
) -> Dict[str, Any]:
    """Trace a field from page -> API -> DB column."""
    svc = _get_service(request)
    traces = await svc.get_field_trace(field_name, page_id=page_id)
    return {"field_name": field_name, "page_id": page_id, "traces": traces}


@router.get("/role-matrix/{role}", tags=["pages"])
async def get_role_matrix(request: Request, role: str) -> Dict[str, Any]:
    """Get all pages and permissions for a role."""
    svc = _get_service(request)
    return await svc.get_role_permissions(role)


@router.get("/cross-repo/{page_id}", tags=["pages"])
async def get_cross_repo_mapping(
    request: Request, page_id: str
) -> Dict[str, Any]:
    """Get cross-repo page mapping (seller <-> admin)."""
    svc = _get_service(request)
    return await svc.get_cross_repo_mapping(page_id)

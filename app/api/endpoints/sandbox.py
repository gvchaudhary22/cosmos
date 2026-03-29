"""
Sandbox API endpoints — test suite management, run execution, and comparison.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from app.services.sandbox import SandboxService

router = APIRouter()
_svc = SandboxService()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class CreateSuiteRequest(BaseModel):
    name: str
    description: str = ""
    agent_type: str = "react"
    repo_id: Optional[str] = None


class AddTestCaseRequest(BaseModel):
    input_prompt: str
    expected_output: Optional[str] = None
    expected_tools: Optional[List[str]] = None
    category: Optional[str] = None
    difficulty: str = "medium"


class StartRunRequest(BaseModel):
    suite_id: str
    agent_version: str


class RecordResultRequest(BaseModel):
    test_case_id: str
    actual_output: str
    actual_tools: Optional[List[str]] = None
    passed: bool = False
    score: float = 0.0
    latency_ms: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0


class CompareRunsRequest(BaseModel):
    run_id_a: str
    run_id_b: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/suites", tags=["sandbox"])
async def create_suite(req: CreateSuiteRequest) -> Dict[str, Any]:
    """Create a new test suite."""
    await _svc.ensure_schema()
    try:
        return await _svc.create_suite(
            name=req.name,
            description=req.description,
            agent_type=req.agent_type,
            repo_id=req.repo_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/suites", tags=["sandbox"])
async def list_suites(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """List all test suites."""
    await _svc.ensure_schema()
    return await _svc.list_suites(limit=limit, offset=offset)


@router.post("/suites/{suite_id}/cases", tags=["sandbox"])
async def add_test_case(suite_id: str, req: AddTestCaseRequest) -> Dict[str, Any]:
    """Add a test case to a suite."""
    await _svc.ensure_schema()
    try:
        return await _svc.add_test_case(
            suite_id=suite_id,
            input_prompt=req.input_prompt,
            expected_output=req.expected_output,
            expected_tools=req.expected_tools,
            category=req.category,
            difficulty=req.difficulty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/suites/{suite_id}/cases", tags=["sandbox"])
async def get_test_cases(
    suite_id: str, limit: int = 200, offset: int = 0
) -> List[Dict[str, Any]]:
    """Get all test cases for a suite."""
    await _svc.ensure_schema()
    return await _svc.get_test_cases(suite_id, limit=limit, offset=offset)


@router.post("/runs", tags=["sandbox"])
async def start_run(req: StartRunRequest) -> Dict[str, Any]:
    """Start a new sandbox run against a suite."""
    await _svc.ensure_schema()
    try:
        return await _svc.start_run(
            suite_id=req.suite_id, agent_version=req.agent_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/runs", tags=["sandbox"])
async def list_runs(
    suite_id: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    """List sandbox runs, optionally filtered by suite."""
    await _svc.ensure_schema()
    return await _svc.list_runs(suite_id=suite_id, limit=limit, offset=offset)


@router.get("/runs/{run_id}", tags=["sandbox"])
async def get_run(run_id: str) -> Dict[str, Any]:
    """Get details and metrics for a specific run."""
    await _svc.ensure_schema()
    try:
        return await _svc.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/runs/{run_id}/results", tags=["sandbox"])
async def record_result(run_id: str, req: RecordResultRequest) -> Dict[str, Any]:
    """Record the result of a single test case within a run."""
    await _svc.ensure_schema()
    try:
        return await _svc.record_result(
            run_id=run_id,
            test_case_id=req.test_case_id,
            actual_output=req.actual_output,
            actual_tools=req.actual_tools,
            passed=req.passed,
            score=req.score,
            latency_ms=req.latency_ms,
            tokens_used=req.tokens_used,
            cost_usd=req.cost_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/runs/{run_id}/complete", tags=["sandbox"])
async def complete_run(run_id: str) -> Dict[str, Any]:
    """Mark a run as completed and compute aggregate metrics."""
    await _svc.ensure_schema()
    try:
        return await _svc.complete_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/runs/compare", tags=["sandbox"])
async def compare_runs(req: CompareRunsRequest) -> Dict[str, Any]:
    """Compare two runs: accuracy delta, latency delta, improved/regressed cases."""
    await _svc.ensure_schema()
    try:
        return await _svc.compare_runs(req.run_id_a, req.run_id_b)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

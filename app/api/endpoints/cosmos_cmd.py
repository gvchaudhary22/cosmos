"""
cosmos_cmd.py — /cosmos:* Command REST API

Exposes all COSMOS workflow commands as HTTP endpoints so MARS (Go)
can call them directly, and so `npm run cosmos:*` works via the CLI.

Routes (all under /cosmos/api/v1/cmd):
  POST /execute              Execute any /cosmos:* command
  GET  /state                Get current STATE.md for a session
  PUT  /state                Update STATE.md
  GET  /agents               List all registered agents
  GET  /agents/{name}        Get a specific agent
  GET  /agents/route         Route a query to the best agent
  GET  /skills               List all available skills
  GET  /workflows            List all registered workflows
  GET  /progress             Current workflow progress
  POST /eval                 Trigger eval benchmark
  GET  /health               Cmd subsystem health

Called from:
  npm run cosmos:plan    → POST /execute {"command": "plan", ...}
  npm run state          → GET /state
  npm run agents         → GET /agents
  MARS (Go) HTTP client  → POST /execute
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import structlog

logger = structlog.get_logger()
router = APIRouter()


# ─── Request / Response models ────────────────────────────────────────────────

class CommandRequest(BaseModel):
    command: str                          # e.g. "plan", "/cosmos:plan", "/orbit:plan"
    session_id: Optional[str] = "default"
    context: Optional[str] = ""
    args: Optional[Dict[str, Any]] = {}


class WaveResultSchema(BaseModel):
    wave_num: int
    phase: str
    agent: str
    output: Any
    latency_ms: float = 0.0
    status: str = "success"


class CommandResponse(BaseModel):
    request_id: str
    command: str
    session_id: str
    status: str
    final_output: Any
    waves: List[WaveResultSchema] = []
    next_command: Optional[str] = None
    next_reason: Optional[str] = None
    latency_ms: float = 0.0
    error: Optional[str] = None


class StateUpdateRequest(BaseModel):
    session_id: Optional[str] = "default"
    project_name: Optional[str] = None
    current_phase: Optional[str] = None
    current_wave: Optional[int] = None
    mode: Optional[str] = None
    status: Optional[str] = None
    state_md: Optional[str] = None
    decisions: Optional[List] = None
    blockers: Optional[List] = None
    completed_tasks: Optional[List] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_executor():
    from app.engine.cosmos_executor import get_cosmos_executor
    return get_cosmos_executor()

def _get_workflow_svc():
    from app.services.cosmos_workflow import CosmosWorkflowService
    return CosmosWorkflowService()


# ─── Execute any /cosmos:* command ───────────────────────────────────────────

@router.post("/execute", response_model=CommandResponse)
async def execute_command(req: CommandRequest, request: Request):
    """
    Execute a COSMOS workflow command.

    Examples:
      {"command": "plan", "context": "build order tracking feature"}
      {"command": "/cosmos:riper", "context": "refactor chunker service"}
      {"command": "forge", "args": {"description": "specialist for RTO diagnosis"}}
    """
    brain = getattr(request.app.state, "brain", None)
    executor = _get_executor()

    try:
        result = await executor.execute(
            command=req.command,
            session_id=req.session_id or "default",
            context=req.context or "",
            args=req.args or {},
            brain=brain,
        )
        return CommandResponse(
            request_id=result.request_id,
            command=result.command,
            session_id=result.session_id,
            status=result.status,
            final_output=result.final_output,
            waves=[
                WaveResultSchema(
                    wave_num=w.wave_num, phase=w.phase, agent=w.agent,
                    output=w.output, latency_ms=w.latency_ms, status=w.status,
                )
                for w in result.waves
            ],
            next_command=result.next_command,
            next_reason=result.next_reason,
            latency_ms=result.latency_ms,
            error=result.error,
        )
    except Exception as e:
        logger.error("cosmos_cmd.execute_failed", command=req.command, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ─── State management ─────────────────────────────────────────────────────────

@router.get("/state")
async def get_state(session_id: str = "default"):
    """Get current STATE.md for a session."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        state_md = await svc.get_state_md(session_id)
        state = await svc.get_state(session_id)
        return {
            "session_id": session_id,
            "state_md": state_md,
            "current_phase": state.get("current_phase") if state else None,
            "current_wave": state.get("current_wave") if state else 0,
            "status": state.get("status") if state else "idle",
            "active_agent": state.get("active_agent") if state else None,
            "project_name": state.get("project_name") if state else None,
        }
    except Exception as e:
        logger.warning("cosmos_cmd.state_failed", error=str(e))
        return {"session_id": session_id, "state_md": "STATE.md not available", "error": str(e)}


@router.put("/state")
async def update_state(req: StateUpdateRequest):
    """Update STATE.md fields for a session."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        updated = await svc.upsert_state(
            session_id=req.session_id or "default",
            project_name=req.project_name,
            current_phase=req.current_phase,
            current_wave=req.current_wave,
            mode=req.mode,
            status=req.status,
            state_md=req.state_md,
            decisions=req.decisions,
            blockers=req.blockers,
            completed_tasks=req.completed_tasks,
        )
        return {"ok": True, "state": updated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Agent registry ───────────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents():
    """List all registered COSMOS agents."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        agents = await svc.list_agents()
        # If DB is empty, fall back to reading from registry JSON
        if not agents:
            agents = _agents_from_registry()
        return {"agents": agents, "count": len(agents)}
    except Exception as e:
        logger.warning("cosmos_cmd.agents_failed", error=str(e))
        return {"agents": _agents_from_registry(), "count": 0, "source": "registry_fallback"}


@router.get("/agents/route")
async def route_agent(query: str):
    """Find the best agent for a query."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        agent = await svc.route_to_agent(query)
        return {"query": query, "agent": agent, "found": agent is not None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{name}")
async def get_agent(name: str):
    """Get a specific agent by name."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        agent = await svc.get_agent(name)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        return agent
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Skills ───────────────────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills():
    """List all available COSMOS skills (from Orbit skills directory)."""
    from pathlib import Path
    skills = []
    orbit_skills = Path(__file__).parent.parent.parent.parent.parent / "orbit" / "skills"
    local_skills = Path(__file__).parent.parent.parent / ".claude" / "skills"

    for base in [local_skills, orbit_skills]:
        if base.exists():
            for f in sorted(base.glob("*.md")):
                skills.append({
                    "name": f.stem,
                    "source": "local" if base == local_skills else "cosmos",
                    "path": str(f),
                })
            break

    return {"skills": skills, "count": len(skills)}


# ─── Workflows ────────────────────────────────────────────────────────────────

@router.get("/workflows")
async def list_workflows():
    """List all registered COSMOS workflow commands."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        workflows = await svc.list_workflows()
        if not workflows:
            workflows = _workflows_from_registry()
        return {"workflows": workflows, "count": len(workflows)}
    except Exception as e:
        return {"workflows": _workflows_from_registry(), "count": 0, "error": str(e)}


# ─── Progress ─────────────────────────────────────────────────────────────────

@router.get("/progress")
async def get_progress(session_id: str = "default"):
    """Get current workflow progress (equivalent to /cosmos:progress)."""
    svc = _get_workflow_svc()
    try:
        await svc.ensure_schema()
        state = await svc.get_state(session_id)
        if not state:
            return {"session_id": session_id, "status": "idle", "message": "No active workflow"}
        return {
            "session_id": session_id,
            "project_name": state.get("project_name"),
            "current_phase": state.get("current_phase"),
            "current_wave": state.get("current_wave"),
            "active_agent": state.get("active_agent"),
            "status": state.get("status"),
            "mode": state.get("mode"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Eval ─────────────────────────────────────────────────────────────────────

@router.post("/eval")
async def trigger_eval(request: Request):
    """Trigger COSMOS eval benchmark (201 ICRM seeds)."""
    try:
        from app.services.kb_eval import KBEval
        kb_path = getattr(request.app.state, "kb_path", None)
        if not kb_path:
            return {"ok": False, "error": "KB path not configured"}
        eval_svc = KBEval(kb_path=kb_path)
        results = await eval_svc.run_benchmark()
        return {"ok": True, "results": results}
    except Exception as e:
        logger.warning("cosmos_cmd.eval_failed", error=str(e))
        return {"ok": False, "error": str(e)}


# ─── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def cmd_health():
    """Health check for the COSMOS command subsystem."""
    from app.engine.cosmos_executor import COMMAND_AGENTS
    return {
        "status": "ok",
        "commands_registered": len(COMMAND_AGENTS),
        "commands": list(COMMAND_AGENTS.keys()),
    }


# ─── Fallback registry readers ────────────────────────────────────────────────

def _agents_from_registry() -> List[Dict]:
    import json
    from pathlib import Path
    reg_path = Path(__file__).parent.parent.parent.parent.parent / "orbit" / "orbit.registry.json"
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text())
            return reg.get("agents", [])
        except Exception:
            pass
    return []

def _workflows_from_registry() -> List[Dict]:
    import json
    from pathlib import Path
    reg_path = Path(__file__).parent.parent.parent.parent.parent / "orbit" / "orbit.registry.json"
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text())
            wfs = reg.get("workflows", [])
            return [
                {**w, "cosmos_cmd": f"/cosmos:{w['name']}"}
                for w in wfs
            ]
        except Exception:
            pass
    return []

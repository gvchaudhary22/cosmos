"""
cosmos_workflow.py — COSMOS Workflow State Manager

Persists STATE.md-style workflow state in MySQL.
Manages agent registry, workflow phases, and wave tracking.

This is the control plane backbone — every /cosmos:* command reads/writes here.

Tables:
  cosmos_workflow_state   — per-session STATE.md equivalent
  rocketmind_agents     — registered agents (synced from RocketMind via rocketmind_sync.py)
  rocketmind_workflows  — registered workflow commands
  cosmos_wave_trace       — per-wave execution trace

Usage:
  from app.services.cosmos_workflow import CosmosWorkflowService
  svc = CosmosWorkflowService()
  state = await svc.get_state(session_id)
  await svc.update_state(session_id, phase="plan", wave=1, status="running")
"""

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()


# ─── Schema bootstrap ─────────────────────────────────────────────────────────

CREATE_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS cosmos_workflow_state (
    id              VARCHAR(64)  PRIMARY KEY,
    session_id      VARCHAR(64)  NOT NULL,
    project_name    VARCHAR(256) DEFAULT NULL,
    current_phase   VARCHAR(64)  DEFAULT NULL,
    current_wave    INT          DEFAULT 0,
    mode            VARCHAR(32)  DEFAULT 'collaborative',
    active_agent    VARCHAR(128) DEFAULT NULL,
    status          VARCHAR(32)  DEFAULT 'idle',
    state_md        LONGTEXT     DEFAULT NULL,
    decisions       JSON         DEFAULT NULL,
    blockers        JSON         DEFAULT NULL,
    completed_tasks JSON         DEFAULT NULL,
    metadata        JSON         DEFAULT NULL,
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_session (session_id),
    INDEX idx_status (status),
    INDEX idx_phase (current_phase)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_WAVE_TRACE_TABLE = """
CREATE TABLE IF NOT EXISTS cosmos_wave_trace (
    id          VARCHAR(64)  PRIMARY KEY,
    session_id  VARCHAR(64)  NOT NULL,
    command     VARCHAR(128) NOT NULL,
    wave_num    INT          DEFAULT 1,
    agent       VARCHAR(128) DEFAULT NULL,
    inputs      JSON         DEFAULT NULL,
    outputs     JSON         DEFAULT NULL,
    status      VARCHAR(32)  DEFAULT 'running',
    started_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME     DEFAULT NULL,
    latency_ms  INT          DEFAULT NULL,
    INDEX idx_session (session_id),
    INDEX idx_command (command)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_AGENTS_TABLE = """
CREATE TABLE IF NOT EXISTS rocketmind_agents (
    id        VARCHAR(64)  PRIMARY KEY,
    name      VARCHAR(128) NOT NULL,
    file      VARCHAR(256) DEFAULT NULL,
    domains   JSON         DEFAULT NULL,
    triggers  JSON         DEFAULT NULL,
    skills    JSON         DEFAULT NULL,
    outputs   JSON         DEFAULT NULL,
    source    VARCHAR(32)  DEFAULT 'rocketmind',
    synced_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_WORKFLOWS_TABLE = """
CREATE TABLE IF NOT EXISTS rocketmind_workflows (
    id          VARCHAR(64)  PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    command     VARCHAR(128) NOT NULL,
    cosmos_cmd  VARCHAR(128) NOT NULL,
    mode        VARCHAR(32)  DEFAULT 'collaborative',
    agents      JSON         DEFAULT NULL,
    inputs      JSON         DEFAULT NULL,
    outputs     JSON         DEFAULT NULL,
    source      VARCHAR(32)  DEFAULT 'cosmos',
    synced_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class CosmosWorkflowService:
    """
    Manages workflow state, agent registry, and wave traces for /cosmos:* commands.
    All data persists in MARS MySQL — resumable across sessions.
    """

    async def ensure_schema(self):
        """Create all COSMOS workflow tables if they don't exist."""
        async with AsyncSessionLocal() as session:
            for ddl in [
                CREATE_STATE_TABLE,
                CREATE_WAVE_TRACE_TABLE,
                CREATE_AGENTS_TABLE,
                CREATE_WORKFLOWS_TABLE,
            ]:
                try:
                    await session.execute(text(ddl))
                except Exception as e:
                    logger.warning("cosmos_workflow.schema_failed", error=str(e))
            await session.commit()
        logger.info("cosmos_workflow.schema_ensured")

    # ── State management ──────────────────────────────────────────────────────

    async def get_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get current workflow state for a session."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT * FROM cosmos_workflow_state WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = result.mappings().first()
            if not row:
                return None
            return dict(row)

    async def upsert_state(
        self,
        session_id: str,
        *,
        project_name: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_wave: Optional[int] = None,
        mode: Optional[str] = None,
        active_agent: Optional[str] = None,
        status: Optional[str] = None,
        state_md: Optional[str] = None,
        decisions: Optional[List] = None,
        blockers: Optional[List] = None,
        completed_tasks: Optional[List] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Upsert workflow state for a session (STATE.md equivalent in MySQL)."""
        state_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cosmos-state-{session_id}"))

        async with AsyncSessionLocal() as db:
            # Try update first
            existing = await db.execute(
                text("SELECT id FROM cosmos_workflow_state WHERE session_id = :sid"),
                {"sid": session_id},
            )
            exists = existing.first()

            if exists:
                updates = {"sid": session_id}
                set_clauses = ["updated_at = CURRENT_TIMESTAMP"]

                def _add(col, val):
                    if val is not None:
                        updates[col] = val if not isinstance(val, (list, dict)) else json.dumps(val)
                        set_clauses.append(f"{col} = :{col}")

                _add("project_name", project_name)
                _add("current_phase", current_phase)
                _add("current_wave", current_wave)
                _add("mode", mode)
                _add("active_agent", active_agent)
                _add("status", status)
                _add("state_md", state_md)
                _add("decisions", decisions)
                _add("blockers", blockers)
                _add("completed_tasks", completed_tasks)
                _add("metadata", metadata)

                await db.execute(
                    text(f"UPDATE cosmos_workflow_state SET {', '.join(set_clauses)} WHERE session_id = :sid"),
                    updates,
                )
            else:
                await db.execute(
                    text("""
                        INSERT INTO cosmos_workflow_state
                            (id, session_id, project_name, current_phase, current_wave,
                             mode, active_agent, status, state_md,
                             decisions, blockers, completed_tasks, metadata)
                        VALUES
                            (:id, :sid, :project_name, :current_phase, :current_wave,
                             :mode, :active_agent, :status, :state_md,
                             :decisions, :blockers, :completed_tasks, :metadata)
                    """),
                    {
                        "id": state_id,
                        "sid": session_id,
                        "project_name": project_name,
                        "current_phase": current_phase or "init",
                        "current_wave": current_wave or 0,
                        "mode": mode or "collaborative",
                        "active_agent": active_agent,
                        "status": status or "idle",
                        "state_md": state_md,
                        "decisions": json.dumps(decisions or []),
                        "blockers": json.dumps(blockers or []),
                        "completed_tasks": json.dumps(completed_tasks or []),
                        "metadata": json.dumps(metadata or {}),
                    },
                )
            await db.commit()

        return await self.get_state(session_id) or {}

    async def get_state_md(self, session_id: str) -> str:
        """Return STATE.md text for a session."""
        state = await self.get_state(session_id)
        if state and state.get("state_md"):
            return state["state_md"]
        return self._default_state_md(session_id)

    def _default_state_md(self, session_id: str) -> str:
        return f"""# COSMOS — Project State
Session: {session_id}

## Active Project
_None. Run `/cosmos:new` to start._

## Current Phase
_None_

## Active Wave
_None_

## Last 5 Completed Tasks
_None yet._

## Decisions Log
| Date | Command | Decision | Rationale |
|------|---------|----------|-----------|

## Blockers
_None._
"""

    # ── Wave trace ─────────────────────────────────────────────────────────────

    async def start_wave(
        self, session_id: str, command: str, wave_num: int, agent: str
    ) -> str:
        """Record wave start. Returns wave trace ID."""
        trace_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""
                    INSERT INTO cosmos_wave_trace
                        (id, session_id, command, wave_num, agent, status)
                    VALUES (:id, :sid, :cmd, :wave, :agent, 'running')
                """),
                {"id": trace_id, "sid": session_id, "cmd": command,
                 "wave": wave_num, "agent": agent},
            )
            await db.commit()
        return trace_id

    async def finish_wave(
        self, trace_id: str, outputs: Dict, status: str = "success", latency_ms: int = 0
    ):
        """Record wave completion."""
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""
                    UPDATE cosmos_wave_trace
                    SET outputs = :outputs, status = :status,
                        finished_at = CURRENT_TIMESTAMP, latency_ms = :latency
                    WHERE id = :id
                """),
                {"id": trace_id, "outputs": json.dumps(outputs),
                 "status": status, "latency": latency_ms},
            )
            await db.commit()

    # ── Agent registry ─────────────────────────────────────────────────────────

    async def list_agents(self) -> List[Dict]:
        """List all registered COSMOS agents."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT name, domains, triggers, skills, outputs, source FROM rocketmind_agents ORDER BY name")
            )
            rows = result.mappings().all()
            agents = []
            for r in rows:
                a = dict(r)
                for f in ("domains", "triggers", "skills", "outputs"):
                    if isinstance(a.get(f), str):
                        try: a[f] = json.loads(a[f])
                        except: a[f] = []
                agents.append(a)
            return agents

    async def get_agent(self, name: str) -> Optional[Dict]:
        """Get a single agent by name."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT * FROM rocketmind_agents WHERE name = :name"),
                {"name": name},
            )
            row = result.mappings().first()
            if not row:
                return None
            a = dict(row)
            for f in ("domains", "triggers", "skills", "outputs"):
                if isinstance(a.get(f), str):
                    try: a[f] = json.loads(a[f])
                    except: a[f] = []
            return a

    async def route_to_agent(self, query: str) -> Optional[Dict]:
        """Find the best agent for a query based on triggers."""
        agents = await self.list_agents()
        query_lower = query.lower()
        best_agent = None
        best_score = 0

        for agent in agents:
            triggers = agent.get("triggers", [])
            score = sum(1 for t in triggers if t.lower() in query_lower)
            if score > best_score:
                best_score = score
                best_agent = agent

        return best_agent if best_score > 0 else None

    # ── Workflow registry ──────────────────────────────────────────────────────

    async def list_workflows(self) -> List[Dict]:
        """List all registered COSMOS workflow commands."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT name, cosmos_cmd, mode, agents, inputs, outputs FROM rocketmind_workflows ORDER BY name")
            )
            rows = result.mappings().all()
            workflows = []
            for r in rows:
                w = dict(r)
                for f in ("agents", "inputs", "outputs"):
                    if isinstance(w.get(f), str):
                        try: w[f] = json.loads(w[f])
                        except: w[f] = []
                workflows.append(w)
            return workflows

    async def get_workflow(self, name: str) -> Optional[Dict]:
        """Get a workflow definition by name."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT * FROM rocketmind_workflows WHERE name = :name OR cosmos_cmd = :cmd"),
                {"name": name, "cmd": f"/cosmos:{name}"},
            )
            row = result.mappings().first()
            if not row:
                return None
            w = dict(row)
            for f in ("agents", "inputs", "outputs"):
                if isinstance(w.get(f), str):
                    try: w[f] = json.loads(w[f])
                    except: w[f] = []
            return w

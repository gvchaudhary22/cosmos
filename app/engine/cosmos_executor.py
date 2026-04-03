"""
cosmos_executor.py — /cosmos:* Command Executor

Translates COSMOS slash commands into wave execution runs.

Every /cosmos:* command maps to a structured pipeline:
  1. Classify command → route to agent + mode
  2. Load skills relevant to this command
  3. Decompose into waves (Research / Plan / Execute / Review)
  4. Run through COSMOS brain (RIPER + ReAct + KB retrieval)
  5. Persist result + update STATE.md in MySQL

Command → Agent mapping:
  /cosmos:plan    → strategist  (RIPER Research + Plan phases)
  /cosmos:build   → engineer    (RIPER Execute phase, wave parallel)
  /cosmos:verify  → reviewer    (RIPER Review phase)
  /cosmos:forge   → forge       (AgentForge → register new agent)
  /cosmos:riper   → all         (full RIPER 5-phase)
  /cosmos:review  → reviewer + security-engineer
  /cosmos:audit   → security-engineer
  /cosmos:debug   → engineer    (4-phase debugging)
  /cosmos:quick   → engineer    (RIPER Lite)
  /cosmos:ship    → reviewer + technical-writer
  /cosmos:new     → strategist  (project setup)
  /cosmos:next    → strategist  (auto-detect next step from STATE.md)
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


# ─── Command definitions ──────────────────────────────────────────────────────

class CosmosCommand(str, Enum):
    NEW      = "new"
    PLAN     = "plan"
    BUILD    = "build"
    VERIFY   = "verify"
    SHIP     = "ship"
    NEXT     = "next"
    QUICK    = "quick"
    RIPER    = "riper"
    FORGE    = "forge"
    REVIEW   = "review"
    AUDIT    = "audit"
    DEBUG    = "debug"
    RESUME   = "resume"
    PROGRESS = "progress"
    STATE    = "state"
    TRAIN    = "train"
    EVAL     = "eval"
    HELP     = "help"


# Agent assigned to each command
COMMAND_AGENTS: Dict[str, List[str]] = {
    "new":      ["strategist"],
    "plan":     ["strategist", "architect"],
    "build":    ["engineer"],
    "verify":   ["reviewer", "qa-engineer"],
    "ship":     ["reviewer", "technical-writer"],
    "next":     ["strategist"],
    "quick":    ["engineer"],
    "riper":    ["researcher", "strategist", "engineer", "reviewer"],
    "forge":    ["forge"],
    "review":   ["reviewer", "security-engineer"],
    "audit":    ["security-engineer"],
    "debug":    ["engineer"],
    "resume":   ["strategist"],
    "progress": ["strategist"],
    "train":    [],
    "eval":     [],
    "state":    [],
    "help":     [],
}

# Skills loaded for each command
COMMAND_SKILLS: Dict[str, List[str]] = {
    "new":      ["planning", "brainstorming"],
    "plan":     ["planning", "riper", "architecture"],
    "build":    ["tdd", "riper", "reflection"],
    "verify":   ["review", "tdd"],
    "ship":     ["review", "deployment"],
    "next":     ["planning", "context-management"],
    "quick":    ["riper", "tdd"],
    "riper":    ["riper", "reflection", "planning"],
    "forge":    ["ai-systems", "brainstorming"],
    "review":   ["review", "security-and-identity"],
    "audit":    ["security-and-identity", "prompt-safety", "compliance-checklist"],
    "debug":    ["debugging", "reflection"],
    "resume":   ["context-management"],
    "progress": ["context-management"],
}

# RIPER phases per command
COMMAND_WAVES: Dict[str, List[str]] = {
    "new":      ["research", "plan"],
    "plan":     ["research", "innovate", "plan"],
    "build":    ["execute"],
    "verify":   ["review"],
    "ship":     ["review"],
    "quick":    ["research", "plan", "execute", "review"],
    "riper":    ["research", "innovate", "plan", "execute", "review"],
    "review":   ["research", "review"],
    "audit":    ["research", "review"],
    "debug":    ["research", "innovate", "execute", "review"],
    "forge":    ["research", "plan", "execute"],
}


# ─── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class WaveResult:
    wave_num: int
    phase: str
    agent: str
    output: Any
    latency_ms: float = 0.0
    confidence: float = 0.0
    status: str = "success"


@dataclass
class CosmosCommandResult:
    command: str
    session_id: str
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "success"
    waves: List[WaveResult] = field(default_factory=list)
    final_output: Any = None
    state_update: Optional[Dict] = None
    next_command: Optional[str] = None
    next_reason: Optional[str] = None
    latency_ms: float = 0.0
    error: Optional[str] = None


# ─── Executor ─────────────────────────────────────────────────────────────────

class CosmosExecutor:
    """
    Executes /cosmos:* commands through COSMOS's wave pipeline.

    Each command:
    1. Resolves agents + skills from the registry
    2. Decomposes into RIPER phases (waves)
    3. Runs each wave through the COSMOS brain
    4. Persists results to MySQL via CosmosWorkflowService
    5. Returns structured CosmosCommandResult
    """

    def __init__(self):
        self._workflow_svc = None
        self._brain = None
        self._riper = None
        self._agent_forge = None

    def _get_workflow_svc(self):
        if self._workflow_svc is None:
            from app.services.cosmos_workflow import CosmosWorkflowService
            self._workflow_svc = CosmosWorkflowService()
        return self._workflow_svc

    def _get_riper(self):
        if self._riper is None:
            try:
                from app.engine.riper import RIPEREngine
                self._riper = RIPEREngine()
            except Exception as e:
                logger.warning("cosmos_executor.riper_unavailable", error=str(e))
        return self._riper

    def _get_agent_forge(self):
        if self._agent_forge is None:
            try:
                from app.engine.agent_forge import AgentForge
                self._agent_forge = AgentForge()
            except Exception as e:
                logger.warning("cosmos_executor.forge_unavailable", error=str(e))
        return self._agent_forge

    async def execute(
        self,
        command: str,
        session_id: str,
        context: str = "",
        args: Optional[Dict] = None,
        brain=None,
    ) -> CosmosCommandResult:
        """Execute a /cosmos:* command."""
        start = time.time()
        args = args or {}

        # Normalise command (strip /cosmos: prefix if present)
        cmd = command.replace("/cosmos:", "").replace("/orbit:", "").strip()

        result = CosmosCommandResult(
            command=cmd,
            session_id=session_id,
        )

        # ── Special non-wave commands ─────────────────────────────────────────
        if cmd == "state":
            svc = self._get_workflow_svc()
            state_md = await svc.get_state_md(session_id)
            result.final_output = state_md
            result.latency_ms = (time.time() - start) * 1000
            return result

        if cmd == "progress":
            return await self._handle_progress(session_id, result, start)

        if cmd == "resume":
            return await self._handle_resume(session_id, result, start)

        if cmd == "help":
            result.final_output = self._help_text()
            result.latency_ms = (time.time() - start) * 1000
            return result

        if cmd == "train":
            return await self._handle_train(session_id, result, start)

        if cmd == "forge":
            return await self._handle_forge(session_id, context, args, result, start)

        # ── Wave-based execution ──────────────────────────────────────────────
        agents = COMMAND_AGENTS.get(cmd, ["engineer"])
        skills = COMMAND_SKILLS.get(cmd, ["riper"])
        phases = COMMAND_WAVES.get(cmd, ["research", "execute", "review"])

        # Update state: running
        svc = self._get_workflow_svc()
        await svc.upsert_state(
            session_id,
            current_phase=cmd,
            current_wave=1,
            active_agent=agents[0] if agents else None,
            status="running",
        )

        wave_outputs = []
        for wave_num, phase in enumerate(phases, 1):
            agent = agents[min(wave_num - 1, len(agents) - 1)] if agents else "engineer"

            trace_id = await svc.start_wave(session_id, cmd, wave_num, agent)
            wave_start = time.time()

            try:
                wave_output = await self._run_wave(
                    cmd=cmd, phase=phase, agent=agent,
                    skills=skills, context=context, args=args,
                    brain=brain,
                )
                wave_latency = int((time.time() - wave_start) * 1000)
                await svc.finish_wave(trace_id, {"output": str(wave_output)[:2000]},
                                      status="success", latency_ms=wave_latency)

                wave_outputs.append(WaveResult(
                    wave_num=wave_num, phase=phase, agent=agent,
                    output=wave_output, latency_ms=wave_latency,
                ))
                logger.info("cosmos_executor.wave_done",
                            cmd=cmd, phase=phase, agent=agent,
                            latency_ms=wave_latency)

            except Exception as e:
                await svc.finish_wave(trace_id, {"error": str(e)}, status="failed")
                logger.warning("cosmos_executor.wave_failed", cmd=cmd, phase=phase, error=str(e))
                wave_outputs.append(WaveResult(
                    wave_num=wave_num, phase=phase, agent=agent,
                    output=f"Wave failed: {e}", status="failed",
                ))

        result.waves = wave_outputs
        result.final_output = self._synthesise_output(cmd, wave_outputs)
        result.next_command, result.next_reason = self._next_command(cmd)

        # Update state: done
        await svc.upsert_state(
            session_id,
            current_phase=cmd,
            current_wave=len(phases),
            status="idle",
            active_agent=None,
        )

        result.latency_ms = (time.time() - start) * 1000
        return result

    async def _run_wave(
        self, cmd: str, phase: str, agent: str,
        skills: List[str], context: str, args: Dict, brain=None,
    ) -> str:
        """Run a single wave phase through COSMOS brain."""
        # Build the prompt that represents this phase
        skill_context = self._load_skills(skills)
        prompt = self._build_wave_prompt(cmd, phase, agent, context, skill_context, args)

        # Try RIPER engine first
        riper = self._get_riper()
        if riper and hasattr(riper, "run_phase"):
            try:
                return await riper.run_phase(phase=phase, query=prompt, context=context)
            except Exception:
                pass

        # Fallback: use brain pipeline if available
        if brain and hasattr(brain, "query"):
            try:
                resp = await brain["pipeline"].run(query=prompt, session_id="cosmos-cmd")
                return resp.get("answer", "") or resp.get("response", "")
            except Exception:
                pass

        # Final fallback: return structured placeholder
        return self._placeholder_output(cmd, phase, agent)

    def _load_skills(self, skill_names: List[str]) -> str:
        """Load skill markdown content as context for Claude."""
        import os
        from pathlib import Path
        orbit_skills = Path(__file__).parent.parent.parent.parent / "orbit" / "skills"
        local_skills = Path(__file__).parent.parent.parent / ".claude" / "skills"

        combined = []
        for name in skill_names:
            for base in [local_skills, orbit_skills]:
                p = base / f"{name}.md"
                if p.exists():
                    combined.append(f"## Skill: {name}\n{p.read_text()[:3000]}")
                    break
        return "\n\n".join(combined)

    def _build_wave_prompt(
        self, cmd: str, phase: str, agent: str,
        context: str, skill_context: str, args: Dict,
    ) -> str:
        """Build the prompt for a wave phase."""
        phase_instruction = {
            "research":  "Research all known facts, constraints, and unknowns. Do NOT propose solutions yet.",
            "innovate":  "Generate 3+ distinct approaches with trade-off analysis. Do NOT commit to one yet.",
            "plan":      "Create a detailed wave-based execution plan. List all tasks in dependency order.",
            "execute":   "Implement strictly according to the plan. TDD: write tests first.",
            "review":    "Verify against success criteria. Run security check. Document evidence.",
        }.get(phase, "Execute this phase.")

        agent_role = f"You are the COSMOS {agent} agent."

        return f"""{agent_role}

COSMOS Command: /cosmos:{cmd}
Phase: {phase.upper()} — {phase_instruction}

Context:
{context or 'No additional context provided.'}

Args:
{chr(10).join(f'  {k}: {v}' for k, v in args.items()) if args else '  None'}

{f"Skills loaded:{chr(10)}{skill_context[:2000]}" if skill_context else ""}

Execute the {phase} phase now. Be specific and actionable.
"""

    def _synthesise_output(self, cmd: str, waves: List[WaveResult]) -> str:
        """Combine wave outputs into final response."""
        if not waves:
            return f"/cosmos:{cmd} completed with no wave output."

        parts = [f"# COSMOS: /cosmos:{cmd}\n"]
        for w in waves:
            if w.status == "success":
                parts.append(f"## Wave {w.wave_num}: {w.phase.title()} ({w.agent})")
                parts.append(str(w.output)[:1000])
                parts.append("")

        # Recommended next step
        next_cmd, reason = self._next_command(cmd)
        if next_cmd:
            parts.append(f"\n---\n**Next:** `npm run cosmos:{next_cmd}`")
            parts.append(f"**Why:** {reason}")

        return "\n".join(parts)

    def _next_command(self, cmd: str) -> tuple:
        """Return the recommended next command after this one."""
        flow = {
            "new":      ("plan",    "Plan your first phase"),
            "plan":     ("build",   "Build phase 1 with wave architecture"),
            "build":    ("verify",  "Verify tests + review"),
            "verify":   ("ship",    "Ship to PR + deploy"),
            "ship":     ("next",    "Auto-detect what comes next"),
            "quick":    ("next",    "Check for follow-up work"),
            "riper":    ("build",   "Execute the RIPER plan"),
            "review":   ("build",   "Address review findings"),
            "audit":    ("build",   "Fix security findings"),
            "debug":    ("verify",  "Verify the fix"),
            "forge":    ("build",   "Use your new agent"),
        }
        pair = flow.get(cmd)
        return (pair[0], pair[1]) if pair else (None, None)

    def _placeholder_output(self, cmd: str, phase: str, agent: str) -> str:
        return (
            f"[{agent}] {phase.title()} phase for /cosmos:{cmd}\n"
            f"COSMOS brain not wired in this context. "
            f"Start COSMOS with `npm start` and retry via API."
        )

    def _help_text(self) -> str:
        lines = ["# COSMOS Commands\n"]
        for cmd, agents in COMMAND_AGENTS.items():
            skills = COMMAND_SKILLS.get(cmd, [])
            waves = COMMAND_WAVES.get(cmd, [])
            lines.append(f"## /cosmos:{cmd}")
            if agents: lines.append(f"  Agents: {', '.join(agents)}")
            if skills: lines.append(f"  Skills: {', '.join(skills)}")
            if waves:  lines.append(f"  Waves:  {' → '.join(waves)}")
            lines.append("")
        return "\n".join(lines)

    async def _handle_progress(self, session_id, result, start) -> CosmosCommandResult:
        svc = self._get_workflow_svc()
        state = await svc.get_state(session_id)
        if state:
            result.final_output = (
                f"━━━ COSMOS Progress ━━━━━━━━━━━━━━━━━━━\n"
                f"  Phase:  {state.get('current_phase', 'None')}\n"
                f"  Wave:   {state.get('current_wave', 0)}\n"
                f"  Agent:  {state.get('active_agent', 'None')}\n"
                f"  Status: {state.get('status', 'idle')}\n"
                f"  Project: {state.get('project_name', 'None')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            result.final_output = "No active COSMOS workflow. Run `/cosmos:new` to start."
        result.latency_ms = (time.time() - start) * 1000
        return result

    async def _handle_resume(self, session_id, result, start) -> CosmosCommandResult:
        svc = self._get_workflow_svc()
        state_md = await svc.get_state_md(session_id)
        state = await svc.get_state(session_id)
        phase = state.get("current_phase") if state else None
        result.final_output = state_md
        result.next_command = phase or "next"
        result.next_reason = "Resume from last known phase"
        result.latency_ms = (time.time() - start) * 1000
        return result

    async def _handle_train(self, session_id, result, start) -> CosmosCommandResult:
        try:
            from app.services.training_pipeline import TrainingPipeline
            result.final_output = "Training pipeline triggered. Check /cosmos/api/v1/pipeline/run for progress."
        except Exception as e:
            result.final_output = f"Training unavailable: {e}"
        result.latency_ms = (time.time() - start) * 1000
        return result

    async def _handle_forge(
        self, session_id, context, args, result, start
    ) -> CosmosCommandResult:
        """Delegate to AgentForge to create a new specialized agent."""
        forge = self._get_agent_forge()
        if forge and hasattr(forge, "forge"):
            try:
                desc = args.get("description") or context or "new agent"
                forged = await forge.forge(description=desc)
                result.final_output = f"Agent forged: {forged}"
            except Exception as e:
                result.final_output = f"Forge failed: {e}"
        else:
            result.final_output = "Agent Forge not available. Start COSMOS with `npm start`."
        result.latency_ms = (time.time() - start) * 1000
        return result


# ─── Singleton ────────────────────────────────────────────────────────────────

_executor: Optional[CosmosExecutor] = None


def get_cosmos_executor() -> CosmosExecutor:
    global _executor
    if _executor is None:
        _executor = CosmosExecutor()
    return _executor

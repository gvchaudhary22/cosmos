"""
Wave Executor — MARS parallel wave execution for COSMOS.

Breaks query processing into numbered waves of parallel tasks.
Each wave completes before the next begins. Progress is tracked
per-task for observability and analytics.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class WaveStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class WaveTask:
    """A single task within a wave."""
    task_id: str
    name: str
    coroutine_factory: Optional[Callable] = None  # callable that returns a coroutine
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Wave:
    """A group of parallel tasks that execute together."""
    wave_id: int
    name: str
    tasks: List[WaveTask] = field(default_factory=list)
    status: WaveStatus = WaveStatus.PENDING
    latency_ms: float = 0.0
    consensus: Optional[bool] = None  # None = no consensus required
    consensus_reason: str = ""


@dataclass
class WaveExecutionResult:
    """Result of executing all waves."""
    waves: List[Wave] = field(default_factory=list)
    total_latency_ms: float = 0.0
    total_tasks: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    skipped_tasks: int = 0


class WaveExecutor:
    """
    Executes tasks in numbered waves. Wave N+1 only starts after Wave N completes.
    Within a wave, all tasks run in parallel via asyncio.gather.

    Supports:
    - Progress callbacks for real-time tracking (SSE)
    - Consensus checking (e.g., require all tasks to succeed)
    - Conditional wave skipping based on previous wave results
    """

    def __init__(self, on_progress: Optional[Callable] = None):
        """
        Args:
            on_progress: Optional async callback(wave_id, task_id, status, data)
                         called on every task status change.
        """
        self.on_progress = on_progress
        self._waves: List[Wave] = []

    def add_wave(
        self,
        name: str,
        tasks: List[WaveTask],
        requires_consensus: bool = False,
    ) -> int:
        """Add a wave. Returns the wave_id (sequential, 1-based)."""
        wave_id = len(self._waves) + 1
        wave = Wave(wave_id=wave_id, name=name, tasks=tasks)
        if requires_consensus:
            wave.consensus = False  # Will be set during execution
        self._waves.append(wave)
        return wave_id

    async def execute(
        self,
        wave_context: Optional[Dict[str, Any]] = None,
        skip_wave_if: Optional[Callable[[int, Dict], bool]] = None,
    ) -> WaveExecutionResult:
        """
        Execute all waves sequentially. Within each wave, tasks run in parallel.

        Args:
            wave_context: Shared context dict passed to all tasks. Updated after each wave.
            skip_wave_if: Optional callable(wave_id, context) -> bool. If True, skip wave.
        """
        total_start = time.monotonic()
        context = wave_context or {}
        result = WaveExecutionResult()

        for wave in self._waves:
            # Check skip condition
            if skip_wave_if and skip_wave_if(wave.wave_id, context):
                wave.status = WaveStatus.SKIPPED
                for task in wave.tasks:
                    task.status = TaskStatus.SKIPPED
                    result.skipped_tasks += 1
                result.waves.append(wave)
                await self._emit_progress(wave.wave_id, "wave", "skipped", {"name": wave.name})
                continue

            wave.status = WaveStatus.RUNNING
            await self._emit_progress(wave.wave_id, "wave", "started", {
                "name": wave.name,
                "task_count": len(wave.tasks),
            })

            wave_start = time.monotonic()

            # Execute all tasks in parallel
            task_coros = []
            for task in wave.tasks:
                if task.coroutine_factory:
                    task_coros.append(self._run_task(wave.wave_id, task, context))
                else:
                    task.status = TaskStatus.SKIPPED
                    result.skipped_tasks += 1

            if task_coros:
                await asyncio.gather(*task_coros, return_exceptions=True)

            wave.latency_ms = (time.monotonic() - wave_start) * 1000

            # Check consensus
            successes = sum(1 for t in wave.tasks if t.status == TaskStatus.SUCCESS)
            errors = sum(1 for t in wave.tasks if t.status == TaskStatus.ERROR)
            result.successful_tasks += successes
            result.failed_tasks += errors
            result.total_tasks += len(wave.tasks)

            if wave.consensus is not None:
                wave.consensus = errors == 0
                wave.consensus_reason = (
                    f"all {successes} tasks succeeded"
                    if wave.consensus
                    else f"{errors} task(s) failed"
                )

            wave.status = WaveStatus.COMPLETED if errors == 0 else WaveStatus.FAILED
            result.waves.append(wave)

            # Update context with wave results for next wave
            context[f"wave_{wave.wave_id}"] = {
                "name": wave.name,
                "status": wave.status.value,
                "results": {t.task_id: t.result for t in wave.tasks if t.result is not None},
                "consensus": wave.consensus,
            }

            await self._emit_progress(wave.wave_id, "wave", "completed", {
                "name": wave.name,
                "latency_ms": round(wave.latency_ms, 1),
                "successes": successes,
                "errors": errors,
                "consensus": wave.consensus,
            })

            logger.info(
                "wave.completed",
                wave_id=wave.wave_id,
                name=wave.name,
                latency_ms=round(wave.latency_ms, 1),
                successes=successes,
                errors=errors,
            )

        result.total_latency_ms = (time.monotonic() - total_start) * 1000
        return result

    async def _run_task(self, wave_id: int, task: WaveTask, context: Dict) -> None:
        """Run a single task with error handling and progress tracking."""
        task.status = TaskStatus.RUNNING
        await self._emit_progress(wave_id, task.task_id, "running", {"name": task.name})

        t0 = time.monotonic()
        try:
            coro = task.coroutine_factory(context)
            task.result = await coro
            task.status = TaskStatus.SUCCESS
        except Exception as e:
            task.error = str(e)
            task.status = TaskStatus.ERROR
            logger.warning("wave.task_error", wave_id=wave_id, task=task.task_id, error=str(e))
        finally:
            task.latency_ms = (time.monotonic() - t0) * 1000

        await self._emit_progress(wave_id, task.task_id, task.status.value, {
            "name": task.name,
            "latency_ms": round(task.latency_ms, 1),
            "has_result": task.result is not None,
            "error": task.error,
        })

    async def _emit_progress(self, wave_id: int, task_id: str, status: str, data: Dict):
        """Emit progress event via callback."""
        if self.on_progress:
            try:
                await self.on_progress(wave_id, task_id, status, data)
            except Exception:
                pass  # Never let progress callback break execution

    def to_summary(self, result: WaveExecutionResult) -> Dict[str, Any]:
        """Format wave execution result for API response."""
        return {
            "total_waves": len(result.waves),
            "total_latency_ms": round(result.total_latency_ms, 1),
            "total_tasks": result.total_tasks,
            "successful": result.successful_tasks,
            "failed": result.failed_tasks,
            "skipped": result.skipped_tasks,
            "waves": [
                {
                    "wave_id": w.wave_id,
                    "name": w.name,
                    "status": w.status.value,
                    "latency_ms": round(w.latency_ms, 1),
                    "consensus": w.consensus,
                    "tasks": [
                        {
                            "task_id": t.task_id,
                            "name": t.name,
                            "status": t.status.value,
                            "latency_ms": round(t.latency_ms, 1),
                            "error": t.error,
                        }
                        for t in w.tasks
                    ],
                }
                for w in result.waves
            ],
        }

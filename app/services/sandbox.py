"""
Sandbox Service — Agent testing, simulation, and evaluation.

Provides test suite management, sandbox run execution, result recording,
and run-to-run comparison for measuring agent quality over time.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# SQL: Table creation (idempotent)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_test_suites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    agent_type  TEXT NOT NULL DEFAULT 'react',
    repo_id     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cosmos_test_cases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id        UUID NOT NULL REFERENCES cosmos_test_suites(id) ON DELETE CASCADE,
    input_prompt    TEXT NOT NULL,
    expected_output TEXT,
    expected_tools  JSONB DEFAULT '[]'::jsonb,
    category        TEXT,
    difficulty      TEXT DEFAULT 'medium',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_test_cases_suite ON cosmos_test_cases(suite_id);

CREATE TABLE IF NOT EXISTS cosmos_sandbox_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id        UUID NOT NULL REFERENCES cosmos_test_suites(id) ON DELETE CASCADE,
    agent_version   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    metrics         JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_sandbox_runs_suite ON cosmos_sandbox_runs(suite_id);

CREATE TABLE IF NOT EXISTS cosmos_run_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES cosmos_sandbox_runs(id) ON DELETE CASCADE,
    test_case_id    UUID NOT NULL REFERENCES cosmos_test_cases(id) ON DELETE CASCADE,
    actual_output   TEXT,
    actual_tools    JSONB DEFAULT '[]'::jsonb,
    passed          BOOLEAN DEFAULT false,
    score           DOUBLE PRECISION DEFAULT 0.0,
    latency_ms      INTEGER DEFAULT 0,
    tokens_used     INTEGER DEFAULT 0,
    cost_usd        DOUBLE PRECISION DEFAULT 0.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_results_run ON cosmos_run_results(run_id);
"""


# ---------------------------------------------------------------------------
# Pydantic-style dicts returned by the service (kept lightweight)
# ---------------------------------------------------------------------------


class SandboxService:
    """Manages test suites, sandbox runs, and result comparison."""

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create tables if they don't exist (safe to call repeatedly)."""
        async with AsyncSessionLocal() as session:
            for statement in _SCHEMA_SQL.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    await session.execute(text(stmt))
            await session.commit()
        logger.info("sandbox.schema_ensured")

    # ------------------------------------------------------------------
    # Suites
    # ------------------------------------------------------------------

    async def create_suite(
        self,
        name: str,
        description: str = "",
        agent_type: str = "react",
        repo_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new test suite and return its record."""
        suite_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO cosmos_test_suites (id, name, description, agent_type, repo_id) "
                    "VALUES (:id, :name, :desc, :agent_type, :repo_id)"
                ),
                {
                    "id": suite_id,
                    "name": name,
                    "desc": description,
                    "agent_type": agent_type,
                    "repo_id": repo_id,
                },
            )
            await session.commit()

        logger.info("sandbox.suite_created", suite_id=suite_id, name=name)
        return await self._fetch_suite(suite_id)

    async def list_suites(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Return all test suites, newest first."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, name, description, agent_type, repo_id, created_at "
                    "FROM cosmos_test_suites ORDER BY created_at DESC LIMIT :lim OFFSET :off"
                ),
                {"lim": limit, "off": offset},
            )
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def _fetch_suite(self, suite_id: str) -> Dict[str, Any]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, name, description, agent_type, repo_id, created_at "
                    "FROM cosmos_test_suites WHERE id = :id"
                ),
                {"id": suite_id},
            )
            row = result.mappings().first()
        if row is None:
            raise ValueError(f"Suite {suite_id} not found")
        return dict(row)

    # ------------------------------------------------------------------
    # Test cases
    # ------------------------------------------------------------------

    async def add_test_case(
        self,
        suite_id: str,
        input_prompt: str,
        expected_output: Optional[str] = None,
        expected_tools: Optional[List[str]] = None,
        category: Optional[str] = None,
        difficulty: str = "medium",
    ) -> Dict[str, Any]:
        """Add a test case to a suite."""
        import json as _json

        case_id = str(uuid.uuid4())
        tools_json = _json.dumps(expected_tools or [])
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO cosmos_test_cases "
                    "(id, suite_id, input_prompt, expected_output, expected_tools, category, difficulty) "
                    "VALUES (:id, :suite_id, :prompt, :expected, :tools::jsonb, :cat, :diff)"
                ),
                {
                    "id": case_id,
                    "suite_id": suite_id,
                    "prompt": input_prompt,
                    "expected": expected_output,
                    "tools": tools_json,
                    "cat": category,
                    "diff": difficulty,
                },
            )
            await session.commit()

        logger.info("sandbox.case_added", case_id=case_id, suite_id=suite_id)
        return await self._fetch_test_case(case_id)

    async def get_test_cases(
        self, suite_id: str, limit: int = 200, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return test cases belonging to a suite."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, suite_id, input_prompt, expected_output, expected_tools, "
                    "category, difficulty, created_at "
                    "FROM cosmos_test_cases WHERE suite_id = :sid "
                    "ORDER BY created_at LIMIT :lim OFFSET :off"
                ),
                {"sid": suite_id, "lim": limit, "off": offset},
            )
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def _fetch_test_case(self, case_id: str) -> Dict[str, Any]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, suite_id, input_prompt, expected_output, expected_tools, "
                    "category, difficulty, created_at "
                    "FROM cosmos_test_cases WHERE id = :id"
                ),
                {"id": case_id},
            )
            row = result.mappings().first()
        if row is None:
            raise ValueError(f"Test case {case_id} not found")
        return dict(row)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def start_run(
        self, suite_id: str, agent_version: str
    ) -> Dict[str, Any]:
        """Create a new sandbox run in 'running' status."""
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO cosmos_sandbox_runs (id, suite_id, agent_version, status, started_at) "
                    "VALUES (:id, :suite_id, :ver, 'running', :now)"
                ),
                {"id": run_id, "suite_id": suite_id, "ver": agent_version, "now": now},
            )
            await session.commit()

        logger.info("sandbox.run_started", run_id=run_id, suite_id=suite_id, version=agent_version)
        return await self.get_run(run_id)

    async def record_result(
        self,
        run_id: str,
        test_case_id: str,
        actual_output: str,
        actual_tools: Optional[List[str]] = None,
        passed: bool = False,
        score: float = 0.0,
        latency_ms: int = 0,
        tokens_used: int = 0,
        cost_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Record the result of a single test case within a run."""
        import json as _json

        result_id = str(uuid.uuid4())
        tools_json = _json.dumps(actual_tools or [])
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO cosmos_run_results "
                    "(id, run_id, test_case_id, actual_output, actual_tools, passed, score, "
                    "latency_ms, tokens_used, cost_usd) "
                    "VALUES (:id, :run, :case, :output, :tools::jsonb, :passed, :score, "
                    ":latency, :tokens, :cost)"
                ),
                {
                    "id": result_id,
                    "run": run_id,
                    "case": test_case_id,
                    "output": actual_output,
                    "tools": tools_json,
                    "passed": passed,
                    "score": score,
                    "latency": latency_ms,
                    "tokens": tokens_used,
                    "cost": cost_usd,
                },
            )
            await session.commit()

        logger.info(
            "sandbox.result_recorded",
            result_id=result_id,
            run_id=run_id,
            passed=passed,
        )
        return {
            "id": result_id,
            "run_id": run_id,
            "test_case_id": test_case_id,
            "passed": passed,
            "score": score,
            "latency_ms": latency_ms,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
        }

    async def complete_run(self, run_id: str) -> Dict[str, Any]:
        """Mark a run as completed and compute aggregate metrics."""
        now = datetime.now(timezone.utc)
        metrics = await self._compute_run_metrics(run_id)

        import json as _json

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE cosmos_sandbox_runs "
                    "SET status = 'completed', completed_at = :now, metrics = :metrics::jsonb "
                    "WHERE id = :id"
                ),
                {"id": run_id, "now": now, "metrics": _json.dumps(metrics)},
            )
            await session.commit()

        logger.info("sandbox.run_completed", run_id=run_id, metrics=metrics)
        return await self.get_run(run_id)

    async def get_run(self, run_id: str) -> Dict[str, Any]:
        """Fetch a single run with its metrics."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, suite_id, agent_version, status, started_at, "
                    "completed_at, metrics FROM cosmos_sandbox_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            row = result.mappings().first()
        if row is None:
            raise ValueError(f"Run {run_id} not found")
        return dict(row)

    async def list_runs(
        self,
        suite_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sandbox runs, optionally filtered by suite."""
        q = (
            "SELECT id, suite_id, agent_version, status, started_at, completed_at, metrics "
            "FROM cosmos_sandbox_runs "
        )
        params: Dict[str, Any] = {"lim": limit, "off": offset}
        if suite_id:
            q += "WHERE suite_id = :sid "
            params["sid"] = suite_id
        q += "ORDER BY started_at DESC LIMIT :lim OFFSET :off"

        async with AsyncSessionLocal() as session:
            result = await session.execute(text(q), params)
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Comparison & accuracy
    # ------------------------------------------------------------------

    async def compare_runs(
        self, run_id_a: str, run_id_b: str
    ) -> Dict[str, Any]:
        """Compare two runs and return a ComparisonResult-style dict.

        Returns accuracy_delta, latency_delta, cost_delta, improved and
        regressed test case IDs.
        """
        metrics_a = await self._compute_run_metrics(run_id_a)
        metrics_b = await self._compute_run_metrics(run_id_b)

        # Per-case comparison
        results_a = await self._get_results_map(run_id_a)
        results_b = await self._get_results_map(run_id_b)

        common_cases = set(results_a.keys()) & set(results_b.keys())
        improved: List[str] = []
        regressed: List[str] = []

        for case_id in common_cases:
            a_passed = results_a[case_id]["passed"]
            b_passed = results_b[case_id]["passed"]
            if not a_passed and b_passed:
                improved.append(case_id)
            elif a_passed and not b_passed:
                regressed.append(case_id)

        accuracy_a = metrics_a.get("accuracy", 0.0)
        accuracy_b = metrics_b.get("accuracy", 0.0)
        avg_latency_a = metrics_a.get("avg_latency_ms", 0.0)
        avg_latency_b = metrics_b.get("avg_latency_ms", 0.0)
        total_cost_a = metrics_a.get("total_cost_usd", 0.0)
        total_cost_b = metrics_b.get("total_cost_usd", 0.0)

        return {
            "run_a": run_id_a,
            "run_b": run_id_b,
            "accuracy_a": accuracy_a,
            "accuracy_b": accuracy_b,
            "accuracy_delta": round(accuracy_b - accuracy_a, 4),
            "avg_latency_a_ms": avg_latency_a,
            "avg_latency_b_ms": avg_latency_b,
            "latency_delta_ms": round(avg_latency_b - avg_latency_a, 2),
            "total_cost_a_usd": total_cost_a,
            "total_cost_b_usd": total_cost_b,
            "cost_delta_usd": round(total_cost_b - total_cost_a, 6),
            "improved_cases": improved,
            "regressed_cases": regressed,
            "common_case_count": len(common_cases),
        }

    async def calculate_accuracy(self, run_id: str) -> Dict[str, Any]:
        """Return accuracy breakdown for a single run."""
        return await self._compute_run_metrics(run_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _compute_run_metrics(self, run_id: str) -> Dict[str, Any]:
        """Aggregate pass/fail, latency, cost, token metrics for a run."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT "
                    "  COUNT(*)                         AS total, "
                    "  COALESCE(SUM(CASE WHEN passed THEN 1 ELSE 0 END), 0) AS passed, "
                    "  COALESCE(AVG(score), 0)          AS avg_score, "
                    "  COALESCE(AVG(latency_ms), 0)     AS avg_latency_ms, "
                    "  COALESCE(SUM(tokens_used), 0)    AS total_tokens, "
                    "  COALESCE(SUM(cost_usd), 0)       AS total_cost_usd, "
                    "  COALESCE(MIN(latency_ms), 0)     AS min_latency_ms, "
                    "  COALESCE(MAX(latency_ms), 0)     AS max_latency_ms "
                    "FROM cosmos_run_results WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
            row = result.mappings().first()

        if row is None or row["total"] == 0:
            return {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "accuracy": 0.0,
                "avg_score": 0.0,
                "avg_latency_ms": 0.0,
                "min_latency_ms": 0,
                "max_latency_ms": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
            }

        total = int(row["total"])
        passed = int(row["passed"])
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": round(passed / total, 4) if total else 0.0,
            "avg_score": round(float(row["avg_score"]), 4),
            "avg_latency_ms": round(float(row["avg_latency_ms"]), 2),
            "min_latency_ms": int(row["min_latency_ms"]),
            "max_latency_ms": int(row["max_latency_ms"]),
            "total_tokens": int(row["total_tokens"]),
            "total_cost_usd": round(float(row["total_cost_usd"]), 6),
        }

    async def _get_results_map(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        """Return {test_case_id: result_dict} for a run."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT test_case_id, passed, score, latency_ms, tokens_used, cost_usd "
                    "FROM cosmos_run_results WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
            rows = result.mappings().all()

        return {
            str(r["test_case_id"]): {
                "passed": r["passed"],
                "score": r["score"],
                "latency_ms": r["latency_ms"],
                "tokens_used": r["tokens_used"],
                "cost_usd": r["cost_usd"],
            }
            for r in rows
        }

"""
gRPC servicer implementation for Sandbox (evaluation) service.

Bridges gRPC requests to the underlying SandboxService,
converting between protobuf messages and domain objects.
"""

from __future__ import annotations

from typing import AsyncIterator

import grpc
import structlog
from google.protobuf import timestamp_pb2

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.services.sandbox import SandboxService

logger = structlog.get_logger(__name__)


def _dict_to_run_response(d: dict) -> cosmos_pb2.RunResponse:
    """Convert a run dict from SandboxService to protobuf RunResponse.

    The underlying service returns ``metrics`` as a nested dict inside the
    run record.  We unpack it into the flat RunResponse fields.
    """
    metrics = d.get("metrics") or {}
    if isinstance(metrics, str):
        import json
        try:
            metrics = json.loads(metrics)
        except (json.JSONDecodeError, TypeError):
            metrics = {}

    resp = cosmos_pb2.RunResponse(
        id=str(d.get("id", "")),
        suite_id=str(d.get("suite_id", "")),
        agent_version=d.get("agent_version", "") or "",
        status=d.get("status", "") or "",
        total_cases=int(metrics.get("total", 0)),
        passed=int(metrics.get("passed", 0)),
        failed=int(metrics.get("failed", 0)),
        accuracy=float(metrics.get("accuracy", 0.0)),
        avg_latency_ms=float(metrics.get("avg_latency_ms", 0.0)),
        total_cost_usd=float(metrics.get("total_cost_usd", 0.0)),
    )

    for field_name in ("started_at", "completed_at"):
        val = d.get(field_name)
        if val is not None:
            ts = timestamp_pb2.Timestamp()
            try:
                ts.FromDatetime(val)
                getattr(resp, field_name).CopyFrom(ts)
            except (TypeError, AttributeError, ValueError):
                pass

    return resp


def _dict_to_suite_response(d: dict) -> cosmos_pb2.SuiteResponse:
    """Convert a suite dict to protobuf SuiteResponse."""
    resp = cosmos_pb2.SuiteResponse(
        id=str(d.get("id", "")),
        name=d.get("name", "") or "",
        description=d.get("description", "") or "",
        agent_type=d.get("agent_type", "") or "",
        repo_id=d.get("repo_id", "") or "",
    )
    val = d.get("created_at")
    if val is not None:
        ts = timestamp_pb2.Timestamp()
        try:
            ts.FromDatetime(val)
            resp.created_at.CopyFrom(ts)
        except (TypeError, AttributeError, ValueError):
            pass
    return resp


def _dict_to_test_case_response(c: dict) -> cosmos_pb2.TestCaseResponse:
    """Convert a test-case dict to protobuf TestCaseResponse."""
    tools = c.get("expected_tools") or []
    if isinstance(tools, str):
        import json
        try:
            tools = json.loads(tools)
        except (json.JSONDecodeError, TypeError):
            tools = []

    return cosmos_pb2.TestCaseResponse(
        id=str(c.get("id", "")),
        suite_id=str(c.get("suite_id", "")),
        input_prompt=c.get("input_prompt", "") or "",
        expected_output=c.get("expected_output", "") or "",
        expected_tools=tools,
    )


class SandboxServicer(cosmos_pb2_grpc.SandboxServiceServicer):
    """gRPC servicer for the sandbox evaluation service."""

    def __init__(self) -> None:
        self._svc = SandboxService()

    async def CreateSuite(
        self, request: cosmos_pb2.CreateSuiteRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.SuiteResponse:
        """Create a new test suite."""
        logger.info("grpc.sandbox.CreateSuite", name=request.name)
        try:
            result = await self._svc.create_suite(
                name=request.name,
                description=request.description,
                agent_type=request.agent_type or "react",
                repo_id=request.repo_id or None,
            )
            return _dict_to_suite_response(result)
        except Exception as exc:
            logger.error("grpc.sandbox.CreateSuite.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.SuiteResponse()

    async def ListSuites(
        self, request: cosmos_pb2.ListSuitesRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ListSuitesResponse:
        """List all test suites."""
        logger.info("grpc.sandbox.ListSuites")
        try:
            suites = await self._svc.list_suites()
            return cosmos_pb2.ListSuitesResponse(
                suites=[_dict_to_suite_response(s) for s in suites],
            )
        except Exception as exc:
            logger.error("grpc.sandbox.ListSuites.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ListSuitesResponse()

    async def AddTestCase(
        self, request: cosmos_pb2.AddTestCaseRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TestCaseResponse:
        """Add a test case to a suite."""
        logger.info("grpc.sandbox.AddTestCase", suite_id=request.suite_id)
        try:
            result = await self._svc.add_test_case(
                suite_id=request.suite_id,
                input_prompt=request.input_prompt,
                expected_output=request.expected_output or None,
                expected_tools=list(request.expected_tools) if request.expected_tools else None,
                category=request.category or None,
                difficulty=request.difficulty or "medium",
            )
            return _dict_to_test_case_response(result)
        except ValueError as exc:
            logger.error("grpc.sandbox.AddTestCase.not_found", error=str(exc))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return cosmos_pb2.TestCaseResponse()
        except Exception as exc:
            logger.error("grpc.sandbox.AddTestCase.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TestCaseResponse()

    async def GetTestCases(
        self, request: cosmos_pb2.GetTestCasesRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.GetTestCasesResponse:
        """Get all test cases for a suite."""
        logger.info("grpc.sandbox.GetTestCases", suite_id=request.suite_id)
        try:
            cases = await self._svc.get_test_cases(suite_id=request.suite_id)
            return cosmos_pb2.GetTestCasesResponse(
                cases=[_dict_to_test_case_response(c) for c in cases],
            )
        except Exception as exc:
            logger.error("grpc.sandbox.GetTestCases.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.GetTestCasesResponse()

    async def StartRun(
        self, request: cosmos_pb2.StartRunRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.RunResponse:
        """Start a new sandbox evaluation run."""
        logger.info(
            "grpc.sandbox.StartRun",
            suite_id=request.suite_id,
            version=request.agent_version,
        )
        try:
            result = await self._svc.start_run(
                suite_id=request.suite_id,
                agent_version=request.agent_version,
            )
            return _dict_to_run_response(result)
        except Exception as exc:
            logger.error("grpc.sandbox.StartRun.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.RunResponse()

    async def RecordResult(
        self, request: cosmos_pb2.RecordResultRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.RecordResultResponse:
        """Record a single test case result within a run."""
        logger.info(
            "grpc.sandbox.RecordResult",
            run_id=request.run_id,
            test_case_id=request.test_case_id,
        )
        try:
            await self._svc.record_result(
                run_id=request.run_id,
                test_case_id=request.test_case_id,
                actual_output=request.actual_output,
                actual_tools=list(request.actual_tools) if request.actual_tools else None,
                passed=request.passed,
                score=request.score,
                latency_ms=request.latency_ms,
                tokens_used=request.tokens_used,
                cost_usd=request.cost_usd,
            )
            return cosmos_pb2.RecordResultResponse(success=True)
        except Exception as exc:
            logger.error("grpc.sandbox.RecordResult.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.RecordResultResponse(success=False)

    async def CompleteRun(
        self, request: cosmos_pb2.CompleteRunRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.RunResponse:
        """Mark a run as completed and compute aggregate metrics."""
        logger.info("grpc.sandbox.CompleteRun", run_id=request.run_id)
        try:
            result = await self._svc.complete_run(run_id=request.run_id)
            return _dict_to_run_response(result)
        except ValueError as exc:
            logger.error("grpc.sandbox.CompleteRun.not_found", error=str(exc))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return cosmos_pb2.RunResponse()
        except Exception as exc:
            logger.error("grpc.sandbox.CompleteRun.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.RunResponse()

    async def GetRun(
        self, request: cosmos_pb2.GetRunRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.RunResponse:
        """Get details of a sandbox run."""
        logger.info("grpc.sandbox.GetRun", run_id=request.run_id)
        try:
            result = await self._svc.get_run(run_id=request.run_id)
            return _dict_to_run_response(result)
        except ValueError as exc:
            logger.error("grpc.sandbox.GetRun.not_found", error=str(exc))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return cosmos_pb2.RunResponse()
        except Exception as exc:
            logger.error("grpc.sandbox.GetRun.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.RunResponse()

    async def ListRuns(
        self, request: cosmos_pb2.ListRunsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ListRunsResponse:
        """List sandbox runs, optionally filtered by suite."""
        logger.info("grpc.sandbox.ListRuns", suite_id=request.suite_id)
        try:
            suite_id = request.suite_id or None
            runs = await self._svc.list_runs(suite_id=suite_id)
            return cosmos_pb2.ListRunsResponse(
                runs=[_dict_to_run_response(r) for r in runs],
            )
        except Exception as exc:
            logger.error("grpc.sandbox.ListRuns.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ListRunsResponse()

    async def CompareRuns(
        self, request: cosmos_pb2.CompareRunsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.CompareResponse:
        """Compare two sandbox runs and return accuracy/latency/cost deltas."""
        logger.info(
            "grpc.sandbox.CompareRuns",
            run_a=request.run_id_a,
            run_b=request.run_id_b,
        )
        try:
            comparison = await self._svc.compare_runs(
                run_id_a=request.run_id_a,
                run_id_b=request.run_id_b,
            )

            run_a = await self._svc.get_run(request.run_id_a)
            run_b = await self._svc.get_run(request.run_id_b)

            return cosmos_pb2.CompareResponse(
                accuracy_delta=comparison.get("accuracy_delta", 0.0),
                latency_delta=comparison.get("latency_delta_ms", 0.0),
                cost_delta=comparison.get("cost_delta_usd", 0.0),
                improved_cases=len(comparison.get("improved_cases", [])),
                regressed_cases=len(comparison.get("regressed_cases", [])),
                run_a=_dict_to_run_response(run_a),
                run_b=_dict_to_run_response(run_b),
            )
        except ValueError as exc:
            logger.error("grpc.sandbox.CompareRuns.not_found", error=str(exc))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return cosmos_pb2.CompareResponse()
        except Exception as exc:
            logger.error("grpc.sandbox.CompareRuns.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.CompareResponse()

    async def RunEvaluationStream(
        self, request: cosmos_pb2.StartRunRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[cosmos_pb2.EvalProgress]:
        """Stream live evaluation progress as test cases execute.

        Creates a run, iterates over every test case in the suite, records
        a placeholder result for each (agent execution is not wired yet),
        and yields ``EvalProgress`` messages so the caller can track
        completion in real time.
        """
        logger.info(
            "grpc.sandbox.RunEvaluationStream",
            suite_id=request.suite_id,
            version=request.agent_version,
        )
        try:
            run = await self._svc.start_run(
                suite_id=request.suite_id,
                agent_version=request.agent_version,
            )
            run_id = str(run["id"])

            cases = await self._svc.get_test_cases(suite_id=request.suite_id)
            total = len(cases)

            if total == 0:
                yield cosmos_pb2.EvalProgress(
                    test_case_id="",
                    status="completed",
                    passed=False,
                    score=0.0,
                    latency_ms=0,
                    overall_progress=1.0,
                )
                return

            for idx, case in enumerate(cases):
                case_id = str(case["id"])

                # Signal that this case is being evaluated
                yield cosmos_pb2.EvalProgress(
                    test_case_id=case_id,
                    status="running",
                    passed=False,
                    score=0.0,
                    latency_ms=0,
                    overall_progress=float(idx) / total,
                )

                # Record placeholder (agent not connected yet)
                await self._svc.record_result(
                    run_id=run_id,
                    test_case_id=case_id,
                    actual_output="[evaluation pending]",
                    passed=False,
                    score=0.0,
                    latency_ms=0,
                    tokens_used=0,
                    cost_usd=0.0,
                )

                yield cosmos_pb2.EvalProgress(
                    test_case_id=case_id,
                    status="completed",
                    passed=False,
                    score=0.0,
                    latency_ms=0,
                    overall_progress=float(idx + 1) / total,
                )

            await self._svc.complete_run(run_id)

        except Exception as exc:
            logger.error("grpc.sandbox.RunEvaluationStream.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))

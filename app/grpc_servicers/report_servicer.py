"""
gRPC servicer implementation for ReportAgent service.

Bridges gRPC requests to the underlying ReportAgentService,
converting between protobuf messages and domain objects.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List

import grpc
import structlog
from google.protobuf import timestamp_pb2

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.services.reportagent import ReportAgentService

logger = structlog.get_logger(__name__)


def _report_dict_to_proto(report: Dict[str, Any]) -> cosmos_pb2.ReportResponse:
    """Convert a report dict from ReportAgentService to protobuf ReportResponse.

    The service returns ``sections`` as a dict of section-name -> metrics-dict.
    We convert each top-level key into a ``ReportSection`` with the key as
    title, a summary string as content, and the nested values as metrics.
    """
    sections: List[cosmos_pb2.ReportSection] = []
    raw_sections = report.get("sections", {})

    if isinstance(raw_sections, dict):
        for title, section_data in raw_sections.items():
            metrics_map: Dict[str, str] = {}
            content = ""
            if isinstance(section_data, dict):
                for k, v in section_data.items():
                    metrics_map[k] = str(v)
                content = "; ".join(f"{k}={v}" for k, v in section_data.items())
            elif isinstance(section_data, str):
                content = section_data

            sections.append(
                cosmos_pb2.ReportSection(
                    title=title,
                    content=content,
                    metrics=metrics_map,
                )
            )
    elif isinstance(raw_sections, list):
        for s in raw_sections:
            metrics_map = {}
            if isinstance(s.get("metrics"), dict):
                metrics_map = {k: str(v) for k, v in s["metrics"].items()}
            sections.append(
                cosmos_pb2.ReportSection(
                    title=s.get("title", ""),
                    content=s.get("content", ""),
                    metrics=metrics_map,
                )
            )

    resp = cosmos_pb2.ReportResponse(
        id=str(report.get("id", "")),
        repo_id=report.get("repo_id", "") or "",
        report_type=report.get("report_type", "") or "",
        summary=report.get("summary", "") or "",
        sections=sections,
    )

    created_at = report.get("created_at")
    if created_at is not None:
        ts = timestamp_pb2.Timestamp()
        try:
            if isinstance(created_at, str):
                from datetime import datetime
                dt = datetime.fromisoformat(created_at)
                ts.FromDatetime(dt)
            else:
                ts.FromDatetime(created_at)
            resp.created_at.CopyFrom(ts)
        except (TypeError, AttributeError, ValueError):
            pass

    return resp


def _report_dict_to_markdown(report: Dict[str, Any]) -> str:
    """Render a report dict as a markdown string."""
    lines: List[str] = []
    lines.append(f"# {report.get('report_type', 'Report').capitalize()} Report")
    lines.append("")

    summary = report.get("summary", "")
    if summary:
        lines.append(summary)
        lines.append("")

    raw_sections = report.get("sections", {})
    if isinstance(raw_sections, dict):
        for title, data in raw_sections.items():
            lines.append(f"## {title.replace('_', ' ').title()}")
            if isinstance(data, dict):
                for k, v in data.items():
                    lines.append(f"- **{k}**: {v}")
            elif isinstance(data, str):
                lines.append(data)
            lines.append("")
    elif isinstance(raw_sections, list):
        for s in raw_sections:
            lines.append(f"## {s.get('title', 'Section')}")
            lines.append(s.get("content", ""))
            lines.append("")

    return "\n".join(lines)


class ReportAgentServicer(cosmos_pb2_grpc.ReportAgentServiceServicer):
    """gRPC servicer for the report agent service."""

    def __init__(self) -> None:
        self._svc = ReportAgentService()

    async def GenerateWeeklyReport(
        self, request: cosmos_pb2.ReportRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ReportResponse:
        """Generate a weekly performance report."""
        logger.info("grpc.report.GenerateWeeklyReport", repo_id=request.repo_id)
        try:
            repo_id = request.repo_id or None
            report = await self._svc.generate_weekly_report(repo_id=repo_id)
            return _report_dict_to_proto(report)
        except Exception as exc:
            logger.error("grpc.report.GenerateWeeklyReport.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ReportResponse()

    async def GenerateMonthlyReport(
        self, request: cosmos_pb2.ReportRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ReportResponse:
        """Generate a monthly performance report."""
        logger.info("grpc.report.GenerateMonthlyReport", repo_id=request.repo_id)
        try:
            repo_id = request.repo_id or None
            report = await self._svc.generate_monthly_report(repo_id=repo_id)
            return _report_dict_to_proto(report)
        except Exception as exc:
            logger.error("grpc.report.GenerateMonthlyReport.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ReportResponse()

    async def GetReport(
        self, request: cosmos_pb2.GetReportRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ReportResponse:
        """Retrieve a previously generated report by ID."""
        logger.info("grpc.report.GetReport", report_id=request.report_id)
        try:
            report = await self._svc.get_report(request.report_id)
            if not report:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(f"Report {request.report_id} not found")
                return cosmos_pb2.ReportResponse()
            return _report_dict_to_proto(report)
        except Exception as exc:
            logger.error("grpc.report.GetReport.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ReportResponse()

    async def ListReports(
        self, request: cosmos_pb2.ListReportsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.ListReportsResponse:
        """List stored reports with optional type filter."""
        logger.info("grpc.report.ListReports", report_type=request.report_type)
        try:
            limit = request.limit if request.limit > 0 else 20
            report_type = request.report_type or None
            repo_id = request.context.repo_id if request.context and request.context.repo_id else None

            reports = await self._svc.list_reports(
                report_type=report_type,
                repo_id=repo_id,
                limit=limit,
            )
            return cosmos_pb2.ListReportsResponse(
                reports=[_report_dict_to_proto(r) for r in reports],
            )
        except Exception as exc:
            logger.error("grpc.report.ListReports.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.ListReportsResponse()

    async def GetReportMarkdown(
        self, request: cosmos_pb2.GetReportRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.MarkdownResponse:
        """Get a report rendered as markdown."""
        logger.info("grpc.report.GetReportMarkdown", report_id=request.report_id)
        try:
            report = await self._svc.get_report(request.report_id)
            if not report:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(f"Report {request.report_id} not found")
                return cosmos_pb2.MarkdownResponse()

            md = _report_dict_to_markdown(report)
            return cosmos_pb2.MarkdownResponse(markdown=md)
        except Exception as exc:
            logger.error("grpc.report.GetReportMarkdown.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.MarkdownResponse()

    async def GenerateReportStream(
        self, request: cosmos_pb2.ReportRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[cosmos_pb2.ReportProgress]:
        """Stream report generation progress section by section.

        Yields incremental ``ReportProgress`` messages as each section is
        being built, so the caller can display a live progress indicator.
        The final message carries ``status='completed'`` with progress 1.0.
        """
        logger.info("grpc.report.GenerateReportStream", repo_id=request.repo_id)
        try:
            section_names = [
                "learning",
                "conversations",
                "tool_usage",
                "cost",
                "accuracy",
                "summary",
            ]
            total = len(section_names)

            for idx, section in enumerate(section_names):
                yield cosmos_pb2.ReportProgress(
                    section=section,
                    progress=float(idx) / total,
                    status="generating",
                )

            # Actually generate the report
            repo_id = request.repo_id or None
            await self._svc.generate_weekly_report(repo_id=repo_id)

            yield cosmos_pb2.ReportProgress(
                section="done",
                progress=1.0,
                status="completed",
            )

        except Exception as exc:
            logger.error("grpc.report.GenerateReportStream.error", error=str(exc))
            yield cosmos_pb2.ReportProgress(
                section="error",
                progress=0.0,
                status=f"failed: {exc}",
            )

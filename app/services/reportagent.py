"""
Report agent service for COSMOS analytics reporting.

Generates weekly and monthly reports by querying COSMOS PostgreSQL tables,
then stores structured reports in cosmos_reports.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

REPORTS_TABLE = "cosmos_reports"


class ReportAgentService:
    """Generates and manages analytics reports from COSMOS data."""

    async def ensure_schema(self) -> None:
        """Create the reports table if it doesn't exist."""
        log = logger.bind(action="ensure_report_schema")
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {REPORTS_TABLE} (
                        id CHAR(36) PRIMARY KEY,
                        repo_id VARCHAR(255),
                        report_type VARCHAR(50) NOT NULL,
                        period_start TIMESTAMP NOT NULL,
                        period_end TIMESTAMP NOT NULL,
                        sections JSON DEFAULT '{{}}',
                        summary TEXT,
                        created_at TIMESTAMP DEFAULT now()
                    )
                """))
                for idx_sql in [
                    f"CREATE INDEX idx_reports_type_period ON {REPORTS_TABLE} (report_type, period_start DESC)",
                    f"CREATE INDEX idx_reports_repo ON {REPORTS_TABLE} (repo_id)",
                ]:
                    try:
                        await session.execute(text(idx_sql))
                    except Exception:
                        pass  # index already exists
                await session.commit()
                log.info("report_schema_ensured")
            except Exception as exc:
                await session.rollback()
                log.error("report_schema_ensure_failed", error=str(exc))
                raise

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    async def _learning_section(
        self, session: Any, period_start: datetime, period_end: datetime
    ) -> Dict[str, Any]:
        """Build learning metrics from icrm_distillation_records."""
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_records,
                    COUNT(DISTINCT session_id) AS unique_sessions,
                    COUNT(DISTINCT intent) AS unique_intents,
                    AVG(confidence) AS avg_confidence,
                    SUM(token_count_input) AS total_input_tokens,
                    SUM(token_count_output) AS total_output_tokens,
                    SUM(cost_usd) AS total_cost,
                    AVG(feedback_score) AS avg_feedback_score
                FROM icrm_distillation_records
                WHERE created_at >= :start AND created_at < :end
            """),
            {"start": period_start, "end": period_end},
        )
        row = result.fetchone()
        if not row or row.total_records == 0:
            return {"total_records": 0, "summary": "No distillation records in period."}

        # Top intents
        intent_result = await session.execute(
            text("""
                SELECT intent, COUNT(*) AS cnt
                FROM icrm_distillation_records
                WHERE created_at >= :start AND created_at < :end
                  AND intent IS NOT NULL
                GROUP BY intent
                ORDER BY cnt DESC
                LIMIT 10
            """),
            {"start": period_start, "end": period_end},
        )
        top_intents = [{"intent": r.intent, "count": r.cnt} for r in intent_result.fetchall()]

        return {
            "total_records": row.total_records,
            "unique_sessions": row.unique_sessions,
            "unique_intents": row.unique_intents,
            "avg_confidence": round(float(row.avg_confidence or 0), 4),
            "total_input_tokens": row.total_input_tokens or 0,
            "total_output_tokens": row.total_output_tokens or 0,
            "total_cost_usd": round(float(row.total_cost or 0), 4),
            "avg_feedback_score": round(float(row.avg_feedback_score or 0), 2),
            "top_intents": top_intents,
        }

    async def _conversation_section(
        self, session: Any, period_start: datetime, period_end: datetime
    ) -> Dict[str, Any]:
        """Build conversation metrics from icrm_sessions."""
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_sessions,
                    COUNT(DISTINCT user_id) AS unique_users,
                    COUNT(DISTINCT company_id) AS unique_companies
                FROM icrm_sessions
                WHERE created_at >= :start AND created_at < :end
            """),
            {"start": period_start, "end": period_end},
        )
        row = result.fetchone()

        # Channel breakdown
        channel_result = await session.execute(
            text("""
                SELECT channel, COUNT(*) AS cnt
                FROM icrm_sessions
                WHERE created_at >= :start AND created_at < :end
                GROUP BY channel
                ORDER BY cnt DESC
            """),
            {"start": period_start, "end": period_end},
        )
        by_channel = {r.channel: r.cnt for r in channel_result.fetchall()}

        # Status breakdown
        status_result = await session.execute(
            text("""
                SELECT status, COUNT(*) AS cnt
                FROM icrm_sessions
                WHERE created_at >= :start AND created_at < :end
                GROUP BY status
                ORDER BY cnt DESC
            """),
            {"start": period_start, "end": period_end},
        )
        by_status = {r.status: r.cnt for r in status_result.fetchall()}

        # Messages per session average
        msg_result = await session.execute(
            text("""
                SELECT AVG(msg_count) AS avg_messages FROM (
                    SELECT s.id, COUNT(m.id) AS msg_count
                    FROM icrm_sessions s
                    LEFT JOIN icrm_messages m ON m.session_id = s.id
                    WHERE s.created_at >= :start AND s.created_at < :end
                    GROUP BY s.id
                ) sub
            """),
            {"start": period_start, "end": period_end},
        )
        avg_msgs = msg_result.scalar()

        return {
            "total_sessions": row.total_sessions if row else 0,
            "unique_users": row.unique_users if row else 0,
            "unique_companies": row.unique_companies if row else 0,
            "by_channel": by_channel,
            "by_status": by_status,
            "avg_messages_per_session": round(float(avg_msgs or 0), 1),
        }

    async def _tool_usage_section(
        self, session: Any, period_start: datetime, period_end: datetime
    ) -> Dict[str, Any]:
        """Build tool usage metrics from icrm_tool_executions."""
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_executions,
                    COUNT(DISTINCT tool_name) AS unique_tools,
                    AVG(duration_ms) AS avg_duration_ms,
                    COUNT(CASE WHEN status = 'success' THEN 1 END) AS success_count,
                    COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed_count
                FROM icrm_tool_executions
                WHERE created_at >= :start AND created_at < :end
            """),
            {"start": period_start, "end": period_end},
        )
        row = result.fetchone()

        # Top tools
        tool_result = await session.execute(
            text("""
                SELECT tool_name, COUNT(*) AS cnt,
                       AVG(duration_ms) AS avg_ms,
                       COUNT(CASE WHEN status = 'success' THEN 1 END) AS successes
                FROM icrm_tool_executions
                WHERE created_at >= :start AND created_at < :end
                GROUP BY tool_name
                ORDER BY cnt DESC
                LIMIT 15
            """),
            {"start": period_start, "end": period_end},
        )
        top_tools = [
            {
                "tool_name": r.tool_name,
                "count": r.cnt,
                "avg_duration_ms": round(float(r.avg_ms or 0), 1),
                "success_rate": round(r.successes / r.cnt * 100, 1) if r.cnt > 0 else 0,
            }
            for r in tool_result.fetchall()
        ]

        total = row.total_executions if row else 0
        success = row.success_count if row else 0

        return {
            "total_executions": total,
            "unique_tools": row.unique_tools if row else 0,
            "avg_duration_ms": round(float(row.avg_duration_ms or 0), 1) if row else 0,
            "success_count": success,
            "failed_count": row.failed_count if row else 0,
            "success_rate_pct": round(success / total * 100, 1) if total > 0 else 0,
            "top_tools": top_tools,
        }

    async def _cost_section(
        self, session: Any, period_start: datetime, period_end: datetime
    ) -> Dict[str, Any]:
        """Build cost metrics from icrm_analytics."""
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_events,
                    SUM(cost_usd) AS total_cost,
                    AVG(cost_usd) AS avg_cost_per_event,
                    SUM(token_count) AS total_tokens,
                    AVG(duration_ms) AS avg_duration_ms
                FROM icrm_analytics
                WHERE created_at >= :start AND created_at < :end
            """),
            {"start": period_start, "end": period_end},
        )
        row = result.fetchone()

        # Cost by model
        model_result = await session.execute(
            text("""
                SELECT model, SUM(cost_usd) AS total_cost, COUNT(*) AS cnt, SUM(token_count) AS tokens
                FROM icrm_analytics
                WHERE created_at >= :start AND created_at < :end
                  AND model IS NOT NULL
                GROUP BY model
                ORDER BY total_cost DESC
            """),
            {"start": period_start, "end": period_end},
        )
        by_model = [
            {
                "model": r.model,
                "total_cost_usd": round(float(r.total_cost or 0), 4),
                "event_count": r.cnt,
                "total_tokens": r.tokens or 0,
            }
            for r in model_result.fetchall()
        ]

        # Daily cost trend
        daily_result = await session.execute(
            text("""
                SELECT DATE(created_at) AS day, SUM(cost_usd) AS cost
                FROM icrm_analytics
                WHERE created_at >= :start AND created_at < :end
                GROUP BY DATE(created_at)
                ORDER BY day
            """),
            {"start": period_start, "end": period_end},
        )
        daily_trend = [
            {"date": str(r.day), "cost_usd": round(float(r.cost or 0), 4)}
            for r in daily_result.fetchall()
        ]

        return {
            "total_events": row.total_events if row else 0,
            "total_cost_usd": round(float(row.total_cost or 0), 4) if row else 0,
            "avg_cost_per_event_usd": round(float(row.avg_cost_per_event or 0), 6) if row else 0,
            "total_tokens": row.total_tokens or 0 if row else 0,
            "avg_duration_ms": round(float(row.avg_duration_ms or 0), 1) if row else 0,
            "by_model": by_model,
            "daily_trend": daily_trend,
        }

    async def _accuracy_section(
        self, session: Any, period_start: datetime, period_end: datetime
    ) -> Dict[str, Any]:
        """Build accuracy metrics from icrm_query_analytics."""
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_queries,
                    AVG(confidence) AS avg_confidence,
                    AVG(latency_ms) AS avg_latency_ms,
                    COUNT(CASE WHEN escalated THEN 1 END) AS escalated_count,
                    COUNT(DISTINCT intent) AS unique_intents,
                    SUM(cost_usd) AS total_cost
                FROM icrm_query_analytics
                WHERE created_at >= :start AND created_at < :end
            """),
            {"start": period_start, "end": period_end},
        )
        row = result.fetchone()

        # Intent accuracy breakdown
        intent_result = await session.execute(
            text("""
                SELECT intent, COUNT(*) AS cnt,
                       AVG(confidence) AS avg_conf,
                       AVG(latency_ms) AS avg_lat,
                       COUNT(CASE WHEN escalated THEN 1 END) AS esc
                FROM icrm_query_analytics
                WHERE created_at >= :start AND created_at < :end
                  AND intent IS NOT NULL
                GROUP BY intent
                ORDER BY cnt DESC
                LIMIT 15
            """),
            {"start": period_start, "end": period_end},
        )
        by_intent = [
            {
                "intent": r.intent,
                "count": r.cnt,
                "avg_confidence": round(float(r.avg_conf or 0), 4),
                "avg_latency_ms": round(float(r.avg_lat or 0), 1),
                "escalation_rate_pct": round(r.esc / r.cnt * 100, 1) if r.cnt > 0 else 0,
            }
            for r in intent_result.fetchall()
        ]

        # Confidence distribution
        conf_result = await session.execute(
            text("""
                SELECT
                    COUNT(CASE WHEN confidence >= 0.9 THEN 1 END) AS high_conf,
                    COUNT(CASE WHEN confidence >= 0.7 AND confidence < 0.9 THEN 1 END) AS med_conf,
                    COUNT(CASE WHEN confidence < 0.7 THEN 1 END) AS low_conf
                FROM icrm_query_analytics
                WHERE created_at >= :start AND created_at < :end
                  AND confidence IS NOT NULL
            """),
            {"start": period_start, "end": period_end},
        )
        conf_row = conf_result.fetchone()

        total = row.total_queries if row else 0
        escalated = row.escalated_count if row else 0

        return {
            "total_queries": total,
            "avg_confidence": round(float(row.avg_confidence or 0), 4) if row else 0,
            "avg_latency_ms": round(float(row.avg_latency_ms or 0), 1) if row else 0,
            "escalated_count": escalated,
            "escalation_rate_pct": round(escalated / total * 100, 1) if total > 0 else 0,
            "unique_intents": row.unique_intents if row else 0,
            "total_cost_usd": round(float(row.total_cost or 0), 4) if row else 0,
            "confidence_distribution": {
                "high_gte_90": conf_row.high_conf if conf_row else 0,
                "medium_70_90": conf_row.med_conf if conf_row else 0,
                "low_lt_70": conf_row.low_conf if conf_row else 0,
            },
            "by_intent": by_intent,
        }

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    async def _build_report(
        self,
        report_type: str,
        period_start: datetime,
        period_end: datetime,
        repo_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build all sections and store the report."""
        log = logger.bind(
            action="build_report",
            report_type=report_type,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )

        async with AsyncSessionLocal() as session:
            try:
                learning = await self._learning_section(session, period_start, period_end)
                conversations = await self._conversation_section(session, period_start, period_end)
                tool_usage = await self._tool_usage_section(session, period_start, period_end)
                costs = await self._cost_section(session, period_start, period_end)
                accuracy = await self._accuracy_section(session, period_start, period_end)

                sections = {
                    "learning": learning,
                    "conversations": conversations,
                    "tool_usage": tool_usage,
                    "costs": costs,
                    "accuracy": accuracy,
                }

                summary = self._generate_summary(sections, report_type, period_start, period_end)

                # Store report
                report_id = str(uuid.uuid4())
                import json

                await session.execute(
                    text(f"""
                        INSERT INTO {REPORTS_TABLE}
                            (id, repo_id, report_type, period_start, period_end, sections, summary)
                        VALUES
                            (:id, :repo_id, :report_type, :period_start, :period_end, :sections, :summary)
                    """),
                    {
                        "id": report_id,
                        "repo_id": repo_id,
                        "report_type": report_type,
                        "period_start": period_start,
                        "period_end": period_end,
                        "sections": json.dumps(sections),
                        "summary": summary,
                    },
                )
                await session.commit()

                log.info("report_generated", report_id=report_id)
                return {
                    "id": report_id,
                    "report_type": report_type,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "sections": sections,
                    "summary": summary,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                await session.rollback()
                log.error("report_generation_failed", error=str(exc))
                raise

    def _generate_summary(
        self,
        sections: Dict[str, Any],
        report_type: str,
        period_start: datetime,
        period_end: datetime,
    ) -> str:
        """Generate a human-readable summary from report sections."""
        lines = [
            f"COSMOS {report_type.capitalize()} Report",
            f"Period: {period_start.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}",
            "",
        ]

        conv = sections.get("conversations", {})
        lines.append(
            f"Sessions: {conv.get('total_sessions', 0)} "
            f"({conv.get('unique_users', 0)} unique users, "
            f"{conv.get('unique_companies', 0)} companies)"
        )

        learn = sections.get("learning", {})
        lines.append(
            f"Learning: {learn.get('total_records', 0)} distillation records, "
            f"avg confidence {learn.get('avg_confidence', 0)}"
        )

        tools = sections.get("tool_usage", {})
        lines.append(
            f"Tools: {tools.get('total_executions', 0)} executions, "
            f"{tools.get('success_rate_pct', 0)}% success rate"
        )

        costs = sections.get("costs", {})
        lines.append(f"Cost: ${costs.get('total_cost_usd', 0)} total, {costs.get('total_tokens', 0)} tokens")

        acc = sections.get("accuracy", {})
        lines.append(
            f"Accuracy: avg confidence {acc.get('avg_confidence', 0)}, "
            f"{acc.get('escalation_rate_pct', 0)}% escalation rate, "
            f"avg latency {acc.get('avg_latency_ms', 0)}ms"
        )

        return "\n".join(lines)

    async def generate_weekly_report(
        self,
        repo_id: Optional[str] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generate a weekly report ending on end_date (defaults to now)."""
        end = end_date or datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        return await self._build_report("weekly", start, end, repo_id)

    async def generate_monthly_report(
        self,
        repo_id: Optional[str] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generate a monthly report ending on end_date (defaults to now)."""
        end = end_date or datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        return await self._build_report("monthly", start, end, repo_id)

    async def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored report by id."""
        log = logger.bind(action="get_report", report_id=report_id)

        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(f"""
                        SELECT id, repo_id, report_type, period_start, period_end,
                               sections, summary, created_at
                        FROM {REPORTS_TABLE}
                        WHERE id = :id
                    """),
                    {"id": report_id},
                )
                row = result.fetchone()
                if not row:
                    log.info("report_not_found")
                    return None

                return {
                    "id": str(row.id),
                    "repo_id": row.repo_id,
                    "report_type": row.report_type,
                    "period_start": row.period_start.isoformat() if row.period_start else None,
                    "period_end": row.period_end.isoformat() if row.period_end else None,
                    "sections": row.sections or {},
                    "summary": row.summary,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            except Exception as exc:
                log.error("get_report_failed", error=str(exc))
                raise

    async def list_reports(
        self,
        report_type: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List stored reports with optional filters."""
        log = logger.bind(action="list_reports", report_type=report_type, limit=limit)

        filters = []
        params: Dict[str, Any] = {"limit": limit}
        if report_type:
            filters.append("report_type = :report_type")
            params["report_type"] = report_type
        if repo_id:
            filters.append("repo_id = :repo_id")
            params["repo_id"] = repo_id

        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)

        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(f"""
                        SELECT id, repo_id, report_type, period_start, period_end,
                               summary, created_at
                        FROM {REPORTS_TABLE}
                        {where_clause}
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    params,
                )
                rows = result.fetchall()
                reports = [
                    {
                        "id": str(r.id),
                        "repo_id": r.repo_id,
                        "report_type": r.report_type,
                        "period_start": r.period_start.isoformat() if r.period_start else None,
                        "period_end": r.period_end.isoformat() if r.period_end else None,
                        "summary": r.summary,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
                log.info("reports_listed", count=len(reports))
                return reports
            except Exception as exc:
                log.error("list_reports_failed", error=str(exc))
                raise

    def format_as_markdown(self, report: Dict[str, Any]) -> str:
        """Convert a full report dict to formatted Markdown."""
        lines: List[str] = []
        rt = report.get("report_type", "report").capitalize()
        lines.append(f"# COSMOS {rt} Report")
        lines.append("")
        lines.append(
            f"**Period:** {report.get('period_start', '?')} to {report.get('period_end', '?')}"
        )
        lines.append(f"**Generated:** {report.get('created_at', '?')}")
        lines.append("")

        # Summary
        summary = report.get("summary")
        if summary:
            lines.append("## Summary")
            lines.append("")
            lines.append(summary)
            lines.append("")

        sections = report.get("sections", {})

        # Conversations
        conv = sections.get("conversations", {})
        if conv:
            lines.append("## Conversations")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Total Sessions | {conv.get('total_sessions', 0)} |")
            lines.append(f"| Unique Users | {conv.get('unique_users', 0)} |")
            lines.append(f"| Unique Companies | {conv.get('unique_companies', 0)} |")
            lines.append(f"| Avg Messages/Session | {conv.get('avg_messages_per_session', 0)} |")
            lines.append("")
            by_ch = conv.get("by_channel", {})
            if by_ch:
                lines.append("**By Channel:**")
                for ch, cnt in by_ch.items():
                    lines.append(f"- {ch}: {cnt}")
                lines.append("")

        # Learning
        learn = sections.get("learning", {})
        if learn and learn.get("total_records", 0) > 0:
            lines.append("## Learning & Distillation")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Distillation Records | {learn.get('total_records', 0)} |")
            lines.append(f"| Avg Confidence | {learn.get('avg_confidence', 0)} |")
            lines.append(f"| Avg Feedback Score | {learn.get('avg_feedback_score', 0)} |")
            lines.append(f"| Total Cost | ${learn.get('total_cost_usd', 0)} |")
            lines.append("")
            top_intents = learn.get("top_intents", [])
            if top_intents:
                lines.append("**Top Intents:**")
                for ti in top_intents:
                    lines.append(f"- {ti['intent']}: {ti['count']}")
                lines.append("")

        # Tool Usage
        tools = sections.get("tool_usage", {})
        if tools and tools.get("total_executions", 0) > 0:
            lines.append("## Tool Usage")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Total Executions | {tools.get('total_executions', 0)} |")
            lines.append(f"| Success Rate | {tools.get('success_rate_pct', 0)}% |")
            lines.append(f"| Avg Duration | {tools.get('avg_duration_ms', 0)}ms |")
            lines.append("")
            top_tools = tools.get("top_tools", [])
            if top_tools:
                lines.append("**Top Tools:**")
                lines.append("")
                lines.append("| Tool | Count | Avg Duration | Success Rate |")
                lines.append("|------|-------|-------------|-------------|")
                for t in top_tools:
                    lines.append(
                        f"| {t['tool_name']} | {t['count']} | {t['avg_duration_ms']}ms | {t['success_rate_pct']}% |"
                    )
                lines.append("")

        # Costs
        costs = sections.get("costs", {})
        if costs and costs.get("total_events", 0) > 0:
            lines.append("## Costs")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Total Cost | ${costs.get('total_cost_usd', 0)} |")
            lines.append(f"| Total Tokens | {costs.get('total_tokens', 0)} |")
            lines.append(f"| Avg Cost/Event | ${costs.get('avg_cost_per_event_usd', 0)} |")
            lines.append("")
            by_model = costs.get("by_model", [])
            if by_model:
                lines.append("**By Model:**")
                lines.append("")
                lines.append("| Model | Cost | Events | Tokens |")
                lines.append("|-------|------|--------|--------|")
                for m in by_model:
                    lines.append(
                        f"| {m['model']} | ${m['total_cost_usd']} | {m['event_count']} | {m['total_tokens']} |"
                    )
                lines.append("")

        # Accuracy
        acc = sections.get("accuracy", {})
        if acc and acc.get("total_queries", 0) > 0:
            lines.append("## Accuracy & Performance")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Total Queries | {acc.get('total_queries', 0)} |")
            lines.append(f"| Avg Confidence | {acc.get('avg_confidence', 0)} |")
            lines.append(f"| Avg Latency | {acc.get('avg_latency_ms', 0)}ms |")
            lines.append(f"| Escalation Rate | {acc.get('escalation_rate_pct', 0)}% |")
            lines.append("")
            conf_dist = acc.get("confidence_distribution", {})
            if conf_dist:
                lines.append("**Confidence Distribution:**")
                lines.append(f"- High (>=90%): {conf_dist.get('high_gte_90', 0)}")
                lines.append(f"- Medium (70-90%): {conf_dist.get('medium_70_90', 0)}")
                lines.append(f"- Low (<70%): {conf_dist.get('low_lt_70', 0)}")
                lines.append("")

        lines.append("---")
        lines.append("*Generated by COSMOS Report Agent*")
        return "\n".join(lines)

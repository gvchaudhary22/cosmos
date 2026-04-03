"""
Low-Confidence Feedback Loop — Persists traces from queries where confidence < threshold,
then auto-generates negative examples, clarification rules, and missing KB candidates.

Usage in hybrid_chat.py:
    from app.services.feedback_loop import FeedbackLoop
    await FeedbackLoop.maybe_persist(orch_result, query, confidence, tools_used)
"""

import json
import time
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_feedback_traces (
    id              CHAR(36) PRIMARY KEY,
    query           TEXT NOT NULL,
    confidence      REAL NOT NULL,
    query_mode      TEXT,
    domain          TEXT,
    tools_used      JSON DEFAULT '[]',
    ralph_verdict   TEXT,
    resolution_tier INTEGER DEFAULT 0,
    wave_trace_id   TEXT,
    trace_data      JSON DEFAULT '{}',
    feedback_type   TEXT DEFAULT 'low_confidence',
    auto_actions    JSON DEFAULT '[]',
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX idx_feedback_confidence ON cosmos_feedback_traces(confidence);
CREATE INDEX idx_feedback_type ON cosmos_feedback_traces(feedback_type);
CREATE INDEX idx_feedback_domain ON cosmos_feedback_traces(domain);
"""

_schema_ensured = False


class FeedbackLoop:
    """Captures low-confidence traces and generates KB improvement candidates."""

    LOW_CONFIDENCE_THRESHOLD = 0.5
    AMBIGUOUS_THRESHOLD = 0.65

    @classmethod
    async def _ensure_schema(cls) -> None:
        global _schema_ensured
        if _schema_ensured:
            return
        try:
            async with AsyncSessionLocal() as session:
                for stmt in _SCHEMA_SQL.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        try:
                            await session.execute(text(stmt))
                        except Exception:
                            pass  # ignore duplicate index errors
                await session.commit()
            _schema_ensured = True
        except Exception as e:
            logger.debug("feedback_loop.schema_failed", error=str(e))

    @classmethod
    async def maybe_persist(
        cls,
        orch_result,
        query: str,
        confidence: float,
        tools_used: List[str],
        query_mode: str = "lookup",
        domain: str = "",
    ) -> Optional[Dict]:
        """Persist trace if confidence is below threshold. Returns auto-generated actions."""
        if confidence >= cls.AMBIGUOUS_THRESHOLD:
            return None

        await cls._ensure_schema()

        ralph = getattr(orch_result, 'ralph_summary', None) or {}
        ralph_verdict = ralph.get("verdict", "unknown") if isinstance(ralph, dict) else "unknown"

        feedback_type = "low_confidence" if confidence < cls.LOW_CONFIDENCE_THRESHOLD else "ambiguous"

        # Auto-generate improvement candidates
        auto_actions = cls._generate_auto_actions(query, confidence, tools_used, ralph_verdict, query_mode)

        trace_data = {
            "intents": getattr(orch_result, 'intents', []),
            "tiers_visited": getattr(orch_result, 'tiers_visited', []),
            "classification": getattr(orch_result, 'request_classification', {}),
        }

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO cosmos_feedback_traces
                        (query, confidence, query_mode, domain, tools_used,
                         ralph_verdict, resolution_tier, trace_data,
                         feedback_type, auto_actions)
                        VALUES (:query, :conf, :mode, :domain, :tools,
                                :ralph, :tier, :trace,
                                :ftype, :actions)
                    """),
                    {
                        "query": query,
                        "conf": confidence,
                        "mode": query_mode,
                        "domain": domain,
                        "tools": json.dumps(tools_used),
                        "ralph": ralph_verdict,
                        "tier": getattr(orch_result, 'resolution_tier', 0),
                        "trace": json.dumps(trace_data, default=str),
                        "ftype": feedback_type,
                        "actions": json.dumps(auto_actions),
                    },
                )
                await session.commit()

            logger.info("feedback_loop.trace_persisted",
                        query=query[:80], confidence=confidence,
                        feedback_type=feedback_type, actions=len(auto_actions))
            return {"feedback_type": feedback_type, "auto_actions": auto_actions}

        except Exception as e:
            logger.warning("feedback_loop.persist_failed", error=str(e))
            return None

    @classmethod
    def _generate_auto_actions(
        cls, query: str, confidence: float, tools_used: List[str],
        ralph_verdict: str, query_mode: str,
    ) -> List[Dict]:
        """Generate KB improvement candidates from a low-confidence trace."""
        actions = []

        # If no tools matched → candidate for new action/workflow doc
        if not tools_used:
            actions.append({
                "type": "missing_action_candidate",
                "query": query,
                "suggestion": f"No tool matched for '{query_mode}' query. Consider adding an action contract.",
            })

        # If confidence < 0.3 → likely missing KB coverage entirely
        if confidence < 0.3:
            actions.append({
                "type": "missing_kb_coverage",
                "query": query,
                "suggestion": "Very low confidence. This query domain may lack KB docs entirely.",
            })

        # If RALPH verdict is "incomplete" → needs clarification rule
        if ralph_verdict in ("incomplete", "partial"):
            actions.append({
                "type": "add_clarification_rule",
                "query": query,
                "suggestion": "Response was incomplete. Add clarification question for this query pattern.",
            })

        # If multiple tools tried → potential negative routing example
        if len(tools_used) > 2:
            actions.append({
                "type": "add_negative_example",
                "query": query,
                "tools_tried": tools_used,
                "suggestion": f"Query routed to {len(tools_used)} tools. Add negative routing to narrow selection.",
            })

        return actions

    @classmethod
    async def get_recent_traces(
        cls, limit: int = 50, feedback_type: Optional[str] = None,
    ) -> List[Dict]:
        """Fetch recent low-confidence traces for review."""
        await cls._ensure_schema()
        try:
            async with AsyncSessionLocal() as session:
                q = "SELECT * FROM cosmos_feedback_traces"
                params: Dict[str, Any] = {"lim": limit}
                if feedback_type:
                    q += " WHERE feedback_type = :ftype"
                    params["ftype"] = feedback_type
                q += " ORDER BY created_at DESC LIMIT :lim"
                result = await session.execute(text(q), params)
                return [dict(r._mapping) for r in result.fetchall()]
        except Exception as e:
            logger.warning("feedback_loop.get_recent_failed", error=str(e))
            return []

    @classmethod
    async def apply_auto_actions(cls, kb_path: str) -> Dict[str, Any]:
        """Process accumulated low-confidence traces and generate KB improvements.

        Reads recent traces, groups by auto_action type, and produces:
        - Skeleton action contract YAMLs for missing_action_candidate
        - Negative routing entries for add_negative_example
        - Clarification rule candidates for add_clarification_rule
        - Summary report for human review

        Returns a report dict. Does NOT auto-commit to KB — human review required.
        """
        import yaml
        from pathlib import Path

        traces = await cls.get_recent_traces(limit=200, feedback_type="low_confidence")
        traces += await cls.get_recent_traces(limit=100, feedback_type="ambiguous")

        report = {
            "total_traces": len(traces),
            "action_candidates": [],
            "negative_examples": [],
            "clarification_rules": [],
            "kb_coverage_gaps": [],
        }

        staging_dir = Path(kb_path) / "MultiChannel_API" / "_feedback_staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        for trace in traces:
            auto_actions = trace.get("auto_actions", [])
            if isinstance(auto_actions, str):
                try:
                    auto_actions = json.loads(auto_actions)
                except Exception:
                    continue

            for action in auto_actions:
                atype = action.get("type", "")
                query = action.get("query", trace.get("query", ""))

                if atype == "missing_action_candidate":
                    report["action_candidates"].append({
                        "query": query,
                        "domain": trace.get("domain", ""),
                        "suggestion": action.get("suggestion", ""),
                    })

                elif atype == "add_negative_example":
                    report["negative_examples"].append({
                        "query": query,
                        "tools_tried": action.get("tools_tried", []),
                    })

                elif atype == "add_clarification_rule":
                    report["clarification_rules"].append({
                        "query": query,
                        "suggestion": action.get("suggestion", ""),
                    })

                elif atype == "missing_kb_coverage":
                    report["kb_coverage_gaps"].append({
                        "query": query,
                        "domain": trace.get("domain", ""),
                    })

        # Write staging report
        report_path = staging_dir / "feedback_report.yaml"
        with open(report_path, "w") as f:
            yaml.dump(report, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Write negative examples to staging file for human review
        if report["negative_examples"]:
            neg_staging = staging_dir / "staged_negative_examples.yaml"
            neg_data = {
                "description": "Auto-generated from low-confidence traces. Review before adding to pillar_8.",
                "examples": [
                    {
                        "user_query": ne["query"],
                        "tools_tried": ne.get("tools_tried", []),
                        "should_not_use": ne["tools_tried"][0] if ne.get("tools_tried") else "",
                        "correct_tool": "REVIEW_NEEDED",
                        "reason": "Low confidence routing — multiple tools attempted",
                    }
                    for ne in report["negative_examples"][:50]
                ],
            }
            with open(neg_staging, "w") as f:
                yaml.dump(neg_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.info("feedback_loop.auto_actions_applied",
                     candidates=len(report["action_candidates"]),
                     negatives=len(report["negative_examples"]),
                     clarifications=len(report["clarification_rules"]),
                     gaps=len(report["kb_coverage_gaps"]))

        return report

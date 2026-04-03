"""
Quality enhancement module for COSMOS GraphRAG.

Enriches graph nodes and edges with 7 quality dimensions:
  1. Edge weight computation (semantic strength)
  2. Node confidence scoring
  3. Negative routing signals
  4. Guardrail extraction
  5. Eval case extraction
  6. Freshness scoring
  7. EnrichmentPipeline (orchestrates all enrichments post-ingestion)

Usage:
    pipeline = EnrichmentPipeline(kb_path="mars/knowledge_base/")
    report   = await pipeline.enrich_all()
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
import yaml
from sqlalchemy import update, select

from app.db.session import AsyncSessionLocal
from app.services.graphrag import GraphNodeRow, GraphEdgeRow

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# EnrichmentReport
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentReport:
    """Aggregate stats produced by a single EnrichmentPipeline.enrich_all() run."""

    apis_enriched: int = 0
    avg_confidence: float = 0.0
    avg_freshness: float = 0.0
    apis_with_eval_cases: int = 0
    apis_with_guardrails: int = 0
    apis_with_evidence: int = 0
    apis_with_negative_signals: int = 0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. compute_edge_weight
# ---------------------------------------------------------------------------

def compute_edge_weight(
    edge_type: str,
    source_props: Dict[str, Any],
    target_props: Dict[str, Any],
    evidence: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Calculate a meaningful edge weight in [0.0, 1.0] based on edge semantics
    and available metadata.

    Parameters
    ----------
    edge_type     : One of the EdgeType string values (e.g. "reads_table").
    source_props  : JSON properties dict of the source node.
    target_props  : JSON properties dict of the target node.
    evidence      : Optional parsed evidence.yaml content.

    Returns
    -------
    float in [0.0, 1.0]
    """
    evidence = evidence or {}

    if edge_type in ("reads_table", "writes_table"):
        access_patterns = source_props.get("access_patterns", [])
        if isinstance(access_patterns, list) and access_patterns:
            # At least one code-path access pattern recorded → high confidence
            has_code_path = any(
                isinstance(ap, dict) and ap.get("code_path")
                for ap in access_patterns
            )
            return 0.9 if has_code_path else 0.6
        # Only inferred from API path domain
        return 0.3

    if edge_type == "implements_tool":
        confidence_by_section = source_props.get("confidence_by_section", {})
        tool_conf_raw = confidence_by_section.get("tool_assignment", "medium")
        return _map_confidence_label(tool_conf_raw)

    if edge_type == "assigned_to_agent":
        role = source_props.get("agent_role", target_props.get("agent_role", "secondary"))
        return 0.9 if role == "owner" else 0.5

    if edge_type == "has_intent":
        intent_type = source_props.get("intent_type", target_props.get("intent_type", "secondary"))
        return 0.9 if intent_type == "primary" else 0.5

    if edge_type == "belongs_to_domain":
        return 1.0

    return 0.5


# ---------------------------------------------------------------------------
# 2. compute_node_confidence
# ---------------------------------------------------------------------------

def compute_node_confidence(
    node_type: str,
    properties: Dict[str, Any],
    confidence_by_section: Optional[Dict[str, str]] = None,
) -> float:
    """
    Compute an overall node confidence score in [0.0, 1.0].

    Parameters
    ----------
    node_type              : e.g. "api_endpoint", "table", "tool", "agent".
    properties             : JSON properties dict of the node.
    confidence_by_section  : Optional dict from index.yaml mapping section →
                             "high" | "medium" | "low".

    Returns
    -------
    float in [0.0, 1.0]
    """
    confidence_by_section = confidence_by_section or {}

    base_score: float

    if confidence_by_section:
        scores = [_map_confidence_label(v) for v in confidence_by_section.values()]
        base_score = sum(scores) / len(scores)
    else:
        base_score = 0.5

    if node_type == "table":
        columns = properties.get("columns", [])
        base_score = 0.9 if columns else 0.5
        return min(1.0, base_score)

    if node_type == "api_endpoint":
        training_ready = properties.get("training_ready", False)
        if training_ready:
            base_score = min(1.0, base_score + 0.2)

    return round(min(1.0, max(0.0, base_score)), 4)


# ---------------------------------------------------------------------------
# 3. extract_negative_signals
# ---------------------------------------------------------------------------

def extract_negative_signals(tool_agent_tags: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse ``negative_routing_examples`` from tool_agent_tags.yaml into
    structured negative-signal records suitable for storage in node properties.

    Each raw entry is expected to be a dict with keys:
        query           - natural-language query that should NOT trigger this tool
        tool            - the tool that should NOT be chosen
        use_instead     - what to use instead (string or list)

    Returns
    -------
    List of dicts with keys:
        query_pattern, should_not_use, use_instead, signal_type
    """
    raw_examples = tool_agent_tags.get("negative_routing_examples", [])
    if not isinstance(raw_examples, list):
        return []

    signals: List[Dict[str, Any]] = []
    for entry in raw_examples:
        if not isinstance(entry, dict):
            continue
        query = entry.get("query") or entry.get("user_query") or entry.get("query_pattern", "")
        tool = entry.get("tool") or entry.get("should_not_use", "")
        use_instead = entry.get("use_instead", "")
        if isinstance(use_instead, list):
            use_instead = ", ".join(str(u) for u in use_instead)

        signals.append(
            {
                "query_pattern": str(query),
                "should_not_use": str(tool),
                "use_instead": str(use_instead),
                "signal_type": "negative_routing",
            }
        )

    return signals


# ---------------------------------------------------------------------------
# 4. extract_guardrails
# ---------------------------------------------------------------------------

def extract_guardrails(guardrails_yaml: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse guardrails.yaml into a flat dict suitable for storage in node
    JSON properties.

    Expected input keys (all optional):
        safety_constraints : list of {rule, severity, action}
        max_params         : int
        rate_limits        : dict
        pii_fields         : list
        requires_approval  : bool

    Returns
    -------
    Dict with keys: safety_constraints, max_params, rate_limits,
                    pii_fields, requires_approval
    """
    if not isinstance(guardrails_yaml, dict):
        return {}

    safety_constraints = guardrails_yaml.get("safety_constraints", [])
    if not isinstance(safety_constraints, list):
        safety_constraints = []

    max_params = guardrails_yaml.get("max_params", None)
    if max_params is not None:
        try:
            max_params = int(max_params)
        except (TypeError, ValueError):
            max_params = None

    rate_limits = guardrails_yaml.get("rate_limits", {})
    if not isinstance(rate_limits, dict):
        rate_limits = {}

    pii_fields = guardrails_yaml.get("pii_fields", [])
    if not isinstance(pii_fields, list):
        pii_fields = []

    requires_approval = bool(guardrails_yaml.get("requires_approval", False))

    return {
        "safety_constraints": safety_constraints,
        "max_params": max_params,
        "rate_limits": rate_limits,
        "pii_fields": pii_fields,
        "requires_approval": requires_approval,
    }


# ---------------------------------------------------------------------------
# 5. extract_eval_cases
# ---------------------------------------------------------------------------

def extract_eval_cases(eval_cases_yaml: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse eval_cases.yaml ``cases`` list into structured validation records.

    Each raw entry is expected to have keys:
        input, expected_tool, expected_params, expected_output, difficulty

    Returns
    -------
    List of dicts with keys:
        input, expected_tool, expected_params, expected_output, difficulty
    """
    if not isinstance(eval_cases_yaml, dict):
        return []

    raw_cases = eval_cases_yaml.get("cases", [])
    if not isinstance(raw_cases, list):
        return []

    cases: List[Dict[str, Any]] = []
    for entry in raw_cases:
        if not isinstance(entry, dict):
            continue
        cases.append(
            {
                "input": entry.get("input", ""),
                "expected_tool": entry.get("expected_tool", ""),
                "expected_params": entry.get("expected_params", {}),
                "expected_output": entry.get("expected_output", ""),
                "difficulty": entry.get("difficulty", "medium"),
            }
        )

    return cases


# ---------------------------------------------------------------------------
# 6. compute_freshness_score
# ---------------------------------------------------------------------------

def compute_freshness_score(
    index_yaml: Dict[str, Any],
    evidence_yaml: Optional[Dict[str, Any]] = None,
    changelog_yaml: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Compute a freshness score in [0.0, 1.0] for an API node.

    Logic (highest-confidence source wins):
    1. Use ``evidence_yaml.verified_at`` if available.
    2. Otherwise use ``index_yaml.last_verified_at``.
    3. Also check latest changelog entry date and use whichever is newer.
    4. Decay: score = max(0, 1.0 - days_since / stale_after_days)

    Parameters
    ----------
    index_yaml      : Parsed index.yaml dict (may contain last_verified_at,
                      stale_after_days).
    evidence_yaml   : Optional parsed evidence.yaml (may contain verified_at).
    changelog_yaml  : Optional parsed changelog.yaml (may contain changes list
                      with date fields).

    Returns
    -------
    float in [0.0, 1.0]
    """
    evidence_yaml = evidence_yaml or {}
    changelog_yaml = changelog_yaml or {}

    stale_after_days: int = int(index_yaml.get("stale_after_days", 90))
    now = datetime.now(timezone.utc)

    # --- collect candidate "last active" timestamps -------------------------
    candidate_dates: List[datetime] = []

    # evidence.yaml → verified_at
    verified_at_raw = evidence_yaml.get("verified_at")
    if verified_at_raw:
        dt = _parse_datetime(verified_at_raw)
        if dt:
            candidate_dates.append(dt)

    # index.yaml → last_verified_at
    last_verified_raw = index_yaml.get("last_verified_at")
    if last_verified_raw:
        dt = _parse_datetime(last_verified_raw)
        if dt:
            candidate_dates.append(dt)

    # changelog.yaml → latest change date
    changes = changelog_yaml.get("changes", [])
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict):
                dt = _parse_datetime(change.get("date"))
                if dt:
                    candidate_dates.append(dt)

    if not candidate_dates:
        # No date info at all → neutral-low
        return 0.3

    latest_date = max(candidate_dates)
    days_since = (now - latest_date).total_seconds() / 86400.0

    # Bonus: source_commit_sha present means actively tracked
    has_commit_sha = bool(evidence_yaml.get("source_commit_sha"))
    score = max(0.0, 1.0 - (days_since / stale_after_days))
    if has_commit_sha and score > 0:
        score = min(1.0, score + 0.05)

    return round(score, 4)


# ---------------------------------------------------------------------------
# 7. EnrichmentPipeline
# ---------------------------------------------------------------------------

class EnrichmentPipeline:
    """
    Post-ingestion enrichment pipeline.

    Walks every API folder under ``kb_path/pillar_3_api_mcp_tools/``,
    reads quality YAML files, and updates graph_nodes + graph_edges in
    PostgreSQL with richer metadata.
    """

    _QUALITY_FILES = [
        "evidence.yaml",
        "guardrails.yaml",
        "errors_retries.yaml",
        "eval_cases.yaml",
        "changelog.yaml",
    ]

    def __init__(self, kb_path: str) -> None:
        self.kb_root = Path(kb_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enrich_all(self) -> EnrichmentReport:
        """
        Walk all API folders under ``kb_path/shiprocket/{repo}/pillar_3_api_mcp_tools/apis/``,
        enrich each API node with quality metadata, then update graph_nodes and graph_edges.

        Returns
        -------
        EnrichmentReport with aggregate statistics.
        """
        report = EnrichmentReport()
        shiprocket_root = self.kb_root / "shiprocket"

        if not shiprocket_root.exists():
            report.errors.append(f"shiprocket root does not exist: {shiprocket_root}")
            logger.warning("enrich_all.root_missing", path=str(shiprocket_root))
            return report

        confidence_scores: List[float] = []
        freshness_scores: List[float] = []

        for repo_dir in sorted(shiprocket_root.iterdir()):
            if not repo_dir.is_dir():
                continue
            repo_name = repo_dir.name
            apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            if not apis_dir.exists():
                continue

            for api_dir in sorted(apis_dir.iterdir()):
                if not api_dir.is_dir():
                    continue
                try:
                    result = await self.enrich_single_api(
                        api_dir=str(api_dir),
                        repo_name=repo_name,
                    )
                    report.apis_enriched += 1
                    confidence_scores.append(result.get("confidence", 0.5))
                    freshness_scores.append(result.get("freshness", 0.3))
                    if result.get("has_eval_cases"):
                        report.apis_with_eval_cases += 1
                    if result.get("has_guardrails"):
                        report.apis_with_guardrails += 1
                    if result.get("has_evidence"):
                        report.apis_with_evidence += 1
                    if result.get("has_negative_signals"):
                        report.apis_with_negative_signals += 1
                except Exception as exc:  # noqa: BLE001
                    msg = f"{api_dir}: {exc}"
                    report.errors.append(msg)
                    logger.exception("enrich_all.api_error", api_dir=str(api_dir), error=str(exc))

        if confidence_scores:
            report.avg_confidence = round(
                sum(confidence_scores) / len(confidence_scores), 4
            )
        if freshness_scores:
            report.avg_freshness = round(
                sum(freshness_scores) / len(freshness_scores), 4
            )

        logger.info(
            "enrich_all.complete",
            apis_enriched=report.apis_enriched,
            avg_confidence=report.avg_confidence,
            avg_freshness=report.avg_freshness,
            errors=len(report.errors),
        )
        return report

    async def enrich_single_api(self, api_dir: str, repo_name: str) -> dict:
        """
        Enrich one API node with quality data from its folder's YAML files.

        Steps
        -----
        1. Load quality YAML files (evidence, guardrails, errors_retries,
           eval_cases, changelog).
        2. Load already-parsed files (index, tool_agent_tags, overview).
        3. Compute confidence, freshness, negative signals.
        4. Merge enrichment data into node properties.
        5. UPDATE graph_nodes WHERE id = api_node_id.
        6. Recompute edge weights and UPDATE graph_edges.

        Returns
        -------
        dict with enrichment summary keys: confidence, freshness,
        has_eval_cases, has_guardrails, has_evidence, has_negative_signals.
        """
        api_path = Path(api_dir)
        api_name = api_path.name

        # --- load YAML files (H4 fix: read from high/medium/low.yaml) --------
        high = _load_yaml(api_path / "high.yaml")
        medium = _load_yaml(api_path / "medium.yaml")
        low = _load_yaml(api_path / "low.yaml")

        def _section(merged, key, fallback_file):
            """Extract section from merged YAML, fall back to individual file."""
            val = merged.get(key) if isinstance(merged, dict) else None
            if isinstance(val, dict) and val.get("_status") != "stub":
                return val
            return _load_yaml(api_path / fallback_file)

        index_yaml = _section(high, "index", "index.yaml")
        tool_agent_tags = _section(high, "tool_agent_tags", "tool_agent_tags.yaml")
        overview_yaml = _section(high, "overview", "overview.yaml")
        evidence_yaml = _section(low, "evidence", "evidence.yaml")
        guardrails_yaml = _section(medium, "guardrails", "guardrails.yaml")
        errors_retries_yaml = _section(medium, "errors_retries", "errors_retries.yaml")
        eval_cases_yaml = _section(low, "eval_cases", "eval_cases.yaml")
        changelog_yaml = _section(low, "changelog", "changelog.yaml")

        # --- derive quality dimensions ---------------------------------------
        confidence_by_section: Dict[str, str] = index_yaml.get(
            "confidence_by_section", {}
        )
        node_confidence = compute_node_confidence(
            node_type="api_endpoint",
            properties={
                "training_ready": index_yaml.get("training_ready", False),
            },
            confidence_by_section=confidence_by_section,
        )
        freshness = compute_freshness_score(
            index_yaml=index_yaml,
            evidence_yaml=evidence_yaml,
            changelog_yaml=changelog_yaml,
        )
        negative_signals = extract_negative_signals(tool_agent_tags)
        guardrails = extract_guardrails(guardrails_yaml)
        eval_cases = extract_eval_cases(eval_cases_yaml)
        retrieval_hints = overview_yaml.get("retrieval_hints", [])

        safety_meta: Dict[str, Any] = index_yaml.get("safety", {})

        # --- compose enriched properties patch ------------------------------
        quality_patch: Dict[str, Any] = {
            "quality": {
                "confidence": node_confidence,
                "freshness": freshness,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        if negative_signals:
            quality_patch["negative_signals"] = negative_signals
        if guardrails:
            quality_patch["guardrails"] = guardrails
        if eval_cases:
            quality_patch["eval_cases"] = eval_cases
        if retrieval_hints:
            quality_patch["retrieval_hints"] = retrieval_hints
        if safety_meta:
            quality_patch["safety"] = safety_meta
        if evidence_yaml:
            quality_patch["evidence"] = {
                "source_commit_sha": evidence_yaml.get("source_commit_sha"),
                "discovery_method": evidence_yaml.get("discovery_method"),
                "verified_at": str(evidence_yaml.get("verified_at", "")),
                "verification_notes": evidence_yaml.get("verification_notes", ""),
            }
        if errors_retries_yaml:
            quality_patch["errors_retries"] = {
                "error_codes": errors_retries_yaml.get("error_codes", []),
                "retry_strategy": errors_retries_yaml.get("retry_strategy", {}),
            }

        # --- resolve node id ------------------------------------------------
        node_id = _build_api_node_id(api_name, repo_name)

        async with AsyncSessionLocal() as session:
            # Fetch existing node to merge properties
            row = await session.execute(
                select(GraphNodeRow).where(GraphNodeRow.id == node_id)
            )
            node_row: Optional[GraphNodeRow] = row.scalars().first()

            if node_row is None:
                logger.warning(
                    "enrich_single_api.node_not_found",
                    node_id=node_id,
                    api_dir=api_dir,
                )
                return {
                    "confidence": node_confidence,
                    "freshness": freshness,
                    "has_eval_cases": bool(eval_cases),
                    "has_guardrails": bool(guardrails),
                    "has_evidence": bool(evidence_yaml),
                    "has_negative_signals": bool(negative_signals),
                }

            existing_props: Dict[str, Any] = dict(node_row.properties or {})
            merged_props = {**existing_props, **quality_patch}

            await session.execute(
                update(GraphNodeRow)
                .where(GraphNodeRow.id == node_id)
                .values(
                    properties=merged_props,
                    updated_at=datetime.now(timezone.utc),
                )
            )

            # --- re-weight outgoing edges ------------------------------------
            edges_result = await session.execute(
                select(GraphEdgeRow).where(GraphEdgeRow.source_id == node_id)
            )
            edges: List[GraphEdgeRow] = list(edges_result.scalars().all())

            for edge in edges:
                new_weight = compute_edge_weight(
                    edge_type=edge.edge_type,
                    source_props=merged_props,
                    target_props={},  # target props not loaded for perf
                    evidence=evidence_yaml,
                )
                await session.execute(
                    update(GraphEdgeRow)
                    .where(GraphEdgeRow.id == edge.id)
                    .values(weight=new_weight)
                )

            await session.commit()

        logger.info(
            "enrich_single_api.done",
            api_name=api_name,
            repo_name=repo_name,
            confidence=node_confidence,
            freshness=freshness,
            edges_reweighted=len(edges),
        )

        return {
            "confidence": node_confidence,
            "freshness": freshness,
            "has_eval_cases": bool(eval_cases),
            "has_guardrails": bool(guardrails),
            "has_evidence": bool(evidence_yaml),
            "has_negative_signals": bool(negative_signals),
        }

    async def get_quality_report(self) -> dict:
        """
        Query the database for quality statistics across all enriched nodes.

        Returns
        -------
        dict with keys:
            total_api_nodes, avg_confidence, avg_freshness,
            pct_with_eval_cases, pct_with_guardrails,
            pct_with_evidence, pct_with_negative_signals
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GraphNodeRow).where(
                    GraphNodeRow.node_type == "api_endpoint"
                )
            )
            nodes: List[GraphNodeRow] = list(result.scalars().all())

        if not nodes:
            return {
                "total_api_nodes": 0,
                "avg_confidence": 0.0,
                "avg_freshness": 0.0,
                "pct_with_eval_cases": 0.0,
                "pct_with_guardrails": 0.0,
                "pct_with_evidence": 0.0,
                "pct_with_negative_signals": 0.0,
            }

        total = len(nodes)
        confidence_sum = 0.0
        freshness_sum = 0.0
        with_eval = 0
        with_guardrails = 0
        with_evidence = 0
        with_neg_signals = 0

        for node in nodes:
            props = node.properties or {}
            quality = props.get("quality", {})
            confidence_sum += quality.get("confidence", 0.5)
            freshness_sum += quality.get("freshness", 0.3)
            if props.get("eval_cases"):
                with_eval += 1
            if props.get("guardrails"):
                with_guardrails += 1
            if props.get("evidence"):
                with_evidence += 1
            if props.get("negative_signals"):
                with_neg_signals += 1

        return {
            "total_api_nodes": total,
            "avg_confidence": round(confidence_sum / total, 4),
            "avg_freshness": round(freshness_sum / total, 4),
            "pct_with_eval_cases": round(100.0 * with_eval / total, 2),
            "pct_with_guardrails": round(100.0 * with_guardrails / total, 2),
            "pct_with_evidence": round(100.0 * with_evidence / total, 2),
            "pct_with_negative_signals": round(100.0 * with_neg_signals / total, 2),
        }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _map_confidence_label(label: str) -> float:
    """Map a string confidence label to a numeric score."""
    mapping = {"high": 1.0, "medium": 0.6, "low": 0.3}
    return mapping.get(str(label).lower().strip(), 0.5)


def _parse_datetime(value: Any) -> Optional[datetime]:
    """
    Attempt to parse *value* as a timezone-aware datetime.
    Accepts datetime objects, ISO 8601 strings, and YYYY-MM-DD date strings.
    Returns None on failure.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file, returning an empty dict if missing or unparseable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_yaml.failed", path=str(path), error=str(exc))
        return {}


def _build_api_node_id(api_name: str, repo_name: str) -> str:
    """
    Reconstruct the deterministic node ID used during ingestion.

    Mirrors the convention in ingest.py: ``f"api:{api_id}"`` where api_id
    comes from index.yaml (which equals the folder name when index.yaml
    is absent or lacks api_id).
    """
    return f"api:{api_name}"

"""
ParamClarificationEngine — KB-driven missing-parameter detection.

Problem with intent-name matching (why we don't do it):
  The intent classifier outputs a string like "admin_shipments_list" — but the
  user can say ANYTHING. If the classifier mislabels, or the intent string doesn't
  exactly match intent_primary in the YAML, the check silently misses.

Solution — vector search result drives the check:
  Wave 1 already runs semantic search and returns the top-K matching KB chunks,
  each with an entity_id (e.g., "mcapi.v1.admin.shipments.get"). This handles
  arbitrary user phrasing. We check the top vector-matched API docs for
  soft_required_context, not the intent name string.

  Flow:
    1. Wave 1 vector search → knowledge_chunks with entity_id
    2. For each high-confidence chunk (similarity > threshold):
       - If chunk.entity_id is in our index (has soft_required_context)
       - Check if required params are present in query / session_context
       - If missing → return ClarificationRequest
    3. Return None if all params present or no matching API found

  Self-registering: add soft_required_context to any high.yaml, re-embed it
  into Qdrant, and on next server restart the index picks it up automatically.
  No code changes needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Minimum vector similarity for a chunk to trigger param clarification.
# Below this threshold the match is too weak to be actionable.
_MIN_SIMILARITY = 0.65

# Regex patterns to extract company/seller IDs from free-form text.
_COMPANY_ID_RE = re.compile(
    r'(?:company|seller|client|cid|client_id|company_id)'
    r'[\s_]*(?:id)?[\s:=#]*(\d{3,10})',
    re.I,
)
# AWB pattern — 8-20 uppercase alphanumeric chars, common SR formats
_AWB_RE = re.compile(r'\b([A-Z]{2,4}\d{6,16}|\d{10,14})\b')


@dataclass
class ClarificationRequest:
    """Single targeted clarification question to ask the user."""
    question: str           # Exact text to show the user
    pending_param: str      # Name of the param we're asking for (e.g., "client_id")
    api_entity_id: str      # e.g., "mcapi.v1.admin.shipments.get"
    similarity: float = 0.0 # Similarity score of the matched chunk


@dataclass
class _SoftRequired:
    """One soft_required_context entry parsed from high.yaml."""
    param: str
    alias: str
    ask_if_missing: str
    skip_if_present: List[str] = field(default_factory=list)
    extract_from_context: List[str] = field(default_factory=list)


@dataclass
class _APIEntry:
    """Cached entry for one API with soft_required_context."""
    api_entity_id: str
    soft_required: List[_SoftRequired] = field(default_factory=list)


class ParamClarificationEngine:
    """
    Checks if soft_required params are present based on the top vector
    search results — not on intent name string matching.

    Usage (in orchestrator, after result.context is populated):
        clarifier = ParamClarificationEngine(kb_root)
        req = await clarifier.check(
            knowledge_chunks=result.context.get("knowledge_chunks", []),
            query=query,
            company_id=company_id,
            session_context=session_context,
        )
        if req:
            result.needs_clarification = True
            result.clarification_prompt = req.question
    """

    def __init__(self, kb_root: str):
        self._kb_root = Path(kb_root)
        # Index keyed by API entity_id: "mcapi.v1.admin.shipments.get" → _APIEntry
        self._index: Optional[Dict[str, _APIEntry]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self,
        knowledge_chunks: List[Dict[str, Any]],
        query: str,
        company_id: Optional[str] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ClarificationRequest]:
        """
        Return a ClarificationRequest if a soft_required param is missing,
        or None if everything needed is present or no soft_required API matched.

        Drives from knowledge_chunks (vector search results) — handles any
        user phrasing without relying on exact intent name matching.
        """
        if not knowledge_chunks:
            return None

        index = self._get_index()
        if not index:
            return None

        sc = session_context or {}

        # Check top chunks in order of relevance (already sorted by similarity)
        for chunk in knowledge_chunks:
            if not isinstance(chunk, dict):
                continue

            similarity = float(chunk.get("similarity", 0.0))
            if similarity < _MIN_SIMILARITY:
                continue  # Weak match — don't trigger clarification

            entity_id = chunk.get("entity_id", "") or ""
            if not entity_id:
                continue

            entry = index.get(entity_id)
            if entry is None:
                continue

            # Found a high-confidence match for an API with soft_required_context.
            # Check each required param.
            for sr in entry.soft_required:
                if self._should_skip(sr, query, company_id, sc):
                    continue
                if not self._param_present(sr, query, company_id, sc):
                    logger.info(
                        "param_clarifier.missing_param",
                        api=entity_id,
                        param=sr.param,
                        similarity=round(similarity, 3),
                    )
                    return ClarificationRequest(
                        question=sr.ask_if_missing,
                        pending_param=sr.param,
                        api_entity_id=entity_id,
                        similarity=similarity,
                    )

        return None

    # ------------------------------------------------------------------
    # Index management (keyed by entity_id, not intent name)
    # ------------------------------------------------------------------

    def _get_index(self) -> Dict[str, _APIEntry]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> Dict[str, _APIEntry]:
        """
        Scan all high.yaml files under kb_root for soft_required_context.
        Index is keyed by api.id (entity_id), not intent_primary.

        Only files that contain soft_required_context are loaded — typically
        a small subset of all 44K files, so the scan is fast.
        """
        index: Dict[str, _APIEntry] = {}
        if not self._kb_root.is_dir():
            logger.warning("param_clarifier.kb_root_missing", path=str(self._kb_root))
            return index

        count = 0
        for high_yaml in self._kb_root.rglob("*/pillar_3_api_mcp_tools/apis/*/high.yaml"):
            data = self._read_yaml(high_yaml)
            if not data or "soft_required_context" not in data:
                continue

            api_id = data.get("overview", {}).get("api", {}).get("id", "")
            if not api_id:
                continue

            raw_list = data["soft_required_context"]
            if not isinstance(raw_list, list):
                continue

            soft_reqs = []
            for item in raw_list:
                if not isinstance(item, dict) or not item.get("param"):
                    continue
                soft_reqs.append(_SoftRequired(
                    param=item["param"],
                    alias=item.get("alias", ""),
                    ask_if_missing=item.get("ask_if_missing", "Please provide more context."),
                    skip_if_present=item.get("skip_if_present", []),
                    extract_from_context=item.get("extract_from_context", []),
                ))

            if soft_reqs:
                index[api_id] = _APIEntry(api_entity_id=api_id, soft_required=soft_reqs)
                count += 1
                logger.debug("param_clarifier.indexed", api=api_id)

        logger.info("param_clarifier.index_built", entries=count, kb_root=str(self._kb_root))
        return index

    # ------------------------------------------------------------------
    # Param presence checks
    # ------------------------------------------------------------------

    def _should_skip(
        self,
        sr: _SoftRequired,
        query: str,
        company_id: Optional[str],
        sc: Dict,
    ) -> bool:
        """Return True if any skip_if_present condition is satisfied."""
        for skip_param in sr.skip_if_present:
            # AWB in query text → skip_if_present["awb"] fires
            if skip_param == "awb" and _AWB_RE.search(query):
                return True
            # company_id/client_id provided as direct arg → skip_if_present["company_id"] fires
            if skip_param in ("company_id", "client_id"):
                if company_id and company_id not in ("", "0"):
                    return True
                if _COMPANY_ID_RE.search(query):
                    return True
            # General: check session_context
            if sc.get(skip_param):
                return True
        return False

    def _param_present(
        self,
        sr: _SoftRequired,
        query: str,
        company_id: Optional[str],
        sc: Dict,
    ) -> bool:
        """Return True if the soft_required param can be resolved from available context."""
        param = sr.param
        alias = sr.alias

        # 1. company_id/client_id — from execute() arg or query regex
        if param in ("client_id", "company_id") or alias in ("client_id", "company_id"):
            if company_id and company_id not in ("", "0"):
                return True
            if _COMPANY_ID_RE.search(query):
                return True

        # 2. AWB — detect from query text via regex
        if param in ("awb", "awb_code") or alias in ("awb", "awb_code"):
            if _AWB_RE.search(query):
                return True

        # 3. From session_context keys (covers id, sr_order_id, and any other param)
        for key in (param, alias):
            if key and sc.get(key):
                return True

        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_yaml(path: Path) -> Optional[Dict]:
        try:
            import yaml
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return None

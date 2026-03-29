"""
KB-Aware Guardrails — Enforces safety rules from knowledge_base YAML files.

Loads guardrails.yaml + index.yaml safety metadata for each API in the KB
and enforces them at tool execution time:

1. BlastRadiusGuardrail — Blocks high/critical blast_radius tools without approval
2. PIIFieldGuardrail — Masks specific PII fields declared in index.yaml per API
3. RoutingGuardrail — Warns when a tool is used outside its intended domain
4. ApprovalModeGuardrail — Enforces approval_mode from KB (auto/confirm/manual)

These supplement the existing guardrails (rules.py) with API-specific knowledge.
"""

import os
import re
import structlog
from typing import Any, Dict, List, Optional

import yaml

from app.guardrails.base import Guardrail, GuardrailResult, GuardrailAction

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# KB Safety Index — loaded once at startup
# ---------------------------------------------------------------------------

class KBSafetyIndex:
    """In-memory index of safety metadata from KB index.yaml + guardrails.yaml files.

    Keyed by candidate_tool name (e.g., "ndr_list", "order_lookup").
    """

    def __init__(self):
        self._tools: Dict[str, dict] = {}  # tool_name → safety metadata
        self._api_guardrails: Dict[str, dict] = {}  # api_id → guardrails dict

    def load_from_kb(self, kb_path: str) -> dict:
        """Walk KB pillar_3 directories and load safety metadata.

        Returns stats dict with counts.
        """
        loaded = 0
        errors = 0

        # Find all pillar_3 API directories
        for repo_dir in _iter_repo_dirs(kb_path):
            apis_dir = os.path.join(repo_dir, "pillar_3_api_mcp_tools", "apis")
            if not os.path.isdir(apis_dir):
                continue

            for api_id in os.listdir(apis_dir):
                api_path = os.path.join(apis_dir, api_id)
                if not os.path.isdir(api_path):
                    continue

                try:
                    self._load_api(api_id, api_path)
                    loaded += 1
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        logger.warning("kb_safety.load_error", api_id=api_id, error=str(e))

        logger.info("kb_safety.loaded", tools=len(self._tools), apis=loaded, errors=errors)
        return {"tools_indexed": len(self._tools), "apis_loaded": loaded, "errors": errors}

    def _load_api(self, api_id: str, api_path: str):
        """Load index.yaml safety + guardrails.yaml for one API."""
        # Load index.yaml
        index_path = os.path.join(api_path, "index.yaml")
        if not os.path.isfile(index_path):
            return

        with open(index_path, "r") as f:
            index = yaml.safe_load(f) or {}

        safety = index.get("safety", {})
        summary = index.get("summary", {})
        tool_name = summary.get("candidate_tool", "")

        if not tool_name:
            return

        entry = {
            "api_id": api_id,
            "tool_name": tool_name,
            "domain": summary.get("domain", ""),
            "method": summary.get("method", "GET"),
            "path": summary.get("path", ""),
            "read_write_type": safety.get("read_write_type", "READ"),
            "idempotent": safety.get("idempotent", True),
            "approval_mode": safety.get("approval_mode", "auto"),
            "blast_radius": safety.get("blast_radius", "low"),
            "pii_fields": safety.get("pii_fields", []),
        }

        # Load guardrails.yaml if exists
        guardrails_path = os.path.join(api_path, "guardrails.yaml")
        if os.path.isfile(guardrails_path):
            with open(guardrails_path, "r") as f:
                guardrails = yaml.safe_load(f) or {}
            entry["routing_guardrails"] = guardrails.get("guardrails", {}).get("routing_guardrails", [])
            entry["request_validation"] = guardrails.get("guardrails", {}).get("request_validation", [])
            entry["data_safety"] = guardrails.get("guardrails", {}).get("data_safety", [])
            self._api_guardrails[api_id] = guardrails.get("guardrails", {})

        self._tools[tool_name] = entry

    def get_tool_safety(self, tool_name: str) -> Optional[dict]:
        """Get safety metadata for a tool by candidate_tool name."""
        return self._tools.get(tool_name)

    def get_pii_fields(self, tool_name: str) -> List[str]:
        """Get PII fields declared for this tool's API."""
        entry = self._tools.get(tool_name)
        return entry.get("pii_fields", []) if entry else []

    def get_stats(self) -> dict:
        blast_counts = {}
        for t in self._tools.values():
            br = t.get("blast_radius", "unknown")
            blast_counts[br] = blast_counts.get(br, 0) + 1
        return {
            "total_tools": len(self._tools),
            "total_guardrails": len(self._api_guardrails),
            "blast_radius_distribution": blast_counts,
        }


def _iter_repo_dirs(kb_path: str):
    """Yield repo directories under KB path (MultiChannel_API, SR_Web, etc.)."""
    if not os.path.isdir(kb_path):
        return
    for name in os.listdir(kb_path):
        full = os.path.join(kb_path, name)
        if os.path.isdir(full):
            yield full


# Singleton instance — loaded during app startup
kb_safety_index = KBSafetyIndex()


# ---------------------------------------------------------------------------
# Guardrail 1: Blast Radius
# ---------------------------------------------------------------------------

class BlastRadiusGuardrail(Guardrail):
    """Block high/critical blast_radius tools unless explicitly approved.

    Uses blast_radius from KB index.yaml safety section.
    - low: auto-approve
    - medium: warn
    - high: require approval
    - critical: require admin approval
    """
    name = "kb_blast_radius"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        tool_name = context.get("tool_name", "")
        safety = kb_safety_index.get_tool_safety(tool_name)

        if safety is None:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        blast = safety.get("blast_radius", "low")
        approved = context.get("approved", False)

        if blast == "critical" and not approved:
            user_role = context.get("user_role", "")
            if user_role != "admin":
                return GuardrailResult(
                    action=GuardrailAction.BLOCK,
                    reason=f"Tool '{tool_name}' has CRITICAL blast radius. Requires admin approval.",
                )

        if blast == "high" and not approved:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Tool '{tool_name}' has HIGH blast radius. Requires explicit approval.",
            )

        if blast == "medium" and not approved:
            return GuardrailResult(
                action=GuardrailAction.WARN,
                reason=f"Tool '{tool_name}' has MEDIUM blast radius. Proceeding with caution.",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


# ---------------------------------------------------------------------------
# Guardrail 2: KB PII Fields
# ---------------------------------------------------------------------------

class KBPIIFieldGuardrail(Guardrail):
    """Mask PII fields declared in KB index.yaml for each API.

    Supplements the generic PIIProtectionGuardrail with API-specific
    field names (customer_name, customer_email, shipping_address, etc.)
    """
    name = "kb_pii_fields"

    AUTHORIZED_ROLES = ["admin", "support_admin"]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_role = context.get("user_role", "")
        if user_role in self.AUTHORIZED_ROLES:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        tool_name = context.get("tool_name", "")
        pii_fields = kb_safety_index.get_pii_fields(tool_name)
        if not pii_fields:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        response = context.get("response", "")
        if not isinstance(response, str) or not response:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        masked = response
        for field_name in pii_fields:
            # Mask JSON field values: "customer_email": "real@email.com" → "customer_email": "[MASKED]"
            pattern = re.compile(
                rf'("{field_name}"\s*:\s*)"([^"]+)"',
                re.IGNORECASE,
            )
            masked = pattern.sub(rf'\1"[MASKED:{field_name}]"', masked)

        if masked != response:
            return GuardrailResult(
                action=GuardrailAction.MASK,
                reason=f"KB PII fields masked: {pii_fields}",
                modified_data=masked,
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


# ---------------------------------------------------------------------------
# Guardrail 3: Approval Mode
# ---------------------------------------------------------------------------

class ApprovalModeGuardrail(Guardrail):
    """Enforce approval_mode from KB index.yaml.

    - auto: allow immediately (read-only, safe)
    - confirm: require user confirmation
    - manual: require admin approval
    """
    name = "kb_approval_mode"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        tool_name = context.get("tool_name", "")
        safety = kb_safety_index.get_tool_safety(tool_name)

        if safety is None:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        mode = safety.get("approval_mode", "auto")
        approved = context.get("approved", False)

        if mode == "auto":
            return GuardrailResult(action=GuardrailAction.ALLOW)

        if mode == "confirm" and not approved:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Tool '{tool_name}' requires user confirmation (approval_mode=confirm).",
            )

        if mode == "manual" and not approved:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Tool '{tool_name}' requires manual admin approval (approval_mode=manual).",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


# ---------------------------------------------------------------------------
# Guardrail 4: Routing Validation
# ---------------------------------------------------------------------------

class RoutingGuardrail(Guardrail):
    """Warn when a tool is used outside its declared domain/intent context.

    Uses routing_guardrails from KB guardrails.yaml to detect misuse.
    This is a post-execution WARN (doesn't block, just logs).
    """
    name = "kb_routing"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        tool_name = context.get("tool_name", "")
        safety = kb_safety_index.get_tool_safety(tool_name)

        if safety is None:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        intent = context.get("intent", "")
        domain = safety.get("domain", "")

        # If the query intent doesn't match the tool's domain, warn
        if domain and intent and domain not in intent and intent not in domain:
            avoid_rules = safety.get("routing_guardrails", [])
            if avoid_rules:
                return GuardrailResult(
                    action=GuardrailAction.WARN,
                    reason=f"Tool '{tool_name}' (domain={domain}) may not match intent '{intent}'. "
                           f"Routing notes: {avoid_rules[0]}" if avoid_rules else "",
                )

        return GuardrailResult(action=GuardrailAction.ALLOW)

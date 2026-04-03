"""
API Layer Classifier — Organizes 12,000+ APIs into System/Process/Experience layers.

MuleSoft-inspired API-led connectivity pattern:
  Experience APIs (10-20)  → Pre-composed for operator workflows
  Process APIs (100-200)   → Business operations composing multiple system APIs
  System APIs (12,000+)    → Raw CRUD on individual resources

The agent prefers higher layers: Experience > Process > System.
System APIs require explicit justification to call.

Classification runs during KBDrivenRegistry.sync_all().
Tags are stored in graph_nodes.properties.layer.

Usage:
    classifier = APILayerClassifier()
    classifier.classify_all(tools)  # Tags each tool with its layer
"""

from typing import Dict, List, Set

import structlog

logger = structlog.get_logger()


# Experience APIs — pre-composed for specific operator workflows
EXPERIENCE_PATTERNS = {
    "operator_daily_briefing": {
        "description": "Get daily overview for an operator: pending NDRs, stuck shipments, wallet alerts",
        "composes": ["ndr_list", "shipment_search", "wallet_balance", "seller_health_score"],
    },
    "escalation_summary": {
        "description": "Full context for escalating a case to supervisor",
        "composes": ["order_lookup", "shipment_track", "tracking_timeline", "ndr_details"],
    },
    "seller_health_check": {
        "description": "Complete seller health: orders, RTOs, disputes, wallet, plan",
        "composes": ["seller_info", "seller_plan", "seller_health_score", "orders_by_company", "billing_query"],
    },
    "shipment_investigation": {
        "description": "Full shipment delay investigation: tracking, SLA, carrier issues, similar cases",
        "composes": ["shipment_track", "tracking_timeline", "order_lookup"],
    },
    "dispute_resolution_wizard": {
        "description": "Guide through weight dispute resolution: evidence, history, resolution",
        "composes": ["order_lookup", "shipment_track", "billing_query"],
    },
}

# Process API patterns — business operations
PROCESS_PATTERNS = {
    "investigate_delay": ["track", "timeline", "lookup"],
    "process_cancellation": ["lookup", "cancel", "refund"],
    "handle_ndr": ["ndr", "reattempt", "address"],
    "check_billing": ["billing", "wallet", "transaction"],
    "manage_return": ["return", "refund", "reverse"],
    "verify_seller": ["seller", "health", "plan", "kyc"],
    "resolve_dispute": ["weight", "dispute", "evidence"],
}

# System API indicators — raw CRUD operations
SYSTEM_INDICATORS = {"get", "list", "create", "update", "delete", "search", "export", "upload"}


class APILayerClassifier:
    """Classifies tools into System/Process/Experience layers."""

    def classify_tool(self, tool_name: str, tool_data: dict) -> str:
        """Classify a single tool into a layer."""
        name_lower = tool_name.lower()

        # Check Experience layer first
        for exp_name, exp_def in EXPERIENCE_PATTERNS.items():
            if name_lower == exp_name:
                return "experience"
            # Check if this tool is a composed experience API
            if tool_name in exp_def.get("composes", []):
                # It's used BY experience APIs, but it's a process API itself
                pass

        # Check Process layer
        for proc_name, proc_keywords in PROCESS_PATTERNS.items():
            matches = sum(1 for kw in proc_keywords if kw in name_lower)
            if matches >= 2:
                return "process"

        # Check if it's a compound tool (multiple verbs or domains)
        parts = name_lower.split("_")
        domains = {"orders", "shipments", "billing", "courier", "ndr", "settings", "returns", "catalog", "auth"}
        domain_count = sum(1 for p in parts if p in domains)
        action_count = sum(1 for p in parts if p in SYSTEM_INDICATORS)

        # Multi-domain or multi-action = likely process
        if domain_count >= 2 or (action_count >= 1 and len(parts) >= 4):
            return "process"

        # Read-write classification hints
        read_write = tool_data.get("read_write", "").upper()
        risk = tool_data.get("risk_level", "low").lower()
        api_count = tool_data.get("api_count", 0)

        # High API count tools are likely process-level compositions
        if api_count >= 20:
            return "process"

        # WRITE + high risk = process (needs business logic wrapper)
        if read_write == "WRITE" and risk in ("high", "critical"):
            return "process"

        # Default: system
        return "system"

    def classify_all(self, tools: Dict) -> Dict[str, str]:
        """Classify all tools and return {tool_name: layer}."""
        classifications = {}
        counts = {"system": 0, "process": 0, "experience": 0}

        for name, tool in tools.items():
            tool_data = {}
            if hasattr(tool, 'read_write'):
                tool_data["read_write"] = tool.read_write
            if hasattr(tool, 'risk_level'):
                tool_data["risk_level"] = tool.risk_level
            if hasattr(tool, 'api_count'):
                tool_data["api_count"] = tool.api_count

            layer = self.classify_tool(name, tool_data)
            classifications[name] = layer
            counts[layer] += 1

        # Add Experience APIs that don't exist as tools yet (virtual)
        for exp_name in EXPERIENCE_PATTERNS:
            if exp_name not in classifications:
                classifications[exp_name] = "experience"
                counts["experience"] += 1

        logger.info("api_layer.classified",
                     system=counts["system"],
                     process=counts["process"],
                     experience=counts["experience"])

        return classifications

    def get_tool_preference_order(self, classifications: Dict[str, str], candidate_tools: List[str]) -> List[str]:
        """Sort candidate tools by layer preference: experience > process > system."""
        layer_order = {"experience": 0, "process": 1, "system": 2}
        return sorted(
            candidate_tools,
            key=lambda t: layer_order.get(classifications.get(t, "system"), 2),
        )

    def get_experience_apis(self) -> Dict[str, Dict]:
        """Return all defined Experience API templates."""
        return EXPERIENCE_PATTERNS

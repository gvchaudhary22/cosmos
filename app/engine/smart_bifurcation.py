"""
Smart Tool Bifurcation — Uses API path semantics to create meaningful tool names.

Instead of: orders_create_general_hyperlocal (meaningless)
Creates:    orders_hyperlocal_create (clear: create hyperlocal orders)

Strategy:
  1. Group APIs by domain + actual resource + action verb
  2. Name tools as: {domain}_{resource}_{verb}
  3. Resource comes from the API path (the NOUN)
  4. Verb comes from the HTTP method + path keywords (the ACTION)
  5. Tools with < 3 APIs merge into {domain}_{verb}_misc

Target: 150-250 tools, each with a clear name that an LLM can understand.

Names like:
  orders_cancel_order          — cancel an order
  orders_track_shipment        — track order's shipment
  orders_export_report         — export order reports
  shipments_schedule_pickup    — schedule courier pickup
  shipments_assign_awb         — assign AWB to shipment
  ndr_reattempt_delivery       — reattempt NDR delivery
  billing_check_wallet         — check wallet balance
  billing_process_refund       — process a refund
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()

# Resource extraction: path segment → meaningful resource name
RESOURCE_MAP = {
    "orders": "order", "order": "order", "order-earning": "order",
    "shipments": "shipment", "shipment": "shipment", "awb": "awb",
    "ndr": "ndr", "non-delivery": "ndr",
    "returns": "return", "return": "return", "exchange": "exchange",
    "tracking": "tracking", "track": "tracking",
    "pickup": "pickup", "pickups": "pickup",
    "manifest": "manifest", "manifests": "manifest",
    "courier": "courier", "couriers": "courier",
    "billing": "billing", "invoice": "invoice", "invoices": "invoice",
    "wallet": "wallet", "recharge": "wallet",
    "refund": "refund", "cod": "cod", "cod-reconcile": "cod",
    "weight": "weight", "weightdispute": "weight_dispute", "weightescalation": "weight_escalation",
    "products": "product", "product": "product", "catalog": "catalog", "sku": "sku",
    "channels": "channel", "channel": "channel",
    "settings": "settings", "company": "company", "plan": "plan",
    "users": "user", "user": "user", "auth": "auth", "login": "login",
    "webhook": "webhook", "webhooks": "webhook",
    "escalation": "escalation", "escalations": "escalation", "support": "support",
    "warehouse": "warehouse", "wms": "warehouse",
    "notification": "notification", "whatsapp": "whatsapp", "sms": "sms",
    "hyperlocal": "hyperlocal", "international": "international",
    "promise": "promise", "vas": "vas",
    "report": "report", "reports": "report", "export": "export",
    "dashboard": "dashboard", "analytics": "analytics",
    "kyc": "kyc", "gstin": "gstin", "bank": "bank",
}

# Action extraction: path keywords → action verb
ACTION_MAP = {
    "create": "create", "store": "create", "add": "create",
    "import": "import", "upload": "upload", "bulk": "bulk",
    "cancel": "cancel", "delete": "delete",
    "update": "update", "edit": "update", "modify": "update",
    "assign": "assign", "reassign": "reassign",
    "schedule": "schedule", "generate": "generate",
    "verify": "verify", "check": "check", "validate": "validate",
    "track": "track", "status": "status",
    "list": "list", "search": "search", "filter": "filter",
    "export": "export", "download": "download", "print": "print",
    "refund": "refund", "recharge": "recharge", "credit": "credit",
    "escalate": "escalate", "callback": "callback",
    "sync": "sync", "connect": "connect",
    "block": "block", "unblock": "unblock", "freeze": "freeze",
    "reattempt": "reattempt",
}


@dataclass
class SmartTool:
    """A meaningfully named tool from smart bifurcation."""
    name: str                       # e.g., orders_cancel_order
    display_name: str               # e.g., "Cancel Order"
    description: str                # e.g., "Cancel an existing order"
    domain: str                     # e.g., "orders"
    resource: str                   # e.g., "order"
    action: str                     # e.g., "cancel"
    risk_level: str = "medium"
    read_write: str = "READ"
    endpoints: List[Dict] = field(default_factory=list)
    api_count: int = 0
    agent_owner: str = ""
    params: List[Dict] = field(default_factory=list)


def extract_resource_and_action(method: str, path: str) -> Tuple[str, str]:
    """Extract meaningful resource noun + action verb from API path."""
    path_lower = path.lower()
    segments = [s for s in path_lower.split("/")
                if s and s not in ("api", "v1", "v1.1", "v1.2", "internal",
                                   "external", "admin", "app", "oneapp")
                and not s.startswith("{")]

    # Find resource (first meaningful noun in path)
    resource = "misc"
    for seg in segments:
        seg_clean = seg.replace("-", "_")
        if seg_clean in RESOURCE_MAP:
            resource = RESOURCE_MAP[seg_clean]
            break

    # Find action (from path keywords or HTTP method)
    action = "manage"  # default
    for seg in reversed(segments):
        seg_clean = seg.replace("-", "_")
        for keyword, verb in ACTION_MAP.items():
            if keyword in seg_clean:
                action = verb
                break
        if action != "manage":
            break

    # Fall back to HTTP method
    if action == "manage":
        method_action = {"GET": "lookup", "POST": "create", "PUT": "update",
                         "DELETE": "delete", "PATCH": "update"}
        action = method_action.get(method, "manage")

    return resource, action


def smart_bifurcate(tools_data: Dict[str, Any], kb_path: str = "") -> Dict[str, SmartTool]:
    """Create meaningfully named tools from KB API data.

    Args:
        tools_data: {tool_name: DynamicTool} from KBDrivenRegistry
        kb_path: path to KB for enrichment

    Returns:
        {name: SmartTool} with human-readable names
    """
    # Group APIs by domain + resource + action
    groups = defaultdict(lambda: {
        "endpoints": [], "risk": set(), "rw": set(), "agents": set(), "params": [],
    })

    for tool_name, tool in tools_data.items():
        for ep in tool.endpoints:
            method = ep.get("method", "GET")
            path = ep.get("path", "")
            resource, action = extract_resource_and_action(method, path)

            # Tool name: domain_action_resource
            smart_name = f"{tool.domain}_{action}_{resource}"
            # Cap length
            smart_name = smart_name[:40]

            groups[smart_name]["endpoints"].append(ep)
            groups[smart_name]["risk"].add(tool.risk_level)
            groups[smart_name]["rw"].add(tool.read_write)
            groups[smart_name]["agents"].add(tool.agent_owner)
            if not groups[smart_name]["params"] and tool.params:
                groups[smart_name]["params"] = tool.params

    # Build SmartTools, merge tiny groups
    result = {}
    for name, info in groups.items():
        count = len(info["endpoints"])

        if count < 2:
            # Merge into domain_action_misc
            parts = name.split("_")
            domain = parts[0]
            action = parts[1] if len(parts) > 1 else "manage"
            misc_name = f"{domain}_{action}_misc"
            if misc_name not in result:
                rw = "WRITE" if "WRITE" in info["rw"] else "READ"
                risk = "high" if "high" in info["risk"] else "medium" if "medium" in info["risk"] else "low"
                result[misc_name] = SmartTool(
                    name=misc_name,
                    display_name=f"{action.title()} {domain.title()} (misc)",
                    description=f"Miscellaneous {action} operations for {domain}",
                    domain=domain, resource="misc", action=action,
                    risk_level=risk, read_write=rw,
                    agent_owner=next(iter(info["agents"]), ""),
                    params=info["params"],
                )
            result[misc_name].endpoints.extend(info["endpoints"])
            result[misc_name].api_count += count
            continue

        parts = name.split("_")
        domain = parts[0]
        action = parts[1] if len(parts) > 1 else "manage"
        resource = parts[2] if len(parts) > 2 else "general"
        rw = "WRITE" if "WRITE" in info["rw"] else "READ"
        risk = "high" if "high" in info["risk"] else "medium" if "medium" in info["risk"] else "low"

        display = f"{action.title()} {resource.replace('_', ' ').title()}"
        desc = f"{action.title()} {resource.replace('_', ' ')} in {domain} domain"

        result[name] = SmartTool(
            name=name,
            display_name=display,
            description=desc,
            domain=domain,
            resource=resource,
            action=action,
            risk_level=risk,
            read_write=rw,
            endpoints=info["endpoints"],
            api_count=count,
            agent_owner=next(iter(info["agents"]), ""),
            params=info["params"],
        )

    logger.info("smart_bifurcation.complete", tools=len(result),
                 avg_apis=sum(t.api_count for t in result.values()) / max(len(result), 1))
    return result

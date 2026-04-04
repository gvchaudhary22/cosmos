"""
Entity Hub Generator — Creates cross-pillar summary docs per business entity.

Each hub merges P1 (schema) + P3 (APIs) + P6 (actions) + P7 (workflows) + P4 (field traces)
into a single 500-800 token retrieval-optimized doc. This gives LangGraph a complete
picture in one hit without losing per-pillar precision.

Usage in training_pipeline.py:
    from app.services.entity_hub_generator import generate_entity_hubs
    hubs = generate_entity_hubs(kb_reader, repo_id="MultiChannel_API")
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Business entities to generate hubs for
ENTITIES = [
    {"name": "orders", "tables": ["orders", "order_products", "order_items"],
     "description": "Order lifecycle: creation, validation, shipping, delivery, cancellation"},
    {"name": "shipments", "tables": ["shipments", "shipment_meta", "manifests"],
     "description": "Shipment management: AWB, courier, tracking, status, label generation"},
    {"name": "billing", "tables": ["billing_transactions", "wallet", "invoices", "rto_pred_charges"],
     "description": "Billing: freight charges, COD remittance, refunds, wallet, weight disputes"},
    {"name": "ndr", "tables": ["ndr_actions", "ndr_history", "rto_ndr_data", "rto_requests"],
     "description": "NDR lifecycle: non-delivery report, reattempt, escalation, RTO decision"},
    {"name": "pickup", "tables": ["pickup_history", "pickup_locations"],
     "description": "Pickup scheduling, reminder, failure, rescheduling"},
    {"name": "returns", "tables": ["returns", "return_items"],
     "description": "Customer returns: request, approval, QC, reverse pickup, refund"},
    {"name": "settings", "tables": ["companies", "company_settings", "plans"],
     "description": "Seller settings: profile, KYC, plan, preferences, integrations"},
    {"name": "courier", "tables": ["courier_partners", "courier_rates", "courier_serviceability"],
     "description": "Courier management: selection, rates, serviceability, performance"},
    {"name": "support", "tables": ["tickets", "communications", "whatsapp_logs"],
     "description": "Support: WhatsApp, tickets, escalation, CSAT"},
    {"name": "channels", "tables": ["channels", "channel_settings"],
     "description": "Sales channels: Shopify, WooCommerce, Amazon sync, inventory"},
]


def generate_entity_hubs(
    kb_path: str,
    repo_id: str = "MultiChannel_API",
    output_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate Entity Hub YAML files and return as ingestable docs."""
    kb = Path(kb_path) / repo_id
    out = Path(output_dir) if output_dir else kb / "entity_hubs"
    out.mkdir(parents=True, exist_ok=True)

    docs = []
    for entity in ENTITIES:
        hub = _build_hub(kb, entity, repo_id)
        if not hub:
            continue

        # Write YAML file
        hub_path = out / f"{entity['name']}.yaml"
        with open(hub_path, "w") as f:
            yaml.dump(hub, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Build embedding doc
        docs.append({
            "content": hub["content"],
            "entity_type": "entity_hub",
            "entity_id": f"entity_hub:{entity['name']}",
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.95,
            "metadata": {
                "pillar": "entity_hub",
                "domain": entity["name"],
                "chunk_type": "entity_hub_summary",
                "query_mode": "lookup",
                "repo_id": repo_id,
            },
        })

    logger.info("entity_hub_generator.complete", count=len(docs))
    return docs


def _build_hub(kb: Path, entity: Dict, repo_id: str) -> Optional[Dict]:
    """Build one entity hub by merging across pillars."""
    name = entity["name"]
    parts = [f"# Entity Hub: {name}", entity["description"], ""]

    # P1: Schema — key tables and columns
    schema_lines = _gather_schema(kb, entity["tables"])
    if schema_lines:
        parts.append("## Schema")
        parts.extend(schema_lines)
        parts.append("")

    # P3: APIs — read/write endpoints
    api_lines = _gather_apis(kb, name)
    if api_lines:
        parts.append("## APIs")
        parts.extend(api_lines)
        parts.append("")

    # P6: Actions — available action contracts
    action_lines = _gather_actions(kb, name)
    if action_lines:
        parts.append("## Actions")
        parts.extend(action_lines)
        parts.append("")

    # P7: Workflows — state machines
    workflow_lines = _gather_workflows(kb, name)
    if workflow_lines:
        parts.append("## Workflows")
        parts.extend(workflow_lines)
        parts.append("")

    # P4: Field traces
    field_lines = _gather_field_traces(kb, name, repo_id)
    if field_lines:
        parts.append("## Field Traces")
        parts.extend(field_lines)
        parts.append("")

    # P9/P10/P11: Agents, Skills, Tools
    ast_lines = _gather_agents_skills_tools(kb, name)
    if ast_lines:
        parts.append("## Agents / Skills / Tools")
        parts.extend(ast_lines)

    content = "\n".join(parts)

    return {
        "entity": name,
        "description": entity["description"],
        "tables": entity["tables"],
        "content": content,
        "generated_at": "2026-03-30",
    }


def _gather_schema(kb: Path, tables: List[str]) -> List[str]:
    """Extract key columns from P1 table schemas."""
    lines = []
    for table in tables:
        high_dir = kb / "pillar_1_schema" / "tables" / table / "high"
        identity = high_dir / "identity.yaml" if high_dir.exists() else None
        if identity and identity.exists():
            try:
                with open(identity) as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    cols = data.get("columns", data.get("key_columns", []))
                    if isinstance(cols, list):
                        col_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in cols[:8]]
                        lines.append(f"- {table}: {', '.join(col_names)}")
                    elif isinstance(cols, dict):
                        lines.append(f"- {table}: {', '.join(list(cols.keys())[:8])}")
                    else:
                        lines.append(f"- {table}")
            except Exception:
                lines.append(f"- {table}")
        else:
            # Try high.yaml directly
            high_yaml = kb / "pillar_1_schema" / "tables" / table / "high.yaml"
            if high_yaml.exists():
                lines.append(f"- {table}")
    return lines


def _gather_apis(kb: Path, domain: str) -> List[str]:
    """Extract top APIs from P3 registry for this domain."""
    lines = []
    reg_path = kb / "pillar_3_api_mcp_tools" / "api_registry.yaml"
    if not reg_path.exists():
        return lines
    try:
        with open(reg_path) as f:
            data = yaml.safe_load(f)
        apis = data.get("apis", [])
        domain_apis = [a for a in apis if isinstance(a, dict) and a.get("domain") == domain]
        for api in domain_apis[:8]:
            lines.append(f"- {api.get('method', '?')} {api.get('path', '')} ({api.get('tool', '')})")
    except Exception:
        pass
    return lines


def _gather_actions(kb: Path, domain: str) -> List[str]:
    """Extract action contracts from P6 for this domain."""
    lines = []
    actions_dir = kb / "pillar_6_action_contracts" / "domains" / domain
    if not actions_dir.exists():
        return lines
    for action_dir in sorted(actions_dir.iterdir()):
        if not action_dir.is_dir():
            continue
        index = action_dir / "index.yaml"
        if index.exists():
            try:
                with open(index) as f:
                    data = yaml.safe_load(f)
                lines.append(f"- {data.get('action_id', action_dir.name)}: {data.get('title', '')} [{data.get('kind', '')}]")
            except Exception:
                lines.append(f"- {action_dir.name}")
    return lines


def _gather_workflows(kb: Path, domain: str) -> List[str]:
    """Extract workflow runbooks from P7 for this domain."""
    lines = []
    wf_dir = kb / "pillar_7_workflow_runbooks" / "domains" / domain
    if not wf_dir.exists():
        # Check other domains that might reference this entity
        for d in (kb / "pillar_7_workflow_runbooks" / "domains").iterdir() if (kb / "pillar_7_workflow_runbooks" / "domains").exists() else []:
            if not d.is_dir():
                continue
            for wf in d.iterdir():
                if not wf.is_dir():
                    continue
                if domain in wf.name:
                    index = wf / "index.yaml"
                    if index.exists():
                        try:
                            with open(index) as f:
                                data = yaml.safe_load(f)
                            lines.append(f"- {data.get('workflow_id', wf.name)}: {data.get('title', '')}")
                        except Exception:
                            pass
        return lines

    for wf in sorted(wf_dir.iterdir()):
        if not wf.is_dir():
            continue
        index = wf / "index.yaml"
        if index.exists():
            try:
                with open(index) as f:
                    data = yaml.safe_load(f)
                lines.append(f"- {data.get('workflow_id', wf.name)}: {data.get('title', '')}")
            except Exception:
                lines.append(f"- {wf.name}")
    return lines


def _gather_field_traces(kb: Path, domain: str, repo_id: str) -> List[str]:
    """Extract field traces from P4 that reference this domain's tables."""
    lines = []
    for web_repo in ["SR_Web", "MultiChannel_Web"]:
        p4_base = kb.parent / web_repo / "pillar_4_page_role_intelligence" / "pages"
        if not p4_base.exists():
            continue
        for page_dir in sorted(p4_base.iterdir()):
            if not page_dir.is_dir():
                continue
            ft_path = page_dir / "field_trace_chain.yaml"
            if not ft_path.exists():
                continue
            try:
                with open(ft_path) as f:
                    data = yaml.safe_load(f)
                chains = data.get("trace_chains", [])
                for chain in chains:
                    if isinstance(chain, dict) and domain in str(chain.get("db_table", "")):
                        lines.append(
                            f"- {chain.get('page_field', '?')} → "
                            f"{chain.get('api_endpoint', '?')} → "
                            f"{chain.get('db_table', '?')}.{chain.get('db_column', '?')}"
                        )
            except Exception:
                pass
    return lines[:8]  # Cap at 8 traces per hub


def _gather_agents_skills_tools(kb: Path, domain: str) -> List[str]:
    """Extract agents (P9), skills (P10), and tools (P11) for this domain."""
    lines: List[str] = []

    # P9: Agents whose domain matches
    p9_dir = kb / "pillar_9_agents"
    if p9_dir.exists():
        for f in sorted(p9_dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and data.get("domain") == domain:
                    lines.append(
                        f"- agent:{data.get('agent_name', f.stem)} "
                        f"({data.get('tier', '')}): {data.get('description', '')[:80]}"
                    )
            except Exception:
                pass

    # P10: Skills whose domain matches
    p10_dir = kb / "pillar_10_skills"
    if p10_dir.exists():
        for f in sorted(p10_dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and data.get("domain") == domain:
                    lines.append(
                        f"- skill:{data.get('skill_name', f.stem)}: "
                        f"{data.get('description', '')[:80]}"
                    )
            except Exception:
                pass

    # P11: Tools whose domain matches
    p11_dir = kb / "pillar_11_tools"
    if p11_dir.exists():
        for f in sorted(p11_dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and data.get("domain") == domain:
                    lines.append(
                        f"- tool:{data.get('tool_name', f.stem)} "
                        f"[{data.get('category', 'read')} risk:{data.get('risk_level', 'low')}]: "
                        f"{str(data.get('description', ''))[:80].splitlines()[0]}"
                    )
            except Exception:
                pass

    return lines

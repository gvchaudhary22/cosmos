"""
Bridge Document Generator — Creates cross-reference docs that link tables ↔ APIs.

Problem: Pillar 1 (tables) and Pillar 3 (APIs) are independent in embedding space.
When someone asks "how do I create an order?", retrieval might find the API doc OR
the table doc, but not both. The connection between them is lost.

Solution: Generate bridge docs that explicitly connect tables to their APIs,
status flows, and side effects in a single dense document.

Bridge doc format:
  "Table orders is written by POST /api/v1/orders/create (required: order_date,
  channel_id, billing_phone). Status flow: NEW→READY_TO_SHIP→SHIPPED→DELIVERED.
  On insert: AddCustomer job, SaveEncryptedPII. Read by GET /api/v1/orders (returns:
  id, status, customer_name). 22 cron jobs including orders:fetch, orders:status."

These bridge docs fill the gap between isolated table/API embeddings.
"""

from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


def generate_bridge_docs(
    table_docs: List[Dict],
    api_docs: List[Dict],
) -> List[Dict]:
    """Generate cross-reference bridge docs linking tables to their APIs.

    Args:
        table_docs: Output of KBIngestor.read_pillar1_schema()
        api_docs: Output of KBIngestor.read_pillar3_apis()

    Returns:
        List of bridge docs with entity_type='bridge'
    """
    bridges = []

    # Build API index by domain for fast lookup
    apis_by_domain: Dict[str, List[Dict]] = {}
    for api in api_docs:
        meta = api.get("metadata", {})
        domain = meta.get("domain", "").lower()
        if domain:
            apis_by_domain.setdefault(domain, []).append(api)

    for table_doc in table_docs:
        meta = table_doc.get("metadata", {})
        table_name = meta.get("table_name", "")
        domain = meta.get("domain", "").lower()
        repo_id = table_doc.get("repo_id", "")

        if not table_name:
            continue

        # Find related APIs by domain match or table name match
        related_apis = []
        # Match by domain
        domain_key = domain.replace("core_", "").replace("_", "")
        for d, apis in apis_by_domain.items():
            if d == domain_key or d == table_name or table_name.startswith(d):
                related_apis.extend(apis[:5])  # Cap at 5 per domain match
                break

        # Match by table name in API content
        if not related_apis:
            for api in api_docs:
                if table_name in api.get("content", "").lower():
                    related_apis.append(api)
                    if len(related_apis) >= 5:
                        break

        if not related_apis:
            continue

        # Build bridge content
        parts = [f"Bridge: table '{table_name}' ({domain})"]

        # Extract write APIs
        write_apis = [a for a in related_apis if a.get("metadata", {}).get("read_write_type", "").upper() in ("WRITE", "")]
        read_apis = [a for a in related_apis if a.get("metadata", {}).get("read_write_type", "").upper() == "READ"]

        if write_apis:
            api_summaries = []
            for a in write_apis[:3]:
                m = a.get("metadata", {})
                api_summaries.append(f"{m.get('method', '')} {m.get('endpoint', '')}")
            parts.append(f"Written by: {', '.join(api_summaries)}")

        if read_apis:
            api_summaries = [f"{a['metadata'].get('method','')} {a['metadata'].get('endpoint','')}" for a in read_apis[:3]]
            parts.append(f"Read by: {', '.join(api_summaries)}")

        # Extract key info from table content
        table_content = table_doc.get("content", "")

        # Status info
        if "Transitions" in table_content:
            trans_start = table_content.find("Transitions")
            trans_end = table_content.find(" |", trans_start + 1)
            if trans_end > trans_start:
                parts.append(table_content[trans_start:trans_end])

        # Constants info
        if "Constants:" in table_content:
            const_start = table_content.find("Constants:")
            const_end = table_content.find(" |", const_start + 1)
            if const_end > const_start:
                parts.append(table_content[const_start:min(const_end, const_start + 300)])

        # Column count
        col_count = meta.get("column_count", 0)
        if col_count:
            parts.append(f"Columns: {col_count}")

        # Side effects
        if "SideEffects:" in table_content:
            se_start = table_content.find("SideEffects:")
            se_end = table_content.find(" |", se_start + 1)
            if se_end > se_start:
                parts.append(table_content[se_start:min(se_end, se_start + 200)])

        # Cron
        if "Cron" in table_content:
            cron_start = table_content.find("Cron")
            cron_end = table_content.find(" |", cron_start + 1)
            if cron_end > cron_start:
                parts.append(table_content[cron_start:min(cron_end, cron_start + 200)])

        content = " | ".join(parts)

        # Only create bridge if it has meaningful cross-reference content
        if len(parts) < 3:
            continue

        bridges.append({
            "entity_type": "bridge",
            "entity_id": f"bridge:{table_name}",
            "content": content[:4000],
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.85,
            "metadata": {
                "table_name": table_name,
                "domain": domain,
                "related_apis": len(related_apis),
                "source": "bridge_generator",
            },
        })

    logger.info("bridge_generator.complete", bridges=len(bridges))
    return bridges

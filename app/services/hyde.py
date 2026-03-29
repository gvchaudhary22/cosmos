"""
HyDE (Hypothetical Document Embedding) — Query expansion for better retrieval.

Problem: User asks "my shipment is stuck" — the embedding of this short query
doesn't match well against technical KB docs about shipment status codes.

Solution: Generate a hypothetical answer document, then embed THAT instead.
The hypothetical is much closer to the real KB docs in embedding space.

Flow:
  1. User query: "my shipment is stuck"
  2. LLM generates: "The shipment may be stuck at status PICKUP_GENERATED (3)
     or IN_TRANSIT (18). Check via GET /api/v1/shipments/{id}/track. Common
     causes: courier delay, address issue, or weight discrepancy."
  3. Embed the hypothetical → search → get real docs about status codes

This module also handles entity-aware retrieval:
  - Detects status codes, order IDs, AWB numbers in the query
  - Expands the query with entity context from the KB
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)


class HyDEExpander:
    """Expand queries using Hypothetical Document Embedding."""

    def __init__(self, vectorstore=None):
        self.vectorstore = vectorstore

    async def expand(self, query: str) -> str:
        """Expand a query into a hypothetical document for better embedding match.

        Returns the expanded query text (may be original query if expansion fails).
        """
        # Step 1: Entity extraction
        entities = extract_entities(query)

        # Step 2: Generate hypothetical document
        hypothetical = await self._generate_hypothetical(query, entities)

        if hypothetical and len(hypothetical) > len(query) * 1.5:
            logger.info("hyde.expanded", original_len=len(query), expanded_len=len(hypothetical))
            return hypothetical

        # Fallback: entity-augmented query
        if entities:
            augmented = self._augment_with_entities(query, entities)
            return augmented

        return query

    async def _generate_hypothetical(self, query: str, entities: Dict) -> Optional[str]:
        """Generate a hypothetical answer document using LLM."""
        try:
            import httpx

            AIGATEWAY_URL = os.environ.get("AIGATEWAY_URL", "https://aigateway.shiprocket.in")
            AIGATEWAY_API_KEY = os.environ.get("AIGATEWAY_API_KEY", "")
            AIGATEWAY_LLM_MODEL = os.environ.get("AIGATEWAY_LLM_MODEL", "gpt-4o-mini")

            if not AIGATEWAY_API_KEY:
                return None

            entity_context = ""
            if entities.get("status_codes"):
                entity_context += f" Status codes mentioned: {entities['status_codes']}."
            if entities.get("domains"):
                entity_context += f" Domain: {', '.join(entities['domains'])}."

            prompt = (
                f"You are a Shiprocket knowledge base expert. "
                f"Write a brief technical answer (2-3 sentences) to this query as if you were "
                f"quoting from the internal KB documentation. Include specific table names, "
                f"API endpoints, status codes, and column names where relevant.{entity_context}\n\n"
                f"Query: {query}\n\nAnswer:"
            )

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{AIGATEWAY_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {AIGATEWAY_API_KEY}"},
                    json={
                        "model": AIGATEWAY_LLM_MODEL,
                        "provider": "openai",
                        "project_key": "cosmos",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 200,
                        "temperature": 0.3,
                    },
                )

                if resp.status_code != 200:
                    return None

                result = resp.json()
                hypothetical = result["choices"][0]["message"]["content"].strip()

                # Combine original query with hypothetical for embedding
                return f"{query}. {hypothetical}"

        except Exception as e:
            logger.debug("hyde.generation_failed", error=str(e))
            return None

    def _augment_with_entities(self, query: str, entities: Dict) -> str:
        """Augment query with extracted entity context."""
        parts = [query]

        if entities.get("status_codes"):
            parts.append(f"(status codes: {', '.join(str(s) for s in entities['status_codes'])})")
        if entities.get("domains"):
            parts.append(f"(domain: {', '.join(entities['domains'])})")
        if entities.get("table_names"):
            parts.append(f"(tables: {', '.join(entities['table_names'])})")
        if entities.get("api_paths"):
            parts.append(f"(APIs: {', '.join(entities['api_paths'])})")

        return " ".join(parts)


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

# Domain keywords → domain name mapping
_DOMAIN_KEYWORDS = {
    "order": "orders", "shipment": "shipments", "courier": "couriers",
    "billing": "billing", "payment": "billing", "wallet": "billing",
    "ndr": "ndr", "non-delivery": "ndr", "rto": "ndr",
    "product": "products", "catalog": "products", "sku": "products",
    "channel": "channels", "shopify": "channels", "amazon": "channels",
    "pickup": "shipments", "awb": "shipments", "tracking": "shipments",
    "user": "users", "login": "users", "auth": "users",
    "warehouse": "warehouses", "address": "warehouses",
    "weight": "weight_discrepancy", "discrepancy": "weight_discrepancy",
    "manifest": "manifests", "label": "shipments",
    "return": "returns", "exchange": "returns",
    "invoice": "invoices", "cod": "cod_remittance",
    "kyc": "users", "settings": "settings",
}

# Status code patterns
_STATUS_PATTERNS = [
    (r'status\s*(?:code\s*)?[=:]\s*(\d+)', "numeric"),
    (r'status\s+(\d+)', "numeric"),
    (r'\b(NEW_ORDER|READY_TO_SHIP|SHIPPED|DELIVERED|CANCELLED|RTO_INITIATED|RTO_DELIVERED|OUT_FOR_DELIVERY|IN_TRANSIT|PICKUP_SCHEDULED|PICKUP_ERROR)\b', "name"),
]


def extract_entities(query: str) -> Dict[str, Any]:
    """Extract entities (status codes, domains, table names, API paths) from query."""
    entities: Dict[str, Any] = {
        "status_codes": [],
        "domains": [],
        "table_names": [],
        "api_paths": [],
        "order_ids": [],
        "awb_numbers": [],
    }

    query_lower = query.lower()

    # Extract status codes
    for pattern, ptype in _STATUS_PATTERNS:
        for match in re.finditer(pattern, query, re.IGNORECASE):
            code = match.group(1)
            if ptype == "numeric":
                entities["status_codes"].append(int(code))
            else:
                entities["status_codes"].append(code)

    # Extract domains
    for keyword, domain in _DOMAIN_KEYWORDS.items():
        if keyword in query_lower:
            if domain not in entities["domains"]:
                entities["domains"].append(domain)

    # Extract API paths
    for match in re.finditer(r'/api/v\d+/[\w/.-]+', query):
        entities["api_paths"].append(match.group(0))

    # Extract order IDs (numeric, typically 6-10 digits)
    for match in re.finditer(r'\border[_ ]?(?:id)?[:\s]*(\d{6,10})\b', query_lower):
        entities["order_ids"].append(match.group(1))

    # Extract AWB numbers
    for match in re.finditer(r'\bawb[:\s]*(\d{10,20})\b', query_lower):
        entities["awb_numbers"].append(match.group(1))

    return entities

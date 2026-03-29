"""
Handler for Shiprocket Channels order webhook events.

Consumes from topic: sc_webhook_orders_wc
Source: shiprocket-channels service (WooCommerce webhooks)

Message schema (from shiprocket-channels):
{
    "identifier": "webhook",
    "channel_id": "12345",
    "base_channel_code": "WC",
    "event": "orders/create" | "orders/updated" | "orders/cancelled",
    "rawData": "<raw JSON string>",
    "data": { ...parsed WC order object... },
    "channel_order_id": "1001",
    "app_id": "1",
    "uniqueId": "uuid-string"
}

COSMOS uses these events to:
1. Build real-time order context for AI queries (e.g., "Where is my order?")
2. Feed order patterns into analytics for intelligent routing
3. Keep the knowledge base updated with latest order statuses
"""

import structlog
from typing import Optional

logger = structlog.get_logger()

# In-memory recent orders cache for AI context enrichment
# Key: channel_order_id, Value: order summary dict
_recent_orders: dict[str, dict] = {}
_MAX_CACHED_ORDERS = 10000


async def handle_order_webhook(event: dict) -> None:
    """Handle sc_webhook_orders_wc events from shiprocket-channels.

    Processes WooCommerce order webhooks and maintains a real-time
    order context cache that the AI engine can query.
    """
    identifier = event.get("identifier", "unknown")
    channel_id = event.get("channel_id")
    event_type = event.get("event", "unknown")
    channel_order_id = event.get("channel_order_id")
    base_channel_code = event.get("base_channel_code", "")
    unique_id = event.get("uniqueId", event.get("unique_id", ""))

    logger.info(
        "order.webhook.received",
        identifier=identifier,
        channel_id=channel_id,
        event_type=event_type,
        channel_order_id=channel_order_id,
        base_channel_code=base_channel_code,
    )

    # Extract order data
    order_data = event.get("data")
    if not order_data:
        logger.warning("order.webhook.no_data", unique_id=unique_id)
        return

    # Build order summary for AI context
    order_summary = _build_order_summary(
        event_type=event_type,
        channel_id=channel_id,
        channel_order_id=channel_order_id,
        base_channel_code=base_channel_code,
        order_data=order_data,
    )

    # Cache for AI context enrichment
    if channel_order_id:
        _cache_order(channel_order_id, order_summary)

    # Log analytics
    logger.info(
        "order.webhook.processed",
        event_type=event_type,
        channel_order_id=channel_order_id,
        status=order_summary.get("status"),
        total=order_summary.get("total"),
    )


def _build_order_summary(
    event_type: str,
    channel_id: Optional[str],
    channel_order_id: Optional[str],
    base_channel_code: str,
    order_data: dict,
) -> dict:
    """Extract key fields from WC order data into a compact summary."""
    # WooCommerce order fields
    billing = order_data.get("billing", {})
    shipping = order_data.get("shipping", {})
    line_items = order_data.get("line_items", [])

    return {
        "event_type": event_type,
        "channel_id": channel_id,
        "channel_order_id": channel_order_id,
        "base_channel_code": base_channel_code,
        "wc_order_id": order_data.get("id"),
        "status": order_data.get("status", "unknown"),
        "total": order_data.get("total"),
        "currency": order_data.get("currency", "INR"),
        "payment_method": order_data.get("payment_method"),
        "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
        "customer_email": billing.get("email"),
        "shipping_city": shipping.get("city"),
        "shipping_state": shipping.get("state"),
        "item_count": len(line_items),
        "items": [
            {
                "name": item.get("name"),
                "quantity": item.get("quantity"),
                "sku": item.get("sku"),
                "total": item.get("total"),
            }
            for item in line_items[:10]  # Cap at 10 items
        ],
    }


def _cache_order(channel_order_id: str, summary: dict) -> None:
    """Cache order summary for AI context. Evicts oldest if over limit."""
    global _recent_orders
    _recent_orders[channel_order_id] = summary
    # Simple eviction: drop oldest entries
    if len(_recent_orders) > _MAX_CACHED_ORDERS:
        excess = len(_recent_orders) - _MAX_CACHED_ORDERS
        keys_to_remove = list(_recent_orders.keys())[:excess]
        for key in keys_to_remove:
            del _recent_orders[key]


def get_recent_order(channel_order_id: str) -> Optional[dict]:
    """Retrieve a cached order summary by channel_order_id.

    Used by the AI engine to enrich order-related queries with real-time context.
    """
    return _recent_orders.get(channel_order_id)


def get_order_cache_stats() -> dict:
    """Return stats about the order cache."""
    return {
        "cached_orders": len(_recent_orders),
        "max_cached": _MAX_CACHED_ORDERS,
    }

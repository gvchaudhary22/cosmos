"""
COSMOS RabbitMQ Event Producer — Publishes execution events for MARS analytics.

Every query generates events consumed by MARS and stored in analytics tables.
LIME reads from MARS APIs to show dashboards.

Flow: COSMOS → RabbitMQ → MARS Consumer → DB → MARS API → LIME Dashboard

Uses aio-pika for async RabbitMQ. Graceful degradation if RabbitMQ unavailable.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/")
_EXCHANGE = "cosmos_events"
_ENABLED = os.environ.get("COSMOS_RABBITMQ_ENABLED", "false").lower() == "true"


class CosmosEventProducer:
    """Publishes COSMOS execution events to RabbitMQ for MARS analytics."""

    def __init__(self):
        self._connection = None
        self._channel = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to RabbitMQ. Returns False if unavailable."""
        if not _ENABLED:
            logger.debug("cosmos_events.rabbitmq_disabled")
            return False
        try:
            import aio_pika
            self._connection = await aio_pika.connect_robust(_RABBITMQ_URL)
            self._channel = await self._connection.channel()
            # Declare exchange
            await self._channel.declare_exchange(
                _EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True,
            )
            self._connected = True
            logger.info("cosmos_events.rabbitmq_connected", url=_RABBITMQ_URL[:30])
            return True
        except Exception as e:
            logger.warning("cosmos_events.rabbitmq_failed", error=str(e))
            self._connected = False
            return False

    async def close(self):
        if self._connection:
            await self._connection.close()
            self._connected = False

    async def _publish(self, routing_key: str, data: Dict) -> bool:
        """Publish event. Returns False if not connected."""
        if not self._connected or not self._channel:
            return False
        try:
            import aio_pika
            exchange = await self._channel.get_exchange(_EXCHANGE)
            message = aio_pika.Message(
                body=json.dumps(data, default=str).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await exchange.publish(message, routing_key=routing_key)
            return True
        except Exception as e:
            logger.debug("cosmos_events.publish_failed", key=routing_key, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Event publishers
    # ------------------------------------------------------------------

    async def publish_query_trace(
        self,
        trace_id: str,
        query: str,
        user_id: str = "",
        company_id: str = "",
        agent_name: str = "",
        query_mode: str = "lookup",
        confidence: float = 0.0,
        grounding_score: float = 0.0,
        ralph_verdict: str = "unknown",
        tools_used: Optional[List[str]] = None,
        wave_trace: Optional[Dict] = None,
        classification: Optional[Dict] = None,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        source: str = "icrm",
    ) -> bool:
        """Publish full query trace for MARS analytics."""
        return await self._publish("cosmos.query.trace", {
            "trace_id": trace_id,
            "query": query,
            "user_id": user_id,
            "company_id": company_id,
            "agent_name": agent_name,
            "query_mode": query_mode,
            "confidence": confidence,
            "grounding_score": grounding_score,
            "ralph_verdict": ralph_verdict,
            "tools_used": tools_used or [],
            "wave_trace": wave_trace,
            "classification": classification,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "source": source,
            "timestamp": time.time(),
        })

    async def publish_agent_execution(
        self,
        agent_name: str,
        tools_used: List[str],
        success: bool,
        handoff_to: Optional[str] = None,
        latency_ms: float = 0.0,
        confidence: float = 0.0,
        domain: str = "",
    ) -> bool:
        """Publish agent execution event for agent metrics."""
        return await self._publish("cosmos.agent.execution", {
            "agent_name": agent_name,
            "tools_used": tools_used,
            "success": success,
            "handoff_to": handoff_to,
            "latency_ms": latency_ms,
            "confidence": confidence,
            "domain": domain,
            "timestamp": time.time(),
        })

    async def publish_registry_change(
        self,
        change_type: str,  # "created", "updated", "deleted"
        item_type: str,    # "tool", "agent", "skill", "action"
        item_id: str,
        item_data: Optional[Dict] = None,
        changed_by: str = "",
    ) -> bool:
        """Publish registry change for sync tracking."""
        return await self._publish("cosmos.registry.change", {
            "change_type": change_type,
            "item_type": item_type,
            "item_id": item_id,
            "item_data": item_data,
            "changed_by": changed_by,
            "timestamp": time.time(),
        })


# Module singleton
cosmos_event_producer = CosmosEventProducer()

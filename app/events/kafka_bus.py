"""
Kafka Event Bus for COSMOS.

Topics:
  stage-cosmos-query-trace  — Every query result (for MARS observability + quality dashboard)
  cosmos.learning.insight   — GREL learning discoveries (for KB pipeline)
  cosmos.feedback.submitted — User feedback events (for model improvement)
  cosmos.kb.updated         — Knowledge base changes (for MARS/N8N notification)

Architecture:
  Producer: Fire-and-forget from request handlers (zero latency impact)
  Consumer: Background asyncio tasks consuming from topics and writing to DB

Graceful degradation: If Kafka is unavailable, events are logged and dropped.
No query is ever blocked or slowed by Kafka failures.
"""

import asyncio
import json
import time
import structlog
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

class Topic(str, Enum):
    QUERY_COMPLETED = "stage-cosmos-query-trace"
    LEARNING_INSIGHT = "cosmos.learning.insight"
    FEEDBACK_SUBMITTED = "cosmos.feedback.submitted"
    KB_UPDATED = "cosmos.kb.updated"
    # External topics (Shiprocket Channels)
    SC_ORDERS_WC = "sc_webhook_orders_wc"


# ---------------------------------------------------------------------------
# Event schemas
# ---------------------------------------------------------------------------

@dataclass
class QueryCompletedEvent:
    """Produced after every chat query is processed.

    Published to Kafka topic: stage-cosmos-query-trace
    Consumed by MARS (mars-cosmos-trace-consumer) which enriches with
    user/session context and persists to cosmos_query_traces MySQL table.
    """
    session_id: str
    user_id: str
    company_id: Optional[str]
    query: str
    intent: str
    entity: str
    confidence: float
    tools_used: List[str]
    response: str
    escalated: bool
    latency_ms: float
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    # Full 5-wave execution trace for MARS observability dashboard
    wave_trace: Optional[List[Dict[str, Any]]] = field(default=None)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), default=str).encode("utf-8")


@dataclass
class LearningInsightEvent:
    """Produced when GREL discovers a learning insight."""
    insight_id: str
    learning_type: str
    description: str
    evidence: str
    proposed_change: str
    risk_level: str
    query_pattern: str
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")


@dataclass
class FeedbackEvent:
    """Produced when a user submits feedback."""
    session_id: str
    message_id: str
    rating: int
    comment: Optional[str]
    tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")


@dataclass
class KBUpdatedEvent:
    """Produced when knowledge base is updated."""
    update_count: int
    source: str  # "github_webhook", "learning_pipeline", "scheduled", "manual"
    doc_ids: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")


# ---------------------------------------------------------------------------
# Kafka Producer (non-blocking, fire-and-forget)
# ---------------------------------------------------------------------------

def _build_sasl_kwargs(
    security_protocol: str = "PLAINTEXT",
    sasl_mechanism: str = "PLAIN",
    sasl_username: Optional[str] = None,
    sasl_password: Optional[str] = None,
) -> dict:
    """Build SASL kwargs for aiokafka producer/consumer if credentials are set."""
    if not sasl_username or security_protocol == "PLAINTEXT":
        return {}
    return {
        "security_protocol": security_protocol,
        "sasl_mechanism": sasl_mechanism,
        "sasl_plain_username": sasl_username,
        "sasl_plain_password": sasl_password,
    }


class KafkaEventProducer:
    """Async Kafka producer with graceful degradation.

    If Kafka is unavailable, events are logged at warning level and dropped.
    Never blocks or fails the request pipeline.
    """

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        security_protocol: str = "PLAINTEXT",
        sasl_mechanism: str = "PLAIN",
        sasl_username: Optional[str] = None,
        sasl_password: Optional[str] = None,
    ):
        self._bootstrap_servers = bootstrap_servers
        self._sasl_kwargs = _build_sasl_kwargs(
            security_protocol, sasl_mechanism, sasl_username, sasl_password,
        )
        self._producer = None
        self._started = False
        self._stats = {"produced": 0, "errors": 0, "topics": {}}

    async def start(self):
        """Start the Kafka producer. Safe to call multiple times."""
        if self._started:
            return
        try:
            from aiokafka import AIOKafkaProducer
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                value_serializer=None,  # We serialize ourselves
                acks="all",
                retry_backoff_ms=100,
                request_timeout_ms=5000,
                max_batch_size=16384,
                linger_ms=10,  # Batch for 10ms for throughput
                **self._sasl_kwargs,
            )
            await self._producer.start()
            self._started = True
            logger.info("kafka.producer.started", servers=self._bootstrap_servers)
        except Exception as e:
            logger.warning("kafka.producer.start_failed", error=str(e))
            self._producer = None

    async def stop(self):
        """Flush and stop the producer."""
        if self._producer and self._started:
            try:
                await self._producer.stop()
            except Exception:
                pass
            self._started = False
            logger.info("kafka.producer.stopped")

    async def produce(self, topic: Topic, event_data: bytes, key: Optional[str] = None):
        """Produce an event. Non-blocking, fire-and-forget.

        If Kafka is down, logs warning and returns (no exception).
        """
        if not self._started or self._producer is None:
            self._stats["errors"] += 1
            return

        try:
            key_bytes = key.encode("utf-8") if key else None
            await self._producer.send_and_wait(topic.value, event_data, key=key_bytes)
            self._stats["produced"] += 1
            self._stats["topics"][topic.value] = self._stats["topics"].get(topic.value, 0) + 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("kafka.produce.failed", topic=topic.value, error=str(e))

    def get_stats(self) -> dict:
        return {
            "started": self._started,
            "total_produced": self._stats["produced"],
            "total_errors": self._stats["errors"],
            "by_topic": self._stats["topics"],
        }


# ---------------------------------------------------------------------------
# Kafka Consumer (background workers)
# ---------------------------------------------------------------------------

class KafkaEventConsumer:
    """Background Kafka consumer that processes events and writes to DB.

    Runs as asyncio tasks — one per topic.
    """

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        group_id: str = "cosmos-workers",
        security_protocol: str = "PLAINTEXT",
        sasl_mechanism: str = "PLAIN",
        sasl_username: Optional[str] = None,
        sasl_password: Optional[str] = None,
    ):
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._sasl_kwargs = _build_sasl_kwargs(
            security_protocol, sasl_mechanism, sasl_username, sasl_password,
        )
        self._handlers: Dict[str, Callable] = {}
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._stats = {"consumed": 0, "errors": 0, "by_topic": {}}

    def register_handler(
        self,
        topic: Topic,
        handler: Callable[..., Coroutine[Any, Any, None]],
    ):
        """Register an async handler for a topic."""
        self._handlers[topic.value] = handler

    async def start(self):
        """Start consumer tasks for all registered topics."""
        if self._running:
            return
        self._running = True

        for topic_name, handler in self._handlers.items():
            task = asyncio.create_task(
                self._consume_loop(topic_name, handler),
                name=f"kafka-consumer-{topic_name}",
            )
            self._tasks.append(task)

        logger.info(
            "kafka.consumer.started",
            topics=list(self._handlers.keys()),
            group_id=self._group_id,
        )

    async def stop(self):
        """Stop all consumer tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("kafka.consumer.stopped")

    async def _consume_loop(self, topic_name: str, handler: Callable):
        """Consume messages from a single topic."""
        try:
            from aiokafka import AIOKafkaConsumer
        except ImportError:
            logger.error("kafka.consumer.aiokafka_not_installed")
            return

        consumer = None
        while self._running:
            try:
                if consumer is None:
                    consumer = AIOKafkaConsumer(
                        topic_name,
                        bootstrap_servers=self._bootstrap_servers,
                        group_id=self._group_id,
                        auto_offset_reset="earliest",
                        enable_auto_commit=True,
                        auto_commit_interval_ms=5000,
                        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                        **self._sasl_kwargs,
                    )
                    await consumer.start()
                    logger.info("kafka.consumer.connected", topic=topic_name)

                async for msg in consumer:
                    if not self._running:
                        break
                    try:
                        await handler(msg.value)
                        self._stats["consumed"] += 1
                        self._stats["by_topic"][topic_name] = (
                            self._stats["by_topic"].get(topic_name, 0) + 1
                        )
                    except Exception as e:
                        self._stats["errors"] += 1
                        logger.error(
                            "kafka.consumer.handler_error",
                            topic=topic_name,
                            error=str(e),
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "kafka.consumer.connection_error",
                    topic=topic_name,
                    error=str(e),
                )
                if consumer:
                    try:
                        await consumer.stop()
                    except Exception:
                        pass
                    consumer = None
                # Backoff before reconnect
                await asyncio.sleep(5)

        if consumer:
            try:
                await consumer.stop()
            except Exception:
                pass

    def get_stats(self) -> dict:
        return {
            "running": self._running,
            "total_consumed": self._stats["consumed"],
            "total_errors": self._stats["errors"],
            "by_topic": self._stats["by_topic"],
            "active_tasks": len(self._tasks),
        }


# ---------------------------------------------------------------------------
# Event Bus (combines producer + consumer)
# ---------------------------------------------------------------------------

class EventBus:
    """Unified event bus combining producer and consumer.

    Usage:
        bus = EventBus(bootstrap_servers="localhost:9092")
        bus.register_handler(Topic.QUERY_COMPLETED, handle_query)
        await bus.start()

        # Produce events (non-blocking)
        await bus.produce_query_completed(event)

        # Shutdown
        await bus.stop()
    """

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        group_id: str = "cosmos-workers",
        enabled: bool = True,
        security_protocol: str = "PLAINTEXT",
        sasl_mechanism: str = "PLAIN",
        sasl_username: Optional[str] = None,
        sasl_password: Optional[str] = None,
    ):
        self._enabled = enabled
        sasl_args = dict(
            security_protocol=security_protocol,
            sasl_mechanism=sasl_mechanism,
            sasl_username=sasl_username,
            sasl_password=sasl_password,
        )
        self.producer = KafkaEventProducer(bootstrap_servers, **sasl_args)
        self.consumer = KafkaEventConsumer(bootstrap_servers, group_id, **sasl_args)

    def register_handler(self, topic: Topic, handler: Callable):
        self.consumer.register_handler(topic, handler)

    async def start(self):
        if not self._enabled:
            logger.info("kafka.event_bus.disabled")
            return
        await self.producer.start()
        await self.consumer.start()

    async def stop(self):
        await self.consumer.stop()
        await self.producer.stop()

    # --- Typed produce methods ---

    async def produce_query_completed(self, event: QueryCompletedEvent):
        await self.producer.produce(
            Topic.QUERY_COMPLETED,
            event.to_json(),
            key=event.session_id,
        )

    async def produce_learning_insight(self, event: LearningInsightEvent):
        await self.producer.produce(
            Topic.LEARNING_INSIGHT,
            event.to_json(),
            key=event.insight_id,
        )

    async def produce_feedback(self, event: FeedbackEvent):
        await self.producer.produce(
            Topic.FEEDBACK_SUBMITTED,
            event.to_json(),
            key=event.session_id,
        )

    async def produce_kb_updated(self, event: KBUpdatedEvent):
        await self.producer.produce(
            Topic.KB_UPDATED,
            event.to_json(),
        )

    def get_stats(self) -> dict:
        return {
            "enabled": self._enabled,
            "producer": self.producer.get_stats(),
            "consumer": self.consumer.get_stats(),
        }

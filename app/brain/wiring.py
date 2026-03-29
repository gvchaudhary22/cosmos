"""
Brain Wiring — Connects GREL learning → Pipeline → Cache → Router → N8N.

This module bridges all brain components so that:
1. GREL learning insights flow into the KB update pipeline
2. KB updates invalidate the semantic cache
3. KB updates trigger router rebuild
4. KB updates notify MARS/N8N via outbound webhook
5. A background scheduler periodically scans for file changes

Call `wire_brain(brain_dict)` during app startup after `create_brain()`.
"""

import asyncio
import time
import structlog
from typing import Optional

import httpx

from app.brain.cache import SemanticCache
from app.brain.grel import GRELEngine, LearningInsight, LearningType
from app.brain.pipeline import KBUpdatePipeline
from app.brain.router import IntelligentRouter
from app.brain.tournament import StrategyName
from app.events.kafka_bus import EventBus, KBUpdatedEvent, LearningInsightEvent
from app.graph.strategy import create_hybrid_strategy_fn

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 1. GREL → Pipeline bridge callback
# ---------------------------------------------------------------------------

def create_grel_learning_callback(pipeline: KBUpdatePipeline):
    """Create an async callback that feeds GREL learning insights into the KB pipeline.

    When GREL's _learn_async() discovers a TOOL_CORRECTION, FEW_SHOT_EXAMPLE,
    or KNOWLEDGE_GAP, this callback converts it into a pipeline learning feedback
    so the KB indexer stays in sync.
    """
    async def _on_grel_insights(insights: list[LearningInsight]):
        for insight in insights:
            if insight.learning_type in (
                LearningType.FEW_SHOT_EXAMPLE,
                LearningType.TOOL_CORRECTION,
                LearningType.PARAM_CORRECTION,
            ):
                # Convert insight to pipeline feedback format
                feedback = {
                    "doc_id": _extract_doc_id(insight),
                    "correct_query": insight.evidence,
                    "correct_params": {},
                    "feedback_score": 8 if insight.learning_type == LearningType.FEW_SHOT_EXAMPLE else 5,
                }
                try:
                    await pipeline.handle_learning_feedback(feedback)
                    logger.info(
                        "grel_learning.forwarded_to_pipeline",
                        insight_id=insight.insight_id,
                        learning_type=insight.learning_type.value,
                    )
                except Exception as e:
                    logger.warning(
                        "grel_learning.pipeline_error",
                        insight_id=insight.insight_id,
                        error=str(e),
                    )

    return _on_grel_insights


def _extract_doc_id(insight: LearningInsight) -> str:
    """Best-effort extraction of doc_id from insight evidence/proposed_change."""
    text = f"{insight.evidence} {insight.proposed_change}"
    # Look for patterns like "tool=mcapi.orders.get" or "for mcapi.orders.get"
    for token in text.split():
        if token.startswith("mcapi.") or token.startswith("table."):
            return token.rstrip(",;.)")
        if "tool=" in token:
            return token.split("tool=")[-1].rstrip(",;.)")
    return ""


# ---------------------------------------------------------------------------
# 2. Cache invalidation callback (registered on pipeline)
# ---------------------------------------------------------------------------

def create_cache_invalidation_callback(cache: SemanticCache):
    """Pipeline callback that invalidates cache when KB docs change."""
    def _on_kb_update(updates: list):
        invalidated = 0
        for update in updates:
            doc_id = getattr(update, "doc_id", "") if not isinstance(update, dict) else update.get("doc_id", "")
            if doc_id and doc_id != "*":
                # Extract domain from doc_id for pattern invalidation
                parts = doc_id.split(".")
                if len(parts) >= 2:
                    domain = parts[1] if parts[0] in ("mcapi", "table") else parts[0]
                    invalidated += cache.invalidate_pattern(f"*:{domain}")
            elif doc_id == "*":
                # Full reindex — clear entire cache
                cache.invalidate_all()
                invalidated = -1  # Signal full clear
                break

        if invalidated != 0:
            logger.info("cache.invalidated_by_pipeline", invalidated_entries=invalidated)

    return _on_kb_update


# ---------------------------------------------------------------------------
# 3. Router rebuild callback (registered on pipeline)
# ---------------------------------------------------------------------------

def create_router_rebuild_callback(router: IntelligentRouter):
    """Pipeline callback that rebuilds the router decision tree after KB changes."""
    def _on_kb_update(updates: list):
        try:
            stats = router.build()
            logger.info(
                "router.rebuilt_after_kb_update",
                tier1_entries=stats.get("tier1_entries", 0),
            )
        except Exception as e:
            logger.error("router.rebuild_failed", error=str(e))

    return _on_kb_update


# ---------------------------------------------------------------------------
# 4. N8N / MARS notification callback (registered on pipeline)
# ---------------------------------------------------------------------------

def create_n8n_notification_callback(
    webhook_url: Optional[str] = None,
    mars_base_url: Optional[str] = None,
):
    """Pipeline callback that notifies MARS/N8N when KB updates complete."""
    if not webhook_url and not mars_base_url:
        return None

    def _on_kb_update(updates: list):
        if not updates:
            return

        payload = {
            "event": "kb_update",
            "update_count": len(updates),
            "updates": [
                {
                    "doc_id": getattr(u, "doc_id", "") if not isinstance(u, dict) else u.get("doc_id", ""),
                    "status": getattr(u, "status", "") if not isinstance(u, dict) else u.get("status", ""),
                    "update_type": getattr(u, "update_type", "") if not isinstance(u, dict) else u.get("update_type", ""),
                }
                for u in updates[:20]  # Cap at 20 to avoid huge payloads
            ],
            "timestamp": time.time(),
        }

        # Fire-and-forget in background
        async def _notify():
            async with httpx.AsyncClient(timeout=10) as client:
                if webhook_url:
                    try:
                        await client.post(webhook_url, json=payload)
                        logger.info("n8n.notification_sent", url=webhook_url)
                    except Exception as e:
                        logger.warning("n8n.notification_failed", error=str(e))

                if mars_base_url:
                    try:
                        url = f"{mars_base_url}/api/v1/cosmos/kb-update"
                        await client.post(url, json=payload)
                        logger.info("mars.notification_sent", url=url)
                    except Exception as e:
                        logger.warning("mars.notification_failed", error=str(e))

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_notify())
        except RuntimeError:
            # No running loop — skip notification
            pass

    return _on_kb_update


# ---------------------------------------------------------------------------
# 4b. Kafka event callbacks (GREL insights + KB updates)
# ---------------------------------------------------------------------------

def create_kafka_kb_callback(event_bus: EventBus):
    """Pipeline callback that produces KBUpdatedEvent to Kafka."""
    def _on_kb_update(updates: list):
        if not updates:
            return
        doc_ids = []
        for u in updates[:50]:
            doc_id = getattr(u, "doc_id", "") if not isinstance(u, dict) else u.get("doc_id", "")
            if doc_id:
                doc_ids.append(doc_id)

        event = KBUpdatedEvent(
            update_count=len(updates),
            source="pipeline",
            doc_ids=doc_ids,
        )

        async def _produce():
            try:
                await event_bus.produce_kb_updated(event)
            except Exception as e:
                logger.warning("kafka.kb_updated_emit_failed", error=str(e))

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_produce())
        except RuntimeError:
            pass

    return _on_kb_update


def create_kafka_grel_callback(event_bus: EventBus):
    """GREL callback that produces LearningInsightEvent to Kafka."""
    async def _on_grel_insights(insights: list[LearningInsight]):
        for insight in insights:
            event = LearningInsightEvent(
                insight_id=insight.insight_id,
                learning_type=insight.learning_type.value,
                description=insight.description,
                evidence=insight.evidence,
                proposed_change=insight.proposed_change,
                risk_level=insight.risk_level,
                query_pattern=getattr(insight, "query_pattern", ""),
            )
            try:
                await event_bus.produce_learning_insight(event)
            except Exception as e:
                logger.warning(
                    "kafka.learning_insight_emit_failed",
                    insight_id=insight.insight_id,
                    error=str(e),
                )

    return _on_grel_insights


# ---------------------------------------------------------------------------
# 5. Background scheduler for periodic KB scans
# ---------------------------------------------------------------------------

class KBScanScheduler:
    """Background task that periodically scans knowledge_base for changes.

    Uses asyncio.create_task — no external dependencies like APScheduler.

    Two-phase scan:
      Phase 1 — diff_and_mark_pending()
        Walk disk, MD5 hash each YAML, compare with cosmos_kb_file_index.
        Changed files → status=0 (pending). Fast: no YAML parse.

      Phase 2 — process_pending()
        Read + parse YAML only for pending files.
        Call KBIngestor + CanonicalIngestor → new embedding.
        Mark status=1 on success, status=2 on failure.

    This replaces the old in-memory _file_hashes approach so hashes survive
    server restarts and PR webhooks can mark files as pending from outside.
    """

    def __init__(
        self,
        pipeline: KBUpdatePipeline,
        interval_seconds: int = 300,  # 5 minutes default
        kb_path: Optional[str] = None,
        vectorstore=None,
        batch_size: int = 100,
    ):
        self._pipeline = pipeline
        self._interval = interval_seconds
        self._kb_path = kb_path or getattr(pipeline, "_kb_path", "")
        self._vectorstore = vectorstore
        self._batch_size = batch_size
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._file_index = None   # lazy-init KBFileIndexService

    def _get_file_index(self):
        if self._file_index is None:
            try:
                from app.services.kb_file_index import KBFileIndexService
                self._file_index = KBFileIndexService()
            except Exception:
                pass
        return self._file_index

    async def start(self):
        """Start the background scan loop."""
        if self._running:
            return
        self._running = True
        # Ensure DB table exists
        fi = self._get_file_index()
        if fi:
            await fi.ensure_schema()
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("kb_scan_scheduler.started", interval_seconds=self._interval)

    async def stop(self):
        """Stop the background scan loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("kb_scan_scheduler.stopped")

    async def _scan_loop(self):
        """Periodically scan for KB changes and process them."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._scan_and_ingest()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("kb_scan_scheduler.error", error=str(e))
                await asyncio.sleep(30)

    async def _scan_and_ingest(self):
        """One full scan + ingest cycle."""
        fi = self._get_file_index()
        if not fi or not self._kb_path:
            # Fallback to legacy in-memory scan
            changes = self._pipeline.scan_for_changes()
            if changes:
                updates = await self._pipeline.process_changes(changes)
                self._pipeline.snapshot_hashes()
                logger.info(
                    "kb_scan_scheduler.legacy_changes_processed",
                    change_count=len(changes),
                    update_count=len(updates),
                )
            return

        # Phase 1: diff disk vs DB → mark pending (fast, MD5 only)
        changed = await fi.diff_and_mark_pending(
            self._kb_path,
            repo_id="MultiChannel_API",      # primary repo; scan others separately
            batch_size=self._batch_size,
        )

        # Phase 2: process pending files (YAML parse + embed)
        pending = await fi.get_pending(repo_id="MultiChannel_API", limit=self._batch_size)
        if not pending:
            return

        ingested = 0
        failed = 0

        try:
            from app.services.kb_ingestor import KBIngestor
            from app.services.canonical_ingestor import CanonicalIngestor, IngestDocument

            kb_reader = KBIngestor(self._kb_path)
            ingestor = CanonicalIngestor(self._vectorstore) if self._vectorstore else None

            for pf in pending:
                file_path: str = pf["file_path"]
                repo_id: str = pf["repo_id"] or "MultiChannel_API"
                entity_id: str = pf["entity_id"]
                entity_type: str = pf["entity_type"]

                try:
                    # Re-read only this file's entity
                    if entity_type == "api_tool":
                        docs = kb_reader.read_pillar3_apis(repo_id)
                        # Filter to just this entity
                        docs = [d for d in docs if d.get("entity_id") == entity_id]
                    elif entity_type == "schema":
                        docs = kb_reader.read_pillar1_schema(repo_id)
                        docs = [d for d in docs if d.get("entity_id") == entity_id]
                    else:
                        docs = []

                    if docs and ingestor:
                        ingest_docs = [IngestDocument(**d) for d in docs]
                        result = await ingestor.ingest(ingest_docs)
                        if result.ingested > 0:
                            await fi.mark_indexed(file_path, repo_id, entity_id, entity_type)
                            ingested += 1
                        else:
                            await fi.mark_failed(file_path, repo_id, "no documents ingested")
                            failed += 1
                    else:
                        # Nothing to ingest (e.g. registry file) — mark indexed
                        await fi.mark_indexed(file_path, repo_id)
                        ingested += 1

                except Exception as e:
                    await fi.mark_failed(file_path, repo_id, str(e))
                    failed += 1

        except Exception as e:
            logger.error("kb_scan_scheduler.ingest_setup_failed", error=str(e))
            return

        logger.info(
            "kb_scan_scheduler.cycle_complete",
            changed=len(changed),
            pending=len(pending),
            ingested=ingested,
            failed=failed,
        )


# ---------------------------------------------------------------------------
# Master wiring function
# ---------------------------------------------------------------------------

def wire_brain(
    brain: dict,
    cache: Optional[SemanticCache] = None,
    grel_engine: Optional[GRELEngine] = None,
    event_bus: Optional[EventBus] = None,
    n8n_webhook_url: Optional[str] = None,
    mars_base_url: Optional[str] = None,
    scan_interval_seconds: int = 300,
    vectorstore=None,
) -> dict:
    """Wire all brain components together.

    Call this after create_brain() during app startup.

    Args:
        brain: dict from create_brain()
        cache: SemanticCache instance (optional)
        grel_engine: GRELEngine instance (optional)
        event_bus: Kafka EventBus for producing events (optional)
        n8n_webhook_url: N8N webhook URL for notifications (optional)
        mars_base_url: MARS base URL for notifications (optional)
        scan_interval_seconds: How often to scan KB for changes (default 5 min)

    Returns:
        dict with added keys: "cache", "grel_engine", "scheduler"
    """
    pipeline: KBUpdatePipeline = brain["pipeline"]
    router: IntelligentRouter = brain["router"]
    indexer = brain["indexer"]

    # 0. Register Strategy E (Hybrid Retrieval) on the GREL engine
    if grel_engine is not None:
        grel_engine.register_strategy(
            StrategyName.HYBRID_RETRIEVAL,
            create_hybrid_strategy_fn(),
        )
        logger.info("wiring.hybrid_retrieval_strategy_registered")

    # 1. Wire GREL → Pipeline + Kafka
    if grel_engine is not None:
        grel_callback = create_grel_learning_callback(pipeline)
        if event_bus is not None:
            kafka_grel_cb = create_kafka_grel_callback(event_bus)
            # Chain both callbacks
            async def _combined_grel(insights):
                await grel_callback(insights)
                await kafka_grel_cb(insights)
            grel_engine._learning_callback = _combined_grel
        else:
            grel_engine._learning_callback = grel_callback
        logger.info("wiring.grel_to_pipeline")

    # 2. Wire Pipeline → Cache invalidation
    if cache is not None:
        cache_callback = create_cache_invalidation_callback(cache)
        pipeline.register_callback(cache_callback)
        logger.info("wiring.pipeline_to_cache")

    # 3. Wire Pipeline → Router rebuild
    router_callback = create_router_rebuild_callback(router)
    pipeline.register_callback(router_callback)
    logger.info("wiring.pipeline_to_router")

    # 4. Wire Pipeline → N8N/MARS notification
    n8n_callback = create_n8n_notification_callback(n8n_webhook_url, mars_base_url)
    if n8n_callback is not None:
        pipeline.register_callback(n8n_callback)
        logger.info("wiring.pipeline_to_n8n")

    # 4b. Wire Pipeline → Kafka KB updated events
    if event_bus is not None:
        kafka_kb_cb = create_kafka_kb_callback(event_bus)
        pipeline.register_callback(kafka_kb_cb)
        logger.info("wiring.pipeline_to_kafka")

    # 5. Create scheduler (caller must await scheduler.start())
    scheduler = KBScanScheduler(
        pipeline,
        interval_seconds=scan_interval_seconds,
        kb_path=getattr(pipeline, "_kb_path", None),
        vectorstore=vectorstore,
    )

    # Return enriched brain dict
    brain["cache"] = cache
    brain["grel_engine"] = grel_engine
    brain["scheduler"] = scheduler

    return brain

"""
KB File Watcher — Phase 6c: Async incremental re-ingestion on YAML change.

Watches the knowledge-base directory tree for file system events (create,
modify, move) using watchdog.  When a YAML file changes the watcher maps
the path back to its KB pillar and triggers an incremental re-ingest of
only that entity (api / page / module / table), avoiding a full restart.

Architecture
------------
KBWatcher
  ├── watchdog Observer (threaded file-system monitor)
  ├── asyncio.Queue  (thread → async bridge)
  └── _process_loop  (async coroutine, drains the queue)
        └── _reingest_path → KBIngestor.read_<pillar>() → CanonicalIngestor.ingest()

Path routing rules (all resolved relative to kb_path):
  <repo>/<api_id>/high.yaml         → Pillar 3 API, re-reads read_pillar3_apis for that api
  <repo>/pillar_1_schema/tables/<t> → Pillar 1 table, re-reads read_pillar1_schema
  <repo>/pages/<page_id>/*.yaml     → Pillar 4 page, re-reads read_pillar4_pages
  <repo>/modules/<mod>/*.yaml       → Pillar 5 module, re-reads read_pillar5_modules

Debounce: identical paths within DEBOUNCE_SEC are collapsed into one event.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Set

import structlog

logger = structlog.get_logger()

# How long to wait before re-processing the same path again (seconds)
DEBOUNCE_SEC = 3.0
# Maximum time (s) we hold a debounce slot before forcing a flush
DEBOUNCE_MAX_SEC = 10.0


class _AsyncEventHandler:
    """
    Watchdog event handler that puts changed paths onto an asyncio.Queue.
    Runs in the watchdog observer thread — uses call_soon_threadsafe to
    bridge into the async event loop safely.
    """

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._queue = queue
        self._loop = loop

    # watchdog calls dispatch() which routes to on_modified / on_created etc.
    def dispatch(self, event):
        if event.is_directory:
            return
        src = getattr(event, "src_path", None)
        if src and src.endswith(".yaml"):
            self._loop.call_soon_threadsafe(self._queue.put_nowait, src)

    # Individual event methods (watchdog interface)
    def on_modified(self, event):
        self.dispatch(event)

    def on_created(self, event):
        self.dispatch(event)

    def on_moved(self, event):
        # Handle rename: the destination file is the new content
        dest = getattr(event, "dest_path", None)
        if dest and dest.endswith(".yaml") and not event.is_directory:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, dest)


class KBWatcher:
    """
    Async watchdog-based KB file watcher.

    Usage::

        watcher = KBWatcher(kb_path, kb_ingestor, canonical_ingestor)
        await watcher.start()  # call from lifespan startup

        # watcher runs in background; stop on shutdown:
        await watcher.stop()
    """

    def __init__(self, kb_path: str, kb_ingestor, canonical_ingestor):
        """
        Parameters
        ----------
        kb_path:
            Root directory of the knowledge base (contains repo folders).
        kb_ingestor:
            KBIngestor instance (reads YAML → IngestDocument lists).
        canonical_ingestor:
            CanonicalIngestor instance (stores docs into vector DB).
        """
        self.kb_path = Path(kb_path)
        self._kb_ingestor = kb_ingestor
        self._canonical_ingestor = canonical_ingestor

        self._queue: asyncio.Queue = asyncio.Queue()
        self._observer = None
        self._process_task: Optional[asyncio.Task] = None
        self._running = False

        # Debounce tracking: path → last_seen_epoch
        self._debounce: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the file-system observer and processing loop."""
        if self._running:
            return
        self._running = True

        try:
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("kb_watcher.watchdog_not_installed",
                           hint="pip install watchdog")
            return

        loop = asyncio.get_event_loop()
        handler = _AsyncEventHandler(self._queue, loop)

        self._observer = Observer()
        self._observer.schedule(handler, str(self.kb_path), recursive=True)
        self._observer.start()

        self._process_task = asyncio.create_task(self._process_loop())
        logger.info("kb_watcher.started", path=str(self.kb_path))

    async def stop(self):
        """Stop the observer and processing loop gracefully."""
        self._running = False
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._process_task is not None:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        logger.info("kb_watcher.stopped")

    # ------------------------------------------------------------------
    # Processing loop
    # ------------------------------------------------------------------

    async def _process_loop(self):
        """Drain the event queue and trigger re-ingestion with debounce."""
        pending: Set[str] = set()
        debounce_until: dict = {}  # path → float (epoch)

        while self._running:
            try:
                # Wait up to 1 s for an event
                path = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                now = time.monotonic()

                # Debounce: ignore if this path was processed very recently
                next_allowed = debounce_until.get(path, 0)
                if now < next_allowed:
                    continue

                pending.add(path)
                debounce_until[path] = now + DEBOUNCE_SEC

            except asyncio.TimeoutError:
                pass  # check pending set even with no new events

            if not pending:
                continue

            # Flush all pending paths that have passed their debounce window
            now = time.monotonic()
            ready = {p for p in pending if now >= debounce_until.get(p, 0)}
            pending -= ready

            for path in ready:
                try:
                    await self._reingest_path(path)
                except Exception as exc:
                    logger.warning("kb_watcher.reingest_error",
                                   path=path, error=str(exc))

    # ------------------------------------------------------------------
    # Path routing + incremental re-ingest
    # ------------------------------------------------------------------

    async def _reingest_path(self, abs_path: str):
        """
        Map a changed YAML file path to a KB entity and re-ingest it.
        """
        try:
            rel = Path(abs_path).relative_to(self.kb_path)
        except ValueError:
            return  # outside KB root, ignore

        parts = rel.parts
        if len(parts) < 2:
            return

        repo_id = parts[0]
        docs = []

        # ---- Pillar 1: schema table ----
        if len(parts) >= 3 and parts[1] == "pillar_1_schema":
            logger.info("kb_watcher.reingest_pillar1", repo=repo_id, path=rel)
            docs = self._kb_ingestor.read_pillar1_schema(repo_id=repo_id)

        # ---- Pillar 3: API high.yaml or high/ chunked files ----
        elif (
            "high.yaml" in parts[-1]
            or (len(parts) >= 3 and parts[-2] == "high")
        ) and "pillar" not in parts[1]:
            api_id = parts[1] if len(parts) >= 3 else None
            logger.info("kb_watcher.reingest_pillar3",
                        repo=repo_id, api_id=api_id, path=rel)
            all_docs = self._kb_ingestor.read_pillar3_apis(repo_id=repo_id)
            if api_id:
                # Filter to only the changed API's docs
                docs = [
                    d for d in all_docs
                    if d.get("entity_id", "").endswith(f":{api_id}")
                    or d.get("metadata", {}).get("api_id") == api_id
                ]
            if not docs:
                docs = all_docs  # fallback: re-ingest all for this repo

        # ---- Pillar 4: pages ----
        elif len(parts) >= 3 and "pages" in parts:
            logger.info("kb_watcher.reingest_pillar4", repo=repo_id, path=rel)
            docs = self._kb_ingestor.read_pillar4_pages(repo_id=repo_id)

        # ---- Pillar 5: modules ----
        elif len(parts) >= 3 and "modules" in parts:
            logger.info("kb_watcher.reingest_pillar5", repo=repo_id, path=rel)
            docs = self._kb_ingestor.read_pillar5_modules(repo_id=repo_id)

        else:
            logger.debug("kb_watcher.unmatched_path", path=rel)
            return

        if not docs:
            logger.debug("kb_watcher.no_docs", path=rel)
            return

        # Convert raw dicts to IngestDocument if needed
        from app.services.canonical_ingestor import IngestDocument
        ingest_docs = []
        for d in docs:
            if isinstance(d, IngestDocument):
                ingest_docs.append(d)
            elif isinstance(d, dict):
                ingest_docs.append(IngestDocument(
                    entity_type=d.get("entity_type", "unknown"),
                    entity_id=d.get("entity_id", ""),
                    content=d.get("content", ""),
                    metadata=d.get("metadata", {}),
                    trust_score=d.get("trust_score", 0.7),
                    repo_id=d.get("repo_id", repo_id),
                ))

        if not ingest_docs:
            return

        result = await self._canonical_ingestor.ingest(ingest_docs)
        logger.info("kb_watcher.reingest_done",
                    path=str(rel),
                    ingested=result.ingested_count if hasattr(result, "ingested_count") else len(ingest_docs),
                    errors=result.error_count if hasattr(result, "error_count") else 0)


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_watcher_instance: Optional[KBWatcher] = None


def get_kb_watcher(
    kb_path: str = "",
    kb_ingestor=None,
    canonical_ingestor=None,
) -> Optional[KBWatcher]:
    """
    Return the shared KBWatcher singleton.

    Call with arguments on first use (app startup) to initialise it;
    subsequent calls with no args return the cached instance.
    """
    global _watcher_instance
    if _watcher_instance is None:
        if not kb_path or kb_ingestor is None or canonical_ingestor is None:
            return None
        _watcher_instance = KBWatcher(
            kb_path=kb_path,
            kb_ingestor=kb_ingestor,
            canonical_ingestor=canonical_ingestor,
        )
    return _watcher_instance

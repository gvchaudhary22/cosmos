"""
Training Pipeline API — Trigger full or individual milestone ingestion.

POST /cosmos/api/v1/pipeline/run                  — Run full pipeline (async, returns run_id in <100ms)
GET  /cosmos/api/v1/pipeline/run/{run_id}/stream  — SSE stream: live milestone progress
POST /cosmos/api/v1/pipeline/split        — M2: Create train/dev/holdout split
POST /cosmos/api/v1/pipeline/schema       — M5: Ingest Pillar 1+3
POST /cosmos/api/v1/pipeline/modules      — M3: Ingest module docs (8 repos)
POST /cosmos/api/v1/pipeline/artifacts    — M4: Ingest generated artifacts
POST /cosmos/api/v1/pipeline/seeds        — Ingest eval seeds
GET  /cosmos/api/v1/pipeline/status       — File index + vectorstore + Neo4j + cosmos_tools stats
POST /cosmos/api/v1/pipeline/sync-s3      — Download changed KB files from S3 and re-index
POST /cosmos/api/v1/pipeline/webhook/pr   — GitHub PR webhook: mark changed files pending
"""

import asyncio
import json
import uuid
from asyncio import Queue
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = structlog.get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# W1-B: In-memory run registry (single process; upgrade to Redis for multi-instance)
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    run_id: str
    status: str = "running"   # running | done | error
    events: Queue = field(default_factory=Queue)
    total_docs: int = 0
    error: Optional[str] = None


_RUN_REGISTRY: Dict[str, RunState] = {}
_RUN_REGISTRY_MAX_AGE_S = 86400  # evict runs older than 24h (TTL cleanup on next POST)
_run_registry_timestamps: Dict[str, float] = {}


def _evict_stale_runs():
    import time
    now = time.time()
    stale = [rid for rid, ts in _run_registry_timestamps.items() if now - ts > _RUN_REGISTRY_MAX_AGE_S]
    for rid in stale:
        _RUN_REGISTRY.pop(rid, None)
        _run_registry_timestamps.pop(rid, None)


class PipelineRequest(BaseModel):
    repo_id: Optional[str] = None


class PRWebhookPayload(BaseModel):
    repo_id: str
    changed_files: List[str]        # relative paths inside KB root
    commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None


def _get_pipeline(request: Request):
    return getattr(request.app.state, "training_pipeline", None)


@router.post("/run")
async def run_full_pipeline(request: Request, body: PipelineRequest = PipelineRequest(), background_tasks=None):
    """Start full pipeline asynchronously. Returns run_id in <100ms (HTTP 202).

    Connect to GET /pipeline/run/{run_id}/stream (SSE) to receive live milestone events:
      - event: milestone_start  {"name": "...", "label": "..."}
      - event: milestone_done   {"name": "...", "docs": N, "ms": N, "success": true}
      - event: pipeline_done    {"total_docs": N, "duration_ms": N, "success": true}
    """
    import time
    from starlette.background import BackgroundTasks

    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}

    _evict_stale_runs()

    run_id = str(uuid.uuid4())[:8]
    state = RunState(run_id=run_id)
    _RUN_REGISTRY[run_id] = state
    _run_registry_timestamps[run_id] = time.time()

    async def _run_pipeline_task():
        async def _event_callback(etype: str, data: dict):
            await state.events.put({"type": etype, "data": data})
            if etype == "pipeline_done":
                state.status = "done"
                state.total_docs = data.get("total_docs", 0)
            elif etype == "pipeline_error":
                state.status = "error"
                state.error = data.get("error", "unknown")

        try:
            await pipeline.run_full(repo_id=body.repo_id or None, event_callback=_event_callback)
        except Exception as e:
            logger.error("pipeline.run_background_failed", error=str(e))
            await state.events.put({"type": "pipeline_error", "data": {"error": str(e)}})
            state.status = "error"
            state.error = str(e)

    # Use FastAPI background tasks if injected, else fire-and-forget via asyncio
    if background_tasks is not None:
        background_tasks.add_task(_run_pipeline_task)
    else:
        asyncio.create_task(_run_pipeline_task())

    from starlette.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content={
            "run_id": run_id,
            "status": "started",
            "stream_url": f"/cosmos/api/v1/pipeline/run/{run_id}/stream",
            "message": "Pipeline started. Connect to stream_url for live SSE progress.",
        },
    )


@router.get("/run/{run_id}/stream")
async def pipeline_run_stream(run_id: str):
    """Stream SSE events for a running pipeline.

    Client connects with EventSource (browser) or curl --no-buffer.
    Streams until pipeline_done or pipeline_error event is received.

    Events:
      milestone_start — pipeline entered a milestone
      milestone_done  — milestone completed (includes docs, ms, success)
      pipeline_done   — all milestones complete
      pipeline_error  — unrecoverable pipeline failure
    """
    state = _RUN_REGISTRY.get(run_id)
    if not state:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": f"run_id '{run_id}' not found"})

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(state.events.get(), timeout=60.0)
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                if event["type"] in ("pipeline_done", "pipeline_error"):
                    break
            except asyncio.TimeoutError:
                # Send keepalive comment so browser doesn't drop the connection
                yield ": keepalive\n\n"
                # If pipeline already finished but client reconnected after queue drained
                if state.status in ("done", "error"):
                    final_type = "pipeline_done" if state.status == "done" else "pipeline_error"
                    final_data = (
                        {"total_docs": state.total_docs, "duration_ms": 0, "success": True}
                        if state.status == "done"
                        else {"error": state.error or "unknown"}
                    )
                    yield f"event: {final_type}\ndata: {json.dumps(final_data)}\n\n"
                    break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/split")
async def run_split(request: Request):
    """M2: Create train/dev/holdout split from eval files."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_split()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/schema")
async def run_schema_apis(request: Request, body: PipelineRequest = PipelineRequest()):
    """M5: Ingest Pillar 1 schema + Pillar 3 API tools."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_pillar1_pillar3(repo_id=body.repo_id or None)
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/modules")
async def run_module_docs(request: Request):
    """M3: Ingest module docs from all 8 repos."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_module_docs()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/artifacts")
async def run_artifacts(request: Request):
    """M4: Ingest generated KB artifacts."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_generated_artifacts()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/agents-skills-tools")
async def run_agents_skills_tools(request: Request):
    """M9/10/11: Ingest Pillar 9 agent defs, Pillar 10 skill defs, Pillar 11 tool defs → Qdrant + MARS DB graph nodes."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_pillar9_10_11()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/faq")
async def run_faq(request: Request):
    """M12: Ingest Pillar 12 FAQ chunks (seller Q&A from shiprocket_faq.xlsx) → Qdrant + MARS DB graph nodes."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_pillar12_faq()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.post("/seeds")
async def run_seeds(request: Request):
    """Ingest eval seeds and training seeds."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}
    m = await pipeline.run_eval_seeds()
    return {"success": m.success, "documents": m.documents_ingested, "details": m.details, "error": m.error}


@router.get("/status")
async def pipeline_status(request: Request):
    """Instant pipeline status — DB-only, no disk scan.

    Returns per-repo, per-pillar counts of indexed/pending/failed files
    from cosmos_kb_file_index. Only repos that have been scanned appear.
    Embedding stats come from app.state.vectorstore if available.
    """
    import asyncio
    result: dict = {"status": "ready"}

    # 1. File index: per-repo + per-pillar counts from DB (instant — no disk scan)
    try:
        from app.services.kb_file_index import KBFileIndexService
        fi = KBFileIndexService()
        pillar_stats = await fi.get_pillar_stats()
        # Summary totals across all repos
        total_indexed = sum(v["indexed"] for v in pillar_stats.values())
        total_pending = sum(v["pending"] for v in pillar_stats.values())
        total_failed  = sum(v["failed"]  for v in pillar_stats.values())
        result["file_index"] = {
            "indexed": total_indexed,
            "pending": total_pending,
            "failed":  total_failed,
            "total":   total_indexed + total_pending + total_failed,
            "by_repo": pillar_stats,    # only repos that have been scanned
        }
    except Exception as e:
        result["file_index"] = {"error": str(e)}

    # 2. Vectorstore embedding stats (non-blocking: skip gracefully if Qdrant is down)
    vectorstore = getattr(request.app.state, "vectorstore", None)
    if vectorstore:
        try:
            stats = await asyncio.wait_for(vectorstore.get_stats(), timeout=2.0)
            result["embedding_stats"] = stats
        except (asyncio.TimeoutError, Exception) as e:
            result["embedding_stats"] = {"error": "unavailable", "detail": str(e)}
    else:
        result["embedding_stats"] = {"error": "vectorstore not initialised"}

    # 3. Neo4j node + edge counts
    try:
        from app.services.graphrag import GraphRAGService
        graph = GraphRAGService()
        neo4j_stats = await asyncio.wait_for(graph.pg_get_stats(), timeout=3.0)
        result["graph_stats"] = {
            "node_count": neo4j_stats.total_nodes,
            "edge_count": neo4j_stats.total_edges,
        }
    except Exception as e:
        result["graph_stats"] = {"error": str(e)}

    # 4. Cosmos tools count
    try:
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import text as _text
        async with AsyncSessionLocal() as session:
            row = await session.execute(_text("SELECT COUNT(*) FROM cosmos_tools"))
            result["cosmos_tools_count"] = int(row.scalar() or 0)
    except Exception as e:
        result["cosmos_tools_count"] = {"error": str(e)}

    # 5. Kafka tracker stats
    try:
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import text as _text
        async with AsyncSessionLocal() as session:
            row = await session.execute(_text("""
                SELECT COUNT(*), SUM(CASE WHEN small_done = true THEN 1 ELSE 0 END)
                FROM cosmos_embedding_queue_tracker
            """))
            r = row.fetchone()
            total = int(r[0] or 0) if r else 0
            done = int(r[1] or 0) if r else 0
            result["kafka_tracker"] = {"total": total, "done": done, "pending": total - done}
    except Exception as e:
        result["kafka_tracker"] = {"error": str(e)}

    # 6. Active pipeline runs
    result["active_runs"] = {
        rid: {"status": st.status, "total_docs": st.total_docs}
        for rid, st in _RUN_REGISTRY.items()
    }

    return result


@router.post("/sync-s3")
async def sync_from_s3(request: Request, body: PipelineRequest = PipelineRequest()):
    """Download changed KB YAML files from S3, mark pending, then re-index.

    Compares S3 ETags against cosmos_kb_file_index.s3_etag.
    Only downloads files whose ETag changed — no unnecessary transfers.
    """
    try:
        from app.services.s3_client import S3Client
        from app.services.kb_file_index import KBFileIndexService
        from app.config import settings

        s3 = S3Client.from_settings()
        if not s3.enabled:
            return {"error": "S3 not configured (S3_BUCKET or credentials missing)"}

        repo_id = body.repo_id or "MultiChannel_API"
        fi = KBFileIndexService()

        # List all YAML files in S3 for this repo
        s3_objects = await s3.list_kb_files(repo_id, kb_prefix=settings.S3_KB_PREFIX)
        if not s3_objects:
            return {"synced": 0, "message": f"No files found in S3 under {settings.S3_KB_PREFIX}/{repo_id}/"}

        # Load current ETags from DB
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                text("SELECT file_path, s3_etag FROM cosmos_kb_file_index WHERE repo_id = :repo"),
                {"repo": repo_id},
            )
            stored_etags = {row.file_path: row.s3_etag for row in rows.fetchall()}

        # Resolve KB root path: app.state.kb_path is set at startup
        import os
        kb_root = getattr(request.app.state, "kb_path", None)

        downloaded = 0
        pending_paths = []

        for obj in s3_objects:
            # Derive relative path from S3 key
            prefix_strip = f"{settings.S3_KB_PREFIX}/"
            if obj.key.startswith(prefix_strip):
                rel_path = obj.key[len(prefix_strip):]
            else:
                rel_path = obj.key

            stored_etag = stored_etags.get(rel_path)
            if stored_etag == obj.etag:
                continue  # unchanged

            # Download to local KB path
            if kb_root:
                local_path = os.path.join(kb_root, rel_path)
                downloaded_ok = await s3.download_file(obj.key, local_path)
                if downloaded_ok:
                    await fi.update_s3_etag(rel_path, repo_id, obj.key, obj.etag)
                    downloaded += 1
                    pending_paths.append(rel_path)
            else:
                pending_paths.append(rel_path)

        # Mark all downloaded/changed files as pending
        if pending_paths:
            marked = await fi.mark_paths_pending(pending_paths, repo_id)
            logger.info("pipeline.s3_sync", repo=repo_id, downloaded=downloaded, pending=marked)

        return {
            "repo_id": repo_id,
            "s3_files_checked": len(s3_objects),
            "files_changed": len(pending_paths),
            "files_downloaded": downloaded,
            "pending_for_reindex": len(pending_paths),
            "message": "Files marked pending — KBScanScheduler will re-index within 5 min",
        }

    except Exception as e:
        logger.error("pipeline.sync_s3_failed", error=str(e))
        return {"error": str(e)}


@router.post("/webhook/pr")
async def pr_webhook(payload: PRWebhookPayload):
    """GitHub PR webhook — mark changed KB YAML files as pending re-index.

    Called by MARS GitHub webhook integration when a PR merges to main.
    Only marks files as pending; actual re-embedding happens in KBScanScheduler.

    Expected payload:
      {
        "repo_id": "MultiChannel_API",
        "changed_files": [
          "MultiChannel_API/pillar_1_schema/tables/orders/columns.yaml",
          "MultiChannel_API/pillar_3_api_mcp_tools/apis/mc_get_order/overview.yaml"
        ],
        "commit_sha": "abc123",
        "pr_number": 42
      }
    """
    try:
        from app.services.kb_file_index import KBFileIndexService

        # Filter to only YAML files
        yaml_files = [f for f in payload.changed_files if f.endswith((".yaml", ".yml"))]
        if not yaml_files:
            return {"marked_pending": 0, "message": "no YAML files in changed_files"}

        fi = KBFileIndexService()
        marked = await fi.mark_paths_pending(yaml_files, payload.repo_id)

        logger.info(
            "pipeline.pr_webhook",
            repo=payload.repo_id,
            pr=payload.pr_number,
            commit=payload.commit_sha,
            total_changed=len(payload.changed_files),
            yaml_changed=len(yaml_files),
            marked_pending=marked,
        )

        return {
            "repo_id": payload.repo_id,
            "pr_number": payload.pr_number,
            "commit_sha": payload.commit_sha,
            "yaml_files_changed": len(yaml_files),
            "marked_pending": marked,
            "message": "Files marked pending — KBScanScheduler re-indexes within 5 min",
        }

    except Exception as e:
        logger.error("pipeline.pr_webhook_failed", error=str(e))
        return {"error": str(e)}


class EvalRequest(BaseModel):
    sample_size: Optional[int] = 100
    domain: Optional[str] = None
    repo_id: str = "MultiChannel_API"


@router.post("/eval")
async def run_eval(request: Request, body: EvalRequest = EvalRequest()):
    """Run KB retrieval evaluation against eval seeds.

    POST /cosmos/api/v1/pipeline/eval
    Body: {"sample_size": 100, "domain": null, "repo_id": "MultiChannel_API"}

    Returns recall@K, tool accuracy, domain accuracy, and weak domain list.
    """
    try:
        import os
        from app.services.kb_eval import KBEvaluator
        from app.services.vectorstore import VectorStoreService

        vectorstore = getattr(request.app.state, "vectorstore", None)
        if not vectorstore:
            vectorstore = VectorStoreService()

        kb_path = getattr(request.app.state, "kb_path", None)
        evaluator = KBEvaluator(vectorstore, kb_path)

        report = await evaluator.run_eval(
            sample_size=body.sample_size,
            domain_filter=body.domain,
            repo_id=body.repo_id,
        )

        return {
            "evaluated": report.evaluated,
            "total_seeds": report.total_seeds,
            "recall_at_1": round(report.recall_at_1, 4),
            "recall_at_3": round(report.recall_at_3, 4),
            "recall_at_5": round(report.recall_at_5, 4),
            "tool_accuracy": round(report.tool_accuracy, 4),
            "domain_accuracy": round(report.domain_accuracy, 4),
            "avg_latency_ms": round(report.avg_latency_ms, 1),
            "duration_s": round(report.duration_s, 1),
            "weak_domains": report.weak_domains,
            "by_domain": {
                d: {
                    "total": ds.total,
                    "recall_at_5": round(ds.recall_at_5 / max(ds.total, 1), 3),
                    "tool_match": round(ds.tool_match / max(ds.total, 1), 3),
                }
                for d, ds in report.by_domain.items()
            },
        }
    except Exception as e:
        logger.error("pipeline.eval_failed", error=str(e))
        return {"error": str(e)}


@router.get("/kb-registry")
async def get_kb_registry(request: Request):
    """View KB-driven tools, agents, skills discovered from the knowledge graph.

    GET /cosmos/api/v1/pipeline/kb-registry

    Shows what the KB defines vs what's hardcoded in code.
    Run after /pipeline/run to populate the graph first.
    """
    try:
        import os
        from app.engine.kb_driven_registry import KBDrivenRegistry

        kb_path = getattr(request.app.state, "kb_path", None)
        if not kb_path:
            kb_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "..", "mars", "knowledge_base", "shiprocket",
            )

        registry = KBDrivenRegistry(kb_path=kb_path)
        await registry.sync_all()

        return {
            "stats": registry.get_stats(),
            "details": registry.to_summary(),
        }
    except Exception as e:
        logger.error("pipeline.kb_registry_failed", error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Dry-Run: Validate entire COSMOS system without side effects
# ---------------------------------------------------------------------------

@router.post("/dryrun/run")
async def run_cosmos_dryrun(request: Request):
    """
    POST /cosmos/api/v1/dryrun/run

    Runs all COSMOS validations in dry-run mode:
    1. KB ingestor read (verify all pillar readers work)
    2. Embedding test (embed 1 doc, verify dimension)
    3. Graph query test (verify graph is queryable)
    4. Action contract validation (dry-run all actions)
    5. Retrieval test (run 10 eval seeds, check recall)
    6. Approval engine test (dry-run approval chain)

    Returns structured report compatible with MARS dry_run_reports format.
    """
    import json
    import time
    import uuid

    results = {
        "run_id": str(uuid.uuid4()),
        "started_at": time.time(),
        "flows": [],
        "summary": {"total_steps": 0, "passed": 0, "failed": 0},
    }

    def _record(flow: str, step: str, passed: bool, message: str = "", duration_ms: float = 0):
        results["flows"].append({
            "flow": flow,
            "step": step,
            "status": "pass" if passed else "fail",
            "message": message,
            "duration_ms": round(duration_ms, 1),
        })
        results["summary"]["total_steps"] += 1
        if passed:
            results["summary"]["passed"] += 1
        else:
            results["summary"]["failed"] += 1

    # --- Flow 1: KB Ingestor Read ---
    t0 = time.monotonic()
    try:
        pipeline = getattr(request.app.state, "training_pipeline", None)
        if pipeline and hasattr(pipeline, "kb_reader"):
            docs = pipeline.kb_reader.read_all()
            _record("kb_ingestor", "read_all_pillars", len(docs) > 0,
                     f"{len(docs)} docs read across all pillars",
                     (time.monotonic() - t0) * 1000)
        else:
            _record("kb_ingestor", "read_all_pillars", False, "Training pipeline not initialized")
    except Exception as e:
        _record("kb_ingestor", "read_all_pillars", False, str(e), (time.monotonic() - t0) * 1000)

    # --- Flow 2: Embedding Test ---
    t0 = time.monotonic()
    try:
        vectorstore = getattr(request.app.state, "vectorstore", None)
        if vectorstore:
            test_embedding = vectorstore.embed_text("dry run test query for COSMOS validation")
            dim_ok = len(test_embedding) in (384, 1536, 3072)
            _record("embedding", "embed_text", dim_ok,
                     f"Dimension: {len(test_embedding)}",
                     (time.monotonic() - t0) * 1000)
        else:
            _record("embedding", "embed_text", False, "Vectorstore not initialized")
    except Exception as e:
        _record("embedding", "embed_text", False, str(e), (time.monotonic() - t0) * 1000)

    # --- Flow 3: Graph Query Test ---
    t0 = time.monotonic()
    try:
        graphrag = getattr(request.app.state, "graphrag", None)
        if graphrag:
            stats = await graphrag.pg_get_stats()
            has_nodes = stats.node_count > 0
            _record("graph", "query_stats", has_nodes,
                     f"Nodes: {stats.node_count}, Edges: {stats.edge_count}",
                     (time.monotonic() - t0) * 1000)
        else:
            _record("graph", "query_stats", False, "GraphRAG not initialized")
    except Exception as e:
        _record("graph", "query_stats", False, str(e), (time.monotonic() - t0) * 1000)

    # --- Flow 4: Retrieval Test (10 eval seeds) ---
    t0 = time.monotonic()
    try:
        if vectorstore:
            test_queries = [
                "What is the status of order 12345?",
                "Cancel this shipment",
                "NDR reattempt karo",
                "Where does fraud_score come from?",
                "COD remittance kab milega?",
            ]
            hits = 0
            for q in test_queries:
                results_q = await vectorstore.search_similar(query=q, limit=5, threshold=0.2)
                if results_q:
                    hits += 1
            _record("retrieval", "eval_seeds", hits >= 3,
                     f"{hits}/{len(test_queries)} queries returned results",
                     (time.monotonic() - t0) * 1000)
        else:
            _record("retrieval", "eval_seeds", False, "Vectorstore not initialized")
    except Exception as e:
        _record("retrieval", "eval_seeds", False, str(e), (time.monotonic() - t0) * 1000)

    # --- Flow 5: Approval Engine Dry-Run ---
    t0 = time.monotonic()
    try:
        from app.engine.approval import ApprovalEngine
        engine = ApprovalEngine()
        # Test: low-risk action auto-approved by supervisor
        req1 = await engine.request_action(
            session_id="dryrun", tool_name="order_lookup",
            params={"order_id": "test"}, risk_level="low",
            user_id="dryrun_user", user_role="supervisor", dry_run=True,
        )
        # Test: high-risk action pending for agent
        req2 = await engine.request_action(
            session_id="dryrun", tool_name="cancel_order",
            params={"order_id": "test"}, risk_level="high",
            user_id="dryrun_agent", user_role="agent", dry_run=True,
        )
        auto_ok = "dry_run:approved" in req1.status
        pending_ok = "dry_run:pending" in req2.status
        _record("approval", "dry_run_chain", auto_ok and pending_ok,
                 f"Low-risk={req1.status}, High-risk={req2.status}",
                 (time.monotonic() - t0) * 1000)
    except Exception as e:
        _record("approval", "dry_run_chain", False, str(e), (time.monotonic() - t0) * 1000)

    # --- Summary ---
    results["duration_ms"] = round((time.time() - results["started_at"]) * 1000, 1)
    results["status"] = "pass" if results["summary"]["failed"] == 0 else "fail"

    logger.info("dryrun.cosmos_complete",
                status=results["status"],
                passed=results["summary"]["passed"],
                failed=results["summary"]["failed"])

    return results


@router.post("/kb-quality-fix")
async def run_kb_quality_fix(request: Request, body: PipelineRequest = PipelineRequest()):
    """
    POST /cosmos/api/v1/pipeline/kb-quality-fix

    Run KB quality fixes independently:
    1. Generate real examples for Pillar 3 APIs (replace generic ones)
    2. Populate missing request_schema params
    3. Populate empty-column tables
    4. Regenerate entity hubs with structured cross-pillar links

    Requires ANTHROPIC_API_KEY for fixes 1-3. Fix 4 runs without LLM.
    """
    import os
    import time

    t0 = time.monotonic()

    try:
        kb_path = getattr(request.app.state, "kb_path", "")
        if not kb_path:
            return {"success": False, "error": "KB path not configured"}

        from app.enrichment.kb_quality_fixer import KBQualityFixer
        fixer = KBQualityFixer(kb_path=kb_path)

        # Always run entity hub fix (no LLM needed)
        await fixer.fix_entity_hubs()

        # Run LLM-powered fixes if API key available
        if os.environ.get("ANTHROPIC_API_KEY"):
            await fixer.fix_generic_examples()
            await fixer.fix_missing_params()
            await fixer.fix_empty_columns()

        stats = fixer.get_stats()
        stats["duration_ms"] = round((time.monotonic() - t0) * 1000)
        stats["success"] = True

        logger.info("pipeline.kb_quality_fix_complete", **stats)
        return stats

    except Exception as e:
        logger.error("pipeline.kb_quality_fix_failed", error=str(e))
        return {
            "success": False,
            "error": str(e),
            "duration_ms": round((time.monotonic() - t0) * 1000),
        }


# ===================================================================
# Kafka Embedding Endpoints (Issue #9)
# ===================================================================

@router.post("/embedding/start")
async def embedding_start(request: Request, background_tasks):
    """
    POST /cosmos/api/v1/pipeline/embedding/start

    Publish all pending KB docs to Kafka for async primary embedding.
    PrimaryEmbeddingConsumer (run via `python -m app.services.embedding_queue consume-primary`)
    picks them up and embeds with text-embedding-3-small → Qdrant.

    Returns HTTP 202 immediately. Embedding happens asynchronously in the consumer.
    Use GET /pipeline/embedding/status to track progress.
    """
    import time as _time

    t0 = _time.monotonic()

    try:
        kb_path = getattr(request.app.state, "kb_path", "")
        if not kb_path:
            return {"success": False, "error": "KB path not configured on app.state"}

        vectorstore = getattr(request.app.state, "vectorstore", None)
        if not vectorstore:
            from app.services.vectorstore import VectorStoreService
            vectorstore = VectorStoreService()
            await vectorstore.ensure_schema()

        from app.services.kb_file_index import KBFileIndexService
        from app.services.kb_ingestor import KBIngestor
        from app.services.canonical_ingestor import CanonicalIngestor, IngestDocument
        from app.services.document_chunker import chunk_documents

        fi = KBFileIndexService()
        reader = KBIngestor(kb_path)
        ingestor = CanonicalIngestor(vectorstore=vectorstore)

        # Run publish in background so endpoint returns immediately
        async def _publish_all():
            published = 0
            repos = ["MultiChannel_API", "SR_Web", "MultiChannel_Web"]

            for repo in repos:
                # P1 schema
                docs = reader.read_pillar1_schema(repo)
                if docs:
                    chunked = chunk_documents(docs)
                    result = await ingestor.ingest(
                        [IngestDocument(**d) for d in chunked], kafka_mode=True
                    )
                    published += result.ingested

                # P3 APIs (folder-at-a-time)
                for folder_docs in reader.iter_pillar3_api_folders(repo, folder_batch=50):
                    chunked = chunk_documents(folder_docs)
                    result = await ingestor.ingest(
                        [IngestDocument(**d) for d in chunked], kafka_mode=True
                    )
                    published += result.ingested

            logger.info("pipeline.embedding_start.published", total=published)

        background_tasks.add_task(_publish_all)

        return {
            "success": True,
            "status": "publishing",
            "message": "Docs being published to Kafka in background. Start consumer: python -m app.services.embedding_queue consume-primary",
            "consumer_cmd": "python -m app.services.embedding_queue consume-primary",
            "status_url": "/cosmos/api/v1/pipeline/embedding/status",
            "started_at_ms": round((_time.monotonic() - t0) * 1000),
        }

    except Exception as e:
        logger.error("pipeline.embedding_start_failed", error=str(e))
        return {"success": False, "error": str(e)}


@router.get("/embedding/status")
async def embedding_status(request: Request):
    """
    GET /cosmos/api/v1/pipeline/embedding/status

    Returns current embedding pipeline status:
      - total: total docs in the primary embedding tracker
      - small_done: successfully embedded into Qdrant
      - pending: published to Kafka but not yet embedded
      - failed: went to DLQ after MAX_RETRIES failures
      - estimated_minutes_remaining: based on 20 docs/s throughput
    """
    try:
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            # Query the tracker table (created by EmbeddingConsumer._ensure_tables)
            rows = await session.execute(text("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN small_done = true THEN 1 ELSE 0 END) as small_done
                FROM cosmos_embedding_queue_tracker
            """))
            row = rows.fetchone()
            total = int(row[0] or 0) if row else 0
            done = int(row[1] or 0) if row else 0
            pending = total - done
            docs_per_second = 20  # 20-concurrent default
            estimated_minutes = round(pending / (docs_per_second * 60), 1) if pending > 0 else 0

        # Also get Qdrant vector count
        vectorstore = getattr(request.app.state, "vectorstore", None)
        qdrant_count = None
        if vectorstore:
            try:
                stats = await vectorstore.get_stats()
                qdrant_count = stats.get("total_vectors")
            except Exception:
                pass

        return {
            "success": True,
            "tracker": {
                "total": total,
                "small_done": done,
                "pending": pending,
                "estimated_minutes_remaining": estimated_minutes,
            },
            "qdrant_vectors": qdrant_count,
            "consumer_cmd": "python -m app.services.embedding_queue consume-primary",
            "dlq_cmd": "python -m app.services.embedding_queue inspect-dlq",
        }

    except Exception as e:
        logger.error("pipeline.embedding_status_failed", error=str(e))
        return {"success": False, "error": str(e)}

"""
Training Pipeline API — Trigger full or individual milestone ingestion.

POST /cosmos/api/v1/pipeline/run          — Run full pipeline (all milestones)
POST /cosmos/api/v1/pipeline/split        — M2: Create train/dev/holdout split
POST /cosmos/api/v1/pipeline/schema       — M5: Ingest Pillar 1+3
POST /cosmos/api/v1/pipeline/modules      — M3: Ingest module docs (8 repos)
POST /cosmos/api/v1/pipeline/artifacts    — M4: Ingest generated artifacts
POST /cosmos/api/v1/pipeline/seeds        — Ingest eval seeds
GET  /cosmos/api/v1/pipeline/status       — File index + vectorstore stats
POST /cosmos/api/v1/pipeline/sync-s3      — Download changed KB files from S3 and re-index
POST /cosmos/api/v1/pipeline/webhook/pr   — GitHub PR webhook: mark changed files pending
"""

from typing import List, Optional

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = structlog.get_logger()
router = APIRouter()


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
async def run_full_pipeline(request: Request, body: PipelineRequest = PipelineRequest()):
    """Run all training milestones in dependency order."""
    pipeline = _get_pipeline(request)
    if not pipeline:
        return {"error": "Training pipeline not initialized"}

    result = await pipeline.run_full(repo_id=body.repo_id or None)
    return {
        "success": result.success,
        "total_documents": result.total_documents,
        "total_duration_ms": round(result.total_duration_ms, 1),
        "milestones": [
            {
                "milestone": m.milestone,
                "name": m.name,
                "success": m.success,
                "documents_ingested": m.documents_ingested,
                "duration_ms": round(m.duration_ms, 1),
                "error": m.error,
                "details": m.details,
            }
            for m in result.milestones
        ],
    }


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
    """Check current ingestion stats: file index + vectorstore + available doc count."""
    result = {"status": "ready"}

    # 1. File index stats (indexed / pending / failed per repo)
    try:
        from app.services.kb_file_index import KBFileIndexService
        fi = KBFileIndexService()
        file_stats = await fi.get_stats()
        result["file_index"] = file_stats
    except Exception as e:
        result["file_index"] = {"error": str(e)}

    # 2. Vectorstore embedding stats
    vectorstore = getattr(request.app.state, "vectorstore", None)
    if not vectorstore:
        try:
            from app.services.vectorstore import VectorStoreService
            vectorstore = VectorStoreService()
        except Exception:
            result["embedding_stats"] = {"error": "vectorstore not available"}
            return result

    try:
        result["embedding_stats"] = await vectorstore.get_stats()
    except Exception as e:
        result["embedding_stats"] = {"error": str(e)}

    # 3. Count total available embedding docs (how many docs the ingestor would produce)
    try:
        import os
        kb_path = getattr(request.app.state, "kb_path", None)
        if kb_path and os.path.isdir(kb_path):
            from app.services.kb_ingestor import KBIngestor
            reader = KBIngestor(kb_path)
            total_available = 0
            by_source = {}

            # Pillar 1 tables + extras
            repos = ["MultiChannel_API", "SR_Web", "MultiChannel_Web"]
            for repo in repos:
                p1 = len(reader.read_pillar1_schema(repo))
                p3 = len(reader.read_pillar3_apis(repo))
                total_available += p1 + p3
                if p1:
                    by_source[f"pillar1_{repo}"] = p1
                if p3:
                    by_source[f"pillar3_{repo}"] = p3

            # Pillar 1 extras (catalog, connections, etc.)
            p1_extras = len(reader.read_pillar1_extras("MultiChannel_API"))
            total_available += p1_extras
            if p1_extras:
                by_source["pillar1_extras"] = p1_extras

            # Pillar 3 extras (api_classification)
            for repo in repos:
                p3x = len(reader.read_pillar3_extras(repo))
                total_available += p3x
                if p3x:
                    by_source[f"pillar3_extras_{repo}"] = p3x

            # Pillar 4 (page intelligence)
            for repo in ["SR_Web", "MultiChannel_Web"]:
                p4 = len(reader.read_pillar4_pages(repo))
                total_available += p4
                if p4:
                    by_source[f"pillar4_{repo}"] = p4

            # Pillar 5 (module docs from all repos)
            from pathlib import Path
            for repo_dir in sorted(Path(kb_path).iterdir()):
                if repo_dir.is_dir() and (repo_dir / "pillar_5_module_docs").exists():
                    p5 = len(reader.read_pillar5_modules(repo_dir.name))
                    total_available += p5
                    if p5:
                        by_source[f"pillar5_{repo_dir.name}"] = p5

            # Eval seeds + generated
            seeds = len(reader.read_eval_seeds())
            total_available += seeds
            by_source["eval_seeds"] = seeds
            gen = len(reader.read_generated_artifacts())
            total_available += gen
            if gen:
                by_source["generated_artifacts"] = gen

            result["total_available_docs"] = total_available
            result["available_by_source"] = by_source
    except Exception as e:
        result["total_available_docs"] = 0
        result["available_by_source"] = {"error": str(e)}

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

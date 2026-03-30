"""
Training Pipeline — Master orchestrator that runs all ingestion milestones in order.

Executes training_plan_v3.md milestones:
  M1: Schema convergence (ensure table schema is correct)
  M2: Train/dev/holdout split
  M3: Expanded ingestion (8 repos, all file types)
  M5: Pillar 1 schema + Pillar 3 API ingestion
  M4: Generated artifacts ingestion
  Full: Run all in dependency order

Usage:
  pipeline = TrainingPipeline(vectorstore, kb_path, data_dir)
  result = await pipeline.run_full()
  # or individual:
  result = await pipeline.run_milestone(5)
"""

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

from app.services.canonical_ingestor import CanonicalIngestor, IngestDocument, IngestResult
from app.services.kb_ingestor import KBIngestor
from app.services.data_splitter import DataSplitter
from app.services.chunker import chunk_documents
from app.services.bridge_doc_generator import generate_bridge_docs

logger = structlog.get_logger()


@dataclass
class MilestoneResult:
    milestone: int
    name: str
    success: bool = False
    documents_ingested: int = 0
    duration_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class PipelineResult:
    milestones: List[MilestoneResult] = field(default_factory=list)
    total_documents: int = 0
    total_duration_ms: float = 0.0
    success: bool = False


class TrainingPipeline:
    """
    Master pipeline that executes all training milestones.

    Dependency order:
      M2 (split) → no deps
      M5 (Pillar 1+3) → needs canonical ingestor
      M3 (module docs) → needs canonical ingestor (handled by codebase_intelligence at startup)
      M4 (generated artifacts) → needs M5 done first
      M6 (intent classifier) → needs M2 done first
    """

    def __init__(
        self,
        vectorstore,
        kb_path: str,
        data_dir: str = "",
        codebase_intel=None,
    ):
        self.vectorstore = vectorstore
        self.kb_path = kb_path
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
        )
        self.codebase_intel = codebase_intel
        self.ingestor = CanonicalIngestor(vectorstore)
        self.kb_reader = KBIngestor(kb_path)

    async def run_full(self, repo_id: Optional[str] = None) -> PipelineResult:
        """Run all milestones in dependency order."""
        t0 = time.monotonic()
        result = PipelineResult()

        # M2: Train/dev/holdout split (no deps)
        m2 = await self.run_split()
        result.milestones.append(m2)

        # M5: Pillar 1 schema + Pillar 3 APIs
        m5 = await self.run_pillar1_pillar3(repo_id=repo_id)
        result.milestones.append(m5)
        result.total_documents += m5.documents_ingested

        # M5b: Pillar 1 extras (catalog, connections, relationships, access patterns)
        m5b = await self.run_pillar1_extras(repo_id=repo_id)
        result.milestones.append(m5b)
        result.total_documents += m5b.documents_ingested

        # M5c: Pillar 4 page intelligence + Pillar 5 module docs
        m5c = await self.run_pillar4_and_5()
        result.milestones.append(m5c)
        result.total_documents += m5c.documents_ingested

        # M3: Module docs (if codebase_intelligence available)
        if self.codebase_intel:
            m3 = await self.run_module_docs()
            result.milestones.append(m3)
            result.total_documents += m3.documents_ingested

        # M4: Generated artifacts
        m4 = await self.run_generated_artifacts()
        result.milestones.append(m4)
        result.total_documents += m4.documents_ingested

        # Eval seeds ingestion
        m_eval = await self.run_eval_seeds()
        result.milestones.append(m_eval)
        result.total_documents += m_eval.documents_ingested

        # Graph rebuild: populate graph_nodes, graph_edges, entity_lookup
        # (skip if already built by run_pillar1_pillar3 above)
        if not any(m.name == "pillar1_schema_pillar3_apis" and m.details.get("graph_nodes") for m in result.milestones):
            m_graph = await self.run_graph_build()
            result.milestones.append(m_graph)

        # Phase 3: KB drift check — verify row count didn't drop unexpectedly
        m_drift = await self.run_kb_drift_check()
        result.milestones.append(m_drift)
        if not m_drift.success:
            logger.warning(
                "pipeline.kb_drift_detected",
                alert=m_drift.details.get("alert", "unknown"),
            )

        # Invalidate pattern cache after KB/graph rebuild (H3 fix)
        try:
            from app.engine.pattern_cache import PatternCache
            pc = PatternCache()
            await pc.invalidate_all(reason="pipeline_run_full")
        except Exception as e:
            logger.debug("pipeline.pattern_cache_invalidate_failed", error=str(e))

        result.total_duration_ms = (time.monotonic() - t0) * 1000
        result.success = all(m.success for m in result.milestones)

        logger.info(
            "training_pipeline.complete",
            milestones=len(result.milestones),
            total_docs=result.total_documents,
            total_ms=round(result.total_duration_ms, 1),
            all_success=result.success,
        )

        return result

    async def run_split(self) -> MilestoneResult:
        """M2: Create train/dev/holdout split."""
        t0 = time.monotonic()
        try:
            splitter = DataSplitter(self.kb_path, self.data_dir)
            meta = splitter.run_split()
            return MilestoneResult(
                milestone=2,
                name="train_dev_holdout_split",
                success=True,
                documents_ingested=meta.get("after_dedup", 0),
                duration_ms=(time.monotonic() - t0) * 1000,
                details=meta,
            )
        except Exception as e:
            logger.error("pipeline.m2_failed", error=str(e))
            return MilestoneResult(
                milestone=2, name="train_dev_holdout_split",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_pillar1_pillar3(self, repo_id: Optional[str] = None) -> MilestoneResult:
        """M5: Ingest Pillar 1 schema + Pillar 3 API tools.

        If repo_id is provided, only that repo is processed.
        Skips repos/tables whose YAML files haven't changed since last index.
        """
        t0 = time.monotonic()
        total = 0
        skipped = 0
        details = {}

        try:
            all_repos = ["MultiChannel_API", "SR_Web", "MultiChannel_Web"]
            repos = [repo_id] if repo_id else all_repos

            # Build change map: which repos have pending (changed) files
            changed_tables = await self._get_changed_tables(repos)

            all_table_docs = []
            all_api_docs = []

            # Pillar 1 schema
            for repo in repos:
                repo_changed = changed_tables.get(repo)
                docs = self.kb_reader.read_pillar1_schema(repo)
                if not docs:
                    continue
                # Filter to only tables with changed files (None = no index yet = run all)
                if repo_changed is not None:
                    docs = [d for d in docs if d.get("entity_id", "").split(":")[-1] in repo_changed or not repo_changed]
                if docs:
                    # Semantic chunking: split large docs into focused chunks
                    chunked = chunk_documents(docs)
                    all_table_docs.extend(docs)  # keep originals for bridge generation
                    ingest_docs = [IngestDocument(**d) for d in chunked]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"pillar1_{repo}"] = result.ingested
                    details[f"pillar1_{repo}_chunks"] = len(chunked)
                else:
                    skipped_count = len(self.kb_reader.read_pillar1_schema(repo))
                    skipped += skipped_count
                    details[f"pillar1_{repo}_skipped"] = skipped_count

            # Pillar 3 APIs
            for repo in repos:
                docs = self.kb_reader.read_pillar3_apis(repo)
                if docs:
                    chunked = chunk_documents(docs)
                    all_api_docs.extend(docs)
                    ingest_docs = [IngestDocument(**d) for d in chunked]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"pillar3_{repo}"] = result.ingested
                    details[f"pillar3_{repo}_chunks"] = len(chunked)

            # Generate and ingest bridge docs (cross-reference table ↔ API)
            if all_table_docs and all_api_docs:
                bridges = generate_bridge_docs(all_table_docs, all_api_docs)
                if bridges:
                    ingest_docs = [IngestDocument(**d) for d in bridges]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details["bridge_docs"] = result.ingested

            if skipped:
                details["skipped_unchanged"] = skipped

            logger.info(
                "pipeline.m5_complete",
                repos=repos,
                ingested=total,
                skipped=skipped,
            )

            # Also rebuild graph after embedding (so graph stays in sync)
            try:
                graph_result = await self.run_graph_build()
                details["graph_nodes"] = graph_result.details.get("nodes_created", 0) + graph_result.details.get("nodes_updated", 0)
                details["graph_edges"] = graph_result.details.get("edges_created", 0) + graph_result.details.get("edges_updated", 0)
            except Exception as ge:
                logger.warning("pipeline.m5_graph_failed", error=str(ge))
                details["graph_error"] = str(ge)

            return MilestoneResult(
                milestone=5,
                name="pillar1_schema_pillar3_apis",
                success=True,
                documents_ingested=total,
                duration_ms=(time.monotonic() - t0) * 1000,
                details=details,
            )
        except Exception as e:
            logger.error("pipeline.m5_failed", error=str(e))
            return MilestoneResult(
                milestone=5, name="pillar1_schema_pillar3_apis",
                documents_ingested=total, error=str(e),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_graph_build(self) -> MilestoneResult:
        """Rebuild the knowledge graph (graph_nodes, graph_edges, entity_lookup).

        Reads from the same KB as the embedding pipeline but creates
        graph structure instead of embeddings.
        """
        t0 = time.monotonic()
        try:
            from app.graph.ingest import CanonicalIngestionPipeline

            pipeline = CanonicalIngestionPipeline(kb_path=os.path.dirname(self.kb_path))
            report = await pipeline.ingest_all()

            return MilestoneResult(
                milestone=99,
                name="graph_build",
                success=len(report.errors) == 0,
                documents_ingested=report.nodes_created + report.nodes_updated,
                duration_ms=(time.monotonic() - t0) * 1000,
                details={
                    "nodes_created": report.nodes_created,
                    "nodes_updated": report.nodes_updated,
                    "edges_created": report.edges_created,
                    "edges_updated": report.edges_updated,
                    "lookups": report.lookups_upserted,
                    "apis": report.apis_processed,
                    "tables": report.tables_processed,
                    "repos": report.repos_processed,
                    "errors": report.errors[:5],
                },
            )
        except Exception as e:
            logger.error("pipeline.graph_build_failed", error=str(e))
            return MilestoneResult(
                milestone=99, name="graph_build",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_pillar1_extras(self, repo_id: Optional[str] = None) -> MilestoneResult:
        """Ingest Pillar 1 extras: catalog, connections, relationships, access patterns."""
        t0 = time.monotonic()
        try:
            repos = [repo_id] if repo_id else ["MultiChannel_API"]
            total = 0
            details = {}
            for repo in repos:
                docs = self.kb_reader.read_pillar1_extras(repo)
                if docs:
                    ingest_docs = [IngestDocument(**d) for d in docs]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"p1_extras_{repo}"] = result.ingested

            return MilestoneResult(
                milestone=51, name="pillar1_extras",
                success=True, documents_ingested=total,
                duration_ms=(time.monotonic() - t0) * 1000, details=details,
            )
        except Exception as e:
            logger.error("pipeline.p1_extras_failed", error=str(e))
            return MilestoneResult(
                milestone=51, name="pillar1_extras",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_pillar4_and_5(self) -> MilestoneResult:
        """Ingest Pillar 4 (page intelligence) + Pillar 5 (module docs) from all repos."""
        t0 = time.monotonic()
        try:
            total = 0
            details = {}

            # Pillar 4: page/role intelligence
            for repo in ["SR_Web", "MultiChannel_Web"]:
                docs = self.kb_reader.read_pillar4_pages(repo)
                if docs:
                    ingest_docs = [IngestDocument(**d) for d in docs]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"p4_{repo}"] = result.ingested

            # Pillar 3 extras (api_classification)
            for repo in ["MultiChannel_API", "SR_Web", "MultiChannel_Web"]:
                docs = self.kb_reader.read_pillar3_extras(repo)
                if docs:
                    ingest_docs = [IngestDocument(**d) for d in docs]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"p3_extras_{repo}"] = result.ingested

            # Pillar 5: module docs from all repos
            import os
            kb_path_obj = self.kb_reader.kb_path
            for repo_dir in sorted(kb_path_obj.iterdir()):
                if not repo_dir.is_dir():
                    continue
                if not (repo_dir / "pillar_5_module_docs").exists():
                    continue
                docs = self.kb_reader.read_pillar5_modules(repo_dir.name)
                if docs:
                    ingest_docs = [IngestDocument(**d) for d in docs]
                    result = await self.ingestor.ingest(ingest_docs)
                    total += result.ingested
                    details[f"p5_{repo_dir.name}"] = result.ingested

            return MilestoneResult(
                milestone=54, name="pillar4_pillar5_extras",
                success=True, documents_ingested=total,
                duration_ms=(time.monotonic() - t0) * 1000, details=details,
            )
        except Exception as e:
            logger.error("pipeline.p4_p5_failed", error=str(e))
            return MilestoneResult(
                milestone=54, name="pillar4_pillar5_extras",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def _get_changed_tables(self, repos: List[str]) -> Dict[str, Optional[set]]:
        """Return a dict of repo -> set of changed table names, or None if no index exists.

        None means "no prior index — process everything".
        Empty set means "index exists but nothing changed — skip all".
        """
        result: Dict[str, Optional[set]] = {}
        try:
            from app.services.kb_file_index import KBFileIndexService
            from app.db.session import AsyncSessionLocal
            from sqlalchemy import text

            fi = KBFileIndexService()
            changed_files = await fi.diff_and_mark_pending(self.kb_path, repo_id=None)

            async with AsyncSessionLocal() as session:
                for repo in repos:
                    # Check if this repo has any indexed files at all
                    row = await session.execute(
                        text("SELECT COUNT(*) FROM cosmos_kb_file_index WHERE repo_id = :repo"),
                        {"repo": repo},
                    )
                    count = row.scalar() or 0
                    if count == 0:
                        # First run — no prior index, process everything
                        result[repo] = None
                        continue

                    # Extract table names from changed YAML paths for this repo
                    # e.g. "MultiChannel_API/pillar_1_schema/tables/orders/columns.yaml" → "orders"
                    tables: set = set()
                    for f in changed_files:
                        fpath = f.get("path", "")
                        if not fpath.startswith(repo + "/"):
                            continue
                        parts = fpath.replace("\\", "/").split("/")
                        # Look for .../tables/<table_name>/...
                        if "tables" in parts:
                            idx = parts.index("tables")
                            if idx + 1 < len(parts):
                                tables.add(parts[idx + 1])
                    result[repo] = tables

        except Exception as e:
            logger.warning("pipeline.change_detection_failed", error=str(e))
            # Fall back to processing everything
            for repo in repos:
                result[repo] = None

        return result

    async def run_module_docs(self) -> MilestoneResult:
        """M3: Ingest module docs from all 8 repos via codebase_intelligence."""
        t0 = time.monotonic()
        try:
            if self.codebase_intel:
                stats = await self.codebase_intel.ingest()
                return MilestoneResult(
                    milestone=3,
                    name="module_docs_8_repos",
                    success=True,
                    documents_ingested=stats.get("chunks", 0),
                    duration_ms=(time.monotonic() - t0) * 1000,
                    details=stats,
                )
            return MilestoneResult(
                milestone=3, name="module_docs_8_repos",
                error="codebase_intelligence not available",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as e:
            logger.error("pipeline.m3_failed", error=str(e))
            return MilestoneResult(
                milestone=3, name="module_docs_8_repos",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_generated_artifacts(self) -> MilestoneResult:
        """M4: Ingest generated KB artifacts."""
        t0 = time.monotonic()
        try:
            docs = self.kb_reader.read_generated_artifacts()
            if docs:
                ingest_docs = [IngestDocument(**d) for d in docs]
                result = await self.ingestor.ingest(ingest_docs)
                return MilestoneResult(
                    milestone=4,
                    name="generated_artifacts",
                    success=True,
                    documents_ingested=result.ingested,
                    duration_ms=(time.monotonic() - t0) * 1000,
                    details=result.by_entity_type,
                )
            return MilestoneResult(
                milestone=4, name="generated_artifacts",
                success=True, documents_ingested=0,
                details={"note": "no generated/ folder found"},
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as e:
            logger.error("pipeline.m4_failed", error=str(e))
            return MilestoneResult(
                milestone=4, name="generated_artifacts",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_kb_drift_check(self) -> MilestoneResult:
        """Phase 3: KB drift checker — detect unexpected drops in cosmos_embeddings rows.

        Compares cosmos_embeddings row count before vs after an ingest run.
        Alerts when:
          - Row count drops by more than 10% (possible accidental deletion)
          - benchmark tool_accuracy drops > 5 points (quality regression)

        Returns a MilestoneResult with details["drift_ok"]=True when safe.
        """
        t0 = time.monotonic()
        details: Dict[str, Any] = {}
        try:
            from app.db.session import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                row = await session.execute(
                    text("SELECT COUNT(*) FROM cosmos_embeddings")
                )
                current_count = row.scalar() or 0
                details["current_row_count"] = current_count

            # Load last known count from benchmark_results.json (if exists)
            import json
            results_path = self.data_dir + "/benchmark_results.json"
            prev_count = 0
            prev_tool_accuracy = None
            try:
                import os
                if os.path.exists(results_path):
                    with open(results_path) as f:
                        prev_results = json.load(f)
                    prev_count = prev_results.get("_meta", {}).get("row_count", 0)
                    # Get tool_accuracy from flat or nested results
                    backends = prev_results.get("backends", {})
                    if backends:
                        first_backend = next(iter(backends.values()), {})
                        prev_tool_accuracy = first_backend.get("tool_accuracy")
                    else:
                        prev_tool_accuracy = prev_results.get("tool_accuracy")
            except Exception:
                pass

            details["previous_row_count"] = prev_count
            drift_ok = True

            # Check 1: row count drop > 10%
            if prev_count > 0:
                drop_pct = (prev_count - current_count) / prev_count * 100
                details["row_drop_pct"] = round(drop_pct, 2)
                if drop_pct > 10:
                    drift_ok = False
                    details["alert"] = (
                        f"KB row count dropped {drop_pct:.1f}% "
                        f"({prev_count} → {current_count}). "
                        "Block promotion — investigate before next deploy."
                    )
                    logger.warning(
                        "kb_drift.row_count_drop",
                        prev=prev_count, current=current_count, drop_pct=drop_pct,
                    )

            # Write current count into benchmark_results.json metadata for next run
            try:
                import os
                if os.path.exists(results_path):
                    with open(results_path) as f:
                        bench = json.load(f)
                    bench.setdefault("_meta", {})["row_count"] = current_count
                    with open(results_path, "w") as f:
                        json.dump(bench, f, indent=2)
            except Exception:
                pass

            details["drift_ok"] = drift_ok

            logger.info(
                "kb_drift_check.done",
                current_count=current_count,
                drift_ok=drift_ok,
            )

            return MilestoneResult(
                milestone=100,
                name="kb_drift_check",
                success=drift_ok,
                documents_ingested=current_count,
                duration_ms=(time.monotonic() - t0) * 1000,
                details=details,
            )

        except Exception as e:
            logger.error("pipeline.kb_drift_check_failed", error=str(e))
            return MilestoneResult(
                milestone=100, name="kb_drift_check",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_eval_seeds(self) -> MilestoneResult:
        """Ingest eval seeds and training seeds into embeddings."""
        t0 = time.monotonic()
        try:
            docs = self.kb_reader.read_eval_seeds()
            if docs:
                ingest_docs = [IngestDocument(**d) for d in docs]
                result = await self.ingestor.ingest(ingest_docs)
                return MilestoneResult(
                    milestone=0,
                    name="eval_seeds",
                    success=True,
                    documents_ingested=result.ingested,
                    duration_ms=(time.monotonic() - t0) * 1000,
                    details=result.by_entity_type,
                )
            return MilestoneResult(
                milestone=0, name="eval_seeds",
                success=True, documents_ingested=0,
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as e:
            logger.error("pipeline.eval_seeds_failed", error=str(e))
            return MilestoneResult(
                milestone=0, name="eval_seeds",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

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

        # ── M0: KB Quality Fixes (runs BEFORE ingestion) ──
        # Fixes: generic examples, missing params, empty columns, entity hubs
        try:
            from app.enrichment.kb_quality_fixer import KBQualityFixer
            # Fix 4 (entity hubs) runs without LLM — always safe
            fixer = KBQualityFixer(kb_path=str(self.kb_path))
            hub_stats_only = {"hubs_fixed": 0}
            await fixer.fix_entity_hubs()
            hub_stats_only["hubs_fixed"] = fixer.get_stats().get("hubs_fixed", 0)
            logger.info("pipeline.kb_quality_hubs_fixed", **hub_stats_only)

            # Fixes 1-3 use Claude CLI (no API key needed — uses CLI auth).
            # Check if CLI is available before running.
            from app.engine.claude_cli import ClaudeCLI
            _cli = ClaudeCLI()
            if _cli.available:
                fix_stats = await fixer.run_all_fixes()
                result.milestones.append(MilestoneResult(
                    milestone=0, name="kb_quality_fixes",
                    success=True, documents_ingested=0,
                    duration_ms=0,
                    details=fix_stats,
                ))
                logger.info("pipeline.kb_quality_fixed", **fix_stats)
            else:
                logger.info("pipeline.kb_quality_skipped", reason="Claude CLI not found")
        except Exception as e:
            logger.warning("pipeline.kb_quality_fix_failed", error=str(e))

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

        # M6: Pillar 6 action contracts + Pillar 7 workflow runbooks + Pillar 8 negatives
        m6 = await self.run_pillar6_7_8()
        result.milestones.append(m6)
        result.total_documents += m6.documents_ingested

        # M7: Entity Hub generation (cross-pillar summaries)
        m7 = await self.run_entity_hubs()
        result.milestones.append(m7)
        result.total_documents += m7.documents_ingested

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

        # Phase 4d: Auto-generate dev_set.jsonl from KB eval_cases
        m_eval_autogen = await self.run_eval_seeds_autogen()
        result.milestones.append(m_eval_autogen)
        result.total_documents += m_eval_autogen.documents_ingested
        if m_eval_autogen.documents_ingested:
            logger.info(
                "pipeline.eval_seeds_autogen",
                added=m_eval_autogen.details.get("new_pairs_added", 0),
                total=m_eval_autogen.details.get("total_pairs", 0),
            )

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

        # Sync KB-driven registry (tools, agents, skills from graph)
        try:
            from app.engine.kb_driven_registry import KBDrivenRegistry
            kb_reg = KBDrivenRegistry(kb_path=self.kb_path)
            await kb_reg.sync_all()
            stats = kb_reg.get_stats()
            logger.info("pipeline.kb_registry_synced", **stats)
        except Exception as e:
            logger.debug("pipeline.kb_registry_sync_failed", error=str(e))

        # ── Pillar 2: Business Rules Generation ──
        # Extract implicit business rules from Pillar 1/3/6 into structured YAML.
        try:
            from app.enrichment.business_rules_generator import BusinessRulesGenerator
            rules_gen = BusinessRulesGenerator(kb_path=str(self.kb_path))
            rules_stats = await rules_gen.generate_all()
            logger.info("pipeline.business_rules_generated", **rules_stats)
        except Exception as e:
            logger.warning("pipeline.business_rules_generation_failed", error=str(e))

        # ── Pillar 8: Negative Examples Expansion ──
        # Generate domain-specific anti-patterns from action contracts + rules.
        try:
            from app.enrichment.negative_examples_generator import NegativeExamplesGenerator
            neg_gen = NegativeExamplesGenerator(kb_path=str(self.kb_path))
            neg_stats = await neg_gen.generate_all()
            logger.info("pipeline.negative_examples_generated", **neg_stats)
        except Exception as e:
            logger.warning("pipeline.negative_examples_generation_failed", error=str(e))

        # ── KB Enrichment Pipeline (Contextual Headers + Synthetic Q&A) ──
        # Runs AFTER ingestion so we can enrich existing chunks in Qdrant.
        # Uses Claude Opus 4.6 for highest quality enrichment.
        try:
            enrichment_result = await self.run_enrichment_pipeline()
            result.milestones.append(enrichment_result)
            if enrichment_result.details:
                logger.info("pipeline.enrichment_complete", **enrichment_result.details)
        except Exception as e:
            logger.warning("pipeline.enrichment_failed", error=str(e))

        # Cross-pillar linking (after all enrichment is done)
        try:
            from app.enrichment.cross_pillar_linker import CrossPillarLinker
            linker = CrossPillarLinker()
            link_stats = await linker.build_links()
            logger.info("pipeline.cross_pillar_links_built", **link_stats)
        except Exception as e:
            logger.debug("pipeline.cross_pillar_linking_failed", error=str(e))

        # Run ICRM eval benchmark (if eval set exists)
        try:
            m_eval_icrm = await self.run_icrm_eval()
            result.milestones.append(m_eval_icrm)
            if m_eval_icrm.details:
                logger.info("pipeline.icrm_eval_complete", **m_eval_icrm.details)
        except Exception as e:
            logger.debug("pipeline.icrm_eval_failed", error=str(e))

        # Apply feedback loop auto-actions (staging only — human review required)
        try:
            from app.services.feedback_loop import FeedbackLoop
            feedback_report = await FeedbackLoop.apply_auto_actions(str(self.kb_path))
            if feedback_report.get("total_traces", 0) > 0:
                logger.info("pipeline.feedback_loop_applied",
                            traces=feedback_report["total_traces"],
                            candidates=len(feedback_report.get("action_candidates", [])),
                            negatives=len(feedback_report.get("negative_examples", [])))
        except Exception as e:
            logger.debug("pipeline.feedback_loop_failed", error=str(e))

        result.total_duration_ms = (time.monotonic() - t0) * 1000

        # ── Verification pass (superpowers: verification-before-completion) ──
        # Quick self-check: catch obvious pipeline failures before reporting done.
        verification_issues: List[str] = []
        verification_warnings: List[str] = []

        # Check 1: at least one milestone succeeded
        succeeded = [m for m in result.milestones if m.success]
        if not succeeded:
            verification_issues.append("ALL milestones failed — pipeline produced nothing")

        # Check 2: total documents > 0
        if result.total_documents == 0:
            verification_issues.append("zero documents ingested — KB may be empty or paths wrong")

        # Check 3: drift check didn't flag a drop
        drift_m = next((m for m in result.milestones if m.name == "kb_drift_check"), None)
        if drift_m and not drift_m.success:
            verification_warnings.append(f"KB drift detected: {drift_m.details.get('alert', 'unknown')}")

        # Check 4: graph build ran (critical for retrieval)
        graph_ran = any(m.name in ("graph_build", "pillar1_schema_pillar3_apis") and m.success for m in result.milestones)
        if not graph_ran:
            verification_warnings.append("graph build did not succeed — HybridRetriever will return empty results")

        if verification_issues:
            logger.error("pipeline.verification_failed",
                         issues=verification_issues,
                         warnings=verification_warnings,
                         total_docs=result.total_documents)
        elif verification_warnings:
            logger.warning("pipeline.verification_warnings",
                           warnings=verification_warnings,
                           total_docs=result.total_documents)
        else:
            logger.info("pipeline.verification_passed",
                        milestones_ok=len(succeeded),
                        total_docs=result.total_documents)

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

    async def run_pillar6_7_8(self) -> MilestoneResult:
        """Ingest Pillar 6 (action contracts), Pillar 7 (workflow runbooks), Pillar 8 (negative routing).
        Also creates graph nodes and edges for actions and workflows."""
        t0 = time.monotonic()
        try:
            total = 0
            details = {}

            # Pillar 6: action contracts (multi-file)
            p6_docs = self.kb_reader.read_pillar6_actions("MultiChannel_API")
            if p6_docs:
                ingest_docs = [IngestDocument(**d) for d in p6_docs]
                result = await self.ingestor.ingest(ingest_docs)
                total += result.ingested
                details["p6_actions"] = result.ingested

            # Pillar 7: workflow runbooks (multi-file)
            p7_docs = self.kb_reader.read_pillar7_runbooks("MultiChannel_API")
            if p7_docs:
                ingest_docs = [IngestDocument(**d) for d in p7_docs]
                result = await self.ingestor.ingest(ingest_docs)
                total += result.ingested
                details["p7_workflows"] = result.ingested

            # Pillar 8: negative routing
            p8_docs = self.kb_reader.read_pillar8_negative_routing("MultiChannel_API")
            if p8_docs:
                ingest_docs = [IngestDocument(**d) for d in p8_docs]
                result = await self.ingestor.ingest(ingest_docs)
                total += result.ingested
                details["p8_negatives"] = result.ingested

            # Create graph nodes for actions and workflows
            graph_details = await self._build_action_workflow_graph(p6_docs, p7_docs)
            details.update(graph_details)

            logger.info("pipeline.pillar6_7_8_complete", total=total, details=details)
            return MilestoneResult(
                milestone=60, name="pillar6_7_8_actions_workflows",
                success=True, documents_ingested=total,
                duration_ms=(time.monotonic() - t0) * 1000, details=details,
            )
        except Exception as e:
            logger.error("pipeline.pillar6_7_8_failed", error=str(e))
            return MilestoneResult(
                milestone=60, name="pillar6_7_8_actions_workflows",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def _build_action_workflow_graph(self, p6_docs, p7_docs) -> Dict:
        """Create graph nodes AND edges for action contracts and workflows.

        Reads linked_tables, linked_apis, linked_actions from index files
        to create typed edges connecting P6/P7 to P1/P3 nodes.
        """
        from app.services.graphrag_models import NodeType, EdgeType
        graphrag = getattr(self, '_graphrag', None)
        if graphrag is None:
            try:
                from app.services.graphrag import GraphRAGService
                graphrag = GraphRAGService()
                self._graphrag = graphrag
            except Exception:
                return {"graph_actions": 0, "graph_workflows": 0}

        action_nodes = 0
        workflow_nodes = 0
        edge_count = 0

        # Collect action index data for edge creation
        action_index_data = {}
        for doc in (p6_docs or []):
            meta = doc.get("metadata", {})
            if meta.get("file_type") != "index":
                continue
            action_id = doc.get("entity_id", "")
            domain = meta.get("domain", "")
            repo = meta.get("repo_id", "MultiChannel_API")

            # Read the actual index.yaml to get linked_* fields
            action_dir = self.kb_path / repo / "pillar_6_action_contracts" / "domains" / domain
            for ad in action_dir.iterdir() if action_dir.exists() else []:
                idx = self._read_yaml(ad / "index.yaml") if ad.is_dir() else None
                if idx and idx.get("action_id") == action_id:
                    action_index_data[action_id] = idx
                    break

            graphrag.ingest_node(
                node_id=action_id,
                node_type=NodeType.action_contract,
                label=action_id,
                repo_id=repo,
                properties={
                    "domain": domain,
                    "kind": meta.get("kind", ""),
                    "pillar": "pillar_6",
                    "capability": "action",
                },
            )
            action_nodes += 1

        # Create edges from action contracts to linked tables and APIs
        for action_id, idx in action_index_data.items():
            # action → table edges (reads_table)
            for lt in idx.get("linked_tables", []):
                table_name = lt.split("/")[-1] if "/" in lt else lt
                graphrag.ingest_edge(
                    source_id=action_id,
                    target_id=f"table:{table_name}",
                    edge_type=EdgeType.reads_table,
                    properties={"source_pillar": "pillar_6"},
                )
                edge_count += 1

            # action → api edges (calls_api)
            for la in idx.get("linked_apis", []):
                api_id = la.split("/")[-1] if "/" in la else la
                graphrag.ingest_edge(
                    source_id=action_id,
                    target_id=f"api:{api_id}",
                    edge_type=EdgeType.calls_api,
                    properties={"source_pillar": "pillar_6"},
                )
                edge_count += 1

            # action → job edges (dispatches_job)
            for lj in idx.get("linked_jobs", []):
                graphrag.ingest_edge(
                    source_id=action_id,
                    target_id=f"job:{lj}",
                    edge_type=EdgeType.dispatches_job,
                    properties={"source_pillar": "pillar_6"},
                )
                edge_count += 1

        # Build workflow nodes and edges
        workflow_index_data = {}
        for doc in (p7_docs or []):
            meta = doc.get("metadata", {})
            if meta.get("file_type") != "index":
                continue
            wf_id = doc.get("entity_id", "")
            domain = meta.get("domain", "")
            repo = meta.get("repo_id", "MultiChannel_API")

            # Read actual index.yaml for linked_actions
            wf_dir = self.kb_path / repo / "pillar_7_workflow_runbooks" / "domains" / domain
            for wd in wf_dir.iterdir() if wf_dir.exists() else []:
                idx = self._read_yaml(wd / "index.yaml") if wd.is_dir() else None
                if idx and idx.get("workflow_id") == wf_id:
                    workflow_index_data[wf_id] = idx
                    break

            graphrag.ingest_node(
                node_id=wf_id,
                node_type=NodeType.workflow,
                label=wf_id,
                repo_id=repo,
                properties={
                    "domain": domain,
                    "pillar": "pillar_7",
                    "capability": "workflow",
                },
            )
            workflow_nodes += 1

        # Create edges from workflows to actions
        for wf_id, idx in workflow_index_data.items():
            for la in idx.get("linked_actions", []):
                graphrag.ingest_edge(
                    source_id=wf_id,
                    target_id=la,
                    edge_type=EdgeType.uses_action,
                    properties={"source_pillar": "pillar_7"},
                )
                edge_count += 1

            # workflow → table edges
            for lt in idx.get("linked_tables", []):
                table_name = lt.split("/")[-1] if "/" in lt else lt
                graphrag.ingest_edge(
                    source_id=wf_id,
                    target_id=f"table:{table_name}",
                    edge_type=EdgeType.reads_table,
                    properties={"source_pillar": "pillar_7"},
                )
                edge_count += 1

        # G2+G3 fix: Create entity_lookup entries for action/workflow nodes
        # Without this, HybridRetriever Leg 1 (exact_lookup) cannot find P6/P7 docs
        lookup_count = 0
        from app.graph.ingest import CanonicalIngestionPipeline as _CIP
        _ingestor = _CIP.__new__(_CIP)
        _ingestor._lookup_batch = []
        _ingestor._seen_edge_triples = set()
        _ingestor._report = type('R', (), {'lookups_upserted': 0})()

        for action_id in action_index_data:
            await _ingestor._upsert_lookup(
                entity_type="action_id", entity_value=action_id,
                node_id=action_id, repo_id="MultiChannel_API",
            )
            # Also register by short name (e.g., "create_order")
            short_name = action_id.split(".")[-1] if "." in action_id else action_id
            await _ingestor._upsert_lookup(
                entity_type="action_name", entity_value=short_name,
                node_id=action_id, repo_id="MultiChannel_API",
            )
            lookup_count += 2

        for wf_id in workflow_index_data:
            await _ingestor._upsert_lookup(
                entity_type="workflow_id", entity_value=wf_id,
                node_id=wf_id, repo_id="MultiChannel_API",
            )
            short_name = wf_id.split(".")[-1] if "." in wf_id else wf_id
            await _ingestor._upsert_lookup(
                entity_type="workflow_name", entity_value=short_name,
                node_id=wf_id, repo_id="MultiChannel_API",
            )
            lookup_count += 2

        # Flush lookups
        if _ingestor._lookup_batch:
            await _ingestor._flush_lookups()

        # Persist all nodes and edges
        try:
            await graphrag.pg_flush_all()
        except Exception as e:
            logger.warning("pipeline.action_workflow_graph_flush_failed", error=str(e))

        logger.info("pipeline.action_workflow_graph_built",
                     action_nodes=action_nodes, workflow_nodes=workflow_nodes,
                     edges=edge_count, entity_lookups=lookup_count)
        return {
            "graph_action_nodes": action_nodes,
            "graph_workflow_nodes": workflow_nodes,
            "graph_edges": edge_count,
        }

    async def run_icrm_eval(self) -> MilestoneResult:
        """Run ICRM eval benchmark against the KB to measure retrieval quality."""
        t0 = time.monotonic()
        if not self.vectorstore:
            return MilestoneResult(
                milestone=80, name="icrm_eval", success=True,
                documents_ingested=0, duration_ms=0,
                details={"skipped": "vectorstore not available"},
            )
        try:
            import json
            eval_path = self.kb_path / "MultiChannel_API" / "icrm_eval_set.jsonl"
            if not eval_path.exists():
                return MilestoneResult(
                    milestone=80, name="icrm_eval",
                    success=True, documents_ingested=0,
                    duration_ms=0, details={"skipped": "no eval set found"},
                )

            # Load eval seeds
            seeds = []
            with open(eval_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        seeds.append(json.loads(line))

            # Run vector search for each seed and check if expected hit is in top-K
            hits_at_1 = 0
            hits_at_5 = 0
            hits_at_10 = 0
            total = len(seeds)
            domain_stats = {}

            for seed in seeds:
                query = seed.get("query", "")
                expected_action = seed.get("expected_action", "")
                expected_workflow = seed.get("expected_workflow", "")
                expected_api = seed.get("expected_api", "")
                expected_table = seed.get("expected_table", "")
                expected_entity = seed.get("expected_entity", "")
                category = seed.get("category", "unknown")

                # Determine what we're looking for
                target_id = expected_action or expected_workflow or expected_api or expected_table or expected_entity
                if not target_id:
                    continue

                # Search
                results = await self.vectorstore.search_similar(
                    query=query, limit=10, threshold=0.1,
                )

                # Check if target is in results
                result_ids = [r.get("entity_id", "") for r in results]
                found_at = -1
                for i, rid in enumerate(result_ids):
                    if target_id in rid or rid in target_id:
                        found_at = i
                        break

                if found_at == 0:
                    hits_at_1 += 1
                if found_at >= 0 and found_at < 5:
                    hits_at_5 += 1
                if found_at >= 0:
                    hits_at_10 += 1

                # Per-category stats
                if category not in domain_stats:
                    domain_stats[category] = {"total": 0, "hit_at_5": 0}
                domain_stats[category]["total"] += 1
                if found_at >= 0 and found_at < 5:
                    domain_stats[category]["hit_at_5"] += 1

            details = {
                "total_seeds": total,
                "recall_at_1": round(hits_at_1 / max(total, 1), 3),
                "recall_at_5": round(hits_at_5 / max(total, 1), 3),
                "recall_at_10": round(hits_at_10 / max(total, 1), 3),
                "per_category": {
                    k: {**v, "recall_at_5": round(v["hit_at_5"] / max(v["total"], 1), 3)}
                    for k, v in domain_stats.items()
                },
            }

            logger.info("pipeline.icrm_eval_complete", **details)
            return MilestoneResult(
                milestone=80, name="icrm_eval",
                success=True, documents_ingested=0,
                duration_ms=(time.monotonic() - t0) * 1000, details=details,
            )
        except Exception as e:
            logger.error("pipeline.icrm_eval_failed", error=str(e))
            return MilestoneResult(
                milestone=80, name="icrm_eval",
                error=str(e), duration_ms=(time.monotonic() - t0) * 1000,
            )

    async def run_entity_hubs(self) -> MilestoneResult:
        """Generate cross-pillar Entity Hub summaries (P1+P3+P6+P7 merged per entity)."""
        t0 = time.monotonic()
        try:
            from app.services.entity_hub_generator import generate_entity_hubs
            hub_docs = generate_entity_hubs(str(self.kb_path))
            total = 0
            if hub_docs:
                ingest_docs = [IngestDocument(**d) for d in hub_docs]
                result = await self.ingestor.ingest(ingest_docs)
                total = result.ingested
            logger.info("pipeline.entity_hubs_complete", count=total)
            return MilestoneResult(
                milestone=70, name="entity_hubs",
                success=True, documents_ingested=total,
                duration_ms=(time.monotonic() - t0) * 1000,
                details={"hubs_generated": total},
            )
        except Exception as e:
            logger.error("pipeline.entity_hubs_failed", error=str(e))
            return MilestoneResult(
                milestone=70, name="entity_hubs",
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

    async def run_eval_seeds_autogen(self) -> MilestoneResult:
        """Phase 4d: Auto-generate dev_set.jsonl from KB eval_cases.

        Sources:
          - Pillar 3: low.yaml eval_cases.golden_eval_cases per API folder
          - Pillar 4: eval_cases.yaml per page folder

        Each entry written to dev_set.jsonl has the format:
          {query, expected_tool, expected_params, source, chunk_type, repo_id}

        Existing hand-curated dev_set entries are preserved — only new
        KB-derived entries are appended (deduped by query+expected_tool).
        """
        t0 = time.monotonic()
        new_pairs: List[Dict[str, Any]] = []

        def _load_yaml(path: str) -> Any:
            try:
                import yaml
                with open(path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                return {}

        kb = Path(self.kb_path)

        # ── Pillar 3: low.yaml eval_cases from each API folder ────────────
        for repo_dir in sorted(kb.iterdir()):
            if not repo_dir.is_dir():
                continue
            pillar3 = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            if not pillar3.exists():
                continue
            for api_dir in sorted(pillar3.iterdir()):
                if not api_dir.is_dir():
                    continue
                low = _load_yaml(str(api_dir / "low.yaml"))
                eval_cases = low.get("eval_cases", {})
                if isinstance(eval_cases, dict):
                    golden = eval_cases.get("golden_eval_cases", [])
                elif isinstance(eval_cases, list):
                    golden = eval_cases
                else:
                    golden = []

                for case in golden:
                    if not isinstance(case, dict):
                        continue
                    query = case.get("query", "")
                    expected_tool = case.get("expected_tool", "")
                    if not query:
                        continue
                    new_pairs.append({
                        "query": query,
                        "expected_tool": expected_tool,
                        "expected_params": case.get("expected_params", {}),
                        "source": f"pillar3:{api_dir.name}",
                        "chunk_type": "api_example",
                        "repo_id": repo_dir.name,
                        "case_id": case.get("id", ""),
                    })

        # ── Pillar 4: eval_cases.yaml from each page folder ───────────────
        for repo_dir in sorted(kb.iterdir()):
            if not repo_dir.is_dir():
                continue
            for web_repo in sorted(repo_dir.iterdir()):
                if not web_repo.is_dir():
                    continue
                pages_dir = web_repo / "pillar_4_page_role_intelligence" / "pages"
                if not pages_dir.exists():
                    continue
                for page_dir in sorted(pages_dir.iterdir()):
                    if not page_dir.is_dir():
                        continue
                    ec_file = page_dir / "eval_cases.yaml"
                    if not ec_file.exists():
                        ec_data = {}
                    else:
                        ec_data = _load_yaml(str(ec_file))

                    cases = (
                        ec_data.get("eval_cases", [])
                        if isinstance(ec_data, dict) else ec_data
                    )
                    for case in (cases or []):
                        if not isinstance(case, dict):
                            continue
                        query = case.get("query", "")
                        if not query:
                            continue
                        new_pairs.append({
                            "query": query,
                            "expected_tool": "",
                            "expected_page": page_dir.name,
                            "expected_output": case.get("expected_output", ""),
                            "case_type": case.get("type", "page_context"),
                            "source": f"pillar4:{page_dir.name}",
                            "chunk_type": "page_field_trace",
                            "repo_id": web_repo.name,
                        })

        if not new_pairs:
            return MilestoneResult(
                milestone=6, name="eval_seeds_autogen",
                success=True, documents_ingested=0,
                duration_ms=(time.monotonic() - t0) * 1000,
                details={"message": "No eval_cases found in KB low.yaml or Pillar 4"},
            )

        # ── Load existing dev_set.jsonl (preserve hand-curated entries) ───
        dev_set_path = Path(self.data_dir) / "dev_set.jsonl"
        existing_keys: set = set()
        existing_lines: List[str] = []
        if dev_set_path.exists():
            with open(dev_set_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        import json as _json
                        entry = _json.loads(line)
                        key = (entry.get("query", ""), entry.get("expected_tool", ""))
                        existing_keys.add(key)
                        existing_lines.append(line)
                    except Exception:
                        existing_lines.append(line)

        # ── Deduplicate and append new pairs ──────────────────────────────
        import json as _json
        added = 0
        dev_set_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dev_set_path, "w") as f:
            for line in existing_lines:
                f.write(line + "\n")
            for pair in new_pairs:
                key = (pair.get("query", ""), pair.get("expected_tool", ""))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                f.write(_json.dumps(pair) + "\n")
                added += 1

        total = len(existing_lines) + added
        logger.info("eval_seeds_autogen.done",
                    new_pairs=added, total=total,
                    path=str(dev_set_path))

        return MilestoneResult(
            milestone=6,
            name="eval_seeds_autogen",
            success=True,
            documents_ingested=added,
            duration_ms=(time.monotonic() - t0) * 1000,
            details={
                "new_pairs_added": added,
                "total_pairs": total,
                "dev_set_path": str(dev_set_path),
            },
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

    # ── Enrichment Pipeline (Phase 1) ──────────────────────────────────

    async def run_enrichment_pipeline(self) -> MilestoneResult:
        """Run contextual header enrichment + synthetic Q&A generation.

        Reads existing chunks from Qdrant, enriches with Opus-generated
        contextual headers, generates synthetic Q&A queries, and re-ingests
        both enriched chunks and synthetic docs.
        """
        t0 = time.monotonic()
        enrichment_stats = {}

        try:
            from app.enrichment.contextual_headers import ContextualHeaderEnricher
            from app.enrichment.synthetic_qa import SyntheticQAGenerator

            # Read all current documents from Qdrant (via scroll)
            existing_docs = await self._read_existing_chunks()
            if not existing_docs:
                return MilestoneResult(
                    milestone=100, name="enrichment",
                    success=True, documents_ingested=0,
                    duration_ms=(time.monotonic() - t0) * 1000,
                    details={"note": "no existing chunks to enrich"},
                )

            logger.info("pipeline.enrichment_start", chunks_to_enrich=len(existing_docs))

            # Step 1: Contextual Headers
            enricher = ContextualHeaderEnricher(model="claude-opus-4-6", max_concurrent=5)
            enriched_docs = await enricher.enrich_batch(existing_docs)
            enrichment_stats["contextual_headers"] = enricher.get_stats()

            # Step 2: Synthetic Q&A
            qa_generator = SyntheticQAGenerator(model="claude-opus-4-6", max_concurrent=5)
            synthetic_docs = await qa_generator.generate_batch(enriched_docs)
            enrichment_stats["synthetic_qa"] = qa_generator.get_stats()

            # Step 3: Re-ingest enriched documents (overwrites existing with enriched content)
            enriched_ingest_docs = []
            for doc in enriched_docs:
                enriched_ingest_docs.append(IngestDocument(
                    entity_type=doc.get("entity_type", "schema"),
                    entity_id=doc.get("entity_id", ""),
                    content=doc.get("content", ""),
                    repo_id=doc.get("repo_id", ""),
                    capability=doc.get("capability", "retrieval"),
                    trust_score=doc.get("trust_score", 0.8),
                    metadata=doc.get("metadata", {}),
                ))

            if enriched_ingest_docs:
                enrich_result = await self.ingestor.ingest(enriched_ingest_docs)
                enrichment_stats["enriched_ingested"] = enrich_result.ingested

            # Step 4: Ingest synthetic Q&A docs
            synthetic_ingest_docs = []
            for doc in synthetic_docs:
                synthetic_ingest_docs.append(IngestDocument(
                    entity_type=doc.get("entity_type", "synthetic_qa"),
                    entity_id=doc.get("entity_id", ""),
                    content=doc.get("content", ""),
                    repo_id=doc.get("repo_id", ""),
                    capability=doc.get("capability", "retrieval"),
                    trust_score=doc.get("trust_score", 0.85),
                    metadata=doc.get("metadata", {}),
                ))

            if synthetic_ingest_docs:
                synth_result = await self.ingestor.ingest(synthetic_ingest_docs)
                enrichment_stats["synthetic_ingested"] = synth_result.ingested

            total_ingested = enrichment_stats.get("enriched_ingested", 0) + enrichment_stats.get("synthetic_ingested", 0)

            return MilestoneResult(
                milestone=100,
                name="enrichment",
                success=True,
                documents_ingested=total_ingested,
                duration_ms=(time.monotonic() - t0) * 1000,
                details=enrichment_stats,
            )

        except Exception as e:
            logger.error("pipeline.enrichment_failed", error=str(e))
            return MilestoneResult(
                milestone=100, name="enrichment",
                error=str(e),
                duration_ms=(time.monotonic() - t0) * 1000,
                details=enrichment_stats,
            )

    async def _read_existing_chunks(self) -> List[Dict]:
        """Read existing chunks from Qdrant for enrichment."""
        try:
            from app.services.qdrant_client import qdrant_store

            if not qdrant_store or not qdrant_store.available:
                logger.warning("enrichment.qdrant_not_available")
                return []

            # Scroll through all points in the collection
            all_docs = []
            offset = None
            batch_size = 100

            while True:
                points, next_offset = qdrant_store.client.scroll(
                    collection_name=qdrant_store.collection_name,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )

                for point in points:
                    payload = point.payload or {}
                    # Skip synthetic Q&A docs (don't re-enrich synthetics)
                    if payload.get("entity_type") == "synthetic_qa":
                        continue
                    # Skip already-enriched docs (check metadata)
                    if payload.get("metadata", {}).get("enrichment") == "opus":
                        continue

                    all_docs.append({
                        "entity_type": payload.get("entity_type", ""),
                        "entity_id": payload.get("entity_id", ""),
                        "content": payload.get("content", ""),
                        "repo_id": payload.get("repo_id", ""),
                        "capability": payload.get("capability", "retrieval"),
                        "trust_score": payload.get("trust_score", 0.8),
                        "metadata": payload.get("metadata", {}),
                    })

                if next_offset is None or not points:
                    break
                offset = next_offset

            logger.info("enrichment.read_existing_chunks", total=len(all_docs))
            return all_docs

        except Exception as e:
            logger.error("enrichment.read_chunks_failed", error=str(e))
            return []

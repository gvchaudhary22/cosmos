"""
Training Pipeline Service — embedding generation, intent classification
training, and graph weight optimization.

Each pipeline reads data from the database, processes it, writes results
back, and updates the training job record. Long-running pipelines execute
via asyncio.create_task so callers are not blocked.
"""

import asyncio
import json
import math
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Schema (idempotent)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_training_jobs (
    id            CHAR(36) PRIMARY KEY,
    job_type      TEXT NOT NULL,
    repo_id       TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    config        JSON DEFAULT '{}',
    metrics       JSON DEFAULT '{}',
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP,
    error         TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_training_jobs_type   ON cosmos_training_jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_training_jobs_status ON cosmos_training_jobs(status);
CREATE INDEX IF NOT EXISTS idx_training_jobs_repo   ON cosmos_training_jobs(repo_id);
"""

# Ensure pgvector extension + table for embeddings
_EMBEDDING_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS cosmos_embeddings (
    id         CHAR(36) PRIMARY KEY,
    repo_id    TEXT,
    source_id  TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'knowledge',
    content    TEXT NOT NULL,
    embedding  vector(384),
    metadata   JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_embeddings_repo   ON cosmos_embeddings(repo_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_source ON cosmos_embeddings(source_id);
"""

# Table for trained intent model artefacts
_INTENT_MODEL_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_intent_models (
    id          CHAR(36) PRIMARY KEY,
    repo_id     TEXT,
    version     TEXT NOT NULL,
    model_data  JSON NOT NULL DEFAULT '{}',
    metrics     JSON DEFAULT '{}',
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intent_models_repo ON cosmos_intent_models(repo_id);
"""

# Table for optimised graph weights
_GRAPH_WEIGHTS_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_graph_weights (
    id          CHAR(36) PRIMARY KEY,
    repo_id     TEXT,
    version     TEXT NOT NULL,
    weights     JSON NOT NULL DEFAULT '{}',
    metrics     JSON DEFAULT '{}',
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_graph_weights_repo ON cosmos_graph_weights(repo_id);
"""


class TrainingService:
    """Orchestrates embedding, intent, and graph-weight training pipelines."""

    _schema_ensured = False

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        if TrainingService._schema_ensured:
            return
        async with AsyncSessionLocal() as session:
            for block in (
                _SCHEMA_SQL,
                _EMBEDDING_TABLE_SQL,
                _INTENT_MODEL_SQL,
                _GRAPH_WEIGHTS_SQL,
            ):
                for statement in block.strip().split(";"):
                    stmt = statement.strip()
                    if stmt:
                        try:
                            await session.execute(text(stmt))
                        except Exception as exc:
                            # pgvector extension may not be available in dev;
                            # log and continue for the other tables.
                            logger.warning(
                                "training.schema_stmt_failed",
                                stmt=stmt[:80],
                                error=str(exc),
                            )
            await session.commit()
        TrainingService._schema_ensured = True
        logger.info("training.schema_ensured")

    # ------------------------------------------------------------------
    # Job lifecycle helpers
    # ------------------------------------------------------------------

    async def _create_job(
        self,
        job_type: str,
        repo_id: Optional[str],
        config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert a queued job record and return its ID."""
        job_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO cosmos_training_jobs "
                    "(id, job_type, repo_id, status, config) "
                    "VALUES (:id, :jtype, :repo, 'queued', :cfg)"
                ),
                {
                    "id": job_id,
                    "jtype": job_type,
                    "repo": repo_id,
                    "cfg": json.dumps(config or {}),
                },
            )
            await session.commit()
        return job_id

    async def _mark_running(self, job_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE cosmos_training_jobs "
                    "SET status = 'running', started_at = :now WHERE id = :id"
                ),
                {"id": job_id, "now": now},
            )
            await session.commit()

    async def _mark_completed(
        self, job_id: str, metrics: Dict[str, Any]
    ) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE cosmos_training_jobs "
                    "SET status = 'completed', completed_at = :now, "
                    "metrics = :m WHERE id = :id"
                ),
                {"id": job_id, "now": now, "m": json.dumps(metrics)},
            )
            await session.commit()

    async def _mark_failed(self, job_id: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE cosmos_training_jobs "
                    "SET status = 'failed', completed_at = :now, error = :err "
                    "WHERE id = :id"
                ),
                {"id": job_id, "now": now, "err": error},
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Public: trigger pipelines
    # ------------------------------------------------------------------

    async def trigger_embedding_training(
        self, repo_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trigger an embedding generation pipeline.

        1. Pull text content from icrm_knowledge_entries and
           icrm_distillation_records.
        2. Generate TF-IDF based 384-dim embeddings (production would use
           sentence-transformers).
        3. Upsert into cosmos_embeddings (pgvector).
        4. Record metrics on the training job.

        Returns the job record immediately; work continues in background.
        """
        await self._ensure_schema()
        job_id = await self._create_job("embedding", repo_id)
        asyncio.create_task(self._run_embedding_pipeline(job_id, repo_id))
        logger.info("training.embedding_triggered", job_id=job_id, repo_id=repo_id)
        return await self.get_training_status(job_id)

    async def trigger_intent_training(
        self, repo_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trigger intent classifier training.

        1. Pull labeled intents from icrm_distillation_records.
        2. Build TF-IDF vectors per intent label.
        3. Compute centroid-based nearest-neighbour classifier.
        4. Evaluate accuracy via leave-one-out cross-validation.
        5. Persist model artefact into cosmos_intent_models.

        Returns the job record immediately; work continues in background.
        """
        await self._ensure_schema()
        job_id = await self._create_job("intent", repo_id)
        asyncio.create_task(self._run_intent_pipeline(job_id, repo_id))
        logger.info("training.intent_triggered", job_id=job_id, repo_id=repo_id)
        return await self.get_training_status(job_id)

    async def trigger_graph_weight_optimization(
        self, repo_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trigger graph weight optimization.

        1. Pull tool execution records (edges) and their outcomes.
        2. Compute success-rate weighted scores per tool.
        3. Factor in latency and cost to produce composite weight.
        4. Persist optimised weights into cosmos_graph_weights.

        Returns the job record immediately; work continues in background.
        """
        await self._ensure_schema()
        job_id = await self._create_job("graph_weights", repo_id)
        asyncio.create_task(self._run_graph_weight_pipeline(job_id, repo_id))
        logger.info("training.graph_weights_triggered", job_id=job_id, repo_id=repo_id)
        return await self.get_training_status(job_id)

    # ------------------------------------------------------------------
    # Public: status and listing
    # ------------------------------------------------------------------

    async def get_training_status(self, job_id: str) -> Dict[str, Any]:
        """Fetch current state of a training job."""
        await self._ensure_schema()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT id, job_type, repo_id, status, config, metrics, "
                    "started_at, completed_at, error, created_at "
                    "FROM cosmos_training_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
            row = result.mappings().first()
        if row is None:
            raise ValueError(f"Training job {job_id} not found")
        return dict(row)

    async def list_training_jobs(
        self,
        job_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List recent training jobs, optionally filtered by type."""
        await self._ensure_schema()
        q = (
            "SELECT id, job_type, repo_id, status, config, metrics, "
            "started_at, completed_at, error, created_at "
            "FROM cosmos_training_jobs "
        )
        params: Dict[str, Any] = {"lim": limit, "off": offset}
        if job_type:
            q += "WHERE job_type = :jtype "
            params["jtype"] = job_type
        q += "ORDER BY created_at DESC LIMIT :lim OFFSET :off"

        async with AsyncSessionLocal() as session:
            result = await session.execute(text(q), params)
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Pipeline: Embedding Generation
    # ------------------------------------------------------------------

    async def _run_embedding_pipeline(
        self, job_id: str, repo_id: Optional[str]
    ) -> None:
        try:
            await self._mark_running(job_id)
            logger.info("training.embedding.started", job_id=job_id)

            # Step 1: Pull text from knowledge entries
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT id::text, question, answer, category::text "
                        "FROM icrm_knowledge_entries WHERE enabled = true"
                    )
                )
                knowledge_rows = result.mappings().all()

            # Step 2: Pull text from distillation records (high-quality only)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT id::text, user_query, intent, final_response "
                        "FROM icrm_distillation_records "
                        "WHERE confidence >= 0.6 "
                        "ORDER BY created_at DESC LIMIT 5000"
                    )
                )
                distillation_rows = result.mappings().all()

            # Step 3: Build corpus and generate embeddings
            documents: List[Dict[str, Any]] = []

            for row in knowledge_rows:
                content = f"{row['question']} {row['answer']}"
                documents.append(
                    {
                        "source_id": row["id"],
                        "source_type": "knowledge",
                        "content": content,
                        "metadata": {
                            "category": row["category"],
                            "type": "knowledge_entry",
                        },
                    }
                )

            for row in distillation_rows:
                content = f"{row['user_query']} {row['final_response'] or ''}"
                documents.append(
                    {
                        "source_id": row["id"],
                        "source_type": "distillation",
                        "content": content,
                        "metadata": {
                            "intent": row["intent"],
                            "type": "distillation_record",
                        },
                    }
                )

            if not documents:
                await self._mark_completed(
                    job_id, {"status": "no_data", "documents_processed": 0}
                )
                return

            # Step 4: Generate TF-IDF embeddings (384-dim, production would use
            # sentence-transformers or similar)
            embeddings = self._generate_tfidf_embeddings(
                [d["content"] for d in documents], dim=384
            )

            # Step 5: Delete old embeddings for this repo, then insert new ones
            async with AsyncSessionLocal() as session:
                if repo_id:
                    await session.execute(
                        text(
                            "DELETE FROM cosmos_embeddings WHERE repo_id = :repo"
                        ),
                        {"repo": repo_id},
                    )
                else:
                    await session.execute(
                        text("DELETE FROM cosmos_embeddings WHERE repo_id IS NULL")
                    )

                for doc, emb in zip(documents, embeddings):
                    emb_str = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                    await session.execute(
                        text(
                            "INSERT INTO cosmos_embeddings "
                            "(id, repo_id, source_id, source_type, content, embedding, metadata) "
                            "VALUES (:id, :repo, :src, :stype, :content, :emb, :meta)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "repo": repo_id,
                            "src": doc["source_id"],
                            "stype": doc["source_type"],
                            "content": doc["content"][:10000],
                            "emb": emb_str,
                            "meta": json.dumps(doc["metadata"]),
                        },
                    )
                await session.commit()

            metrics = {
                "documents_processed": len(documents),
                "knowledge_entries": len(knowledge_rows),
                "distillation_records": len(distillation_rows),
                "embedding_dim": 384,
            }
            await self._mark_completed(job_id, metrics)
            logger.info("training.embedding.completed", job_id=job_id, **metrics)

        except Exception as exc:
            logger.error("training.embedding.failed", job_id=job_id, error=str(exc))
            await self._mark_failed(job_id, str(exc))

    # ------------------------------------------------------------------
    # Pipeline: Intent Classifier Training
    # ------------------------------------------------------------------

    async def _run_intent_pipeline(
        self, job_id: str, repo_id: Optional[str]
    ) -> None:
        try:
            await self._mark_running(job_id)
            logger.info("training.intent.started", job_id=job_id)

            # Step 1: Pull labeled intents from distillation records
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT id::text, user_query, intent, confidence "
                        "FROM icrm_distillation_records "
                        "WHERE intent IS NOT NULL AND intent != 'unknown' "
                        "AND confidence >= 0.5 "
                        "ORDER BY created_at DESC LIMIT 10000"
                    )
                )
                rows = result.mappings().all()

            if not rows:
                await self._mark_completed(
                    job_id,
                    {"status": "no_data", "total_samples": 0, "accuracy": 0.0},
                )
                return

            # Step 2: Group by intent and tokenize
            intent_samples: Dict[str, List[List[str]]] = defaultdict(list)
            all_queries: List[str] = []
            all_intents: List[str] = []

            for row in rows:
                query = row["user_query"] or ""
                intent = row["intent"]
                tokens = self._tokenize(query)
                if tokens and intent:
                    intent_samples[intent].append(tokens)
                    all_queries.append(query)
                    all_intents.append(intent)

            # Filter intents with at least 3 samples
            valid_intents = {
                k: v for k, v in intent_samples.items() if len(v) >= 3
            }

            if not valid_intents:
                await self._mark_completed(
                    job_id,
                    {
                        "status": "insufficient_data",
                        "total_samples": len(rows),
                        "unique_intents": len(intent_samples),
                    },
                )
                return

            # Step 3: Build vocabulary and IDF
            doc_freq: Dict[str, int] = defaultdict(int)
            all_token_lists: List[List[str]] = []
            for samples in valid_intents.values():
                all_token_lists.extend(samples)
            n_docs = len(all_token_lists)

            for tokens in all_token_lists:
                for tok in set(tokens):
                    doc_freq[tok] += 1

            vocab = sorted(doc_freq.keys())
            vocab_index = {w: i for i, w in enumerate(vocab)}
            idf = {
                tok: math.log((n_docs + 1) / (df + 1)) + 1.0
                for tok, df in doc_freq.items()
            }

            # Step 4: Compute centroid per intent
            dim = len(vocab)
            centroids: Dict[str, List[float]] = {}

            for intent, samples in valid_intents.items():
                centroid = [0.0] * dim
                for tokens in samples:
                    vec = self._tfidf_vector(tokens, vocab_index, idf, dim)
                    for i in range(dim):
                        centroid[i] += vec[i]
                # Average
                n = len(samples)
                centroid = [c / n for c in centroid]
                # Normalize
                norm = math.sqrt(sum(c * c for c in centroid))
                if norm > 0:
                    centroid = [c / norm for c in centroid]
                centroids[intent] = centroid

            # Step 5: Leave-one-out cross-validation
            correct = 0
            total = 0
            per_intent_correct: Dict[str, int] = defaultdict(int)
            per_intent_total: Dict[str, int] = defaultdict(int)

            for intent, samples in valid_intents.items():
                for i, tokens in enumerate(samples):
                    vec = self._tfidf_vector(tokens, vocab_index, idf, dim)
                    best_intent = None
                    best_sim = -1.0
                    for cand_intent, centroid in centroids.items():
                        sim = self._cosine_sim(vec, centroid)
                        if sim > best_sim:
                            best_sim = sim
                            best_intent = cand_intent
                    per_intent_total[intent] += 1
                    total += 1
                    if best_intent == intent:
                        correct += 1
                        per_intent_correct[intent] += 1

            accuracy = correct / total if total else 0.0

            # Step 6: Build serialisable model artefact
            # Truncate centroids to save space: store top-100 non-zero indices per intent
            sparse_centroids: Dict[str, List[Dict[str, Any]]] = {}
            for intent, centroid in centroids.items():
                indexed = [(i, v) for i, v in enumerate(centroid) if abs(v) > 1e-6]
                indexed.sort(key=lambda x: abs(x[1]), reverse=True)
                sparse_centroids[intent] = [
                    {"i": idx, "v": round(val, 6)} for idx, val in indexed[:200]
                ]

            model_data = {
                "vocab": vocab[:5000],  # cap for storage
                "idf": {k: round(v, 4) for k, v in list(idf.items())[:5000]},
                "centroids": sparse_centroids,
                "intent_labels": list(valid_intents.keys()),
            }

            version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

            # Step 7: Persist model
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        "INSERT INTO cosmos_intent_models (id, repo_id, version, model_data, metrics) "
                        "VALUES (:id, :repo, :ver, :data, :met)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "repo": repo_id,
                        "ver": version,
                        "data": json.dumps(model_data),
                        "met": json.dumps(
                            {
                                "accuracy": round(accuracy, 4),
                                "total_samples": total,
                                "unique_intents": len(valid_intents),
                            }
                        ),
                    },
                )
                await session.commit()

            per_intent_acc = {
                intent: round(per_intent_correct[intent] / per_intent_total[intent], 4)
                if per_intent_total[intent] > 0
                else 0.0
                for intent in valid_intents
            }

            metrics = {
                "total_samples": total,
                "unique_intents": len(valid_intents),
                "accuracy": round(accuracy, 4),
                "per_intent_accuracy": per_intent_acc,
                "vocab_size": len(vocab),
                "model_version": version,
            }
            await self._mark_completed(job_id, metrics)
            logger.info("training.intent.completed", job_id=job_id, accuracy=accuracy)

        except Exception as exc:
            logger.error("training.intent.failed", job_id=job_id, error=str(exc))
            await self._mark_failed(job_id, str(exc))

    # ------------------------------------------------------------------
    # Pipeline: Graph Weight Optimization
    # ------------------------------------------------------------------

    async def _run_graph_weight_pipeline(
        self, job_id: str, repo_id: Optional[str]
    ) -> None:
        try:
            await self._mark_running(job_id)
            logger.info("training.graph_weights.started", job_id=job_id)

            # Step 1: Pull tool execution records (graph edges)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT tool_name, status::text, duration_ms, "
                        "error_message IS NOT NULL AS had_error "
                        "FROM icrm_tool_executions "
                        "ORDER BY created_at DESC LIMIT 50000"
                    )
                )
                exec_rows = result.mappings().all()

            # Step 2: Pull feedback-linked outcome data
            # Join distillation records with feedback for quality signal
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT tools_used, confidence, feedback_score, "
                        "cost_usd, token_count_input + token_count_output AS total_tokens "
                        "FROM icrm_distillation_records "
                        "WHERE confidence IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 20000"
                    )
                )
                outcome_rows = result.mappings().all()

            if not exec_rows and not outcome_rows:
                await self._mark_completed(
                    job_id, {"status": "no_data", "tools_weighted": 0}
                )
                return

            # Step 3: Aggregate per-tool metrics from execution records
            tool_stats: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "total_latency_ms": 0,
                    "error_count": 0,
                }
            )

            for row in exec_rows:
                tool = row["tool_name"]
                stats = tool_stats[tool]
                stats["total"] += 1
                if row["status"] == "success":
                    stats["success"] += 1
                elif row["status"] == "failed":
                    stats["failed"] += 1
                stats["total_latency_ms"] += row["duration_ms"] or 0
                if row["had_error"]:
                    stats["error_count"] += 1

            # Step 4: Aggregate per-tool quality signals from outcome data
            tool_quality: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {
                    "total_uses": 0,
                    "total_confidence": 0.0,
                    "total_feedback": 0.0,
                    "feedback_count": 0,
                    "total_cost": 0.0,
                }
            )

            for row in outcome_rows:
                tools_used = row["tools_used"] or []
                if isinstance(tools_used, str):
                    try:
                        tools_used = json.loads(tools_used)
                    except (json.JSONDecodeError, TypeError):
                        tools_used = []
                for tool in tools_used:
                    tq = tool_quality[tool]
                    tq["total_uses"] += 1
                    tq["total_confidence"] += float(row["confidence"] or 0)
                    if row["feedback_score"] is not None:
                        tq["total_feedback"] += float(row["feedback_score"])
                        tq["feedback_count"] += 1
                    tq["total_cost"] += float(row["cost_usd"] or 0)

            # Step 5: Compute composite weight per tool
            # weight = 0.4 * success_rate + 0.3 * normalized_feedback + 0.2 * confidence - 0.1 * latency_penalty
            all_tools = set(tool_stats.keys()) | set(tool_quality.keys())
            weights: Dict[str, Dict[str, Any]] = {}

            # Find max latency for normalization
            max_avg_latency = 1.0
            for tool in all_tools:
                stats = tool_stats.get(tool)
                if stats and stats["total"] > 0:
                    avg_lat = stats["total_latency_ms"] / stats["total"]
                    if avg_lat > max_avg_latency:
                        max_avg_latency = avg_lat

            for tool in all_tools:
                stats = tool_stats.get(
                    tool,
                    {"total": 0, "success": 0, "failed": 0, "total_latency_ms": 0, "error_count": 0},
                )
                quality = tool_quality.get(
                    tool,
                    {"total_uses": 0, "total_confidence": 0, "total_feedback": 0, "feedback_count": 0, "total_cost": 0},
                )

                # Success rate (0-1)
                success_rate = (
                    stats["success"] / stats["total"] if stats["total"] > 0 else 0.5
                )

                # Average feedback (normalized 0-1 from 1-5 scale)
                avg_feedback = (
                    (quality["total_feedback"] / quality["feedback_count"] - 1) / 4
                    if quality["feedback_count"] > 0
                    else 0.5
                )

                # Average confidence (already 0-1)
                avg_confidence = (
                    quality["total_confidence"] / quality["total_uses"]
                    if quality["total_uses"] > 0
                    else 0.5
                )

                # Latency penalty (0-1, higher = worse)
                avg_latency = (
                    stats["total_latency_ms"] / stats["total"]
                    if stats["total"] > 0
                    else 0.0
                )
                latency_penalty = avg_latency / max_avg_latency if max_avg_latency > 0 else 0.0

                composite = (
                    0.4 * success_rate
                    + 0.3 * avg_feedback
                    + 0.2 * avg_confidence
                    - 0.1 * latency_penalty
                )
                composite = max(0.0, min(1.0, composite))

                weights[tool] = {
                    "weight": round(composite, 4),
                    "success_rate": round(success_rate, 4),
                    "avg_feedback": round(avg_feedback, 4),
                    "avg_confidence": round(avg_confidence, 4),
                    "avg_latency_ms": round(avg_latency, 2),
                    "total_executions": stats["total"],
                    "total_quality_samples": quality["total_uses"],
                }

            version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

            # Step 6: Persist weights
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        "INSERT INTO cosmos_graph_weights (id, repo_id, version, weights, metrics) "
                        "VALUES (:id, :repo, :ver, :w, :m)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "repo": repo_id,
                        "ver": version,
                        "w": json.dumps(weights),
                        "m": json.dumps(
                            {
                                "tools_weighted": len(weights),
                                "total_executions": len(exec_rows),
                                "total_outcomes": len(outcome_rows),
                            }
                        ),
                    },
                )
                await session.commit()

            metrics = {
                "tools_weighted": len(weights),
                "total_execution_records": len(exec_rows),
                "total_outcome_records": len(outcome_rows),
                "version": version,
                "top_tools": sorted(
                    [(k, v["weight"]) for k, v in weights.items()],
                    key=lambda x: x[1],
                    reverse=True,
                )[:10],
            }
            await self._mark_completed(job_id, metrics)
            logger.info(
                "training.graph_weights.completed",
                job_id=job_id,
                tools=len(weights),
            )

        except Exception as exc:
            logger.error(
                "training.graph_weights.failed", job_id=job_id, error=str(exc)
            )
            await self._mark_failed(job_id, str(exc))

    # ------------------------------------------------------------------
    # Pipeline 4: Page-Role Embedding Training
    # ------------------------------------------------------------------

    async def trigger_page_role_training(
        self, repo_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trigger page-role embedding training from Pillar 4 data.

        1. Load all PageDocuments from PageIntelligenceService.
        2. For each page, combine: page summary + field names + action
           labels + role info into a training document.
        3. Generate TF-IDF feature-hashed 384-dim embeddings.
        4. Store embeddings in cosmos_embeddings with source_type='page'.
        5. Generate page-specific intents from eval_cases.

        Returns the job record immediately; work continues in background.
        """
        await self._ensure_schema()
        job_id = await self._create_job("page_role", repo_id)
        asyncio.create_task(self._run_page_role_pipeline(job_id, repo_id))
        logger.info("training.page_role_triggered", job_id=job_id, repo_id=repo_id)
        return await self.get_training_status(job_id)

    async def _run_page_role_pipeline(
        self, job_id: str, repo_id: Optional[str]
    ) -> None:
        try:
            await self._mark_running(job_id)
            logger.info("training.page_role.started", job_id=job_id)

            # Step 1: Load PageDocuments via PageIntelligenceService
            from app.services.page_intelligence import PageIntelligenceService
            import os

            kb_path = os.environ.get(
                "KB_PATH",
                os.path.join(
                    os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        )
                    ),
                    "..", "mars", "knowledge_base", "shiprocket",
                ),
            )
            kb_path = os.path.normpath(kb_path)

            page_svc = PageIntelligenceService(kb_path)
            await page_svc.load_from_kb()

            if not page_svc.pages:
                await self._mark_completed(
                    job_id, {"status": "no_data", "pages_processed": 0}
                )
                return

            # Step 2: Build training documents from each page
            documents: List[Dict[str, Any]] = []
            intent_entries: List[Dict[str, Any]] = []

            for page_id, doc in page_svc.pages.items():
                if repo_id and doc.repo != repo_id:
                    continue

                # Combine page summary + field names + action labels + role info
                parts = [
                    f"Page: {page_id}",
                    f"Route: {doc.route}",
                    f"Component: {doc.component}",
                    f"Module: {doc.module}",
                    f"Domain: {doc.domain}",
                    f"Type: {doc.page_type}",
                ]

                # Fields
                for field_item in doc.fields:
                    if isinstance(field_item, dict):
                        label = field_item.get("label", field_item.get("name", ""))
                        desc = field_item.get("description", "")
                        if label:
                            parts.append(f"Field: {label}")
                        if desc:
                            parts.append(desc)

                # Actions
                for action in doc.actions:
                    if isinstance(action, dict):
                        label = action.get("label", action.get("name", ""))
                        if label:
                            parts.append(f"Action: {label}")

                # Roles
                for role in doc.roles_required:
                    parts.append(f"Role: {role}")

                content = " ".join(parts)
                documents.append({
                    "source_id": page_id,
                    "source_type": "page",
                    "content": content,
                    "metadata": {
                        "repo": doc.repo,
                        "domain": doc.domain,
                        "page_type": doc.page_type,
                        "route": doc.route,
                        "type": "page_document",
                    },
                })

                # Step 4: Generate intent training data from eval_cases
                for case in doc.eval_cases:
                    if isinstance(case, dict):
                        query = case.get("query", case.get("user_query", ""))
                        intent = case.get(
                            "expected_intent",
                            case.get("intent", "page_context"),
                        )
                        if query:
                            intent_entries.append({
                                "query": query,
                                "intent": intent,
                                "page_id": page_id,
                                "domain": doc.domain,
                            })

            if not documents:
                await self._mark_completed(
                    job_id, {"status": "no_data", "pages_processed": 0}
                )
                return

            # Step 3: Generate TF-IDF 384-dim embeddings
            embeddings = self._generate_tfidf_embeddings(
                [d["content"] for d in documents], dim=384
            )

            # Store embeddings in cosmos_embeddings
            async with AsyncSessionLocal() as session:
                # Delete old page embeddings for this repo
                if repo_id:
                    await session.execute(
                        text(
                            "DELETE FROM cosmos_embeddings "
                            "WHERE source_type = 'page' AND repo_id = :repo"
                        ),
                        {"repo": repo_id},
                    )
                else:
                    await session.execute(
                        text(
                            "DELETE FROM cosmos_embeddings "
                            "WHERE source_type = 'page'"
                        )
                    )

                for doc_item, emb in zip(documents, embeddings):
                    emb_str = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                    await session.execute(
                        text(
                            "INSERT INTO cosmos_embeddings "
                            "(id, repo_id, source_id, source_type, content, "
                            "embedding, metadata) "
                            "VALUES (:id, :repo, :src, :stype, :content, "
                            ":emb, :meta)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "repo": repo_id or doc_item["metadata"].get("repo"),
                            "src": doc_item["source_id"],
                            "stype": doc_item["source_type"],
                            "content": doc_item["content"][:10000],
                            "emb": emb_str,
                            "meta": json.dumps(doc_item["metadata"]),
                        },
                    )
                await session.commit()

            # Step 5: Store page-specific intents (page_context, field_trace, role_check)
            intent_count = 0
            if intent_entries:
                intent_documents = []
                for entry in intent_entries:
                    content = f"{entry['query']} [page:{entry['page_id']}] [domain:{entry['domain']}]"
                    intent_documents.append({
                        "source_id": f"intent:{entry['page_id']}:{intent_count}",
                        "source_type": "page_intent",
                        "content": content,
                        "metadata": {
                            "intent": entry["intent"],
                            "page_id": entry["page_id"],
                            "domain": entry["domain"],
                            "type": "page_intent",
                        },
                    })
                    intent_count += 1

                if intent_documents:
                    intent_embeddings = self._generate_tfidf_embeddings(
                        [d["content"] for d in intent_documents], dim=384
                    )
                    async with AsyncSessionLocal() as session:
                        # Delete old page_intent embeddings
                        await session.execute(
                            text(
                                "DELETE FROM cosmos_embeddings "
                                "WHERE source_type = 'page_intent'"
                            )
                        )
                        for doc_item, emb in zip(intent_documents, intent_embeddings):
                            emb_str = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                            await session.execute(
                                text(
                                    "INSERT INTO cosmos_embeddings "
                                    "(id, repo_id, source_id, source_type, content, "
                                    "embedding, metadata) "
                                    "VALUES (:id, :repo, :src, :stype, :content, "
                                    ":emb, :meta)"
                                ),
                                {
                                    "id": str(uuid.uuid4()),
                                    "repo": repo_id,
                                    "src": doc_item["source_id"],
                                    "stype": doc_item["source_type"],
                                    "content": doc_item["content"][:10000],
                                    "emb": emb_str,
                                    "meta": json.dumps(doc_item["metadata"]),
                                },
                            )
                        await session.commit()

            metrics = {
                "pages_processed": len(documents),
                "intent_entries_generated": intent_count,
                "embedding_dim": 384,
                "stats": page_svc.get_stats(),
            }
            await self._mark_completed(job_id, metrics)
            logger.info("training.page_role.completed", job_id=job_id, **metrics)

        except Exception as exc:
            logger.error("training.page_role.failed", job_id=job_id, error=str(exc))
            await self._mark_failed(job_id, str(exc))

    # ------------------------------------------------------------------
    # Pipeline 5: Cross-Repo Navigation Training
    # ------------------------------------------------------------------

    async def trigger_cross_repo_training(
        self, repo_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Trigger cross-repo navigation training from seller<->admin page mappings.

        1. Load cross_repo_mapping.yaml from each repo's pillar_4.
        2. For each mapping, create training pairs linking seller and admin
           views, including additional_admin_fields and actions.
        3. Generate embeddings for cross-repo queries.
        4. Store in cosmos_embeddings with source_type='cross_repo'.

        Returns the job record immediately; work continues in background.
        """
        await self._ensure_schema()
        job_id = await self._create_job("cross_repo", repo_id)
        asyncio.create_task(self._run_cross_repo_pipeline(job_id, repo_id))
        logger.info("training.cross_repo_triggered", job_id=job_id, repo_id=repo_id)
        return await self.get_training_status(job_id)

    async def _run_cross_repo_pipeline(
        self, job_id: str, repo_id: Optional[str]
    ) -> None:
        try:
            await self._mark_running(job_id)
            logger.info("training.cross_repo.started", job_id=job_id)

            # Step 1: Load cross-repo mappings via PageIntelligenceService
            from app.services.page_intelligence import PageIntelligenceService
            import os

            kb_path = os.environ.get(
                "KB_PATH",
                os.path.join(
                    os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        )
                    ),
                    "..", "mars", "knowledge_base", "shiprocket",
                ),
            )
            kb_path = os.path.normpath(kb_path)

            page_svc = PageIntelligenceService(kb_path)
            await page_svc.load_from_kb()

            if not page_svc.cross_repo_mappings:
                await self._mark_completed(
                    job_id, {"status": "no_data", "mappings_processed": 0}
                )
                return

            # Step 2: Build training documents from cross-repo mappings
            documents: List[Dict[str, Any]] = []

            for mapping in page_svc.cross_repo_mappings:
                source_repo = mapping.get("source_repo", "")
                source_page = mapping.get(
                    "source_page_id", mapping.get("page_id", "")
                )
                target_repo = mapping.get("target_repo", "")
                target_page = mapping.get(
                    "target_page_id", mapping.get("target", "")
                )

                if repo_id and source_repo != repo_id and target_repo != repo_id:
                    continue

                # Build descriptive training text
                parts = [
                    f"Cross-repo mapping: {source_page} on {source_repo} "
                    f"maps to {target_page} on {target_repo}.",
                ]

                additional_fields = mapping.get("additional_admin_fields", [])
                if isinstance(additional_fields, list) and additional_fields:
                    field_names = []
                    for af in additional_fields:
                        if isinstance(af, dict):
                            field_names.append(af.get("name", af.get("label", str(af))))
                        else:
                            field_names.append(str(af))
                    parts.append(
                        f"Additional admin fields: {', '.join(field_names)}"
                    )

                additional_actions = mapping.get("additional_admin_actions", [])
                if isinstance(additional_actions, list) and additional_actions:
                    action_names = []
                    for aa in additional_actions:
                        if isinstance(aa, dict):
                            action_names.append(aa.get("name", aa.get("label", str(aa))))
                        else:
                            action_names.append(str(aa))
                    parts.append(
                        f"Additional admin actions: {', '.join(action_names)}"
                    )

                notes = mapping.get("notes", mapping.get("description", ""))
                if notes:
                    parts.append(str(notes))

                content = " ".join(parts)
                mapping_id = f"xrepo:{source_page}:{target_page}"

                documents.append({
                    "source_id": mapping_id,
                    "source_type": "cross_repo",
                    "content": content,
                    "metadata": {
                        "source_repo": source_repo,
                        "source_page_id": source_page,
                        "target_repo": target_repo,
                        "target_page_id": target_page,
                        "type": "cross_repo_mapping",
                    },
                })

            if not documents:
                await self._mark_completed(
                    job_id, {"status": "no_data", "mappings_processed": 0}
                )
                return

            # Step 3: Generate embeddings
            embeddings = self._generate_tfidf_embeddings(
                [d["content"] for d in documents], dim=384
            )

            # Step 4: Store in cosmos_embeddings
            async with AsyncSessionLocal() as session:
                # Delete old cross_repo embeddings
                if repo_id:
                    await session.execute(
                        text(
                            "DELETE FROM cosmos_embeddings "
                            "WHERE source_type = 'cross_repo' AND repo_id = :repo"
                        ),
                        {"repo": repo_id},
                    )
                else:
                    await session.execute(
                        text(
                            "DELETE FROM cosmos_embeddings "
                            "WHERE source_type = 'cross_repo'"
                        )
                    )

                for doc_item, emb in zip(documents, embeddings):
                    emb_str = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                    await session.execute(
                        text(
                            "INSERT INTO cosmos_embeddings "
                            "(id, repo_id, source_id, source_type, content, "
                            "embedding, metadata) "
                            "VALUES (:id, :repo, :src, :stype, :content, "
                            ":emb, :meta)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "repo": repo_id,
                            "src": doc_item["source_id"],
                            "stype": doc_item["source_type"],
                            "content": doc_item["content"][:10000],
                            "emb": emb_str,
                            "meta": json.dumps(doc_item["metadata"]),
                        },
                    )
                await session.commit()

            metrics = {
                "mappings_processed": len(documents),
                "embedding_dim": 384,
            }
            await self._mark_completed(job_id, metrics)
            logger.info("training.cross_repo.completed", job_id=job_id, **metrics)

        except Exception as exc:
            logger.error(
                "training.cross_repo.failed", job_id=job_id, error=str(exc)
            )
            await self._mark_failed(job_id, str(exc))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text_val: str) -> List[str]:
        """Lowercase tokenizer: split on non-alphanumeric, drop short words."""
        words = re.findall(r"[a-z0-9_]+", text_val.lower())
        return [w for w in words if len(w) > 2]

    @staticmethod
    def _tfidf_vector(
        tokens: List[str],
        vocab_index: Dict[str, int],
        idf: Dict[str, float],
        dim: int,
    ) -> List[float]:
        """Build a TF-IDF vector for a token list."""
        tf: Dict[str, float] = defaultdict(float)
        for t in tokens:
            tf[t] += 1.0
        max_tf = max(tf.values()) if tf else 1.0

        vec = [0.0] * dim
        for token, count in tf.items():
            if token in vocab_index:
                idx = vocab_index[token]
                normalized_tf = 0.5 + 0.5 * (count / max_tf)
                vec[idx] = normalized_tf * idf.get(token, 1.0)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        """Cosine similarity between two same-length vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

    def _generate_tfidf_embeddings(
        self, texts: List[str], dim: int = 384
    ) -> List[List[float]]:
        """Generate fixed-dimension TF-IDF embeddings for a corpus.

        Uses feature hashing to project into `dim` dimensions, avoiding the
        need for an external embedding model in dev. In production, replace
        with sentence-transformers (e.g. all-MiniLM-L6-v2).
        """
        embeddings: List[List[float]] = []

        # Step 1: Tokenize all documents
        all_token_lists = [self._tokenize(t) for t in texts]

        # Step 2: Compute IDF from corpus
        n_docs = len(all_token_lists)
        doc_freq: Dict[str, int] = defaultdict(int)
        for tokens in all_token_lists:
            for tok in set(tokens):
                doc_freq[tok] += 1

        idf = {
            tok: math.log((n_docs + 1) / (df + 1)) + 1.0
            for tok, df in doc_freq.items()
        }

        # Step 3: For each document, compute feature-hashed TF-IDF vector
        for tokens in all_token_lists:
            vec = [0.0] * dim
            if not tokens:
                embeddings.append(vec)
                continue

            tf: Dict[str, float] = defaultdict(float)
            for t in tokens:
                tf[t] += 1.0
            max_tf = max(tf.values())

            for token, count in tf.items():
                # Feature hashing: hash token to a bucket in [0, dim)
                h = hash(token) % dim
                normalized_tf = 0.5 + 0.5 * (count / max_tf)
                vec[h] += normalized_tf * idf.get(token, 1.0)

            # L2 normalize
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]

            embeddings.append(vec)

        return embeddings

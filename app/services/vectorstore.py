"""
Vector store service using pgvector for COSMOS embeddings.

Embedding provider:
  Production: Shiprocket AI Gateway → text-embedding-3-small (1536 dims)
  Dev-only:   Local all-MiniLM-L6-v2 (384 dims) — ONLY if ENV=development
  Test-only:  Deterministic hash — ONLY if ENV=test

Rules:
  - One active table = one embedding dimension (no mixed 384 + 1536)
  - Silent production fallback is BLOCKED (fail loud, not wrong)
  - content_hash for efficient upsert (skip if unchanged)
  - UPSERT on canonical unique key (no duplicates ever)
  - Search ranked by: similarity × trust_score × freshness × capability_fit

Model selection policy:
  - Default production: text-embedding-3-small (1536 dims)
  - Upgrade to text-embedding-3-large only if holdout retrieval@5 improves materially
  - Never switch models without full re-embedding and benchmark comparison
"""

import hashlib
import json
import os
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Environment + Backend Configuration
# ---------------------------------------------------------------------------

ENV = os.environ.get("ENV", "development")

AIGATEWAY_URL = os.environ.get("AIGATEWAY_URL", "https://aigateway.shiprocket.in")
AIGATEWAY_API_KEY = os.environ.get("AIGATEWAY_API_KEY", "")
AIGATEWAY_MODEL = os.environ.get("AIGATEWAY_EMBEDDING_MODEL", "text-embedding-3-small")
AIGATEWAY_PROVIDER = os.environ.get("AIGATEWAY_PROVIDER", "openai")

_MODEL_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "gemini-embedding-001": 768,
    "shunya-embedding-model": 1536,
    "all-MiniLM-L6-v2": 384,
    "hash-fallback": 384,
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_URL = "https://api.openai.com/v1/embeddings"

EMBEDDING_TABLE = "cosmos_embeddings"

# Determine backend and dimension based on environment
if ENV == "test":
    _BACKEND = "hash"
    _EMBEDDING_MODEL = "hash-fallback"
    _EMBEDDING_DIM = 384
    logger.info("vectorstore.test_mode", backend="hash", dim=384)
elif ENV == "development" and not AIGATEWAY_API_KEY:
    # Dev-only: allow local MiniLM if no gateway key
    try:
        from sentence_transformers import SentenceTransformer
        _BACKEND = "local"
        _EMBEDDING_MODEL = "all-MiniLM-L6-v2"
        _EMBEDDING_DIM = 384
        logger.info("vectorstore.dev_mode", backend="local", dim=384)
    except ImportError:
        _BACKEND = "hash"
        _EMBEDDING_MODEL = "hash-fallback"
        _EMBEDDING_DIM = 384
        logger.warning("vectorstore.dev_mode_hash_fallback")
else:
    # Production + any env with gateway key: use AI Gateway ONLY
    if not AIGATEWAY_API_KEY:
        raise RuntimeError(
            "AIGATEWAY_API_KEY is required in production. "
            "Set ENV=development for local fallback."
        )
    _BACKEND = "aigateway"
    _EMBEDDING_MODEL = AIGATEWAY_MODEL
    _EMBEDDING_DIM = _MODEL_DIMS.get(AIGATEWAY_MODEL, 1536)
    logger.info("vectorstore.production_mode", backend="aigateway", model=AIGATEWAY_MODEL, dim=_EMBEDDING_DIM)

EMBEDDING_DIM = _EMBEDDING_DIM

# Lazy-loaded local model (dev only)
_local_model = None


def _get_local_model():
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _local_model


def _hash_embed(text_input: str, dim: int = 384) -> List[float]:
    """Deterministic hash embedding — test mode only."""
    raw = text_input.encode("utf-8")
    rounds = (dim * 4 // 64) + 1
    hash_bytes = b""
    for i in range(rounds):
        hash_bytes += hashlib.sha512(raw + struct.pack(">I", i)).digest()
    floats = list(struct.unpack(f">{dim}f", hash_bytes[:dim * 4]))
    floats = [0.0 if (f != f or abs(f) == float("inf")) else f for f in floats]
    norm = sum(x * x for x in floats) ** 0.5
    if norm > 0:
        floats = [x / norm for x in floats]
    return floats


def _compute_content_hash(content: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# VectorStoreService
# ---------------------------------------------------------------------------

class VectorStoreService:
    """Async vector store backed by pgvector in PostgreSQL."""

    def __init__(self, session_factory=None):
        self._session_factory = session_factory

    async def ensure_schema(self) -> None:
        """Create pgvector extension and converged embeddings table."""
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {EMBEDDING_TABLE} (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        repo_id VARCHAR(255) NOT NULL DEFAULT '',
                        entity_type VARCHAR(255) NOT NULL,
                        entity_id VARCHAR(500) NOT NULL,
                        capability VARCHAR(50) NOT NULL DEFAULT 'retrieval',
                        content TEXT NOT NULL,
                        content_hash VARCHAR(32) NOT NULL DEFAULT '',
                        embedding vector({EMBEDDING_DIM}),
                        trust_score FLOAT NOT NULL DEFAULT 0.5,
                        freshness TIMESTAMPTZ,
                        embedding_model VARCHAR(100) NOT NULL DEFAULT '{_EMBEDDING_MODEL}',
                        embedding_version VARCHAR(50) NOT NULL DEFAULT 'v1',
                        embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """))

                # Canonical unique key for upsert
                await session.execute(text(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_identity
                    ON {EMBEDDING_TABLE} (repo_id, entity_type, entity_id)
                """))

                # Search + filter indexes
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_embeddings_entity_type
                    ON {EMBEDDING_TABLE} (entity_type)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_embeddings_repo
                    ON {EMBEDDING_TABLE} (repo_id)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_embeddings_trust
                    ON {EMBEDDING_TABLE} (trust_score)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_embeddings_capability
                    ON {EMBEDDING_TABLE} (capability)
                """))

                await session.commit()
                logger.info("schema_ensured", dim=EMBEDDING_DIM, model=_EMBEDDING_MODEL, env=ENV)
            except Exception as exc:
                await session.rollback()
                logger.error("schema_ensure_failed", error=str(exc))
                raise

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed_text(self, content: str) -> List[float]:
        """
        Compute embedding using environment-appropriate backend.

        Production: AI Gateway ONLY (no silent fallback — fail loud)
        Dev: local MiniLM
        Test: deterministic hash
        """
        if _BACKEND == "aigateway":
            return self._embed_via_gateway(content)
        elif _BACKEND == "local":
            model = _get_local_model()
            return model.encode(content, normalize_embeddings=True).tolist()
        else:
            return _hash_embed(content, EMBEDDING_DIM)

    def _embed_via_gateway(self, content: str) -> List[float]:
        """Call AI Gateway with OpenAI direct fallback + retry.

        Strategy:
          1. Try AI Gateway (Shiprocket) — primary
          2. If 429 (rate limit) → fallback to OpenAI direct API (same model, same dimensions)
          3. Retry with exponential backoff on transient errors (500, 502, 503, timeout)
          4. Never fall back to a different-dimension model (prevents search corruption)
        """
        import httpx

        # Try AI Gateway first
        embedding = self._try_aigateway(content)
        if embedding:
            return embedding

        # Fallback: OpenAI direct API (same model = same dimensions)
        if OPENAI_API_KEY:
            logger.info("vectorstore.openai_fallback", reason="aigateway_failed")
            embedding = self._try_openai_direct(content)
            if embedding:
                return embedding

        # All failed
        raise EmbeddingError("Both AI Gateway and OpenAI direct API failed")

    def _try_aigateway(self, content: str, max_retries: int = 2) -> Optional[List[float]]:
        """Try AI Gateway with retry on transient errors."""
        import httpx

        for attempt in range(max_retries + 1):
            try:
                response = httpx.post(
                    f"{AIGATEWAY_URL}/api/v1/embedding",
                    json={
                        "input": content[:8000],
                        "provider": AIGATEWAY_PROVIDER,
                        "model": AIGATEWAY_MODEL,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": AIGATEWAY_API_KEY,
                    },
                    timeout=15.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        output = data.get("data", {})
                        embedding = output.get("embedding") or output.get("output", {}).get("embedding")
                        if embedding and len(embedding) == EMBEDDING_DIM:
                            return embedding

                # 429 = rate limit — don't retry, fall through to OpenAI
                if response.status_code == 429:
                    logger.warning("aigateway.rate_limited", attempt=attempt)
                    return None

                # 5xx = transient — retry with backoff
                if response.status_code >= 500 and attempt < max_retries:
                    wait = 2 ** attempt  # 1s, 2s
                    logger.warning("aigateway.retry", status=response.status_code, wait=wait)
                    import time
                    time.sleep(wait)
                    continue

                logger.error("aigateway.embed_failed",
                             status=response.status_code, body=response.text[:200])
                return None

            except Exception as e:
                if attempt < max_retries:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                logger.error("aigateway.connection_error", error=str(e))
                return None

        return None

    def _try_openai_direct(self, content: str, max_retries: int = 3) -> Optional[List[float]]:
        """Direct OpenAI API call — same model, same dimensions."""
        import httpx

        for attempt in range(max_retries + 1):
            try:
                response = httpx.post(
                    OPENAI_API_URL,
                    json={
                        "model": AIGATEWAY_MODEL,  # same model = same dimensions
                        "input": content[:8000],
                    },
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    embedding = data["data"][0]["embedding"]
                    if len(embedding) == EMBEDDING_DIM:
                        return embedding
                    logger.error("openai.dim_mismatch", got=len(embedding), expected=EMBEDDING_DIM)
                    return None

                if response.status_code == 429 and attempt < max_retries:
                    # OpenAI rate limit — retry with backoff
                    wait = 2 ** (attempt + 1)
                    logger.warning("openai.rate_limited", wait=wait)
                    import time
                    time.sleep(wait)
                    continue

                logger.error("openai.embed_failed",
                             status=response.status_code, body=response.text[:200])
                return None

            except Exception as e:
                if attempt < max_retries:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                logger.error("openai.connection_error", error=str(e))
                return None

        return None

    def embed_batch(self, texts: List[str], batch_size: int = 50) -> List[List[float]]:
        """Batch embed multiple texts in one API call (50x fewer API calls).

        Uses OpenAI direct API for batch (AI Gateway doesn't support batch).
        Falls back to single-call if batch fails.
        """
        if not texts:
            return []

        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = [t[:8000] for t in texts[i:i + batch_size]]

            # Try batch via OpenAI direct (supports batch natively)
            if OPENAI_API_KEY:
                batch_result = self._batch_openai(batch)
                if batch_result and len(batch_result) == len(batch):
                    all_embeddings.extend(batch_result)
                    continue

            # Fallback: single calls
            for text in batch:
                all_embeddings.append(self.embed_text(text))

        return all_embeddings

    def _batch_openai(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Batch embed via OpenAI direct API (up to 2048 texts per call)."""
        import httpx

        try:
            response = httpx.post(
                OPENAI_API_URL,
                json={
                    "model": AIGATEWAY_MODEL,
                    "input": texts,
                },
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            )

            if response.status_code == 200:
                data = response.json()
                embeddings = [d["embedding"] for d in data["data"]]
                if all(len(e) == EMBEDDING_DIM for e in embeddings):
                    logger.info("openai.batch_embed_ok", count=len(embeddings))
                    return embeddings

            logger.warning("openai.batch_failed", status=response.status_code)
            return None

        except Exception as e:
            logger.warning("openai.batch_error", error=str(e))
            return None

    async def embed_text_async(self, content: str) -> List[float]:
        """Async version for AI Gateway calls."""
        if _BACKEND == "aigateway":
            return await self._embed_via_gateway_async(content)
        return self.embed_text(content)

    async def _embed_via_gateway_async(self, content: str) -> List[float]:
        """Async AI Gateway call with same strict production rules."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{AIGATEWAY_URL}/api/v1/embedding",
                    json={
                        "input": content[:8000],
                        "provider": AIGATEWAY_PROVIDER,
                        "model": AIGATEWAY_MODEL,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": AIGATEWAY_API_KEY,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        output = data.get("data", {})
                        embedding = output.get("embedding") or output.get("output", {}).get("embedding")
                        if embedding and len(embedding) == EMBEDDING_DIM:
                            return embedding

            if ENV == "development":
                return _hash_embed(content, EMBEDDING_DIM)
            raise EmbeddingError(f"Async AI Gateway failed: {response.status_code}")

        except EmbeddingError:
            raise
        except Exception as e:
            if ENV == "development":
                return _hash_embed(content, EMBEDDING_DIM)
            raise EmbeddingError(f"Async gateway error: {e}")

    # ------------------------------------------------------------------
    # Store (UPSERT with content_hash change detection)
    # ------------------------------------------------------------------

    async def store_embedding(
        self,
        entity_type: str,
        entity_id: str,
        content: str,
        repo_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Embed and store with UPSERT + content_hash change detection.

        - If (repo_id, entity_type, entity_id) doesn't exist → INSERT
        - If exists AND content_hash changed → UPDATE embedding + content
        - If exists AND content_hash same → SKIP (no re-embed needed)
        """
        meta = dict(metadata or {})
        trust_score = meta.pop("trust_score", 0.5)
        capability = meta.pop("capability", "retrieval")
        freshness = meta.pop("freshness", None)
        content_hash = _compute_content_hash(content)
        effective_repo = repo_id or ""

        # Check if content changed (skip expensive re-embed if not)
        async with AsyncSessionLocal() as session:
            try:
                existing = await session.execute(
                    text(f"""
                        SELECT content_hash FROM {EMBEDDING_TABLE}
                        WHERE repo_id = :repo_id AND entity_type = :entity_type AND entity_id = :entity_id
                        LIMIT 1
                    """),
                    {"repo_id": effective_repo, "entity_type": entity_type, "entity_id": entity_id},
                )
                row = existing.fetchone()

                if row and row.content_hash == content_hash:
                    # Content unchanged — update trust/meta only, skip re-embed
                    await session.execute(
                        text(f"""
                            UPDATE {EMBEDDING_TABLE}
                            SET trust_score = :trust_score,
                                capability = :capability,
                                metadata = CAST(:metadata AS jsonb)
                            WHERE repo_id = :repo_id AND entity_type = :entity_type AND entity_id = :entity_id
                        """),
                        {
                            "repo_id": effective_repo,
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                            "trust_score": trust_score,
                            "capability": capability,
                            "metadata": json.dumps(meta),
                        },
                    )
                    await session.commit()
                    return "skipped_unchanged"

                # Content changed or new — embed and upsert
                embedding = self.embed_text(content)
                row_id = str(uuid.uuid4())

                await session.execute(
                    text(f"""
                        INSERT INTO {EMBEDDING_TABLE}
                            (id, repo_id, entity_type, entity_id, capability, content,
                             content_hash, embedding, trust_score, freshness,
                             embedding_model, embedding_version, embedded_at, metadata)
                        VALUES
                            (:id, :repo_id, :entity_type, :entity_id, :capability, :content,
                             :content_hash, :embedding, :trust_score, :freshness,
                             :embedding_model, :embedding_version, now(), CAST(:metadata AS jsonb))
                        ON CONFLICT (repo_id, entity_type, entity_id)
                        DO UPDATE SET
                            content = EXCLUDED.content,
                            content_hash = EXCLUDED.content_hash,
                            embedding = EXCLUDED.embedding,
                            trust_score = EXCLUDED.trust_score,
                            capability = EXCLUDED.capability,
                            freshness = EXCLUDED.freshness,
                            embedding_model = EXCLUDED.embedding_model,
                            embedded_at = now(),
                            metadata = EXCLUDED.metadata
                    """),
                    {
                        "id": row_id,
                        "repo_id": effective_repo,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "capability": capability,
                        "content": content,
                        "content_hash": content_hash,
                        "embedding": str(embedding),
                        "trust_score": trust_score,
                        "freshness": freshness,
                        "embedding_model": _EMBEDDING_MODEL,
                        "embedding_version": "v1",
                        "metadata": json.dumps(meta),
                    },
                )
                await session.commit()
                return row_id

            except EmbeddingError:
                await session.rollback()
                raise
            except Exception as exc:
                await session.rollback()
                logger.error("store_embedding_failed", entity_id=entity_id[:60], error=str(exc))
                raise

    # ------------------------------------------------------------------
    # Search (trust-weighted + freshness + capability ranking)
    # ------------------------------------------------------------------

    async def search_similar(
        self,
        query: str,
        limit: int = 5,
        entity_type: Optional[str] = None,
        repo_id: Optional[str] = None,
        capability: Optional[str] = None,
        threshold: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Search with multi-signal ranking:
          relevance = similarity × trust_score × freshness_weight × capability_weight

        freshness_weight: 1.0 if embedded in last 7 days, decays to 0.7 over 90 days
        capability_weight: 1.0 if matches requested capability, 0.8 otherwise
        """
        query_embedding = self.embed_text(query)

        filters = []
        params: Dict[str, Any] = {
            "embedding": str(query_embedding),
            "limit": limit,
        }

        if entity_type:
            filters.append("entity_type = :entity_type")
            params["entity_type"] = entity_type
        if repo_id:
            filters.append("repo_id = :repo_id")
            params["repo_id"] = repo_id
        if capability:
            filters.append("capability = :capability")
            params["capability"] = capability

        where_clause = "WHERE " + " AND ".join(filters) if filters else ""

        async with AsyncSessionLocal() as session:
            try:
                # M4 fix: full multi-signal ranking with freshness + capability weight
                cap_clause = ""
                if capability:
                    cap_clause = f"""
                              * CASE WHEN capability = '{capability}' THEN 1.0 ELSE 0.8 END"""

                result = await session.execute(
                    text(f"""
                        SELECT
                            id, repo_id, entity_type, entity_id, capability, content, metadata,
                            trust_score, embedding_model, embedded_at, freshness,
                            1 - (embedding <=> :embedding::vector) AS similarity,
                            (1 - (embedding <=> :embedding::vector))
                              * COALESCE(trust_score, 0.5)
                              * CASE
                                  WHEN COALESCE(freshness, embedded_at) > now() - interval '7 days' THEN 1.0
                                  WHEN COALESCE(freshness, embedded_at) > now() - interval '30 days' THEN 0.9
                                  WHEN COALESCE(freshness, embedded_at) > now() - interval '90 days' THEN 0.8
                                  ELSE 0.7
                                END
                              {cap_clause}
                            AS relevance
                        FROM {EMBEDDING_TABLE}
                        {where_clause}
                        ORDER BY relevance DESC
                        LIMIT :limit
                    """),
                    params,
                )
                rows = result.fetchall()
                results = []
                for row in rows:
                    sim = float(row.similarity)
                    if sim < threshold:
                        continue
                    results.append({
                        "id": str(row.id),
                        "repo_id": row.repo_id,
                        "entity_type": row.entity_type,
                        "entity_id": row.entity_id,
                        "capability": row.capability,
                        "content": row.content,
                        "metadata": row.metadata or {},
                        "similarity": round(sim, 4),
                        "trust_score": float(row.trust_score) if row.trust_score else 0.5,
                        "relevance": round(float(row.relevance), 4),
                        "embedding_model": row.embedding_model,
                        "embedded_at": row.embedded_at.isoformat() if row.embedded_at else None,
                    })
                return results
            except Exception as exc:
                logger.error("search_similar_failed", error=str(exc))
                raise

    # ------------------------------------------------------------------
    # Batch Operations
    # ------------------------------------------------------------------

    async def batch_embed(
        self,
        items: List[Dict[str, Any]],
        repo_id: Optional[str] = None,
    ) -> List[str]:
        """Batch embed and store with upsert + content_hash."""
        row_ids: List[str] = []

        for item in items:
            try:
                rid = await self.store_embedding(
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                    content=item["content"],
                    repo_id=repo_id or item.get("repo_id"),
                    metadata=item.get("metadata"),
                )
                row_ids.append(rid)
            except Exception as e:
                logger.warning("batch_embed_item_failed", entity_id=item.get("entity_id", "?"), error=str(e))

        logger.info("batch_embed_complete", total=len(items), stored=len(row_ids))
        return row_ids

    async def delete_by_entity(self, entity_type: str, entity_id: str) -> int:
        """Delete all embeddings for a given entity."""
        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(f"""
                        DELETE FROM {EMBEDDING_TABLE}
                        WHERE entity_type = :entity_type AND entity_id = :entity_id
                    """),
                    {"entity_type": entity_type, "entity_id": entity_id},
                )
                await session.commit()
                return result.rowcount
            except Exception as exc:
                await session.rollback()
                raise

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Comprehensive embedding statistics including trust tier distribution."""
        async with AsyncSessionLocal() as session:
            try:
                total_result = await session.execute(
                    text(f"SELECT COUNT(*) AS cnt FROM {EMBEDDING_TABLE}")
                )
                total = total_result.scalar() or 0

                by_type_result = await session.execute(
                    text(f"""
                        SELECT entity_type, COUNT(*) AS cnt
                        FROM {EMBEDDING_TABLE}
                        GROUP BY entity_type ORDER BY cnt DESC
                    """)
                )
                by_type = {row.entity_type: row.cnt for row in by_type_result.fetchall()}

                by_repo_result = await session.execute(
                    text(f"""
                        SELECT repo_id, COUNT(*) AS cnt
                        FROM {EMBEDDING_TABLE}
                        WHERE repo_id IS NOT NULL AND repo_id != ''
                        GROUP BY repo_id ORDER BY cnt DESC LIMIT 20
                    """)
                )
                by_repo = {row.repo_id: row.cnt for row in by_repo_result.fetchall()}

                trust_result = await session.execute(
                    text(f"""
                        SELECT
                            CASE
                                WHEN trust_score >= 0.85 THEN 'tier_A'
                                WHEN trust_score >= 0.7 THEN 'tier_B'
                                WHEN trust_score >= 0.5 THEN 'tier_C'
                                ELSE 'tier_D'
                            END as tier,
                            COUNT(*) as cnt
                        FROM {EMBEDDING_TABLE}
                        GROUP BY tier ORDER BY tier
                    """)
                )
                by_trust = {row.tier: row.cnt for row in trust_result.fetchall()}

                by_model_result = await session.execute(
                    text(f"""
                        SELECT embedding_model, COUNT(*) AS cnt
                        FROM {EMBEDDING_TABLE}
                        GROUP BY embedding_model ORDER BY cnt DESC
                    """)
                )
                by_model = {row.embedding_model: row.cnt for row in by_model_result.fetchall()}

                latest_result = await session.execute(
                    text(f"SELECT MAX(embedded_at) AS latest FROM {EMBEDDING_TABLE}")
                )
                latest = latest_result.scalar()

                return {
                    "total_embeddings": total,
                    "by_entity_type": by_type,
                    "by_repo": by_repo,
                    "by_trust_tier": by_trust,
                    "by_embedding_model": by_model,
                    "active_embedding_dim": EMBEDDING_DIM,
                    "active_embedding_model": _EMBEDDING_MODEL,
                    "backend": _BACKEND,
                    "env": ENV,
                    "latest_embedding_at": latest.isoformat() if latest else None,
                    "model_selection_policy": {
                        "default": "text-embedding-3-small",
                        "upgrade_condition": "only if holdout retrieval@5 improves >5%",
                        "rule": "never switch without full re-embed + benchmark",
                    },
                }
            except Exception as exc:
                logger.error("get_stats_failed", error=str(exc))
                raise


class EmbeddingError(Exception):
    """Raised when embedding generation fails in production."""
    pass

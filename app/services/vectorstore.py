"""
Vector store service using Qdrant vector database for COSMOS embeddings.

Embedding provider:
  Production: Shiprocket AI Gateway → text-embedding-3-small (1536 dims)
  Dev-only:   Local all-MiniLM-L6-v2 (384 dims) — ONLY if ENV=development
  Test-only:  Deterministic hash — ONLY if ENV=test

Storage backend: Qdrant (exclusive) — fast ANN search with payload filtering

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
import os
import struct
import time
from typing import Any, Dict, List, Optional

import structlog

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

# Production: Use OpenAI direct if OPENAI_API_KEY is set, else AI Gateway
# OpenAI direct avoids CloudFront POST blocking on AI Gateway
if OPENAI_API_KEY:
    _BACKEND = "openai_direct"
    _EMBEDDING_MODEL = AIGATEWAY_MODEL  # same model name
    _EMBEDDING_DIM = _MODEL_DIMS.get(AIGATEWAY_MODEL, 1536)
    logger.info("vectorstore.production_mode", backend="openai_direct", model=AIGATEWAY_MODEL, dim=_EMBEDDING_DIM)
elif AIGATEWAY_API_KEY:
    _BACKEND = "aigateway"
    _EMBEDDING_MODEL = AIGATEWAY_MODEL
    _EMBEDDING_DIM = _MODEL_DIMS.get(AIGATEWAY_MODEL, 1536)
    logger.info("vectorstore.production_mode", backend="aigateway", model=AIGATEWAY_MODEL, dim=_EMBEDDING_DIM)
else:
    raise RuntimeError(
        "Either OPENAI_API_KEY or AIGATEWAY_API_KEY is required. "
        "Set in .env or environment."
    )

EMBEDDING_DIM = _EMBEDDING_DIM


def _get_local_model():
    """Not used in production. Kept for backward compatibility."""
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
    """Async vector store — uses Qdrant vector database exclusively.

    Storage backend: Qdrant — fast ANN search with payload filtering
    Embedding backend: AI Gateway → OpenAI → local → hash
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._qdrant = None
        self._qdrant_ready = False

    async def ensure_schema(self) -> None:
        """Initialize Qdrant vector storage backend."""
        try:
            from app.services.qdrant_client import qdrant_store
            self._qdrant_ready = await qdrant_store.ensure_collection()
            if self._qdrant_ready:
                self._qdrant = qdrant_store
                logger.info("vectorstore.qdrant_ready", status="ready",
                            dim=EMBEDDING_DIM, model=_EMBEDDING_MODEL, env=ENV)
            else:
                raise RuntimeError("Qdrant collection creation returned False")
        except Exception as e:
            logger.error("vectorstore.qdrant_init_failed", error=str(e))
            raise

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    # Content quality patterns that indicate stubs/junk
    _STUB_PATTERNS = {"todo", "placeholder", "unknown", "tbd", "n/a", "none", "null", "empty"}

    def _validate_content_quality(self, content: str, entity_id: str = "") -> bool:
        """Quality gate: reject content that would pollute vector search.
        Returns True if content is good enough to embed."""
        if not content or len(content.strip()) < 50:
            logger.debug("vectorstore.quality_gate_rejected", reason="too_short",
                         entity_id=entity_id, length=len(content.strip()) if content else 0)
            return False

        stripped = content.strip()

        # Reject if >80% non-alphanumeric (punctuation/whitespace)
        alpha_count = sum(1 for c in stripped if c.isalnum())
        if len(stripped) > 0 and alpha_count / len(stripped) < 0.2:
            logger.debug("vectorstore.quality_gate_rejected", reason="low_alpha_ratio",
                         entity_id=entity_id)
            return False

        # Reject stub patterns
        lower = stripped.lower()
        if lower in self._STUB_PATTERNS or all(w in self._STUB_PATTERNS for w in lower.split()):
            logger.debug("vectorstore.quality_gate_rejected", reason="stub_content",
                         entity_id=entity_id)
            return False

        return True

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
        """Async version — uses OpenAI direct or AI Gateway based on config."""
        if _BACKEND == "openai_direct":
            return await self._embed_via_openai_direct_async(content)
        if _BACKEND == "aigateway":
            return await self._embed_via_gateway_async(content)
        return self.embed_text(content)

    async def _embed_via_openai_direct_async(self, content: str) -> List[float]:
        """Async OpenAI direct API call (bypasses AI Gateway CloudFront)."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OPENAI_API_URL,
                    json={
                        "model": _EMBEDDING_MODEL,
                        "input": content[:8000],
                    },
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )

            if response.status_code == 200:
                data = response.json()
                embedding = data["data"][0]["embedding"]
                if len(embedding) == EMBEDDING_DIM:
                    return embedding
                logger.warning("openai_direct.dim_mismatch", expected=EMBEDDING_DIM, got=len(embedding))
            elif response.status_code == 429:
                logger.warning("openai_direct.rate_limited")
            else:
                logger.warning("openai_direct.error", status=response.status_code)

            return self.embed_text(content)  # sync fallback

        except Exception as e:
            logger.warning("openai_direct.async_failed", error=str(e))
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
        - Content is validated before embedding (quality gate)
        """
        # Quality gate: reject junk content that would pollute vector search
        if not self._validate_content_quality(content, entity_id):
            return

        meta = dict(metadata or {})
        trust_score = meta.pop("trust_score", 0.5)
        capability = meta.pop("capability", "retrieval")
        freshness = meta.pop("freshness", None)
        content_hash = _compute_content_hash(content)
        effective_repo = repo_id or ""

        if not self._qdrant_ready or not self._qdrant:
            raise RuntimeError("Qdrant is not initialized. Call ensure_schema() first.")

        # Content-hash skip: check Qdrant BEFORE expensive embed_text() call
        # If the point exists with same content_hash → skip entirely (saves API cost)
        point_id = self._qdrant._point_id(effective_repo, entity_type, entity_id)
        try:
            existing = self._qdrant._client.retrieve(
                collection_name=self._qdrant._collection,
                ids=[point_id],
                with_payload=["content_hash"],
            )
            if existing and existing[0].payload.get("content_hash") == content_hash:
                return entity_id  # Skip — content unchanged, save embed_text() cost
        except Exception:
            pass  # Point doesn't exist yet, continue

        # Content changed or new → embed and store
        embedding = self.embed_text(content)
        await self._qdrant.upsert(
            repo_id=effective_repo,
            entity_type=entity_type,
            entity_id=entity_id,
            content=content,
            vector=embedding,
            metadata=meta,
            trust_score=trust_score,
            capability=capability,
            content_hash=content_hash,
        )
        return entity_id

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
        threshold: float = 0.25,
        pillar: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        query_mode: Optional[str] = None,
        module: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search with multi-signal ranking via Qdrant.

        relevance = similarity × trust_score × freshness_weight × capability_weight
        """
        if not self._qdrant_ready or not self._qdrant:
            raise RuntimeError("Qdrant is not initialized. Call ensure_schema() first.")

        query_embedding = self.embed_text(query)

        results = await self._qdrant.search(
            query_vector=query_embedding,
            limit=limit,
            threshold=threshold,
            entity_type=entity_type,
            repo_id=repo_id,
            capability=capability,
            pillar=pillar,
            domain=domain,
            query_mode=query_mode,
        )
        return results

    # ------------------------------------------------------------------
    # Batch Operations
    # ------------------------------------------------------------------

    async def batch_embed(
        self,
        items: List[Dict[str, Any]],
        repo_id: Optional[str] = None,
        concurrency: int = 20,
    ) -> List[str]:
        """Batch embed and store with upsert + content_hash.

        Runs up to `concurrency` embeddings in parallel for faster throughput.
        """
        import asyncio

        semaphore = asyncio.Semaphore(concurrency)
        row_ids: List[str] = []
        errors = 0

        async def _embed_one(item):
            nonlocal errors
            async with semaphore:
                try:
                    rid = await self.store_embedding(
                        entity_type=item["entity_type"],
                        entity_id=item["entity_id"],
                        content=item["content"],
                        repo_id=repo_id or item.get("repo_id"),
                        metadata=item.get("metadata"),
                    )
                    return rid
                except Exception as e:
                    errors += 1
                    logger.warning("batch_embed_item_failed", entity_id=item.get("entity_id", "?"), error=str(e))
                    return None

        # Process in chunks of concurrency*5 to avoid overwhelming memory
        chunk_size = concurrency * 5
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            results = await asyncio.gather(*[_embed_one(item) for item in chunk])
            row_ids.extend([r for r in results if r is not None])

        logger.info("batch_embed_complete", total=len(items), stored=len(row_ids), errors=errors, concurrency=concurrency)
        return row_ids

    async def delete_by_entity(self, entity_type: str, entity_id: str) -> int:
        """Delete all embeddings for a given entity via Qdrant."""
        if not self._qdrant_ready or not self._qdrant:
            raise RuntimeError("Qdrant is not initialized. Call ensure_schema() first.")

        return await self._qdrant.delete_by_entity(entity_type=entity_type, entity_id=entity_id)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Comprehensive embedding statistics from Qdrant."""
        if not self._qdrant_ready or not self._qdrant:
            return {
                "total_embeddings": 0,
                "active_embedding_dim": EMBEDDING_DIM,
                "active_embedding_model": _EMBEDDING_MODEL,
                "backend": _BACKEND,
                "storage": "qdrant",
                "env": ENV,
                "status": "not_initialized",
            }

        try:
            qdrant_stats = await self._qdrant.get_stats()
            return {
                **qdrant_stats,
                "active_embedding_dim": EMBEDDING_DIM,
                "active_embedding_model": _EMBEDDING_MODEL,
                "backend": _BACKEND,
                "storage": "qdrant",
                "env": ENV,
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

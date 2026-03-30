"""
Pluggable embedding backends for COSMOS retrieval simulation.

Supports four backends:
  hash         – deterministic hash fallback (384 dims) — always available
  local        – sentence-transformers all-MiniLM-L6-v2 (384 dims)
  openai-small – text-embedding-3-small (1536 dims) via OpenAI or AI Gateway
  openai-large – text-embedding-3-large (3072 dims) via OpenAI or AI Gateway

Factory usage::

    backend = EmbeddingBackendFactory.create("openai-small", api_key="sk-...")
    vectors = await backend.embed_batch(["query one", "query two"])
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Dimension registry
# ---------------------------------------------------------------------------

MODEL_DIMS = {
    "hash-fallback": 384,
    "all-MiniLM-L6-v2": 384,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseEmbeddingBackend(ABC):
    """Contract every backend must satisfy."""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dims(self) -> int: ...

    @abstractmethod
    async def embed(self, text: str) -> List[float]: ...

    async def embed_batch(self, texts: List[str], batch_size: int = 64) -> List[List[float]]:
        """Embed a list of texts, chunked to avoid API limits."""
        results: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            batch_vecs = await self._embed_batch_chunk(chunk)
            results.extend(batch_vecs)
        return results

    async def _embed_batch_chunk(self, texts: List[str]) -> List[List[float]]:
        """Default: embed sequentially. Override for true batch APIs."""
        import asyncio
        return await asyncio.gather(*[self.embed(t) for t in texts])


# ---------------------------------------------------------------------------
# Backend A: Deterministic hash (no external deps, always works)
# ---------------------------------------------------------------------------

class HashFallbackBackend(BaseEmbeddingBackend):
    """384-dim deterministic hash embedding — used in dev/test with no API key."""

    @property
    def model_name(self) -> str:
        return "hash-fallback"

    @property
    def dims(self) -> int:
        return 384

    async def embed(self, text: str) -> List[float]:
        return self._hash_embed(text)

    @staticmethod
    def _hash_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode()).digest()
        # Repeat digest to fill 384 floats (each float = 4 bytes → 1536 bytes needed)
        raw = (digest * 48)[:1536]
        floats = [struct.unpack("f", raw[i : i + 4])[0] for i in range(0, 1536, 4)]
        # Normalize to unit vector
        norm = sum(x * x for x in floats) ** 0.5 or 1.0
        return [x / norm for x in floats]


# ---------------------------------------------------------------------------
# Backend B: Local sentence-transformers (MiniLM, 384 dims)
# ---------------------------------------------------------------------------

class LocalMiniLMBackend(BaseEmbeddingBackend):
    """384-dim local MiniLM model — no API key required, GPU optional."""

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("embedding_backend.local_minilm.loaded")
        except ImportError:
            raise RuntimeError("sentence-transformers not installed. pip install sentence-transformers")

    @property
    def model_name(self) -> str:
        return "all-MiniLM-L6-v2"

    @property
    def dims(self) -> int:
        return 384

    async def embed(self, text: str) -> List[float]:
        import asyncio
        loop = asyncio.get_event_loop()
        vec = await loop.run_in_executor(None, self._model.encode, text)
        return vec.tolist()

    async def _embed_batch_chunk(self, texts: List[str]) -> List[List[float]]:
        import asyncio
        loop = asyncio.get_event_loop()
        vecs = await loop.run_in_executor(None, self._model.encode, texts)
        return [v.tolist() for v in vecs]


# ---------------------------------------------------------------------------
# Backend C / D: OpenAI text-embedding-3-small / text-embedding-3-large
# ---------------------------------------------------------------------------

class OpenAIEmbeddingBackend(BaseEmbeddingBackend):
    """
    OpenAI embedding API backend.

    Routes through:
      1. AI Gateway (AIGATEWAY_URL) if api_key is a gateway key
      2. Direct OpenAI API (api.openai.com) if api_key starts with 'sk-'
    """

    _OPENAI_URL = "https://api.openai.com/v1/embeddings"

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: Optional[str] = None,
        gateway_url: Optional[str] = None,
    ) -> None:
        if model not in ("text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"):
            raise ValueError(f"Unknown OpenAI embedding model: {model}")

        self._model = model
        self._dims = MODEL_DIMS[model]
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("AIGATEWAY_API_KEY", "")
        self._gateway_url = gateway_url or os.environ.get("AIGATEWAY_URL", "")

        if not self._api_key:
            raise RuntimeError(
                f"No API key for {model}. Set OPENAI_API_KEY or AIGATEWAY_API_KEY."
            )

        # Decide which URL to use
        if self._gateway_url and not self._api_key.startswith("sk-"):
            self._url = f"{self._gateway_url.rstrip('/')}/v1/embeddings"
        else:
            self._url = self._OPENAI_URL

        logger.info("embedding_backend.openai.init", model=model, url=self._url, dims=self._dims)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    async def embed(self, text: str) -> List[float]:
        return (await self._embed_batch_chunk([text]))[0]

    async def _embed_batch_chunk(self, texts: List[str]) -> List[List[float]]:
        payload = {"model": self._model, "input": texts}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        latency_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "embedding_backend.openai.request",
            model=self._model,
            count=len(texts),
            latency_ms=round(latency_ms, 1),
        )

        # OpenAI returns data sorted by index
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class EmbeddingBackendFactory:
    """
    Create the right embedding backend from a string name.

    Supported names:
      "hash"          → HashFallbackBackend (384 dims, always works)
      "local"         → LocalMiniLMBackend  (384 dims, requires sentence-transformers)
      "openai-small"  → OpenAIEmbeddingBackend(text-embedding-3-small, 1536 dims)
      "openai-large"  → OpenAIEmbeddingBackend(text-embedding-3-large, 3072 dims)
    """

    @staticmethod
    def create(
        name: str,
        api_key: Optional[str] = None,
        gateway_url: Optional[str] = None,
    ) -> BaseEmbeddingBackend:
        if name == "hash":
            return HashFallbackBackend()
        if name == "local":
            return LocalMiniLMBackend()
        if name == "openai-small":
            return OpenAIEmbeddingBackend(
                model="text-embedding-3-small",
                api_key=api_key,
                gateway_url=gateway_url,
            )
        if name == "openai-large":
            return OpenAIEmbeddingBackend(
                model="text-embedding-3-large",
                api_key=api_key,
                gateway_url=gateway_url,
            )
        raise ValueError(f"Unknown backend: {name!r}. Choose from: hash, local, openai-small, openai-large")

    @staticmethod
    def auto() -> BaseEmbeddingBackend:
        """Pick the best available backend automatically."""
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        gateway_key = os.environ.get("AIGATEWAY_API_KEY", "")
        gateway_url = os.environ.get("AIGATEWAY_URL", "")

        if openai_key or (gateway_key and gateway_url):
            try:
                return EmbeddingBackendFactory.create("openai-small", api_key=openai_key or gateway_key)
            except Exception as e:
                logger.warning("embedding_backend.auto.openai_failed", error=str(e))

        try:
            return EmbeddingBackendFactory.create("local")
        except Exception:
            pass

        logger.warning("embedding_backend.auto.using_hash_fallback")
        return EmbeddingBackendFactory.create("hash")

"""
Voyage AI Embedding Client — Retrieval-optimized embeddings with query/document distinction.

Models:
  voyage-3-large  (1024-dim) — best retrieval quality, general docs
  voyage-3        (1024-dim) — balanced quality/cost
  voyage-3-lite   (512-dim)  — budget option
  voyage-code-3   (1024-dim) — optimized for code + technical docs

Key feature: input_type="query" vs "document"
  At INGESTION: embed KB docs with input_type="document"
  At SEARCH:    embed user query with input_type="query"
  This asymmetry gives ~3-5% retrieval improvement over symmetric embeddings.

Usage:
  client = VoyageClient(api_key="pa-...")

  # Embed a document (at ingestion time)
  doc_embedding = client.embed("Table: orders | Domain: orders | ...", input_type="document")

  # Embed a query (at search time)
  query_embedding = client.embed("where is my order", input_type="query")

  # Embed code/technical content
  code_embedding = client.embed("def cancel_order(order_id)...", model="voyage-code-3", input_type="document")
"""

import os
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger()

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"

_MODEL_DIMS = {
    "voyage-3-large": 1024,
    "voyage-3": 1024,
    "voyage-3-lite": 512,
    "voyage-code-3": 1024,
}


class VoyageClient:
    """Direct REST client for Voyage AI embedding API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "voyage-3-large",
    ):
        self.api_key = api_key or os.environ.get("VOYAGE_API_KEY", "")
        self.default_model = default_model
        self.default_dim = _MODEL_DIMS.get(default_model, 1024)

        if not self.api_key:
            logger.warning("voyage_client.no_api_key")

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def embed(
        self,
        text: str,
        input_type: str = "document",
        model: Optional[str] = None,
    ) -> List[float]:
        """
        Synchronous embedding call.

        Args:
            text: Content to embed (max 16K tokens for voyage-3-large)
            input_type: "query" for search queries, "document" for KB docs
            model: Model override (default: voyage-3-large)
        """
        model = model or self.default_model

        response = httpx.post(
            VOYAGE_API_URL,
            json={
                "input": [text[:16000]],
                "model": model,
                "input_type": input_type,
            },
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

        if response.status_code == 200:
            data = response.json()
            embedding = data["data"][0]["embedding"]

            # Validate dimension
            expected_dim = _MODEL_DIMS.get(model, 1024)
            if len(embedding) != expected_dim:
                raise VoyageError(
                    f"Dimension mismatch: got {len(embedding)}, expected {expected_dim}"
                )

            return embedding

        error = response.text[:300]
        logger.error("voyage.embed_failed", status=response.status_code, error=error)
        raise VoyageError(f"Voyage API {response.status_code}: {error}")

    async def embed_async(
        self,
        text: str,
        input_type: str = "document",
        model: Optional[str] = None,
    ) -> List[float]:
        """Async embedding call."""
        model = model or self.default_model

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                VOYAGE_API_URL,
                json={
                    "input": [text[:16000]],
                    "model": model,
                    "input_type": input_type,
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                data = response.json()
                return data["data"][0]["embedding"]

            raise VoyageError(f"Voyage API {response.status_code}: {response.text[:300]}")

    def embed_batch(
        self,
        texts: List[str],
        input_type: str = "document",
        model: Optional[str] = None,
    ) -> List[List[float]]:
        """
        Batch embedding — up to 128 texts per call.
        More efficient than single calls for ingestion.
        """
        model = model or self.default_model
        results = []

        # Voyage allows up to 128 inputs per batch
        for i in range(0, len(texts), 128):
            batch = [t[:16000] for t in texts[i:i + 128]]

            response = httpx.post(
                VOYAGE_API_URL,
                json={
                    "input": batch,
                    "model": model,
                    "input_type": input_type,
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )

            if response.status_code == 200:
                data = response.json()
                for item in data["data"]:
                    results.append(item["embedding"])
            else:
                raise VoyageError(f"Batch embed failed: {response.status_code}")

            logger.info("voyage.batch_embedded", batch_size=len(batch), total_so_far=len(results))

        return results

    def get_model_info(self) -> Dict[str, Any]:
        """Return model dimensions and availability."""
        return {
            "default_model": self.default_model,
            "default_dim": self.default_dim,
            "available": self.is_available,
            "models": {
                "voyage-3-large": {"dim": 1024, "best_for": "general retrieval", "cost": "$0.18/1M tokens"},
                "voyage-3": {"dim": 1024, "best_for": "balanced", "cost": "$0.06/1M tokens"},
                "voyage-3-lite": {"dim": 512, "best_for": "budget", "cost": "$0.02/1M tokens"},
                "voyage-code-3": {"dim": 1024, "best_for": "code + technical docs", "cost": "$0.18/1M tokens"},
            },
        }


class VoyageError(Exception):
    """Raised when Voyage API call fails."""
    pass

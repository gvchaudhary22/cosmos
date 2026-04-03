"""
Qdrant Vector Database Client — Replaces pgvector for embedding storage and similarity search.

Architecture:
  - Qdrant: vector embeddings + payload filtering (replaces cosmos_embeddings table)
  - Neo4j: graph nodes + edges + entity lookup (replaces graph_nodes/edges/entity_lookup)
  - MySQL (MARS): relational data (sessions, analytics, audit, registry)

Usage:
    from app.services.qdrant_client import qdrant_store
    await qdrant_store.ensure_collection()
    await qdrant_store.upsert(point_id, vector, payload)
    results = await qdrant_store.search(query_vector, limit=5, filters={})
"""

import hashlib
import os
import time
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
_COLLECTION = os.environ.get("QDRANT_COLLECTION", "cosmos_embeddings")
# Vector size: always 1536 (production: text-embedding-3-small)
# System is treated as production — no dev/test dimension switching
_VECTOR_SIZE = 1536


class QdrantVectorStore:
    """Async-compatible Qdrant client for COSMOS embedding operations."""

    def __init__(self, url: str = _QDRANT_URL, collection: str = _COLLECTION):
        self._url = url
        self._collection = collection
        self._client = None
        self._ready = False

    async def ensure_collection(self, vector_size: int = 0) -> bool:
        """Create collection if it doesn't exist. Returns True if ready."""
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            self._client = QdrantClient(url=self._url, timeout=10)

            # Auto-detect vector size from embedding backend
            effective_size = vector_size or _VECTOR_SIZE
            if effective_size == 0:
                try:
                    from app.services.vectorstore import EMBEDDING_DIM
                    effective_size = EMBEDDING_DIM
                except ImportError:
                    effective_size = 1536  # Default to production size

            # Check if collection exists
            collections = self._client.get_collections().collections
            exists = any(c.name == self._collection for c in collections)

            if not exists:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=effective_size,
                        distance=Distance.COSINE,
                    ),
                )
                # Create payload indexes for filtering
                self._client.create_payload_index(
                    self._collection, "entity_type", "keyword")
                self._client.create_payload_index(
                    self._collection, "repo_id", "keyword")
                self._client.create_payload_index(
                    self._collection, "capability", "keyword")
                self._client.create_payload_index(
                    self._collection, "pillar", "keyword")
                self._client.create_payload_index(
                    self._collection, "domain", "keyword")
                self._client.create_payload_index(
                    self._collection, "query_mode", "keyword")
                self._client.create_payload_index(
                    self._collection, "trust_score", "float")
                logger.info("qdrant.collection_created", collection=self._collection,
                            vector_size=_VECTOR_SIZE)

            self._ready = True
            info = self._client.get_collection(self._collection)
            logger.info("qdrant.ready", collection=self._collection,
                        points=info.points_count)
            return True

        except ImportError:
            logger.warning("qdrant.not_installed", hint="pip install qdrant-client")
            return False
        except Exception as e:
            logger.warning("qdrant.init_failed", error=str(e))
            return False

    @property
    def ready(self) -> bool:
        return self._ready and self._client is not None

    def _point_id(self, repo_id: str, entity_type: str, entity_id: str) -> str:
        """Generate deterministic point ID from identity triple."""
        key = f"{repo_id}:{entity_type}:{entity_id}"
        return hashlib.md5(key.encode()).hexdigest()

    async def upsert(
        self,
        repo_id: str,
        entity_type: str,
        entity_id: str,
        content: str,
        vector: List[float],
        metadata: Optional[Dict] = None,
        trust_score: float = 0.5,
        capability: str = "retrieval",
        content_hash: str = "",
    ) -> bool:
        """Upsert a single embedding point.

        Content-hash skip: if the point already exists with the same content_hash,
        skip the expensive re-embedding. This makes re-runs fast — only changed
        docs get re-embedded.
        """
        if not self.ready:
            return False
        try:
            from qdrant_client.models import PointStruct

            point_id = self._point_id(repo_id, entity_type, entity_id)

            # Content-hash skip: check if this exact content already exists
            if content_hash:
                try:
                    existing = self._client.retrieve(
                        collection_name=self._collection,
                        ids=[point_id],
                        with_payload=["content_hash"],
                    )
                    if existing and existing[0].payload.get("content_hash") == content_hash:
                        return True  # Skip — content unchanged
                except Exception:
                    pass  # Point doesn't exist yet, continue with insert

            meta = metadata or {}

            payload = {
                "repo_id": repo_id or "",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "capability": capability,
                "trust_score": trust_score,
                "content": content[:5000],  # Cap content for storage
                "content_hash": content_hash,
                "pillar": meta.get("pillar", ""),
                "domain": meta.get("domain", ""),
                "query_mode": meta.get("query_mode", ""),
                "chunk_type": meta.get("chunk_type", ""),
                "parent_doc_id": meta.get("parent_doc_id", ""),
                "file_type": meta.get("file_type", ""),
                "embedding_model": meta.get("embedding_model", "text-embedding-3-small"),
                "embedded_at": time.time(),
            }

            self._client.upsert(
                collection_name=self._collection,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
            return True
        except Exception as e:
            logger.warning("qdrant.upsert_failed", entity_id=entity_id, error=str(e))
            return False

    async def upsert_batch(self, points: List[Dict]) -> int:
        """Batch upsert multiple points. Returns count of successfully upserted."""
        if not self.ready or not points:
            return 0
        try:
            from qdrant_client.models import PointStruct

            qdrant_points = []
            for p in points:
                point_id = self._point_id(
                    p.get("repo_id", ""), p.get("entity_type", ""), p.get("entity_id", ""))
                meta = p.get("metadata", {})
                payload = {
                    "repo_id": p.get("repo_id", ""),
                    "entity_type": p.get("entity_type", ""),
                    "entity_id": p.get("entity_id", ""),
                    "capability": p.get("capability", "retrieval"),
                    "trust_score": p.get("trust_score", 0.5),
                    "content": (p.get("content", "") or "")[:5000],
                    "content_hash": p.get("content_hash", ""),
                    "pillar": meta.get("pillar", ""),
                    "domain": meta.get("domain", ""),
                    "query_mode": meta.get("query_mode", ""),
                    "chunk_type": meta.get("chunk_type", ""),
                    "parent_doc_id": meta.get("parent_doc_id", ""),
                    "file_type": meta.get("file_type", ""),
                    "embedding_model": meta.get("embedding_model", ""),
                    "embedded_at": time.time(),
                }
                qdrant_points.append(
                    PointStruct(id=point_id, vector=p["vector"], payload=payload))

            # Batch upsert in chunks of 100
            total = 0
            for i in range(0, len(qdrant_points), 100):
                batch = qdrant_points[i:i+100]
                self._client.upsert(collection_name=self._collection, points=batch)
                total += len(batch)

            return total
        except Exception as e:
            logger.warning("qdrant.batch_upsert_failed", count=len(points), error=str(e))
            return 0

    async def search(
        self,
        query_vector: List[float],
        limit: int = 5,
        threshold: float = 0.25,
        entity_type: Optional[str] = None,
        repo_id: Optional[str] = None,
        capability: Optional[str] = None,
        pillar: Optional[str] = None,
        domain: Optional[str] = None,
        query_mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar vectors with optional payload filtering."""
        if not self.ready:
            return []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            # Build filter conditions
            conditions = []
            if entity_type:
                conditions.append(FieldCondition(key="entity_type", match=MatchValue(value=entity_type)))
            if repo_id:
                conditions.append(FieldCondition(key="repo_id", match=MatchValue(value=repo_id)))
            if capability:
                conditions.append(FieldCondition(key="capability", match=MatchValue(value=capability)))
            if pillar:
                conditions.append(FieldCondition(key="pillar", match=MatchValue(value=pillar)))
            if domain:
                conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
            if query_mode:
                conditions.append(FieldCondition(key="query_mode", match=MatchValue(value=query_mode)))

            query_filter = Filter(must=conditions) if conditions else None

            # qdrant-client v1.7+ uses query_points, older uses search
            try:
                from qdrant_client.models import QueryRequest
                results = self._client.query_points(
                    collection_name=self._collection,
                    query=query_vector,
                    limit=limit,
                    score_threshold=threshold,
                    query_filter=query_filter,
                    with_payload=True,
                ).points
            except (ImportError, AttributeError, TypeError):
                results = self._client.search(
                    collection_name=self._collection,
                    query_vector=query_vector,
                    limit=limit,
                    score_threshold=threshold,
                    query_filter=query_filter,
                    with_payload=True,
                )

            # Convert to dict format matching existing vectorstore.search_similar output
            output = []
            for hit in results:
                payload = hit.payload or {}
                similarity = hit.score  # Qdrant cosine similarity (0-1)
                trust = payload.get("trust_score", 0.5)

                # Compute relevance matching existing formula
                freshness_weight = 1.0  # Qdrant doesn't track freshness natively
                embedded_at = payload.get("embedded_at", 0)
                if embedded_at:
                    age_days = (time.time() - embedded_at) / 86400
                    if age_days <= 7:
                        freshness_weight = 1.0
                    elif age_days <= 30:
                        freshness_weight = 0.9
                    elif age_days <= 90:
                        freshness_weight = 0.8
                    else:
                        freshness_weight = 0.7

                cap_weight = 1.0
                if capability and payload.get("capability") == capability:
                    cap_weight = 1.0
                elif capability:
                    cap_weight = 0.8

                relevance = similarity * trust * freshness_weight * cap_weight

                output.append({
                    "id": str(hit.id),
                    "repo_id": payload.get("repo_id", ""),
                    "entity_type": payload.get("entity_type", ""),
                    "entity_id": payload.get("entity_id", ""),
                    "capability": payload.get("capability", ""),
                    "content": payload.get("content", ""),
                    "metadata": {
                        "pillar": payload.get("pillar", ""),
                        "domain": payload.get("domain", ""),
                        "query_mode": payload.get("query_mode", ""),
                        "chunk_type": payload.get("chunk_type", ""),
                        "parent_doc_id": payload.get("parent_doc_id", ""),
                        "file_type": payload.get("file_type", ""),
                    },
                    "similarity": round(similarity, 4),
                    "trust_score": trust,
                    "relevance": round(relevance, 4),
                    "embedding_model": payload.get("embedding_model", ""),
                    "embedded_at": payload.get("embedded_at"),
                })

            return output
        except Exception as e:
            logger.warning("qdrant.search_failed", error=str(e))
            return []

    async def get_stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        if not self.ready:
            return {"available": False}
        try:
            info = self._client.get_collection(self._collection)
            return {
                "available": True,
                "points_count": info.points_count,
                "collection": self._collection,
                "vector_size": _VECTOR_SIZE,
            }
        except Exception as e:
            return {"available": False, "error": str(e)}

    async def delete_by_filter(self, entity_type: Optional[str] = None,
                                repo_id: Optional[str] = None) -> int:
        """Delete points matching filter. Used for cleanup."""
        if not self.ready:
            return 0
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            conditions = []
            if entity_type:
                conditions.append(FieldCondition(key="entity_type", match=MatchValue(value=entity_type)))
            if repo_id:
                conditions.append(FieldCondition(key="repo_id", match=MatchValue(value=repo_id)))
            if not conditions:
                return 0
            self._client.delete(
                collection_name=self._collection,
                points_selector=Filter(must=conditions),
            )
            return 1  # Qdrant doesn't return count on delete
        except Exception as e:
            logger.warning("qdrant.delete_failed", error=str(e))
            return 0


# Module singleton
qdrant_store = QdrantVectorStore()

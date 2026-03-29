"""
gRPC servicer implementation for VectorStore service.

Bridges gRPC requests to the underlying VectorStoreService,
converting between protobuf messages and domain objects.
"""

from __future__ import annotations

from typing import AsyncIterator

import grpc
import structlog

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.services.vectorstore import VectorStoreService

logger = structlog.get_logger(__name__)


class VectorStoreServicer(cosmos_pb2_grpc.VectorStoreServiceServicer):
    """gRPC servicer for the vector embedding store."""

    def __init__(self) -> None:
        self._svc = VectorStoreService()

    async def EmbedAndStore(
        self, request: cosmos_pb2.EmbedRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.EmbedResponse:
        """Embed text content and store the resulting vector in pgvector."""
        logger.info(
            "grpc.vectorstore.EmbedAndStore",
            entity_type=request.entity_type,
            entity_id=request.entity_id,
        )
        try:
            metadata = dict(request.metadata) if request.metadata else None
            repo_id = request.context.repo_id or None

            embedding_id = await self._svc.store_embedding(
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                content=request.content,
                repo_id=repo_id,
                metadata=metadata,
            )

            return cosmos_pb2.EmbedResponse(
                success=True,
                embedding_id=embedding_id,
                dimensions=384,
            )
        except Exception as exc:
            logger.error("grpc.vectorstore.EmbedAndStore.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.EmbedResponse(success=False)

    async def SearchSimilar(
        self, request: cosmos_pb2.SearchRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.SearchResponse:
        """Search for semantically similar embeddings using cosine distance."""
        logger.info("grpc.vectorstore.SearchSimilar", query=request.query[:80])
        try:
            top_k = request.top_k if request.top_k > 0 else 5
            entity_type = request.entity_type or None
            repo_id = request.repo_id or None
            min_score = request.min_score if request.min_score > 0 else 0.0

            results = await self._svc.search_similar(
                query=request.query,
                limit=top_k,
                entity_type=entity_type,
                repo_id=repo_id,
                threshold=min_score,
            )

            proto_results = []
            for r in results:
                meta = r.get("metadata", {})
                proto_results.append(
                    cosmos_pb2.SearchResult(
                        entity_type=r.get("entity_type", ""),
                        entity_id=r.get("entity_id", ""),
                        content=r.get("content", ""),
                        score=float(r.get("similarity", 0.0)),
                        metadata={k: str(v) for k, v in meta.items()} if meta else {},
                    )
                )

            return cosmos_pb2.SearchResponse(results=proto_results)
        except Exception as exc:
            logger.error("grpc.vectorstore.SearchSimilar.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.SearchResponse()

    async def BatchEmbed(
        self, request: cosmos_pb2.BatchEmbedRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.BatchEmbedResponse:
        """Batch embed and store multiple items in a single transaction."""
        logger.info("grpc.vectorstore.BatchEmbed", count=len(request.items))
        try:
            repo_id = request.context.repo_id or None
            items = []
            for item in request.items:
                items.append({
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "content": item.content,
                    "metadata": dict(item.metadata) if item.metadata else {},
                })

            row_ids = await self._svc.batch_embed(items, repo_id=repo_id)

            return cosmos_pb2.BatchEmbedResponse(
                total=len(items),
                success_count=len(row_ids),
                error_count=len(items) - len(row_ids),
                errors=[],
            )
        except Exception as exc:
            logger.error("grpc.vectorstore.BatchEmbed.error", error=str(exc))
            return cosmos_pb2.BatchEmbedResponse(
                total=len(request.items),
                success_count=0,
                error_count=len(request.items),
                errors=[str(exc)],
            )

    async def DeleteByEntity(
        self, request: cosmos_pb2.DeleteEmbeddingRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.DeleteResponse:
        """Delete all embeddings for a given entity type + id pair."""
        logger.info(
            "grpc.vectorstore.DeleteByEntity",
            entity_type=request.entity_type,
            entity_id=request.entity_id,
        )
        try:
            deleted = await self._svc.delete_by_entity(
                entity_type=request.entity_type,
                entity_id=request.entity_id,
            )
            return cosmos_pb2.DeleteResponse(deleted_count=deleted)
        except Exception as exc:
            logger.error("grpc.vectorstore.DeleteByEntity.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.DeleteResponse()

    async def GetStats(
        self, request: cosmos_pb2.VectorStatsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.VectorStatsResponse:
        """Return statistics about stored embeddings."""
        logger.info("grpc.vectorstore.GetStats")
        try:
            stats = await self._svc.get_stats()

            by_type = stats.get("by_entity_type", {})
            embeddings_by_type = {k: int(v) for k, v in by_type.items()}

            return cosmos_pb2.VectorStatsResponse(
                total_embeddings=stats.get("total_embeddings", 0),
                embeddings_by_type=embeddings_by_type,
                dimensions=stats.get("embedding_dim", 384),
            )
        except Exception as exc:
            logger.error("grpc.vectorstore.GetStats.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.VectorStatsResponse()

    async def SearchStream(
        self, request: cosmos_pb2.SearchRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[cosmos_pb2.SearchResult]:
        """Stream search results one at a time as they are retrieved.

        Performs the same cosine similarity search as ``SearchSimilar`` but
        yields each result individually so the caller can start processing
        before the full batch is ready.
        """
        logger.info("grpc.vectorstore.SearchStream", query=request.query[:80])
        try:
            top_k = request.top_k if request.top_k > 0 else 5
            entity_type = request.entity_type or None
            repo_id = request.repo_id or None
            min_score = request.min_score if request.min_score > 0 else 0.0

            results = await self._svc.search_similar(
                query=request.query,
                limit=top_k,
                entity_type=entity_type,
                repo_id=repo_id,
                threshold=min_score,
            )

            for r in results:
                meta = r.get("metadata", {})
                yield cosmos_pb2.SearchResult(
                    entity_type=r.get("entity_type", ""),
                    entity_id=r.get("entity_id", ""),
                    content=r.get("content", ""),
                    score=float(r.get("similarity", 0.0)),
                    metadata={k: str(v) for k, v in meta.items()} if meta else {},
                )
        except Exception as exc:
            logger.error("grpc.vectorstore.SearchStream.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))

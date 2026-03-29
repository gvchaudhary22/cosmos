"""
gRPC servicer implementation for GraphRAG service.

Bridges gRPC requests to the underlying GraphRAGService,
converting between protobuf messages and domain objects.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Set, Tuple, List

import grpc
import structlog
from google.protobuf import timestamp_pb2

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.services.graphrag import graphrag_service, GraphRAGService
from app.services.graphrag_models import NodeType, EdgeType

logger = structlog.get_logger(__name__)


def _to_proto_node(node) -> cosmos_pb2.GraphNode:
    """Convert a domain GraphNode to protobuf GraphNode."""
    pb = cosmos_pb2.GraphNode(
        id=node.id,
        node_type=node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type),
        name=node.label,
        repo_id=node.repo_id or "",
        properties=json.dumps(node.properties) if node.properties else "{}",
    )
    if node.created_at:
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(node.created_at)
        pb.created_at.CopyFrom(ts)
    return pb


def _to_proto_edge(edge) -> cosmos_pb2.GraphEdge:
    """Convert a domain GraphEdge to protobuf GraphEdge."""
    return cosmos_pb2.GraphEdge(
        source_id=edge.source_id,
        target_id=edge.target_id,
        edge_type=edge.edge_type.value if hasattr(edge.edge_type, "value") else str(edge.edge_type),
        weight=edge.weight,
        repo_id=edge.repo_id or "",
        properties=json.dumps(edge.properties) if edge.properties else "{}",
    )


class GraphRAGServicer(cosmos_pb2_grpc.GraphRAGServiceServicer):
    """gRPC servicer for the GraphRAG knowledge graph service."""

    def __init__(self) -> None:
        self._svc: GraphRAGService = graphrag_service

    async def IngestModuleDeps(
        self, request: cosmos_pb2.IngestModuleDepsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.IngestResponse:
        """Ingest module dependency relationships.

        Converts the protobuf request into the list-of-dicts format that
        GraphRAGService.ingest_module_deps expects. Each import becomes an
        ``imports`` edge, each dependency a ``depends_on`` edge, and each
        function a ``calls`` edge from the module node.
        """
        logger.info("grpc.graphrag.IngestModuleDeps", module=request.module_name)
        try:
            repo_id = request.context.repo_id or ""
            modules: List[dict] = []

            for dep in request.dependencies:
                modules.append({
                    "source": request.module_name,
                    "target": dep,
                    "edge_type": "depends_on",
                })
            for imp in request.imports:
                modules.append({
                    "source": request.module_name,
                    "target": imp,
                    "edge_type": "imports",
                })
            for func in request.functions:
                modules.append({
                    "source": request.module_name,
                    "target": func,
                    "edge_type": "calls",
                })

            count = await self._svc.ingest_module_deps(repo_id, modules)

            return cosmos_pb2.IngestResponse(
                success=True,
                message=f"Ingested {count} edge(s) for module {request.module_name}",
                edges_created=count,
                nodes_created=count * 2,
            )
        except Exception as exc:
            logger.error("grpc.graphrag.IngestModuleDeps.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.IngestResponse(success=False, message=str(exc))

    async def IngestCourierRelationship(
        self, request: cosmos_pb2.IngestCourierRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.IngestResponse:
        """Ingest courier-seller relationships including NDR data."""
        logger.info("grpc.graphrag.IngestCourierRelationship", courier=request.courier_name)
        try:
            repo_id = request.context.repo_id or ""
            courier_id = f"courier:{request.courier_name}"
            seller_id = f"seller:{request.seller_name}" if request.seller_name else None
            seller_name = request.seller_name or None

            edges = await self._svc.ingest_courier_relationship(
                repo_id=repo_id,
                courier_id=courier_id,
                courier_name=request.courier_name,
                seller_id=seller_id,
                seller_name=seller_name,
                ndr_count=0,
                properties={
                    "region": request.region,
                    "ndr_rate": request.ndr_rate,
                },
            )

            return cosmos_pb2.IngestResponse(
                success=True,
                message=f"Ingested courier {request.courier_name} with {edges} edge(s)",
                edges_created=edges,
                nodes_created=2 if seller_id else 1,
            )
        except Exception as exc:
            logger.error("grpc.graphrag.IngestCourierRelationship.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.IngestResponse(success=False, message=str(exc))

    async def IngestChannelRelationship(
        self, request: cosmos_pb2.IngestChannelRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.IngestResponse:
        """Ingest channel-seller relationships."""
        logger.info("grpc.graphrag.IngestChannelRelationship", channel=request.channel_name)
        try:
            repo_id = request.context.repo_id or ""
            channel_id = f"channel:{request.channel_name}"
            seller_id = f"seller:{request.seller_name}"

            edges = await self._svc.ingest_channel_relationship(
                repo_id=repo_id,
                channel_id=channel_id,
                channel_name=request.channel_name,
                seller_id=seller_id,
                seller_name=request.seller_name,
            )

            return cosmos_pb2.IngestResponse(
                success=True,
                message=f"Ingested channel {request.channel_name} with {edges} edge(s)",
                edges_created=edges,
                nodes_created=2,
            )
        except Exception as exc:
            logger.error("grpc.graphrag.IngestChannelRelationship.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.IngestResponse(success=False, message=str(exc))

    async def QueryRelated(
        self, request: cosmos_pb2.GraphQueryRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.GraphQueryResponse:
        """Query related nodes by keyword search + BFS expansion."""
        logger.info("grpc.graphrag.QueryRelated", query=request.query)
        try:
            repo_id = request.context.repo_id or None
            max_depth = request.max_depth if request.max_depth > 0 else 2

            result = await self._svc.query_related(
                q=request.query,
                repo_id=repo_id,
                max_depth=max_depth,
            )

            formatted = await self._svc.format_as_context(result)

            all_nodes = result.matched_nodes + result.related_nodes
            proto_nodes = [_to_proto_node(n) for n in all_nodes]
            proto_edges = [_to_proto_edge(e) for e in result.related_edges]

            return cosmos_pb2.GraphQueryResponse(
                nodes=proto_nodes,
                edges=proto_edges,
                formatted_context=formatted,
                total_nodes=len(proto_nodes),
                total_edges=len(proto_edges),
            )
        except Exception as exc:
            logger.error("grpc.graphrag.QueryRelated.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.GraphQueryResponse()

    async def Traverse(
        self, request: cosmos_pb2.TraverseRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TraversalResponse:
        """BFS traversal from a starting node."""
        logger.info("grpc.graphrag.Traverse", node=request.start_node_id)
        try:
            max_depth = request.max_depth if request.max_depth > 0 else 2

            result = await self._svc.traverse(
                node_id=request.start_node_id,
                max_depth=max_depth,
            )

            return cosmos_pb2.TraversalResponse(
                nodes=[_to_proto_node(n) for n in result.nodes],
                edges=[_to_proto_edge(e) for e in result.edges],
                path=[n.id for n in result.nodes],
            )
        except Exception as exc:
            logger.error("grpc.graphrag.Traverse.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TraversalResponse()

    async def TraverseStream(
        self, request: cosmos_pb2.TraverseRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[cosmos_pb2.TraversalNode]:
        """Server-streaming BFS traversal -- yields nodes as they are discovered.

        Unlike the unary ``Traverse`` RPC which returns the full result at once,
        this streams ``TraversalNode`` messages incrementally so the caller can
        begin processing before the entire BFS completes.
        """
        logger.info("grpc.graphrag.TraverseStream", node=request.start_node_id)
        try:
            max_depth = request.max_depth if request.max_depth > 0 else 2
            graph = self._svc._graph

            if not graph.has_node(request.start_node_id):
                return

            visited: Set[str] = set()
            queue: List[Tuple[str, int, str]] = [(request.start_node_id, 0, "")]

            while queue:
                current, depth, parent = queue.pop(0)
                if current in visited or depth > max_depth:
                    continue
                visited.add(current)

                node_model = self._svc._node_to_model(current)
                proto_node = _to_proto_node(node_model)

                yield cosmos_pb2.TraversalNode(
                    node=proto_node,
                    depth=depth,
                    parent_id=parent,
                )

                for neighbor in graph.successors(current):
                    if neighbor not in visited and depth + 1 <= max_depth:
                        queue.append((neighbor, depth + 1, current))
                for neighbor in graph.predecessors(current):
                    if neighbor not in visited and depth + 1 <= max_depth:
                        queue.append((neighbor, depth + 1, current))

        except Exception as exc:
            logger.error("grpc.graphrag.TraverseStream.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))

    async def FindNodes(
        self, request: cosmos_pb2.FindNodesRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.FindNodesResponse:
        """Filter and return nodes from the graph."""
        logger.info("grpc.graphrag.FindNodes", node_type=request.node_type)
        try:
            node_type = None
            if request.node_type:
                try:
                    node_type = NodeType(request.node_type)
                except ValueError:
                    pass

            limit = request.limit if request.limit > 0 else 50
            repo_id = request.repo_id or None
            label_contains = request.name_pattern or None

            nodes = await self._svc.find_nodes(
                node_type=node_type,
                repo_id=repo_id,
                label_contains=label_contains,
                limit=limit,
            )

            return cosmos_pb2.FindNodesResponse(
                nodes=[_to_proto_node(n) for n in nodes],
            )
        except Exception as exc:
            logger.error("grpc.graphrag.FindNodes.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.FindNodesResponse()

    async def GetStats(
        self, request: cosmos_pb2.StatsRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.GraphStatsResponse:
        """Return aggregate graph statistics."""
        logger.info("grpc.graphrag.GetStats")
        try:
            stats = await self._svc.get_stats()

            return cosmos_pb2.GraphStatsResponse(
                total_nodes=stats.total_nodes,
                total_edges=stats.total_edges,
                nodes_by_type=stats.node_type_counts,
                edges_by_type=stats.edge_type_counts,
            )
        except Exception as exc:
            logger.error("grpc.graphrag.GetStats.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.GraphStatsResponse()

    async def GetShortestPath(
        self, request: cosmos_pb2.PathRequest, context: grpc.aio.ServicerContext
    ) -> cosmos_pb2.TraversalResponse:
        """Find shortest path between two nodes."""
        logger.info(
            "grpc.graphrag.GetShortestPath",
            from_node=request.from_node_id,
            to_node=request.to_node_id,
        )
        try:
            result = await self._svc.get_shortest_path(
                source_id=request.from_node_id,
                target_id=request.to_node_id,
            )

            if result is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(
                    f"No path found between {request.from_node_id} and {request.to_node_id}"
                )
                return cosmos_pb2.TraversalResponse()

            return cosmos_pb2.TraversalResponse(
                nodes=[_to_proto_node(n) for n in result.nodes],
                edges=[_to_proto_edge(e) for e in result.edges],
                path=[n.id for n in result.nodes],
            )
        except Exception as exc:
            logger.error("grpc.graphrag.GetShortestPath.error", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return cosmos_pb2.TraversalResponse()

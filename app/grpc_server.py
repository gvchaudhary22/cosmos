"""
COSMOS gRPC server -- runs alongside FastAPI on port 50051.

Registers all five service servicers (GraphRAG, VectorStore, Sandbox,
ReportAgent, Training) and optionally enables gRPC server reflection for
debugging with tools like ``grpcurl``.
"""

import grpc
from concurrent import futures
import structlog

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc
from app.grpc_servicers.graphrag_servicer import GraphRAGServicer
from app.grpc_servicers.vectorstore_servicer import VectorStoreServicer
from app.grpc_servicers.sandbox_servicer import SandboxServicer
from app.grpc_servicers.report_servicer import ReportAgentServicer
from app.grpc_servicers.training_servicer import TrainingServicer

logger = structlog.get_logger(__name__)


async def start_grpc_server(port: int = 50051) -> grpc.aio.Server:
    """Create, configure, and start the async gRPC server.

    Returns the running ``grpc.aio.Server`` instance so the caller (the
    FastAPI lifespan) can stop it gracefully during shutdown.
    """
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
            ("grpc.keepalive_time_ms", 30000),
            ("grpc.keepalive_timeout_ms", 10000),
        ],
    )

    # Register all servicers
    cosmos_pb2_grpc.add_GraphRAGServiceServicer_to_server(GraphRAGServicer(), server)
    cosmos_pb2_grpc.add_VectorStoreServiceServicer_to_server(VectorStoreServicer(), server)
    cosmos_pb2_grpc.add_SandboxServiceServicer_to_server(SandboxServicer(), server)
    cosmos_pb2_grpc.add_ReportAgentServiceServicer_to_server(ReportAgentServicer(), server)
    cosmos_pb2_grpc.add_TrainingServiceServicer_to_server(TrainingServicer(), server)

    # NOTE: Page Intelligence (Pillar 4) is available via REST only for now.
    # A PageIntelligenceService gRPC servicer can be added here when the
    # proto definition is extended with page/role query RPCs.

    # Enable reflection for debugging with grpcurl / grpcui
    try:
        from grpc_reflection.v1alpha import reflection

        SERVICE_NAMES = (
            cosmos_pb2.DESCRIPTOR.services_by_name["GraphRAGService"].full_name,
            cosmos_pb2.DESCRIPTOR.services_by_name["VectorStoreService"].full_name,
            cosmos_pb2.DESCRIPTOR.services_by_name["SandboxService"].full_name,
            cosmos_pb2.DESCRIPTOR.services_by_name["ReportAgentService"].full_name,
            cosmos_pb2.DESCRIPTOR.services_by_name["TrainingService"].full_name,
            reflection.SERVICE_NAME,
        )
        reflection.enable_server_reflection(SERVICE_NAMES, server)
        logger.info("grpc.reflection_enabled", services=len(SERVICE_NAMES) - 1)
    except ImportError:
        logger.warning("grpc.reflection_unavailable", hint="pip install grpcio-reflection")

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    logger.info("grpc.server_started", address=listen_addr, port=port)
    return server

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import structlog

from app.config import settings
from app.db.session import init_db, close_db
from app.api.routes import router as api_router, health_router
from app.api.endpoints.metrics import router as metrics_router
from app.monitoring.metrics import init_metrics
from app.middleware.metrics import MetricsMiddleware
from app.middleware.rate_limiter import HTTPRateLimiter
from app.events.kafka_bus import EventBus, Topic
from app.events.handlers import (
    handle_query_completed,
    handle_learning_insight,
    handle_feedback,
    handle_kb_updated,
)
from app.events.order_handler import handle_order_webhook

logger = structlog.get_logger()


def _init_react_engine():
    """Create and return a ReActEngine with classifier, tool_registry, llm_client, guardrails.

    Returns None if any critical dependency fails to initialize.
    """
    try:
        from cosmos.app.engine.classifier import IntentClassifier
        from cosmos.app.engine.react import ReActEngine
        from cosmos.app.engine.llm_client import LLMClient
        from app.guardrails.setup import create_guardrail_pipeline

        classifier = IntentClassifier(hinglish_enabled=settings.HINGLISH_ENABLED)
        guardrails = create_guardrail_pipeline()

        # Build LLM client (works with or without API key — raises on use if missing)
        llm_client = LLMClient(api_key=settings.ANTHROPIC_API_KEY)

        # Build tool registry — requires MCAPI and ELK clients
        tool_registry = None
        try:
            from app.clients.mcapi import MCAPIClient
            from app.clients.elk import ELKClient
            from app.tools.setup import create_tool_registry

            mcapi = MCAPIClient(
                base_url=settings.MCAPI_BASE_URL,
                timeout=settings.MCAPI_TIMEOUT,
                rate_limit=settings.MCAPI_RATE_LIMIT,
            )
            elk = ELKClient(
                hosts=settings.ELASTICSEARCH_HOSTS,
                username=settings.ELASTICSEARCH_USERNAME,
                password=settings.ELASTICSEARCH_PASSWORD,
            )
            tool_registry = create_tool_registry(mcapi, elk)
        except Exception as exc:
            logger.warning("react_engine.tool_registry_failed", error=str(exc))

        # Use a minimal dict-like registry wrapper if the real one failed
        if tool_registry is None:
            from app.tools.registry import ToolRegistry
            tool_registry = ToolRegistry()

        engine = ReActEngine(classifier, tool_registry, llm_client, guardrails)
        logger.info("react_engine.initialized")
        return engine
    except Exception as exc:
        logger.warning("react_engine.init_failed", error=str(exc))
        return None


def _init_tournament_engine():
    """Create and return a TournamentEngine.

    Returns None if initialization fails.
    """
    try:
        from cosmos.app.brain.tournament import TournamentEngine, TournamentMode
        engine = TournamentEngine(mode=TournamentMode.TOURNAMENT)
        logger.info("tournament_engine.initialized")
        return engine
    except Exception as exc:
        logger.warning("tournament_engine.init_failed", error=str(exc))
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting COSMOS", env=settings.ENV, port=settings.PORT)
    init_metrics()
    await init_db()

    # Initialize Cosmos Workflow Settings cache (three-layer: MySQL → Postgres → in-memory)
    try:
        from app.services.workflow_settings_repo import WorkflowSettingsRepo
        from app.services.workflow_settings import CosmosSettingsCache
        _settings_repo = WorkflowSettingsRepo()
        await _settings_repo.ensure_table()
        settings_cache = CosmosSettingsCache(_settings_repo)
        await settings_cache.initialize()
        app.state.settings_cache = settings_cache
    except Exception as _e:
        logger.warning("cosmos_settings_cache.init_failed", error=str(_e))
        from app.services.workflow_settings import CosmosSettingsCache, WorkflowSettings
        # Fallback: no-op repo, defaults only
        class _NopRepo:
            async def load(self): return WorkflowSettings.balanced()
            async def upsert(self, _): pass
        app.state.settings_cache = CosmosSettingsCache(_NopRepo())
        await app.state.settings_cache.initialize()

    # Initialize S3 client (no-op if S3_BUCKET not set)
    try:
        from app.services.s3_client import S3Client
        s3_client = S3Client.from_settings()
        app.state.s3_client = s3_client
        if s3_client.enabled:
            logger.info("S3 client initialized", bucket=settings.S3_BUCKET, region=settings.AWS_REGION)
        else:
            logger.info("S3 client disabled (S3_BUCKET not set)")
    except Exception as e:
        logger.warning("S3 client failed to init", error=str(e))
        app.state.s3_client = None

    # Initialize KB file index schema (persistent hash tracker)
    try:
        from app.services.kb_file_index import KBFileIndexService
        fi = KBFileIndexService()
        await fi.ensure_schema()
        app.state.kb_file_index = fi
        logger.info("KB file index schema ensured")
    except Exception as e:
        logger.warning("KB file index schema failed", error=str(e))
        app.state.kb_file_index = None

    # Initialize GraphRAG service (load persisted graph into memory)
    try:
        from app.services.graphrag import graphrag_service
        await graphrag_service.load_from_db()
        app.state.graphrag = graphrag_service
        logger.info("GraphRAG service loaded")
    except Exception as e:
        logger.warning("GraphRAG service failed to load", error=str(e))
        app.state.graphrag = None

    # Ensure lexical search GIN indexes exist (M5 fix)
    try:
        from app.graph.retrieval import ensure_lexical_indexes
        await ensure_lexical_indexes()
    except Exception as e:
        logger.warning("Lexical index bootstrap failed", error=str(e))

    # Initialize ReAct engine
    app.state.react_engine = _init_react_engine()

    # Initialize Tournament engine
    app.state.tournament_engine = _init_tournament_engine()

    # Initialize Kafka event bus (before brain wiring so it can be passed in)
    event_bus = EventBus(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=settings.KAFKA_CONSUMER_GROUP,
        enabled=settings.KAFKA_ENABLED,
        security_protocol=settings.KAFKA_SECURITY_PROTOCOL,
        sasl_mechanism=settings.KAFKA_SASL_MECHANISM,
        sasl_username=settings.KAFKA_CLUSTER_USERNAME,
        sasl_password=settings.KAFKA_CLUSTER_PASSWORD,
    )
    # Internal COSMOS topics
    event_bus.register_handler(Topic.QUERY_COMPLETED, handle_query_completed)
    event_bus.register_handler(Topic.LEARNING_INSIGHT, handle_learning_insight)
    event_bus.register_handler(Topic.FEEDBACK_SUBMITTED, handle_feedback)
    event_bus.register_handler(Topic.KB_UPDATED, handle_kb_updated)
    # External: Shiprocket Channels order webhooks
    event_bus.register_handler(Topic.SC_ORDERS_WC, handle_order_webhook)
    await event_bus.start()
    app.state.event_bus = event_bus

    # Load KB safety index for guardrails (blast_radius, PII fields, approval_mode)
    kb_path = os.environ.get(
        "KB_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "..", "mars", "knowledge_base", "shiprocket",
        ),
    )
    kb_path = os.path.normpath(kb_path)
    app.state.kb_path = kb_path   # expose for pipeline endpoints
    if os.path.isdir(kb_path):
        # Load KB safety metadata for guardrails
        try:
            from app.guardrails.kb_guardrails import kb_safety_index
            safety_stats = kb_safety_index.load_from_kb(kb_path)
            logger.info("KB safety index loaded", **safety_stats)
        except Exception as e:
            logger.warning("KB safety index failed to load", error=str(e))

        try:
            from app.brain.setup import create_brain
            from app.brain.wiring import wire_brain
            from app.brain.cache import SemanticCache
            from app.brain.grel import GRELEngine

            brain = create_brain(kb_path)

            # Create semantic cache
            cache = SemanticCache(
                indexer=brain["indexer"],
                ttl_seconds=3600,
            )

            # Create GREL engine
            grel_engine = GRELEngine(
                llm_client=None,  # Wired later when LLM client is available
                mcapi_client=None,
            )

            # Initialize vectorstore BEFORE wire_brain (H1 fix: was used before init)
            try:
                from app.services.vectorstore import VectorStoreService
                vectorstore_svc = VectorStoreService()
                logger.info("vectorstore_service.early_init")
            except Exception as vs_err:
                logger.warning("vectorstore.early_init_failed", error=str(vs_err))
                vectorstore_svc = None

            # Wire everything: GREL→Pipeline→Cache+Router+N8N+Kafka
            brain = wire_brain(
                brain=brain,
                cache=cache,
                grel_engine=grel_engine,
                event_bus=event_bus,
                mars_base_url=settings.MARS_BASE_URL if settings.MARS_BRIDGE_ENABLED else None,
                n8n_webhook_url=os.environ.get("N8N_WEBHOOK_URL"),
                scan_interval_seconds=int(os.environ.get("KB_SCAN_INTERVAL", "300")),
                vectorstore=vectorstore_svc,
            )

            # Start background KB scanner
            scheduler = brain.get("scheduler")
            if scheduler:
                await scheduler.start()

            app.state.brain = brain
            app.state.grel_engine = grel_engine
            app.state.semantic_cache = cache
            logger.info("RAG Brain initialized and wired", document_count=brain["document_count"])

            # Initialize Page Intelligence (Pillar 4)
            try:
                from app.services.page_intelligence import PageIntelligenceService
                page_service = PageIntelligenceService(kb_path)
                stats = await page_service.load_from_kb()
                app.state.page_intelligence = page_service
                logger.info("Page Intelligence loaded", **stats)
            except Exception as e:
                logger.warning("Page Intelligence failed to load", error=str(e))
                app.state.page_intelligence = None

            # Initialize Hybrid Query Orchestrator (two-stage parallel probe + conditional deepening)
            try:
                from app.services.query_orchestrator import QueryOrchestrator
                from app.engine.classifier import IntentClassifier

                # Reuse classifier from react_engine if available, otherwise create new
                react_engine = getattr(app.state, "react_engine", None)
                if react_engine and hasattr(react_engine, "classifier"):
                    orch_classifier = react_engine.classifier
                else:
                    orch_classifier = IntentClassifier(hinglish_enabled=settings.HINGLISH_ENABLED)

                # Reuse vectorstore from early init (H1 fix)
                if vectorstore_svc is None:
                    try:
                        from app.services.vectorstore import VectorStoreService
                        vectorstore_svc = VectorStoreService()
                    except Exception as vs_err:
                        logger.warning("orchestrator.vectorstore_unavailable", error=str(vs_err))

                orchestrator = QueryOrchestrator(
                    classifier=orch_classifier,
                    vectorstore=vectorstore_svc,
                    graphrag=getattr(app.state, "graphrag", None),
                    page_intelligence=getattr(app.state, "page_intelligence", None),
                    react_engine=getattr(app.state, "react_engine", None),
                    event_bus=getattr(app.state, "event_bus", None),
                    semantic_cache=getattr(app.state, "semantic_cache", None),
                )
                app.state.query_orchestrator = orchestrator
                logger.info("Hybrid Query Orchestrator initialized")

                # Initialize Tier 2: Codebase Intelligence (retrieval-driven, pre-indexed)
                try:
                    from app.engine.codebase_intelligence import CodebaseIntelligence
                    repos_path = os.path.join(
                        os.path.dirname(kb_path), "..", "repos", "shiprocket"
                    )
                    repos_path = os.path.normpath(repos_path)
                    if os.path.isdir(repos_path) and vectorstore_svc:
                        codebase_intel = CodebaseIntelligence(repos_path, vectorstore=vectorstore_svc)
                        ingest_stats = await codebase_intel.ingest()
                        app.state.codebase_intelligence = codebase_intel
                        logger.info("Codebase Intelligence ingested", **ingest_stats)
                    else:
                        app.state.codebase_intelligence = None
                        logger.info("Codebase Intelligence: repos or vectorstore not available")
                except Exception as e:
                    logger.warning("Codebase Intelligence failed to init", error=str(e))
                    app.state.codebase_intelligence = None

                # Initialize Tier 3: Safe DB Tool (calls MARS via HTTP for DB queries)
                try:
                    from app.engine.safe_query_executor import SafeDBTool
                    mars_url = os.environ.get("MARS_BASE_URL", "http://localhost:8080")
                    app.state.safe_db_tool = SafeDBTool(mars_http_url=mars_url)
                    logger.info("Safe DB Tool initialized", mars_url=mars_url)
                except Exception as e:
                    logger.warning("Safe DB Tool failed to init", error=str(e))
                    app.state.safe_db_tool = None

                # Wire Tier 2 + Tier 3 into orchestrator
                if orchestrator:
                    orchestrator.codebase_intel = getattr(app.state, "codebase_intelligence", None)
                    orchestrator.safe_db_tool = getattr(app.state, "safe_db_tool", None)

                # Initialize Training Pipeline (master ingestion orchestrator)
                try:
                    from app.services.training_pipeline import TrainingPipeline
                    data_dir = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data",
                    )
                    training_pipeline = TrainingPipeline(
                        vectorstore=vectorstore_svc,
                        kb_path=kb_path,
                        data_dir=data_dir,
                        codebase_intel=getattr(app.state, "codebase_intelligence", None),
                    )
                    app.state.training_pipeline = training_pipeline
                    app.state.vectorstore = vectorstore_svc
                    logger.info("Training Pipeline initialized", kb_path=kb_path, data_dir=data_dir)
                except Exception as e:
                    logger.warning("Training Pipeline failed to init", error=str(e))
                    app.state.training_pipeline = None
            except Exception as e:
                logger.warning("Hybrid Query Orchestrator failed to init", error=str(e))
                app.state.query_orchestrator = None
        except Exception as e:
            logger.warning("Failed to initialize RAG Brain", error=str(e))
            app.state.brain = None
    else:
        logger.info("Knowledge base path not found, brain disabled", kb_path=kb_path)
        app.state.brain = None

    # Start gRPC server (port 50051) alongside FastAPI
    grpc_server = None
    try:
        from app.grpc_server import start_grpc_server
        grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
        grpc_server = await start_grpc_server(port=grpc_port)
        app.state.grpc_server = grpc_server
    except Exception as e:
        logger.warning("gRPC server failed to start", error=str(e))

    yield

    # Shutdown settings cache refresh loop
    sc = getattr(app.state, "settings_cache", None)
    if sc:
        await sc.stop()

    # Shutdown Kafka event bus
    event_bus = getattr(app.state, "event_bus", None)
    if event_bus:
        await event_bus.stop()
        logger.info("Kafka event bus stopped")

    # Shutdown gRPC server
    if grpc_server:
        await grpc_server.stop(grace=5)
        logger.info("gRPC server stopped")

    # Shutdown: stop scheduler
    brain = getattr(app.state, "brain", None)
    if brain and brain.get("scheduler"):
        await brain["scheduler"].stop()

    await close_db()
    logger.info("COSMOS shutdown complete")

app = FastAPI(
    title="COSMOS — AI Engine for Shiprocket ICRM",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware — order matters: outermost first
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(MetricsMiddleware)
app.add_middleware(HTTPRateLimiter, default_limit=60, window_seconds=60)

app.include_router(api_router, prefix="/cosmos/api/v1")
app.include_router(health_router, prefix="/cosmos")
app.include_router(metrics_router, prefix="/cosmos")

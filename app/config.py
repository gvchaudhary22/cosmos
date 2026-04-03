from dotenv import load_dotenv
load_dotenv()  # inject .env into os.environ before any module reads it directly

from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Service
    PORT: int = 10001
    ENV: str = "development"
    LOG_LEVEL: str = "info"

    # Database (MySQL — MARS DB for relational data)
    DATABASE_URL: str = "mysql+aiomysql://root:@127.0.0.1:3309/mars"
    DATABASE_POOL_SIZE: int = 10           # Max 10 connections to MARS MySQL
    DATABASE_MAX_OVERFLOW: int = 5         # Allow 5 extra under burst (total max: 15)
    DATABASE_POOL_RECYCLE: int = 1800      # Recycle connections after 30 min
    DATABASE_POOL_PRE_PING: bool = True    # Health check before using connection
    DATABASE_TIMEOUT: int = 30             # Connection timeout seconds

    # Prod Slave DB (read-only, for Tier 3 safe DB queries via MARS)
    PROD_SLAVE_POOL_SIZE: int = 5          # Max 5 connections to prod slave
    PROD_SLAVE_MAX_OVERFLOW: int = 2       # Allow 2 extra under burst (total max: 7)

    # MCAPI
    MCAPI_BASE_URL: str = "https://apiv2.shiprocket.in"
    MCAPI_AUTH_MODE: str = "user_jwt"
    MCAPI_SERVICE_TOKEN: Optional[str] = None
    MCAPI_TIMEOUT: int = 10
    MCAPI_RATE_LIMIT: int = 100

    # LLM / AI
    ANTHROPIC_API_KEY: Optional[str] = None
    LLM_MODE: str = "cli"  # "api" | "cli" | "hybrid" — cli uses Max plan claude binary
    LLM_MODEL_HAIKU: str = "claude-haiku-4-5-20251001"
    LLM_MODEL_SONNET: str = "claude-sonnet-4-6"
    LLM_MODEL_OPUS: str = "claude-opus-4-6"
    LLM_TIMEOUT: int = 30

    # Cost Governance (Phase 4)
    COST_DAILY_BUDGET_USD: float = 50.0
    COST_SESSION_BUDGET_USD: float = 1.0

    # AI Gateway + OpenAI embedding
    AIGATEWAY_API_KEY: Optional[str] = None
    AIGATEWAY_URL: str = "https://aigateway.shiprocket.in"
    AIGATEWAY_EMBEDDING_MODEL: str = "text-embedding-3-small"
    AIGATEWAY_PROVIDER: str = "openai"
    VOYAGE_API_KEY: Optional[str] = None

    # Neo4j
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "cosmospass123"

    # Qdrant (vector database — replaces pgvector)
    QDRANT_URL: str = "http://127.0.0.1:6333"
    QDRANT_COLLECTION: str = "cosmos_embeddings"

    # MARS MySQL (direct connection for relational data)
    MARS_DB_HOST: str = "127.0.0.1"
    MARS_DB_PORT: str = "3309"
    MARS_DB_USER: str = "root"
    MARS_DB_PASSWORD: str = ""
    MARS_DB_NAME: str = "mars"

    # Knowledge Base path
    KB_PATH: str = ""

    # Elasticsearch
    ELASTICSEARCH_HOSTS: str = "http://localhost:9200"
    ELASTICSEARCH_USERNAME: Optional[str] = None
    ELASTICSEARCH_PASSWORD: Optional[str] = None

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    SESSION_TTL: int = 1800

    # MARS Backend
    MARS_BASE_URL: str = "http://localhost:8080"
    MARS_TIMEOUT: float = 30.0
    MARS_BRIDGE_ENABLED: bool = True
    HINGLISH_ENABLED: bool = True

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_ENABLED: bool = True
    KAFKA_CONSUMER_GROUP: str = "cosmos-workers"
    KAFKA_CLUSTER_USERNAME: Optional[str] = None
    KAFKA_CLUSTER_PASSWORD: Optional[str] = None
    KAFKA_SECURITY_PROTOCOL: str = "PLAINTEXT"  # PLAINTEXT for local, SASL_PLAINTEXT for staging
    KAFKA_SASL_MECHANISM: str = "PLAIN"

    # Shiprocket Channels Kafka (external topics)
    KAFKA_TOPIC_ORDERS_WC: str = "sc_webhook_orders_wc"

    # OpenAI Direct API (fallback when AI Gateway rate limit hit)
    OPENAI_API_KEY: Optional[str] = None

    # S3 Storage (training exports + KB file sync)
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "ap-south-1"
    S3_BUCKET: Optional[str] = None
    S3_KB_PREFIX: str = "knowledge_base/shiprocket"          # KB YAMLs in S3
    S3_TRAINING_PREFIX: str = "cosmos/training-exports"       # DPO/SFT exports
    S3_EMBEDDING_BACKUP_PREFIX: str = "cosmos/embedding-backups"
    S3_ENABLED: bool = False  # auto-set to True in __init__ if S3_BUCKET present

    # KB Sync schedule (Mon–Thu daily ingestion of 50+ file changes)
    KB_SYNC_ENABLED: bool = True
    KB_SYNC_BATCH_SIZE: int = 100          # files per scheduler tick
    KB_SYNC_INTERVAL_SECONDS: int = 300    # 5-min default, override via KB_SCAN_INTERVAL

    # Feature Flags
    FF_DRY_RUN: bool = False
    FF_PROMPT_SAFETY: bool = True
    FF_TOKEN_ECONOMICS: bool = True

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

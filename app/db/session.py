"""
Database Session — Connection pooling for MARS MySQL.

Pool settings:
  MARS DB (MySQL via aiomysql):
    pool_size: 10 (max 10 connections)
    max_overflow: 5 (burst up to 15 total)
    pool_recycle: 1800 (recycle after 30 min)
    pool_pre_ping: True (health check before use)
    pool_timeout: 30 (wait 30s for available connection)
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

# Use MARS MySQL for relational data
MARS_DB_HOST = os.environ.get("MARS_DB_HOST", settings.MARS_DB_HOST)
MARS_DB_PORT = os.environ.get("MARS_DB_PORT", settings.MARS_DB_PORT)
MARS_DB_USER = os.environ.get("MARS_DB_USER", settings.MARS_DB_USER)
MARS_DB_PASSWORD = os.environ.get("MARS_DB_PASSWORD", settings.MARS_DB_PASSWORD)
MARS_DB_NAME = os.environ.get("MARS_DB_NAME", settings.MARS_DB_NAME)

DATABASE_URL = f"mysql+aiomysql://{MARS_DB_USER}:{MARS_DB_PASSWORD}@{MARS_DB_HOST}:{MARS_DB_PORT}/{MARS_DB_NAME}"

# MARS MySQL — max 10 connections + 5 overflow
engine = create_async_engine(
    DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,          # 10
    max_overflow=settings.DATABASE_MAX_OVERFLOW,     # 5 (total max: 15)
    pool_recycle=settings.DATABASE_POOL_RECYCLE,     # 1800 seconds (30 min)
    pool_pre_ping=settings.DATABASE_POOL_PRE_PING,   # True
    pool_timeout=settings.DATABASE_TIMEOUT,           # 30 seconds
    echo=settings.ENV == "development",
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    from app.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_engine():
    """Return the SQLAlchemy async engine (used by health checks)."""
    return engine


async def close_db():
    await engine.dispose()

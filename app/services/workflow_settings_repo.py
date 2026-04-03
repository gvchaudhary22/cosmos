"""
Cosmos Workflow Settings Repo — Layer 2 (Postgres write-through cache).

Single-row table `cosmos_settings_cache` with a  constraint.
On first call the row is created from balanced defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import text
import structlog

from app.db.session import AsyncSessionLocal
from app.services.workflow_settings import WorkflowSettings

logger = structlog.get_logger()

# The CREATE TABLE DDL — idempotent via IF NOT EXISTS.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cosmos_settings_cache (
    id         INTEGER PRIMARY KEY ,
    settings   JSON,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

# Seed the single row if absent.
_SEED_ROW_SQL = """
INSERT INTO cosmos_settings_cache (id, settings, updated_at)
VALUES (1, :settings, NOW())
ON CONFLICT (id) DO NOTHING;
"""

_SELECT_SQL = "SELECT settings FROM cosmos_settings_cache WHERE id = 1"

_UPSERT_SQL = """
INSERT INTO cosmos_settings_cache (id, settings, updated_at)
VALUES (1, :settings, NOW())
ON CONFLICT (id) DO UPDATE SET
    settings   = EXCLUDED.settings,
    updated_at = EXCLUDED.updated_at;
"""


class WorkflowSettingsRepo:
    """Postgres-backed persistence for WorkflowSettings (Layer 2)."""

    async def ensure_table(self) -> None:
        """Create the cache table if it does not exist and seed the default row."""
        import json
        defaults = WorkflowSettings.balanced().to_dict()
        async with AsyncSessionLocal() as session:
            await session.execute(text(_CREATE_TABLE_SQL))
            await session.execute(
                text(_SEED_ROW_SQL),
                {"settings": json.dumps(defaults)},
            )
            await session.commit()
        logger.info("workflow_settings_repo.table_ensured")

    async def load(self) -> WorkflowSettings:
        """Read the single settings row from Postgres."""
        import json
        async with AsyncSessionLocal() as session:
            result = await session.execute(text(_SELECT_SQL))
            row = result.fetchone()
            if row is None:
                logger.warning("workflow_settings_repo.no_row_found", fallback="balanced")
                return WorkflowSettings.balanced()
            raw = row[0]
            if isinstance(raw, str):
                raw = json.loads(raw)
            return WorkflowSettings.from_dict(raw)

    async def upsert(self, settings: WorkflowSettings) -> None:
        """Write settings to Postgres (upsert on id=1)."""
        import json
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(_UPSERT_SQL),
                {"settings": json.dumps(settings.to_dict())},
            )
            await session.commit()
        logger.info("workflow_settings_repo.upserted", quality_mode=settings.quality_mode)

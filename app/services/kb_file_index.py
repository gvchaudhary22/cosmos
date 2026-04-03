"""
KBFileIndexService — DB-backed file hash tracker.

Replaces KBUpdatePipeline._file_hashes (in-memory dict) with a persistent
table so that:
  1. File hashes survive server restarts
  2. PR webhooks can mark files as pending (status=0) before re-index
  3. The UI can show indexed / pending / failed counts per repo
  4. S3 ETags are stored alongside local MD5s for S3-synced files

Table: cosmos_kb_file_index
  status: 0=pending, 1=indexed, 2=failed

Usage (from KBScanScheduler):
    svc = KBFileIndexService()
    changed = await svc.diff_and_mark_pending(kb_path)
    pending = await svc.get_pending(repo_id="MultiChannel_API", limit=100)
    await svc.mark_indexed(file_path, entity_id, entity_type)
    await svc.mark_failed(file_path, error_msg)
"""

import hashlib
import os
from datetime import datetime
from typing import Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

STATUS_PENDING = 0
STATUS_INDEXED = 1
STATUS_FAILED  = 2


class KBFileIndexService:
    """Async service for cosmos_kb_file_index CRUD."""

    TABLE = "cosmos_kb_file_index"

    # ------------------------------------------------------------------
    # Ensure table exists (called at startup)
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create table + indexes if they don't exist yet."""
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        id          CHAR(36) PRIMARY KEY,
                        repo_id     VARCHAR(255) NOT NULL DEFAULT '',
                        file_path   VARCHAR(1000) NOT NULL,
                        file_hash   VARCHAR(64)  NOT NULL DEFAULT '',
                        entity_id   VARCHAR(500) NOT NULL DEFAULT '',
                        entity_type VARCHAR(100) NOT NULL DEFAULT '',
                        status      SMALLINT NOT NULL DEFAULT 0,
                        s3_key      VARCHAR(1000),
                        s3_etag     VARCHAR(64),
                        last_indexed_at TIMESTAMP,
                        error_msg   TEXT,
                        created_at  TIMESTAMP NOT NULL DEFAULT now(),
                        updated_at  TIMESTAMP NOT NULL DEFAULT now(),
                        CONSTRAINT uq_kb_file_repo_path UNIQUE (repo_id, file_path)
                    )
                """))
                for idx_sql in [
                    f"CREATE INDEX idx_kb_file_status ON {self.TABLE} (status)",
                    f"CREATE INDEX idx_kb_file_repo ON {self.TABLE} (repo_id)",
                ]:
                    try:
                        await session.execute(text(idx_sql))
                    except Exception:
                        pass  # index already exists (errno 1061)
                await session.commit()
                logger.info("kb_file_index.schema_ensured")
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.schema_error", error=str(e))

    # ------------------------------------------------------------------
    # Change detection — walk disk, compare hashes, mark pending
    # ------------------------------------------------------------------

    async def diff_and_mark_pending(
        self,
        kb_path: str,
        repo_id: Optional[str] = None,
        batch_size: int = 100,
    ) -> List[Dict]:
        """Walk KB directory, hash each YAML file, compare with DB.

        Files that are new or whose MD5 has changed are marked status=0.
        Returns list of changed file dicts for the caller to process.

        This is the persistent replacement for KBUpdatePipeline._file_hashes.
        """
        if not os.path.isdir(kb_path):
            return []

        # 1. Build current file-hash map from disk
        disk_hashes: Dict[str, str] = {}  # rel_path -> md5
        for root, _dirs, files in os.walk(kb_path):
            for fname in files:
                if not fname.endswith((".yaml", ".yml")):
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, kb_path)
                disk_hashes[rel] = _md5_file(full)

        # Filter by repo_id if specified (e.g. only MultiChannel_API)
        if repo_id:
            prefix = repo_id + os.sep
            disk_hashes = {k: v for k, v in disk_hashes.items() if k.startswith(prefix)}

        # 2. Load stored hashes from DB
        stored_hashes = await self._load_hashes(kb_path, repo_id)

        # 3. Find changed / new files
        changed = []
        for rel_path, md5 in disk_hashes.items():
            stored_md5 = stored_hashes.get(rel_path)
            if stored_md5 != md5:
                change_type = "new" if stored_md5 is None else "modified"
                changed.append({"path": rel_path, "change": change_type, "hash": md5})

        # Deleted files → mark failed so they're visible in UI
        for rel_path in stored_hashes:
            if rel_path not in disk_hashes:
                changed.append({"path": rel_path, "change": "deleted", "hash": ""})

        # 4. Upsert changed files as pending in DB (in batches)
        if changed:
            for i in range(0, len(changed), batch_size):
                batch = changed[i:i + batch_size]
                await self._upsert_pending_batch(batch, kb_path)
            logger.info(
                "kb_file_index.diff_complete",
                total_on_disk=len(disk_hashes),
                changed=len(changed),
                repo_id=repo_id or "all",
            )

        return changed

    async def _load_hashes(self, kb_path: str, repo_id: Optional[str]) -> Dict[str, str]:
        """Load (file_path, file_hash) from DB for comparison."""
        async with AsyncSessionLocal() as session:
            try:
                if repo_id:
                    rows = await session.execute(
                        text(f"SELECT file_path, file_hash FROM {self.TABLE} WHERE repo_id = :repo"),
                        {"repo": repo_id},
                    )
                else:
                    rows = await session.execute(
                        text(f"SELECT file_path, file_hash FROM {self.TABLE}")
                    )
                return {row.file_path: row.file_hash for row in rows.fetchall()}
            except Exception:
                return {}

    async def _upsert_pending_batch(self, batch: List[Dict], kb_path: str) -> None:
        """Upsert a batch of files as status=pending into the index."""
        async with AsyncSessionLocal() as session:
            try:
                for item in batch:
                    rel_path = item["path"]
                    file_hash = item["hash"]
                    repo_id = rel_path.split(os.sep)[0] if os.sep in rel_path else ""
                    entity_id, entity_type = _infer_entity(rel_path)

                    if item["change"] == "deleted":
                        # Mark as failed with deletion note
                        await session.execute(text(f"""
                            UPDATE {self.TABLE}
                            SET status = :status, error_msg = :msg, updated_at = now()
                            WHERE file_path = :path AND repo_id = :repo
                        """), {
                            "status": STATUS_FAILED,
                            "msg": "file deleted from disk",
                            "path": rel_path,
                            "repo": repo_id,
                        })
                    else:
                        await session.execute(text(f"""
                            INSERT INTO {self.TABLE}
                                (id, repo_id, file_path, file_hash, entity_id, entity_type, status, updated_at, created_at)
                            VALUES
                                (UUID(), :repo, :path, :hash, :entity_id, :entity_type, :status, now(), now())
                            ON DUPLICATE KEY UPDATE
                                file_hash = VALUES(file_hash),
                                status = VALUES(status),
                                error_msg = NULL,
                                updated_at = now()
                        """), {
                            "repo": repo_id,
                            "path": rel_path,
                            "hash": file_hash,
                            "entity_id": entity_id,
                            "entity_type": entity_type,
                            "status": STATUS_PENDING,
                        })
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.upsert_failed", error=str(e))

    # ------------------------------------------------------------------
    # Get pending files (for scheduler to process)
    # ------------------------------------------------------------------

    async def get_pending(
        self,
        repo_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Return up to `limit` files with status=pending."""
        async with AsyncSessionLocal() as session:
            try:
                if repo_id:
                    rows = await session.execute(
                        text(f"""
                            SELECT file_path, file_hash, entity_id, entity_type, repo_id, s3_key
                            FROM {self.TABLE}
                            WHERE status = 0 AND repo_id = :repo
                            ORDER BY updated_at DESC
                            LIMIT :lim
                        """),
                        {"repo": repo_id, "lim": limit},
                    )
                else:
                    rows = await session.execute(
                        text(f"""
                            SELECT file_path, file_hash, entity_id, entity_type, repo_id, s3_key
                            FROM {self.TABLE}
                            WHERE status = 0
                            ORDER BY updated_at DESC
                            LIMIT :lim
                        """),
                        {"lim": limit},
                    )
                return [dict(row._mapping) for row in rows.fetchall()]
            except Exception as e:
                logger.warning("kb_file_index.get_pending_failed", error=str(e))
                return []

    # ------------------------------------------------------------------
    # Mark indexed / failed
    # ------------------------------------------------------------------

    async def mark_indexed(
        self,
        file_path: str,
        repo_id: str,
        entity_id: str = "",
        entity_type: str = "",
        new_hash: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as session:
            try:
                params: Dict = {
                    "path": file_path,
                    "repo": repo_id,
                    "now": datetime.utcnow(),
                }
                extra_set = ""
                if entity_id:
                    extra_set += ", entity_id = :entity_id"
                    params["entity_id"] = entity_id
                if entity_type:
                    extra_set += ", entity_type = :entity_type"
                    params["entity_type"] = entity_type
                if new_hash:
                    extra_set += ", file_hash = :hash"
                    params["hash"] = new_hash

                await session.execute(text(f"""
                    UPDATE {self.TABLE}
                    SET status = 1, last_indexed_at = :now, error_msg = NULL,
                        updated_at = now() {extra_set}
                    WHERE file_path = :path AND repo_id = :repo
                """), params)
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.mark_indexed_failed", path=file_path, error=str(e))

    async def mark_failed(self, file_path: str, repo_id: str, error_msg: str) -> None:
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    UPDATE {self.TABLE}
                    SET status = 2, error_msg = :msg, updated_at = now()
                    WHERE file_path = :path AND repo_id = :repo
                """), {"path": file_path, "repo": repo_id, "msg": error_msg[:500]})
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.mark_failed_failed", path=file_path, error=str(e))

    # ------------------------------------------------------------------
    # PR webhook — mark files as pending by path list
    # ------------------------------------------------------------------

    async def mark_paths_pending(self, file_paths: List[str], repo_id: str) -> int:
        """Mark a list of file paths as pending (called from GitHub PR webhook).

        Returns count of rows updated.
        """
        if not file_paths:
            return 0

        updated = 0
        async with AsyncSessionLocal() as session:
            try:
                for path in file_paths:
                    result = await session.execute(text(f"""
                        UPDATE {self.TABLE}
                        SET status = 0, updated_at = now()
                        WHERE file_path = :path AND repo_id = :repo
                    """), {"path": path, "repo": repo_id})
                    updated += result.rowcount

                    # If not yet tracked, insert as pending
                    if result.rowcount == 0:
                        entity_id, entity_type = _infer_entity(path)
                        await session.execute(text(f"""
                            INSERT INTO {self.TABLE}
                                (id, repo_id, file_path, entity_id, entity_type, status, created_at, updated_at)
                            VALUES (UUID(), :repo, :path, :eid, :etype, 0, now(), now())
                            ON DUPLICATE KEY UPDATE
                                status = 0, updated_at = now()
                        """), {
                            "repo": repo_id,
                            "path": path,
                            "eid": entity_id,
                            "etype": entity_type,
                        })
                        updated += 1

                await session.commit()
                logger.info("kb_file_index.pr_pending_marked", count=updated, repo=repo_id)
                return updated
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.mark_paths_pending_failed", error=str(e))
                return 0

    # ------------------------------------------------------------------
    # S3 ETag sync — update stored etag after S3 download
    # ------------------------------------------------------------------

    async def update_s3_etag(self, file_path: str, repo_id: str, s3_key: str, etag: str) -> None:
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f"""
                    UPDATE {self.TABLE}
                    SET s3_key = :s3key, s3_etag = :etag, updated_at = now()
                    WHERE file_path = :path AND repo_id = :repo
                """), {"s3key": s3_key, "etag": etag, "path": file_path, "repo": repo_id})
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.s3_etag_update_failed", error=str(e))

    # ------------------------------------------------------------------
    # Stats for UI
    # ------------------------------------------------------------------

    async def get_stats(self, repo_id: Optional[str] = None) -> Dict:
        """Return indexed/pending/failed counts per repo (or overall)."""
        async with AsyncSessionLocal() as session:
            try:
                if repo_id:
                    rows = await session.execute(text(f"""
                        SELECT status, COUNT(*) AS cnt
                        FROM {self.TABLE}
                        WHERE repo_id = :repo
                        GROUP BY status
                    """), {"repo": repo_id})
                else:
                    rows = await session.execute(text(f"""
                        SELECT repo_id, status, COUNT(*) AS cnt
                        FROM {self.TABLE}
                        GROUP BY repo_id, status
                        ORDER BY repo_id, status
                    """))

                counts: Dict = {"indexed": 0, "pending": 0, "failed": 0, "total": 0}
                by_repo: Dict = {}

                for row in rows.fetchall():
                    if repo_id:
                        label = {0: "pending", 1: "indexed", 2: "failed"}.get(row.status, "unknown")
                        counts[label] = int(row.cnt)
                        counts["total"] += int(row.cnt)
                    else:
                        r = row.repo_id or "unknown"
                        label = {0: "pending", 1: "indexed", 2: "failed"}.get(row.status, "unknown")
                        if r not in by_repo:
                            by_repo[r] = {"indexed": 0, "pending": 0, "failed": 0, "total": 0}
                        by_repo[r][label] = int(row.cnt)
                        by_repo[r]["total"] += int(row.cnt)
                        counts[label] = counts.get(label, 0) + int(row.cnt)
                        counts["total"] += int(row.cnt)

                if not repo_id:
                    counts["by_repo"] = by_repo

                return counts
            except Exception as e:
                logger.warning("kb_file_index.stats_failed", error=str(e))
                return {"indexed": 0, "pending": 0, "failed": 0, "total": 0, "error": str(e)}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _md5_file(path: str) -> str:
    """MD5 hash of raw file bytes. Cheap — no YAML parse needed."""
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
    except OSError:
        return ""
    return hasher.hexdigest()


def _infer_entity(rel_path: str) -> tuple[str, str]:
    """Best-effort entity_id + entity_type from a relative KB file path.

    Examples:
      MultiChannel_API/pillar_3_api_mcp_tools/apis/mc_get_order/overview.yaml
        → ("api:MultiChannel_API:mc_get_order", "api_tool")

      MultiChannel_API/pillar_1_schema/tables/orders/_meta.yaml
        → ("table:orders", "schema")
    """
    parts = rel_path.replace("\\", "/").split("/")

    if "pillar_3_api_mcp_tools" in parts and "apis" in parts:
        idx = parts.index("apis")
        if idx + 1 < len(parts):
            repo = parts[0]
            api_id = parts[idx + 1]
            return f"api:{repo}:{api_id}", "api_tool"

    if "pillar_1_schema" in parts and "tables" in parts:
        idx = parts.index("tables")
        if idx + 1 < len(parts):
            table = parts[idx + 1]
            return f"table:{table}", "schema"

    return rel_path, "unknown"

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

        # 1. Build current file-hash map from disk.
        # Run in a thread pool so the synchronous os.walk + MD5 hashing does NOT
        # block the asyncio event loop (44K files can take 60-90s synchronously).
        _SKIP_FILENAMES = frozenset({"medium.yaml", "low.yaml", "medium.yml", "low.yml"})

        def _build_disk_hashes() -> Dict[str, str]:
            """Sync disk walk — called via asyncio.to_thread to avoid blocking."""
            hashes: Dict[str, str] = {}
            scan_root = os.path.join(kb_path, repo_id) if repo_id else kb_path
            for root, _dirs, files in os.walk(scan_root):
                for fname in files:
                    if not fname.endswith((".yaml", ".yml")) or fname in _SKIP_FILENAMES:
                        continue
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, kb_path)
                    hashes[rel] = _md5_file(full)
            return hashes

        import asyncio as _asyncio
        disk_hashes: Dict[str, str] = await _asyncio.to_thread(_build_disk_hashes)

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

        # 5. Remove any medium.yaml / low.yaml rows that were registered in previous runs
        purged = await self._purge_non_embeddable(repo_id)
        if purged:
            logger.info("kb_file_index.purged_non_embeddable", count=purged)

        return changed

    async def _purge_non_embeddable(self, repo_id: Optional[str] = None) -> int:
        """Delete medium.yaml and low.yaml rows from the index.

        These files are never read by the KB reader, so registering them causes
        the pending count to be permanently inflated. Safe to delete — re-scanning
        will not re-add them because diff_and_mark_pending now skips them.
        """
        async with AsyncSessionLocal() as session:
            try:
                if repo_id:
                    result = await session.execute(
                        text(f"""
                            DELETE FROM {self.TABLE}
                            WHERE repo_id = :repo
                              AND (file_path LIKE '%/medium.yaml'
                                   OR file_path LIKE '%/low.yaml'
                                   OR file_path LIKE '%/medium.yml'
                                   OR file_path LIKE '%/low.yml')
                        """),
                        {"repo": repo_id},
                    )
                else:
                    result = await session.execute(
                        text(f"""
                            DELETE FROM {self.TABLE}
                            WHERE file_path LIKE '%/medium.yaml'
                               OR file_path LIKE '%/low.yaml'
                               OR file_path LIKE '%/medium.yml'
                               OR file_path LIKE '%/low.yml'
                        """)
                    )
                await session.commit()
                return result.rowcount or 0
            except Exception as e:
                logger.debug("kb_file_index.purge_non_embeddable_failed", error=str(e))
                return 0

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
                                file_hash = :hash,
                                status = :status,
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

    async def bulk_mark_indexed(self, repo_ids: Optional[List[str]] = None) -> int:
        """Mark all pending files as indexed for the given repos (or all repos).

        Called at the end of a successful pipeline run. Files that failed the
        quality gate are still counted as 'indexed' here (they stay skipped in
        Qdrant, but the file index is a display helper, not source of truth).

        Returns the number of rows updated.
        """
        async with AsyncSessionLocal() as session:
            try:
                if repo_ids:
                    placeholders = ", ".join(f":r{i}" for i in range(len(repo_ids)))
                    params = {f"r{i}": r for i, r in enumerate(repo_ids)}
                    result = await session.execute(text(f"""
                        UPDATE {self.TABLE}
                        SET status = 1, last_indexed_at = now(), updated_at = now()
                        WHERE status = 0 AND repo_id IN ({placeholders})
                    """), params)
                else:
                    result = await session.execute(text(f"""
                        UPDATE {self.TABLE}
                        SET status = 1, last_indexed_at = now(), updated_at = now()
                        WHERE status = 0
                    """))
                await session.commit()
                count = result.rowcount
                logger.info("kb_file_index.bulk_mark_indexed", updated=count, repos=repo_ids)
                return count
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.bulk_mark_indexed_failed", error=str(e))
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
    # cosmos_tools seeder — called by training pipeline after P11 ingest
    # ------------------------------------------------------------------

    async def ingest_tool_definitions(self, kb_path: str) -> Dict:
        """
        Scan all pillar_11_tools/*.yaml files across repos and upsert rows
        into cosmos_tools table.

        Reads:
          - tool_name, description, domain/entity, risk_level, approval_mode
          - endpoints[0].method + path  →  http_method + endpoint_path
          - parameters[]               →  request_schema (Anthropic format)

        Uses content-hash skip: only re-upserts if YAML has changed.
        Called at the end of run_pillar9_10_11() in training_pipeline.py.

        Returns: {"tools_upserted": N, "tools_skipped": N, "errors": N}
        """
        import glob
        import yaml as _yaml

        upserted = 0
        skipped = 0
        errors = 0

        pattern = os.path.join(kb_path, "*", "pillar_11_tools", "*.yaml")
        yaml_files = glob.glob(pattern)

        if not yaml_files:
            logger.warning("kb_file_index.ingest_tools.no_files", pattern=pattern)
            return {"tools_upserted": 0, "tools_skipped": 0, "errors": 0}

        for fpath in sorted(yaml_files):
            try:
                content_hash = _md5_file(fpath)
                tool_id = os.path.splitext(os.path.basename(fpath))[0]  # "orders_create"

                # Content-hash skip: check if already indexed with same hash
                already = await self._get_tool_hash(tool_id)
                if already == content_hash:
                    skipped += 1
                    continue

                with open(fpath, "r", encoding="utf-8") as f:
                    data = _yaml.safe_load(f)

                if not isinstance(data, dict):
                    errors += 1
                    continue

                row = self._p11_yaml_to_tool_row(tool_id, data, content_hash)
                await self._upsert_cosmos_tool(row)
                upserted += 1

            except Exception as exc:
                logger.warning("kb_file_index.ingest_tools.error", file=fpath, error=str(exc))
                errors += 1

        logger.info(
            "kb_file_index.ingest_tools.complete",
            upserted=upserted, skipped=skipped, errors=errors,
        )
        return {"tools_upserted": upserted, "tools_skipped": skipped, "errors": errors}

    @staticmethod
    def _p11_yaml_to_tool_row(tool_id: str, data: Dict, content_hash: str) -> Dict:
        """Convert a parsed P11 YAML dict to a cosmos_tools row dict."""
        import json

        # Endpoint metadata (use first endpoint if list)
        endpoints = data.get("endpoints") or []
        ep = endpoints[0] if endpoints else {}
        http_method = (ep.get("method") or "POST").upper()
        endpoint_path = ep.get("path") or ""

        # Auth type: infer from auth field
        auth_str = (ep.get("auth") or "").lower()
        if "getuserfromtoken" in auth_str or "token" in auth_str:
            auth_type = "seller_token"
        else:
            auth_type = "seller_token"  # default for MCAPI

        # Build Anthropic-format input_schema from parameters list
        params = data.get("parameters") or []
        required = [p["name"] for p in params if p.get("required")]
        properties = {}
        for p in params:
            pname = p.get("name", "")
            ptype = p.get("type", "string")
            # Map YAML type names to JSON Schema types
            type_map = {"boolean": "boolean", "integer": "integer",
                        "number": "number", "array": "array", "object": "object"}
            json_type = type_map.get(ptype, "string")
            prop: Dict = {"type": json_type}
            if p.get("description"):
                prop["description"] = p["description"]
            if p.get("enum"):
                prop["enum"] = p["enum"]
            properties[pname] = prop

        request_schema = {
            "type": "object",
            "required": required,
            "properties": properties,
        }

        # Governance
        risk = (data.get("risk_level") or "medium").lower()
        approval = (data.get("approval_mode") or "confirm").lower()
        # normalise: "confirm" → "manual"
        if approval in ("confirm", "required", "yes"):
            approval = "manual"
        elif approval in ("none", "no", "skip"):
            approval = "auto"

        entity = data.get("domain") or data.get("category") or "unknown"
        intent = "act" if data.get("category") == "action" else "lookup"

        return {
            "id": tool_id,
            "name": data.get("tool_name") or tool_id,
            "display_name": data.get("display_name") or tool_id,
            "description": (data.get("description") or "").strip(),
            "pillar": "P11",
            "entity": entity,
            "intent": intent,
            "http_method": http_method,
            "endpoint_path": endpoint_path,
            "base_url_key": "MCAPI_BASE_URL",
            "auth_type": auth_type,
            "request_schema": json.dumps(request_schema),
            "risk_level": risk,
            "approval_mode": approval,
            "allowed_roles": json.dumps(["operator", "seller"]),
            "kb_doc_id": f"pillar_11_tools/{tool_id}",
            "trust_score": 0.9,
            "training_ready": 1,
            "content_hash": content_hash,
        }

    async def _get_tool_hash(self, tool_id: str) -> Optional[str]:
        """Return stored content_hash for a tool, or None if not indexed yet."""
        async with AsyncSessionLocal() as session:
            try:
                row = await session.execute(
                    text("SELECT content_hash FROM cosmos_tools WHERE id = :id"),
                    {"id": tool_id},
                )
                r = row.first()
                return r[0] if r else None
            except Exception:
                return None

    async def _upsert_cosmos_tool(self, row: Dict) -> None:
        """INSERT ... ON DUPLICATE KEY UPDATE for a single cosmos_tools row."""
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text("""
                    INSERT INTO cosmos_tools
                        (id, name, display_name, description, pillar, entity, intent,
                         http_method, endpoint_path, base_url_key, auth_type,
                         request_schema, risk_level, approval_mode, allowed_roles,
                         kb_doc_id, trust_score, training_ready, content_hash,
                         enabled, created_at, updated_at)
                    VALUES
                        (:id, :name, :display_name, :description, :pillar, :entity, :intent,
                         :http_method, :endpoint_path, :base_url_key, :auth_type,
                         :request_schema, :risk_level, :approval_mode, :allowed_roles,
                         :kb_doc_id, :trust_score, :training_ready, :content_hash,
                         1, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                        name          = VALUES(name),
                        display_name  = VALUES(display_name),
                        description   = VALUES(description),
                        http_method   = VALUES(http_method),
                        endpoint_path = VALUES(endpoint_path),
                        auth_type     = VALUES(auth_type),
                        request_schema = VALUES(request_schema),
                        risk_level    = VALUES(risk_level),
                        approval_mode = VALUES(approval_mode),
                        allowed_roles = VALUES(allowed_roles),
                        trust_score   = VALUES(trust_score),
                        content_hash  = VALUES(content_hash),
                        updated_at    = NOW()
                """), row)
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("kb_file_index.upsert_tool_failed", tool=row.get("id"), error=str(e))

    # ------------------------------------------------------------------
    # Stats for UI
    # ------------------------------------------------------------------

    async def get_pillar_stats(self, repo_id: Optional[str] = None) -> Dict:
        """Return per-repo, per-pillar breakdown of indexed/pending/failed counts.

        Uses only DB data (cosmos_kb_file_index) — no disk scan. Instant.
        Only repos that have at least one tracked file are returned.

        Returns:
            {
              "MultiChannel_API": {
                "total": 100,
                "indexed": 80,
                "pending": 15,
                "failed": 5,
                "by_pillar": {
                  "pillar_1_schema": {"indexed": 30, "pending": 5, "failed": 0},
                  "pillar_3_api_mcp_tools": {"indexed": 50, "pending": 10, "failed": 5},
                }
              },
              ...
            }
        """
        async with AsyncSessionLocal() as session:
            try:
                params = {}
                where = "WHERE 1=1"
                if repo_id:
                    where += " AND repo_id = :repo"
                    params["repo"] = repo_id

                # Extract pillar from file_path: repo/pillar_dir/... → pillar_dir
                # file_path format: "MultiChannel_API/pillar_1_schema/tables/orders/_meta.yaml"
                # SUBSTRING_INDEX(SUBSTRING_INDEX(file_path, '/', 2), '/', -1) → "pillar_1_schema"
                rows = await session.execute(text(f"""
                    SELECT
                        repo_id,
                        SUBSTRING_INDEX(SUBSTRING_INDEX(file_path, '/', 2), '/', -1) AS pillar,
                        status,
                        COUNT(*) AS cnt
                    FROM {self.TABLE}
                    {where}
                    GROUP BY repo_id, pillar, status
                    ORDER BY repo_id, pillar, status
                """), params)

                result: Dict = {}
                status_label = {0: "pending", 1: "indexed", 2: "failed"}

                for row in rows.fetchall():
                    repo = row.repo_id or "unknown"
                    pillar = row.pillar or "unknown"
                    label = status_label.get(row.status, "unknown")
                    cnt = int(row.cnt)

                    if repo not in result:
                        result[repo] = {"total": 0, "indexed": 0, "pending": 0, "failed": 0, "by_pillar": {}}
                    if pillar not in result[repo]["by_pillar"]:
                        result[repo]["by_pillar"][pillar] = {"indexed": 0, "pending": 0, "failed": 0}

                    result[repo][label] = result[repo].get(label, 0) + cnt
                    result[repo]["total"] += cnt
                    result[repo]["by_pillar"][pillar][label] = cnt

                return result
            except Exception as e:
                logger.warning("kb_file_index.pillar_stats_failed", error=str(e))
                return {}

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

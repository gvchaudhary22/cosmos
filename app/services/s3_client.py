"""
S3Client — boto3 wrapper for COSMOS storage operations.

Used for:
  1. KB YAML sync   — download changed files from S3, track ETags
  2. Training exports — upload DPO/SFT JSONL after generation
  3. Embedding backups — periodic backup of cosmos_embeddings

All operations are guarded: if S3_BUCKET is not set, every method is a no-op
that returns a safe default. No code outside this module needs to check
settings.S3_ENABLED.

Usage:
    s3 = S3Client.from_settings()
    await s3.upload_bytes(b"...", "cosmos/training-exports/dpo_2026-03-29.jsonl")
    key_etag_pairs = await s3.list_prefix("knowledge_base/shiprocket/MultiChannel_API/")
"""

import asyncio
import io
import os
from dataclasses import dataclass
from functools import cached_property
from typing import AsyncIterator, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class S3Object:
    key: str
    etag: str          # MD5 ETag from S3, strip surrounding quotes
    size: int
    last_modified: Optional[str] = None


class S3Client:
    """Async-friendly S3 client wrapping boto3.

    boto3 is synchronous; we run it in a thread pool via asyncio.to_thread
    so it doesn't block the FastAPI event loop.
    """

    def __init__(
        self,
        bucket: Optional[str],
        access_key: Optional[str],
        secret_key: Optional[str],
        region: str = "ap-south-1",
    ):
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._enabled = bool(bucket and access_key and secret_key)

        if not self._enabled:
            logger.info("s3_client.disabled", reason="missing bucket or credentials")

    @classmethod
    def from_settings(cls) -> "S3Client":
        """Build from app settings."""
        from app.config import settings
        return cls(
            bucket=settings.S3_BUCKET,
            access_key=settings.AWS_ACCESS_KEY_ID,
            secret_key=settings.AWS_SECRET_ACCESS_KEY,
            region=settings.AWS_REGION,
        )

    @cached_property
    def _boto_client(self):
        """Lazy-init boto3 client. Only called when S3 is enabled."""
        import boto3
        return boto3.client(
            "s3",
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Upload bytes to S3. Returns the S3 key on success, None on failure."""
        if not self._enabled:
            return None

        def _upload():
            self._boto_client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata=metadata or {},
            )
            return key

        try:
            result = await asyncio.to_thread(_upload)
            logger.info("s3.uploaded", key=key, size=len(data))
            return result
        except Exception as e:
            logger.warning("s3.upload_failed", key=key, error=str(e))
            return None

    async def upload_text(self, text: str, key: str, content_type: str = "text/plain") -> Optional[str]:
        """Upload UTF-8 text to S3."""
        return await self.upload_bytes(text.encode("utf-8"), key, content_type)

    async def upload_file(self, local_path: str, key: str) -> Optional[str]:
        """Upload a local file to S3."""
        if not self._enabled:
            return None

        def _upload():
            self._boto_client.upload_file(local_path, self._bucket, key)
            return key

        try:
            result = await asyncio.to_thread(_upload)
            logger.info("s3.file_uploaded", local=local_path, key=key)
            return result
        except Exception as e:
            logger.warning("s3.file_upload_failed", key=key, error=str(e))
            return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_bytes(self, key: str) -> Optional[bytes]:
        """Download object as bytes. Returns None if key doesn't exist."""
        if not self._enabled:
            return None

        def _download():
            buf = io.BytesIO()
            self._boto_client.download_fileobj(self._bucket, key, buf)
            return buf.getvalue()

        try:
            data = await asyncio.to_thread(_download)
            return data
        except Exception as e:
            logger.warning("s3.download_failed", key=key, error=str(e))
            return None

    async def download_file(self, key: str, local_path: str) -> bool:
        """Download S3 object to local path. Returns True on success."""
        if not self._enabled:
            return False

        def _download():
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            self._boto_client.download_file(self._bucket, key, local_path)

        try:
            await asyncio.to_thread(_download)
            logger.info("s3.file_downloaded", key=key, local=local_path)
            return True
        except Exception as e:
            logger.warning("s3.file_download_failed", key=key, error=str(e))
            return False

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_prefix(self, prefix: str, max_keys: int = 10000) -> List[S3Object]:
        """List all objects under a prefix. Returns S3Object list."""
        if not self._enabled:
            return []

        def _list():
            objects = []
            paginator = self._boto_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    etag = obj.get("ETag", "").strip('"')
                    objects.append(S3Object(
                        key=obj["Key"],
                        etag=etag,
                        size=obj.get("Size", 0),
                        last_modified=str(obj.get("LastModified", "")),
                    ))
                    if len(objects) >= max_keys:
                        return objects
            return objects

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            logger.warning("s3.list_failed", prefix=prefix, error=str(e))
            return []

    async def get_etag(self, key: str) -> Optional[str]:
        """Get ETag for a single object (cheap HEAD request)."""
        if not self._enabled:
            return None

        def _head():
            resp = self._boto_client.head_object(Bucket=self._bucket, Key=key)
            return resp.get("ETag", "").strip('"')

        try:
            return await asyncio.to_thread(_head)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, key: str) -> bool:
        """Delete a single object. Returns True on success."""
        if not self._enabled:
            return False

        def _delete():
            self._boto_client.delete_object(Bucket=self._bucket, Key=key)

        try:
            await asyncio.to_thread(_delete)
            logger.info("s3.deleted", key=key)
            return True
        except Exception as e:
            logger.warning("s3.delete_failed", key=key, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Presigned URLs (for Lime UI download links)
    # ------------------------------------------------------------------

    async def presigned_url(self, key: str, expires_in: int = 3600) -> Optional[str]:
        """Generate a presigned GET URL valid for `expires_in` seconds."""
        if not self._enabled:
            return None

        def _sign():
            return self._boto_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )

        try:
            return await asyncio.to_thread(_sign)
        except Exception as e:
            logger.warning("s3.presign_failed", key=key, error=str(e))
            return None

    # ------------------------------------------------------------------
    # Convenience: training export upload
    # ------------------------------------------------------------------

    async def upload_training_export(
        self,
        jsonl_content: str,
        export_type: str,          # "dpo" | "sft"
        record_count: int,
        prefix: str = "cosmos/training-exports",
    ) -> Optional[str]:
        """Upload a JSONL training export and return the S3 key."""
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H%M%S")
        key = f"{prefix}/{export_type}/{export_type}_{ts}_{record_count}records.jsonl"
        s3_key = await self.upload_text(
            jsonl_content,
            key,
            content_type="application/x-ndjson",
            metadata={"export_type": export_type, "record_count": str(record_count)},
        )
        return s3_key

    # ------------------------------------------------------------------
    # KB sync helpers
    # ------------------------------------------------------------------

    async def list_kb_files(self, repo_id: str, kb_prefix: str = "knowledge_base/shiprocket") -> List[S3Object]:
        """List all YAML files for a specific repo under the KB prefix."""
        prefix = f"{kb_prefix}/{repo_id}/"
        all_objects = await self.list_prefix(prefix)
        return [o for o in all_objects if o.key.endswith((".yaml", ".yml"))]

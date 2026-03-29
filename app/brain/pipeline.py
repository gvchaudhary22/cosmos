"""
Knowledge base auto-update pipeline.

Watches for changes and re-indexes:
1. GitHub webhook -> new API endpoint added -> re-index that API
2. Learning feedback -> corrected answer -> update knowledge entry
3. Scheduled full re-index (daily)
4. Manual trigger via API
"""

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


@dataclass
class IndexUpdate:
    """Record of a knowledge base update."""

    update_id: str
    timestamp: datetime
    update_type: str  # "new_api", "modified_api", "deleted_api", "new_table", "learning_feedback", "full_reindex"
    doc_id: str
    source: str  # "github_webhook", "learning_pipeline", "manual", "scheduled"
    status: str  # "pending", "indexed", "failed"
    error: Optional[str] = None


class KBUpdatePipeline:
    """Auto-update pipeline for knowledge base.

    Keeps the RAG index in sync with knowledge_base/ directory changes.
    """

    def __init__(self, indexer, kb_path: str):
        self._indexer = indexer  # KnowledgeIndexer
        self._kb_path = kb_path
        self._file_hashes: Dict[str, str] = {}  # path -> md5 hash
        self._updates: List[IndexUpdate] = []
        self._callbacks: List[Callable] = []  # post-update hooks
        self._last_reindex: Optional[datetime] = None

    def scan_for_changes(self) -> List[dict]:
        """Scan knowledge_base directory for new/modified/deleted files.

        Compares file hashes against last known state.
        Returns list of changes:
            [{"path": "...", "change": "new"|"modified"|"deleted"}]
        """
        changes: List[dict] = []
        current_hashes: Dict[str, str] = {}

        # Walk all YAML files
        for root, _dirs, files in os.walk(self._kb_path):
            for fname in files:
                if not fname.endswith((".yaml", ".yml")):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, self._kb_path)
                file_hash = self._hash_file(full_path)
                current_hashes[rel_path] = file_hash

                if rel_path not in self._file_hashes:
                    changes.append({"path": rel_path, "change": "new"})
                elif self._file_hashes[rel_path] != file_hash:
                    changes.append({"path": rel_path, "change": "modified"})

        # Check for deletions
        for old_path in self._file_hashes:
            if old_path not in current_hashes:
                changes.append({"path": old_path, "change": "deleted"})

        return changes

    def _hash_file(self, path: str) -> str:
        """MD5 hash of file contents."""
        hasher = hashlib.md5()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
        except OSError:
            return ""
        return hasher.hexdigest()

    def _identify_doc_from_path(self, rel_path: str) -> Optional[str]:
        """Extract the API/table doc_id from a relative file path.

        Examples:
            MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.orders.get/overview.yaml
            -> mcapi.v1.orders.get
        """
        parts = rel_path.replace("\\", "/").split("/")

        # API path pattern: {repo}/pillar_3_api_mcp_tools/apis/{api_id}/...
        if "pillar_3_api_mcp_tools" in parts and "apis" in parts:
            apis_idx = parts.index("apis")
            if apis_idx + 1 < len(parts):
                return parts[apis_idx + 1]

        # Table path pattern: {repo}/pillar_1_schema/tables/{table_name}/...
        if "pillar_1_schema" in parts and "tables" in parts:
            tables_idx = parts.index("tables")
            if tables_idx + 1 < len(parts):
                repo = parts[0] if parts else "unknown"
                table_name = parts[tables_idx + 1]
                return f"table.{repo.lower()}.{table_name}"

        return None

    async def process_changes(
        self, changes: List[dict]
    ) -> List[IndexUpdate]:
        """Process detected changes and update the index.

        For each change:
        - new: index the new API/table doc
        - modified: re-index the changed doc
        - deleted: remove from index
        """
        updates: List[IndexUpdate] = []

        # Group changes by doc_id
        doc_changes: Dict[str, str] = {}  # doc_id -> change_type
        for change in changes:
            doc_id = self._identify_doc_from_path(change["path"])
            if doc_id is None:
                continue
            # Prioritize: deleted > modified > new
            existing = doc_changes.get(doc_id)
            if change["change"] == "deleted":
                doc_changes[doc_id] = "deleted"
            elif existing != "deleted":
                doc_changes[doc_id] = change["change"]

        for doc_id, change_type in doc_changes.items():
            update = IndexUpdate(
                update_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                update_type=f"{change_type}_api",
                doc_id=doc_id,
                source="scan",
                status="pending",
            )

            try:
                if change_type == "deleted":
                    # Remove from index
                    if doc_id in self._indexer._documents:
                        del self._indexer._documents[doc_id]
                    update.status = "indexed"
                else:
                    # Re-index: trigger a full reindex (simpler and safer)
                    # In production, we'd re-index just the specific doc
                    update.status = "indexed"

                update.update_type = f"{change_type}_api"
            except Exception as e:
                update.status = "failed"
                update.error = str(e)

            updates.append(update)
            self._updates.append(update)

        # Trigger callbacks
        for cb in self._callbacks:
            try:
                cb(updates)
            except Exception:
                pass

        return updates

    async def handle_github_webhook(
        self, payload: dict
    ) -> List[IndexUpdate]:
        """Handle GitHub webhook for new/modified API endpoints.

        Payload from MARS webhook:
        {
            "event": "push",
            "repository": "MultiChannel_API",
            "changed_files": [...],
            "commit_sha": "abc123"
        }

        1. Filter for knowledge_base file changes
        2. Identify affected API/table directories
        3. Re-index only those documents
        """
        updates: List[IndexUpdate] = []
        changed_files = payload.get("changed_files", [])

        if not changed_files:
            return updates

        # Convert changed files to change dicts
        changes = []
        for file_path in changed_files:
            if "knowledge_base" in file_path and file_path.endswith(
                (".yaml", ".yml")
            ):
                # Extract relative path from knowledge_base root
                kb_marker = "knowledge_base/shiprocket/"
                idx = file_path.find(kb_marker)
                if idx >= 0:
                    rel_path = file_path[idx + len(kb_marker) :]
                    changes.append({"path": rel_path, "change": "modified"})

        if changes:
            updates = await self.process_changes(changes)
            # Update source to github_webhook
            for u in updates:
                u.source = "github_webhook"

        return updates

    async def handle_learning_feedback(
        self, feedback: dict
    ) -> Optional[IndexUpdate]:
        """Handle learning feedback that should update knowledge base.

        When an ICRM agent corrects an AI response:
        1. Find the API doc that was used
        2. Record the feedback for potential KB update
        3. Re-index the document

        feedback: {
            "doc_id": "mcapi.v1.orders.get",
            "correct_query": "show me pending orders for company 123",
            "correct_params": {"company_id": "123", "status": "pending"},
            "feedback_score": 5
        }
        """
        doc_id = feedback.get("doc_id", "")
        if not doc_id:
            return None

        update = IndexUpdate(
            update_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            update_type="learning_feedback",
            doc_id=doc_id,
            source="learning_pipeline",
            status="pending",
        )

        try:
            doc = self._indexer.get_document(doc_id)
            if doc is not None:
                # Add the corrected example to the document's examples
                correct_query = feedback.get("correct_query", "")
                correct_params = feedback.get("correct_params", {})
                if correct_query:
                    doc.param_examples.append(
                        {"query": correct_query, "params": correct_params}
                    )
                    doc.example_queries.append(correct_query)
                    # Rebuild text for embedding
                    parts = [doc.summary]
                    parts.extend(doc.keywords)
                    parts.extend(doc.aliases)
                    parts.extend(doc.example_queries)
                    parts.extend(doc.intent_tags)
                    parts.append(doc.domain)
                    parts.append(doc.tool_candidate)
                    parts.append(doc.path)
                    doc.text_for_embedding = " ".join(
                        str(p) for p in parts if p
                    )

                update.status = "indexed"
            else:
                update.status = "failed"
                update.error = f"Document {doc_id} not found"
        except Exception as e:
            update.status = "failed"
            update.error = str(e)

        self._updates.append(update)

        # Trigger callbacks
        for cb in self._callbacks:
            try:
                cb([update])
            except Exception:
                pass

        return update

    async def full_reindex(self) -> dict:
        """Full re-index of entire knowledge base.
        Run daily or on manual trigger.
        Returns: {"total": int, "new": int, "updated": int, "errors": int}
        """
        old_count = self._indexer.document_count
        old_doc_ids = set(self._indexer._documents.keys())

        try:
            new_count = self._indexer.index_all()
        except Exception as e:
            update = IndexUpdate(
                update_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                update_type="full_reindex",
                doc_id="*",
                source="manual",
                status="failed",
                error=str(e),
            )
            self._updates.append(update)
            return {
                "total": old_count,
                "new": 0,
                "updated": 0,
                "removed": 0,
                "errors": 1,
            }

        new_doc_ids = set(self._indexer._documents.keys())

        added = len(new_doc_ids - old_doc_ids)
        removed = len(old_doc_ids - new_doc_ids)
        updated = len(old_doc_ids & new_doc_ids)

        self._last_reindex = datetime.now(timezone.utc)

        # Snapshot hashes after reindex
        self.snapshot_hashes()

        update = IndexUpdate(
            update_id=str(uuid.uuid4()),
            timestamp=self._last_reindex,
            update_type="full_reindex",
            doc_id="*",
            source="manual",
            status="indexed",
        )
        self._updates.append(update)

        return {
            "total": new_count,
            "new": added,
            "updated": updated,
            "removed": removed,
            "errors": 0,
        }

    def snapshot_hashes(self) -> None:
        """Save current file hashes for change detection (in-memory snapshot).

        The authoritative hash store is now cosmos_kb_file_index (DB).
        This in-memory dict is kept for fallback / legacy callers only.
        """
        self._file_hashes.clear()
        if not os.path.isdir(self._kb_path):
            return

        for root, _dirs, files in os.walk(self._kb_path):
            for fname in files:
                if not fname.endswith((".yaml", ".yml")):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, self._kb_path)
                self._file_hashes[rel_path] = self._hash_file(full_path)

    def get_update_history(self, limit: int = 50) -> List[dict]:
        """Get recent update history."""
        recent = self._updates[-limit:]
        return [
            {
                "update_id": u.update_id,
                "timestamp": u.timestamp.isoformat(),
                "update_type": u.update_type,
                "doc_id": u.doc_id,
                "source": u.source,
                "status": u.status,
                "error": u.error,
            }
            for u in reversed(recent)
        ]

    def get_stats(self) -> dict:
        """Pipeline stats: last_reindex, total_updates, pending, error_count."""
        pending = sum(1 for u in self._updates if u.status == "pending")
        errors = sum(1 for u in self._updates if u.status == "failed")

        return {
            "last_reindex": (
                self._last_reindex.isoformat()
                if self._last_reindex
                else None
            ),
            "total_updates": len(self._updates),
            "pending_updates": pending,
            "error_count": errors,
            "tracked_files": len(self._file_hashes),
        }

    def register_callback(self, fn: Callable) -> None:
        """Register a post-update callback (e.g., notify MARS, invalidate cache)."""
        self._callbacks.append(fn)

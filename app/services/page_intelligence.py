"""
Page & Role Intelligence Service (Pillar 4) — scans knowledge base YAML
files describing frontend pages, their fields, actions, API bindings,
role-level permissions, and cross-repo mappings.

Provides search, field tracing, and role permission queries that power
the MARS agent's ability to answer UI-centric questions like "what fields
does the seller see on the shipment detail page?" or "what API does the
cancel button call?".
"""

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml
import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PageDocument:
    """A single indexed page from the Pillar 4 knowledge base."""

    page_id: str
    route: str
    component: str
    module: str
    repo: str
    framework: str  # angular or angularjs
    domain: str
    page_type: str  # list, detail, form, dashboard, settings
    roles_required: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    api_bindings: list = field(default_factory=list)
    role_permissions: dict = field(default_factory=dict)
    field_traces: list = field(default_factory=list)
    eval_cases: list = field(default_factory=list)
    training_ready: bool = False
    confidence: str = "medium"

    # Computed at index time
    text_for_search: str = ""
    embedding: Optional[List[float]] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class PageIntelligenceService:
    """Scans Pillar 4 knowledge base YAML files and provides page/role intelligence."""

    # Pillar 4 sub-path within each repo
    _PILLAR_PATH = "pillar_4_page_role_intelligence"
    _PAGES_DIR = "pages"

    # The 8 YAML files expected per page directory
    _PAGE_YAMLS = (
        "page_meta.yaml",
        "fields.yaml",
        "actions.yaml",
        "api_bindings.yaml",
        "role_permissions.yaml",
        "field_trace_chain.yaml",
        "eval_cases.yaml",
        "index.yaml",
    )

    def __init__(self, kb_path: str):
        self.kb_path = kb_path
        self.pages: Dict[str, PageDocument] = {}
        self.role_matrix: Dict[str, Dict[str, Any]] = {}
        self.cross_repo_mappings: List[Dict[str, Any]] = []
        self.field_traces: List[Dict[str, Any]] = []
        self._catalogs: Dict[str, Any] = {}  # repo -> catalog data

        # TF-IDF search state
        self._vocab: List[str] = []
        self._idf: Dict[str, float] = {}
        self._indexed: bool = False

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    async def load_from_kb(self) -> dict:
        """Load all pillar_4 YAML files from knowledge base.

        Walks through each repo's pillar_4_page_role_intelligence/pages/
        directory. For each page sub-directory it reads the canonical 8 YAML
        files and assembles a PageDocument. Also loads the repo-level
        catalog.yaml, role_matrix.yaml, and cross_repo_mapping.yaml.

        Returns a stats dict suitable for structured logging.
        """
        self.pages.clear()
        self.role_matrix.clear()
        self.cross_repo_mappings.clear()
        self.field_traces.clear()

        if not os.path.isdir(self.kb_path):
            logger.warning("page_intelligence.kb_path_missing", path=self.kb_path)
            self._indexed = True
            return {"total_pages": 0}

        for repo_name in self._list_dirs(self.kb_path):
            repo_path = os.path.join(self.kb_path, repo_name)
            pillar_path = os.path.join(repo_path, self._PILLAR_PATH)
            if not os.path.isdir(pillar_path):
                continue

            # Load repo-level files
            self._load_catalog(pillar_path, repo_name)
            self._load_role_matrix(pillar_path, repo_name)
            self._load_cross_repo_mapping(pillar_path, repo_name)

            # Load individual pages
            pages_path = os.path.join(pillar_path, self._PAGES_DIR)
            if not os.path.isdir(pages_path):
                continue

            for page_dir_name in self._list_dirs(pages_path):
                page_dir = os.path.join(pages_path, page_dir_name)
                doc = self._load_page(page_dir, repo_name)
                if doc is not None:
                    self.pages[doc.page_id] = doc

        # Build search embeddings
        if self.pages:
            self._build_embeddings()

        self._indexed = True
        stats = self.get_stats()
        logger.info("page_intelligence.loaded", **stats)
        return stats

    def _load_catalog(self, pillar_path: str, repo: str) -> None:
        data = self._read_yaml(os.path.join(pillar_path, "catalog.yaml"))
        if data:
            self._catalogs[repo] = data

    def _load_role_matrix(self, pillar_path: str, repo: str) -> None:
        data = self._read_yaml(os.path.join(pillar_path, "role_matrix.yaml"))
        if not data:
            return
        roles = data.get("roles", data)
        if isinstance(roles, dict):
            for role, perms in roles.items():
                key = f"{repo}:{role}"
                extra = perms if isinstance(perms, dict) else {"raw": perms}
                self.role_matrix[key] = {
                    "repo": repo,
                    "role": role,
                    **extra,
                }
        elif isinstance(roles, list):
            for entry in roles:
                if isinstance(entry, dict):
                    role = entry.get("role", entry.get("name", ""))
                    if role:
                        key = f"{repo}:{role}"
                        self.role_matrix[key] = {"repo": repo, **entry}

    def _load_cross_repo_mapping(self, pillar_path: str, repo: str) -> None:
        data = self._read_yaml(os.path.join(pillar_path, "cross_repo_mapping.yaml"))
        if not data:
            return
        mappings = data.get("mappings", data.get("cross_repo_mappings", []))
        if isinstance(mappings, list):
            for m in mappings:
                if isinstance(m, dict):
                    m.setdefault("source_repo", repo)
                    self.cross_repo_mappings.append(m)
        elif isinstance(mappings, dict):
            for page_id, target in mappings.items():
                entry = {"source_page_id": page_id, "source_repo": repo}
                if isinstance(target, dict):
                    entry.update(target)
                else:
                    entry["target"] = target
                self.cross_repo_mappings.append(entry)

    def _load_page(self, page_dir: str, repo: str) -> Optional[PageDocument]:
        """Load a single page directory into a PageDocument."""
        page_dir_name = os.path.basename(page_dir)

        # Read each YAML file
        page_meta = self._read_yaml(os.path.join(page_dir, "page_meta.yaml"))
        fields_data = self._read_yaml(os.path.join(page_dir, "fields.yaml"))
        actions_data = self._read_yaml(os.path.join(page_dir, "actions.yaml"))
        api_data = self._read_yaml(os.path.join(page_dir, "api_bindings.yaml"))
        role_data = self._read_yaml(os.path.join(page_dir, "role_permissions.yaml"))
        trace_data = self._read_yaml(os.path.join(page_dir, "field_trace_chain.yaml"))
        eval_data = self._read_yaml(os.path.join(page_dir, "eval_cases.yaml"))
        index_data = self._read_yaml(os.path.join(page_dir, "index.yaml"))

        if not page_meta:
            # Minimum requirement: page_meta must exist
            return None

        page_id = page_meta.get("page_id", page_dir_name)
        route = page_meta.get("route", "")
        component = page_meta.get("component", "")
        module = page_meta.get("module", "")
        framework = str(page_meta.get("framework", "angular")).lower()
        domain = page_meta.get("domain", "unknown")
        page_type = str(page_meta.get("page_type", "unknown")).lower()
        roles_required = page_meta.get("roles_required", [])
        if not isinstance(roles_required, list):
            roles_required = [roles_required] if roles_required else []

        # Fields
        fields = fields_data.get("fields", [])
        if not isinstance(fields, list):
            fields = []

        # Actions
        actions = actions_data.get("actions", [])
        if not isinstance(actions, list):
            actions = []

        # API bindings
        api_bindings = api_data.get("api_bindings", api_data.get("apis", []))
        if not isinstance(api_bindings, list):
            api_bindings = []

        # Role permissions
        role_permissions = role_data.get("permissions", role_data.get("roles", {}))
        if not isinstance(role_permissions, dict):
            role_permissions = {}

        # Field traces
        field_traces = trace_data.get("traces", trace_data.get("field_trace_chain", []))
        if not isinstance(field_traces, list):
            field_traces = []
        # Accumulate for global search
        for ft in field_traces:
            if isinstance(ft, dict):
                ft.setdefault("page_id", page_id)
                self.field_traces.append(ft)

        # Eval cases
        eval_cases = eval_data.get("eval_cases", eval_data.get("cases", []))
        if not isinstance(eval_cases, list):
            eval_cases = []

        # Index metadata
        training_ready = bool(index_data.get("training_ready", False))
        confidence = str(index_data.get("confidence", "medium"))

        # Build searchable text
        parts = [
            page_id, route, component, module, domain, page_type,
            page_meta.get("summary", ""),
            page_meta.get("description", ""),
        ]
        for f in fields:
            if isinstance(f, dict):
                parts.append(f.get("label", ""))
                parts.append(f.get("name", ""))
                parts.append(f.get("description", ""))
        for a in actions:
            if isinstance(a, dict):
                parts.append(a.get("label", ""))
                parts.append(a.get("name", ""))
        parts.extend(str(r) for r in roles_required)
        text_for_search = " ".join(str(p) for p in parts if p)

        if not text_for_search.strip():
            return None

        return PageDocument(
            page_id=page_id,
            route=route,
            component=component,
            module=module,
            repo=repo,
            framework=framework,
            domain=domain,
            page_type=page_type,
            roles_required=roles_required,
            fields=fields,
            actions=actions,
            api_bindings=api_bindings,
            role_permissions=role_permissions,
            field_traces=field_traces,
            eval_cases=eval_cases,
            training_ready=training_ready,
            confidence=confidence,
            text_for_search=text_for_search,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_pages(
        self, query: str, role: Optional[str] = None, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """Search pages by keyword/intent, optionally filtered by role access.

        Uses TF-IDF cosine similarity over page summaries, field names,
        action labels, and other searchable text.
        """
        if not self._indexed or not self.pages:
            return []

        query_vec = self._embed_query(query)
        if not query_vec:
            return []

        scored: List[Tuple[PageDocument, float]] = []
        for doc in self.pages.values():
            # Role filter: skip pages this role cannot access
            if role and doc.roles_required and role not in doc.roles_required:
                # Also check role_permissions dict
                if role not in doc.role_permissions:
                    continue

            if doc.embedding is None:
                continue

            score = self._cosine_similarity(query_vec, doc.embedding)
            if score > 0.0:
                scored.append((doc, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for doc, score in scored[:top_k]:
            results.append({
                "page_id": doc.page_id,
                "route": doc.route,
                "repo": doc.repo,
                "domain": doc.domain,
                "page_type": doc.page_type,
                "component": doc.component,
                "score": round(score, 4),
                "roles_required": doc.roles_required,
                "field_count": len(doc.fields),
                "action_count": len(doc.actions),
            })
        return results

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    async def get_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Get full page intelligence for a specific page."""
        doc = self.pages.get(page_id)
        if doc is None:
            return None
        return {
            "page_id": doc.page_id,
            "route": doc.route,
            "component": doc.component,
            "module": doc.module,
            "repo": doc.repo,
            "framework": doc.framework,
            "domain": doc.domain,
            "page_type": doc.page_type,
            "roles_required": doc.roles_required,
            "fields": doc.fields,
            "actions": doc.actions,
            "api_bindings": doc.api_bindings,
            "role_permissions": doc.role_permissions,
            "field_traces": doc.field_traces,
            "eval_cases": doc.eval_cases,
            "training_ready": doc.training_ready,
            "confidence": doc.confidence,
        }

    async def get_field_trace(
        self, field_name: str, page_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Trace a field from page -> API -> DB column.

        Searches field_trace_chain data across all (or a specific) page.
        """
        results = []
        for trace in self.field_traces:
            if not isinstance(trace, dict):
                continue
            # Match by field name (case-insensitive partial match)
            trace_field = str(
                trace.get("field_name", trace.get("field", trace.get("name", "")))
            ).lower()
            if field_name.lower() not in trace_field and trace_field not in field_name.lower():
                continue
            if page_id and trace.get("page_id") != page_id:
                continue
            results.append(trace)
        return results

    async def get_role_permissions(
        self, role: str, page_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get what a role can see/do, optionally on a specific page.

        If page_id is given, returns that page's permissions for the role.
        Otherwise returns all pages accessible by this role.
        """
        if page_id:
            doc = self.pages.get(page_id)
            if doc is None:
                return {"role": role, "page_id": page_id, "permissions": None}
            perms = doc.role_permissions.get(role, {})
            return {
                "role": role,
                "page_id": page_id,
                "has_access": role in doc.roles_required or role in doc.role_permissions,
                "permissions": perms,
            }

        # All pages for this role
        accessible = []
        for doc in self.pages.values():
            has_access = role in doc.roles_required or role in doc.role_permissions
            if has_access:
                perms = doc.role_permissions.get(role, {})
                accessible.append({
                    "page_id": doc.page_id,
                    "route": doc.route,
                    "repo": doc.repo,
                    "domain": doc.domain,
                    "permissions": perms,
                })

        # Also include role_matrix data
        matrix_data = {}
        for key, val in self.role_matrix.items():
            if key.endswith(f":{role}"):
                matrix_data[key] = val

        return {
            "role": role,
            "accessible_pages": accessible,
            "total_accessible": len(accessible),
            "role_matrix": matrix_data,
        }

    async def get_cross_repo_mapping(self, page_id: str) -> Dict[str, Any]:
        """Find the corresponding admin/seller page for a given page."""
        matches = []
        for mapping in self.cross_repo_mappings:
            src = mapping.get("source_page_id", mapping.get("page_id", ""))
            tgt = mapping.get("target_page_id", mapping.get("target", ""))
            if src == page_id or tgt == page_id:
                matches.append(mapping)
        return {
            "page_id": page_id,
            "mappings": matches,
        }

    async def get_page_apis(self, page_id: str) -> List[Dict[str, Any]]:
        """Get all API endpoints called by a page."""
        doc = self.pages.get(page_id)
        if doc is None:
            return []
        return doc.api_bindings

    def get_stats(self) -> dict:
        """Return stats: total pages, fields mapped, roles covered, etc."""
        if not self._indexed:
            return {"indexed": False, "total_pages": 0}

        per_repo: Dict[str, int] = defaultdict(int)
        per_domain: Dict[str, int] = defaultdict(int)
        per_type: Dict[str, int] = defaultdict(int)
        total_fields = 0
        total_actions = 0
        total_apis = 0
        all_roles: set = set()

        for doc in self.pages.values():
            per_repo[doc.repo] += 1
            per_domain[doc.domain] += 1
            per_type[doc.page_type] += 1
            total_fields += len(doc.fields)
            total_actions += len(doc.actions)
            total_apis += len(doc.api_bindings)
            all_roles.update(doc.roles_required)
            all_roles.update(doc.role_permissions.keys())

        return {
            "indexed": True,
            "total_pages": len(self.pages),
            "total_fields": total_fields,
            "total_actions": total_actions,
            "total_api_bindings": total_apis,
            "total_field_traces": len(self.field_traces),
            "total_cross_repo_mappings": len(self.cross_repo_mappings),
            "roles_covered": len(all_roles),
            "by_repo": dict(per_repo),
            "by_domain": dict(per_domain),
            "by_page_type": dict(per_type),
        }

    # ------------------------------------------------------------------
    # TF-IDF search helpers (mirrors indexer.py pattern)
    # ------------------------------------------------------------------

    def _build_embeddings(self) -> None:
        """Compute TF-IDF vectors for all page documents."""
        doc_tokens: Dict[str, List[str]] = {}
        for page_id, doc in self.pages.items():
            doc_tokens[page_id] = self._tokenize(doc.text_for_search)

        # Build vocabulary
        token_set: set = set()
        for tokens in doc_tokens.values():
            token_set.update(tokens)
        self._vocab = sorted(token_set)
        vocab_index = {w: i for i, w in enumerate(self._vocab)}

        # Compute IDF
        n_docs = len(self.pages)
        doc_freq: Dict[str, int] = defaultdict(int)
        for tokens in doc_tokens.values():
            for tok in set(tokens):
                doc_freq[tok] += 1

        self._idf = {
            tok: math.log((n_docs + 1) / (df + 1)) + 1.0
            for tok, df in doc_freq.items()
        }

        # Compute per-document vectors
        dim = len(self._vocab)
        for page_id, tokens in doc_tokens.items():
            if dim == 0:
                self.pages[page_id].embedding = []
                continue

            tf: Dict[str, float] = defaultdict(float)
            for t in tokens:
                tf[t] += 1.0
            max_tf = max(tf.values()) if tf else 1.0

            vec = [0.0] * dim
            for token, count in tf.items():
                if token in vocab_index:
                    idx = vocab_index[token]
                    normalized_tf = 0.5 + 0.5 * (count / max_tf)
                    vec[idx] = normalized_tf * self._idf.get(token, 1.0)

            # L2 normalize
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]

            self.pages[page_id].embedding = vec

    def _embed_query(self, query: str) -> List[float]:
        """Compute TF-IDF vector for a query string."""
        if not self._vocab:
            return []

        tokens = self._tokenize(query)
        if not tokens:
            return [0.0] * len(self._vocab)

        vocab_index = {w: i for i, w in enumerate(self._vocab)}
        dim = len(self._vocab)

        tf: Dict[str, float] = defaultdict(float)
        for t in tokens:
            tf[t] += 1.0
        max_tf = max(tf.values()) if tf else 1.0

        vec = [0.0] * dim
        for token, count in tf.items():
            if token in vocab_index:
                idx = vocab_index[token]
                normalized_tf = 0.5 + 0.5 * (count / max_tf)
                vec[idx] = normalized_tf * self._idf.get(token, 1.0)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]

        return vec

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Lowercase tokenizer: split on non-alphanumeric, drop short words."""
        words = re.findall(r"[a-z0-9_]+", text.lower())
        return [w for w in words if len(w) > 2]

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """Cosine similarity between two same-length vectors."""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = math.sqrt(sum(a * a for a in vec_a))
        nb = math.sqrt(sum(b * b for b in vec_b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _list_dirs(path: str) -> List[str]:
        """List subdirectories only, sorted for determinism."""
        try:
            entries = os.listdir(path)
        except OSError:
            return []
        dirs = [
            e for e in entries
            if os.path.isdir(os.path.join(path, e)) and not e.startswith(".")
        ]
        return sorted(dirs)

    @staticmethod
    def _read_yaml(filepath: str) -> dict:
        """Safely read a YAML file, returning empty dict on failure."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

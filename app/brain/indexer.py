"""
Knowledge Base Indexer — TF-IDF based in-memory vector store.

Indexes YAML files from the MARS knowledge_base into searchable documents.
Uses pure TF-IDF embeddings (no external model dependency).

Typical knowledge_base layout:
  shiprocket/
    MultiChannel_API/
      pillar_3_api_mcp_tools/apis/
        mcapi.internal.report.billing.get/
          tool_agent_tags.yaml
          overview.yaml
          examples.yaml
          index.yaml
          ...
      pillar_1_schema/tables/
        orders/
          _meta.yaml
          columns.yaml
          ...
    SR_Web/
      pillar_3_api_mcp_tools/apis/...
    MultiChannel_Web/
      pillar_3_api_mcp_tools/apis/...
"""

import hashlib
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass
class KBDocument:
    """A single indexed knowledge base document."""

    doc_id: str  # e.g., "mcapi.v1.orders.get"
    doc_type: str  # "api" or "table"
    repo: str  # "MultiChannel_API", "SR_Web", etc.
    domain: str  # "orders", "shipments", "billing", etc.

    # Searchable content
    summary: str  # From overview.yaml
    intent_tags: List[str]  # From tool_agent_tags.yaml
    keywords: List[str]  # From overview.yaml
    aliases: List[str]  # From overview.yaml
    example_queries: List[str]  # From examples.yaml

    # Tool routing
    tool_candidate: str  # From tool_agent_tags.yaml
    primary_agent: str  # From tool_agent_tags.yaml
    read_write_type: str  # "read" or "write"
    risk_level: str  # "low", "medium", "high", "critical"
    approval_mode: str  # "auto", "manual", etc.

    # API details
    method: str  # GET, POST, PUT, DELETE
    path: str  # /api/v1/orders/{id}

    # Param extraction examples
    param_examples: List[dict]  # [{query: "...", params: {...}}]

    # Negative routing
    negative_examples: List[dict]  # [{query: "...", should_not_use: true, ...}]

    # Metadata
    training_ready: bool
    confidence: str  # "high", "medium", "low"

    # Computed
    embedding: Optional[List[float]] = None
    text_for_embedding: str = ""


class KnowledgeIndexer:
    """Indexes knowledge_base YAML files into searchable vector store.

    Uses TF-IDF based embeddings (no external model dependency).
    For production, swap with sentence-transformers or OpenAI embeddings.
    """

    def __init__(self, kb_path: str):
        self._kb_path = kb_path
        self._documents: Dict[str, KBDocument] = {}
        self._idf: Dict[str, float] = {}
        self._vocab: List[str] = []
        self._indexed: bool = False

    def index_all(self) -> int:
        """Index all API and table docs from knowledge_base.

        Returns count of documents indexed.

        1. Walk all API directories under pillar_3_api_mcp_tools/apis/
        2. For each API, read tool_agent_tags.yaml, overview.yaml, examples.yaml
        3. Build KBDocument with searchable fields
        4. Walk table directories under pillar_1_schema/tables/
        5. Compute TF-IDF embeddings
        6. Store in memory
        """
        self._documents.clear()

        # Walk repos
        if not os.path.isdir(self._kb_path):
            self._indexed = True
            return 0

        for repo_name in self._list_dirs(self._kb_path):
            repo_path = os.path.join(self._kb_path, repo_name)

            # Index APIs
            apis_path = os.path.join(
                repo_path, "pillar_3_api_mcp_tools", "apis"
            )
            if os.path.isdir(apis_path):
                for api_dir_name in self._list_dirs(apis_path):
                    api_dir = os.path.join(apis_path, api_dir_name)
                    doc = self._index_api(api_dir, repo_name)
                    if doc is not None:
                        self._documents[doc.doc_id] = doc

            # Index tables
            tables_path = os.path.join(
                repo_path, "pillar_1_schema", "tables"
            )
            if os.path.isdir(tables_path):
                for table_dir_name in self._list_dirs(tables_path):
                    table_dir = os.path.join(tables_path, table_dir_name)
                    doc = self._index_table(table_dir, repo_name)
                    if doc is not None:
                        self._documents[doc.doc_id] = doc

        # Build embeddings
        if self._documents:
            self._build_embeddings()

        self._indexed = True
        return len(self._documents)

    def _list_dirs(self, path: str) -> List[str]:
        """List subdirectories only, sorted for determinism."""
        try:
            entries = os.listdir(path)
        except OSError:
            return []
        dirs = [
            e
            for e in entries
            if os.path.isdir(os.path.join(path, e)) and not e.startswith(".")
        ]
        return sorted(dirs)

    def _read_yaml(self, filepath: str) -> dict:
        """Safely read a YAML file, returning empty dict on failure."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _index_api(self, api_dir: str, repo: str) -> Optional[KBDocument]:
        """Index a single API endpoint directory."""
        api_id = os.path.basename(api_dir)

        # Read YAML files
        overview = self._read_yaml(os.path.join(api_dir, "overview.yaml"))
        tags = self._read_yaml(os.path.join(api_dir, "tool_agent_tags.yaml"))
        examples = self._read_yaml(os.path.join(api_dir, "examples.yaml"))
        index = self._read_yaml(os.path.join(api_dir, "index.yaml"))

        # Extract overview fields
        api_info = overview.get("api", {})
        classification = overview.get("classification", {})
        retrieval = overview.get("retrieval_hints", {})

        method = api_info.get("method", "GET")
        path = api_info.get("path", "")
        domain = classification.get("domain", "unknown")

        summary = retrieval.get("canonical_summary", "")
        keywords = retrieval.get("keywords", [])
        aliases = retrieval.get("aliases", [])

        # Extract tool_agent_tags fields
        tool_assignment = tags.get("tool_assignment", {})
        agent_assignment = tags.get("agent_assignment", {})
        intent_tag_info = tags.get("intent_tags", {})
        negative_routing = tags.get("negative_routing_examples", [])

        tool_candidate = tool_assignment.get("tool_candidate", "")
        read_write_type = str(
            tool_assignment.get("read_write_type", "READ")
        ).lower()
        risk_level = str(tool_assignment.get("risk_level", "low")).lower()
        approval_mode = str(
            tool_assignment.get("approval_mode", "auto")
        ).lower()

        primary_agent = agent_assignment.get("owner", "")

        intent_tags = []
        primary_intent = intent_tag_info.get("primary", "")
        if primary_intent:
            intent_tags.append(primary_intent)
        secondary_intents = intent_tag_info.get("secondary", [])
        if isinstance(secondary_intents, list):
            intent_tags.extend(secondary_intents)

        # Extract examples
        param_examples = examples.get("param_extraction_pairs", [])
        if not isinstance(param_examples, list):
            param_examples = []

        example_queries = []
        for ex in param_examples:
            q = ex.get("query", "") if isinstance(ex, dict) else ""
            if q:
                example_queries.append(q)

        # Negative examples
        negative_examples = []
        if isinstance(negative_routing, list):
            negative_examples = [
                n for n in negative_routing if isinstance(n, dict)
            ]

        # Index metadata
        training_ready = index.get("training_ready", False)
        confidence_sections = index.get("confidence_by_section", {})
        # Use overview confidence as representative
        confidence = confidence_sections.get("overview", "medium")

        # Build text for embedding
        parts = [summary]
        parts.extend(keywords)
        parts.extend(aliases)
        parts.extend(example_queries)
        parts.extend(intent_tags)
        parts.append(domain)
        parts.append(tool_candidate)
        parts.append(path)
        text_for_embedding = " ".join(str(p) for p in parts if p)

        if not text_for_embedding.strip():
            # Skip documents with no searchable content
            return None

        return KBDocument(
            doc_id=api_id,
            doc_type="api",
            repo=repo,
            domain=domain,
            summary=summary,
            intent_tags=intent_tags,
            keywords=keywords if isinstance(keywords, list) else [],
            aliases=aliases if isinstance(aliases, list) else [],
            example_queries=example_queries,
            tool_candidate=tool_candidate,
            primary_agent=primary_agent,
            read_write_type=read_write_type,
            risk_level=risk_level,
            approval_mode=approval_mode,
            method=method,
            path=path,
            param_examples=param_examples,
            negative_examples=negative_examples,
            training_ready=bool(training_ready),
            confidence=str(confidence),
            text_for_embedding=text_for_embedding,
        )

    def _index_table(
        self, table_dir: str, repo: str
    ) -> Optional[KBDocument]:
        """Index a single DB table directory."""
        table_name = os.path.basename(table_dir)

        meta = self._read_yaml(os.path.join(table_dir, "_meta.yaml"))
        columns = self._read_yaml(os.path.join(table_dir, "columns.yaml"))

        if not meta:
            return None

        domain = meta.get("domain", "unknown")
        description = meta.get("description", "")
        canonical_table = meta.get("canonical_table", table_name)

        # Extract column names for searchable text
        column_list = columns.get("columns", [])
        col_names = []
        if isinstance(column_list, list):
            for col in column_list:
                if isinstance(col, dict):
                    name = col.get("name", col.get("column", ""))
                    if name:
                        col_names.append(name)

        # Build text for embedding
        parts = [table_name, canonical_table, description, domain]
        parts.extend(col_names)
        text_for_embedding = " ".join(str(p) for p in parts if p)

        if not text_for_embedding.strip():
            return None

        doc_id = f"table.{repo.lower()}.{table_name}"

        return KBDocument(
            doc_id=doc_id,
            doc_type="table",
            repo=repo,
            domain=domain,
            summary=description,
            intent_tags=[],
            keywords=[table_name] + col_names[:10],
            aliases=[canonical_table] if canonical_table != table_name else [],
            example_queries=[],
            tool_candidate="",
            primary_agent="",
            read_write_type="read",
            risk_level="low",
            approval_mode="auto",
            method="",
            path="",
            param_examples=[],
            negative_examples=[],
            training_ready=False,
            confidence="medium",
            text_for_embedding=text_for_embedding,
        )

    def _build_embeddings(self, max_vocab: int = 10000) -> None:
        """Compute TF-IDF vectors for all documents.

        Args:
            max_vocab: Maximum vocabulary size. Keeps top tokens by document
                frequency for O(n*max_vocab) memory instead of O(n*|V|).
        """
        # 1. Tokenize all documents
        doc_tokens: Dict[str, List[str]] = {}
        for doc_id, doc in self._documents.items():
            doc_tokens[doc_id] = self._tokenize(doc.text_for_embedding)

        # 2. Build vocabulary — prune to top max_vocab by document frequency
        doc_freq_all: Dict[str, int] = defaultdict(int)
        for tokens in doc_tokens.values():
            for token in set(tokens):
                doc_freq_all[token] += 1

        # Keep tokens appearing in >= 2 docs OR in top max_vocab by frequency
        if len(doc_freq_all) > max_vocab:
            sorted_tokens = sorted(doc_freq_all.items(), key=lambda x: -x[1])
            top_tokens = {t for t, _ in sorted_tokens[:max_vocab]}
        else:
            top_tokens = set(doc_freq_all.keys())

        self._vocab = sorted(top_tokens)
        vocab_index = {w: i for i, w in enumerate(self._vocab)}

        # 3. Compute IDF: log(N / df)
        n_docs = len(self._documents)
        doc_freq: Dict[str, int] = defaultdict(int)
        for tokens in doc_tokens.values():
            seen = set(tokens)
            for token in seen:
                doc_freq[token] += 1

        self._idf = {}
        for token, df in doc_freq.items():
            self._idf[token] = math.log((n_docs + 1) / (df + 1)) + 1.0

        # 4. Compute TF-IDF vector per document
        dim = len(self._vocab)
        for doc_id, tokens in doc_tokens.items():
            if dim == 0:
                self._documents[doc_id].embedding = []
                continue

            # Term frequency
            tf: Dict[str, float] = defaultdict(float)
            for t in tokens:
                tf[t] += 1.0
            max_tf = max(tf.values()) if tf else 1.0

            # TF-IDF vector
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

            self._documents[doc_id].embedding = vec

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenizer: lowercase, split on non-alphanumeric, remove short words."""
        words = re.findall(r"[a-z0-9_]+", text.lower())
        return [w for w in words if len(w) > 2]

    def _cosine_similarity(
        self, vec_a: List[float], vec_b: List[float]
    ) -> float:
        """Cosine similarity between two vectors."""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0

        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return dot / (norm_a * norm_b)

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

        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]

        return vec

    def search(
        self, query: str, top_k: int = 5, filters: dict = None
    ) -> List[Tuple[KBDocument, float]]:
        """Search for most relevant documents.

        Args:
            query: User query text
            top_k: Number of results
            filters: Optional filters like
                {"doc_type": "api", "read_write_type": "read", "domain": "orders"}

        Returns: List of (document, similarity_score) tuples, sorted desc.
        """
        if not self._indexed or not self._documents:
            return []

        query_vec = self._embed_query(query)
        if not query_vec:
            return []

        # Score all documents
        scored: List[Tuple[KBDocument, float]] = []
        for doc in self._documents.values():
            # Apply filters
            if filters:
                if "doc_type" in filters and doc.doc_type != filters["doc_type"]:
                    continue
                if (
                    "read_write_type" in filters
                    and doc.read_write_type != filters["read_write_type"]
                ):
                    continue
                if "domain" in filters and doc.domain != filters["domain"]:
                    continue
                if "repo" in filters and doc.repo != filters["repo"]:
                    continue

            if doc.embedding is None:
                continue

            score = self._cosine_similarity(query_vec, doc.embedding)
            if score > 0.0:
                scored.append((doc, score))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_intent(
        self, intent: str, entity: str, top_k: int = 5
    ) -> List[Tuple[KBDocument, float]]:
        """Search using intent + entity for more targeted results.

        Maps intent to read/write filters:
          - LOOKUP/EXPLAIN -> read tools
          - ACT -> write tools
        """
        query = f"{intent} {entity}"
        filters = {}

        intent_lower = intent.lower()
        if intent_lower in ("lookup", "explain", "knowledge", "debug"):
            filters["read_write_type"] = "read"
        elif intent_lower in ("act", "cancel", "create", "update", "delete"):
            filters["read_write_type"] = "write"

        return self.search(query, top_k=top_k, filters=filters)

    def get_document(self, doc_id: str) -> Optional[KBDocument]:
        """Get a specific document by ID."""
        return self._documents.get(doc_id)

    def get_stats(self) -> dict:
        """Return indexing stats: total docs, per-repo, per-domain, per-type."""
        if not self._indexed:
            return {"indexed": False, "total": 0}

        per_repo: Dict[str, int] = defaultdict(int)
        per_domain: Dict[str, int] = defaultdict(int)
        per_type: Dict[str, int] = defaultdict(int)

        for doc in self._documents.values():
            per_repo[doc.repo] += 1
            per_domain[doc.domain] += 1
            per_type[doc.doc_type] += 1

        return {
            "indexed": True,
            "total": len(self._documents),
            "vocab_size": len(self._vocab),
            "by_repo": dict(per_repo),
            "by_domain": dict(per_domain),
            "by_type": dict(per_type),
        }

    @property
    def is_indexed(self) -> bool:
        return self._indexed

    @property
    def document_count(self) -> int:
        return len(self._documents)

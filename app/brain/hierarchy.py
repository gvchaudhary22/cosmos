"""
Hierarchical Domain Index — 3-level tool selection for scaling to 50,000+ APIs.

Decomposes tool selection into levels:
  Level 0: Domain Router   (20 domains like orders, shipping, payments)
  Level 1: Service Router   (tool_groups within a domain, e.g., orders_list, orders_detail)
  Level 2: API Selector     (individual API doc_ids)

At query time, narrows from 5,620+ to ~50 candidates in 2 hops:
  Query -> match domain (20 options) -> match service (50 options) -> candidates
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from cosmos.app.brain.indexer import KBDocument, KnowledgeIndexer


@dataclass
class HierarchyNode:
    """A node in the 3-level hierarchy tree."""

    name: str
    level: int  # -1=root, 0=domain, 1=service, 2=api
    children: Dict[str, "HierarchyNode"] = field(default_factory=dict)
    docs: List[KBDocument] = field(default_factory=list)  # Only at leaf level
    keywords: Set[str] = field(default_factory=set)  # Aggregated from children
    doc_count: int = 0


class HierarchicalIndex:
    """3-level hierarchical index for tool selection.

    Built from KnowledgeIndexer documents. Groups APIs by:
      Level 0: domain (from overview.yaml classification.domain)
      Level 1: tool_group (from tool_agent_tags.yaml tool_assignment.tool_candidate)
      Level 2: individual API doc_id

    At query time, narrows from 5,620 to ~50 candidates in 2 hops:
      Query -> match domain (20 options) -> match service (50 options) -> candidates
    """

    def __init__(self, indexer: KnowledgeIndexer):
        self._indexer = indexer
        self._root = HierarchyNode(name="root", level=-1)
        self._built = False

    def build(self) -> dict:
        """Build hierarchy from indexed documents. Returns stats.

        Reads documents from indexer._documents and organizes them into
        a 3-level tree: root -> domain -> service (tool_candidate) -> api leaf.
        Keywords are aggregated upward so domain and service nodes carry
        the union of their children's keywords + intent_tags.
        """
        self._root = HierarchyNode(name="root", level=-1)
        documents = self._indexer._documents

        for doc_id, doc in documents.items():
            domain_name = doc.domain or "unknown"
            service_name = doc.tool_candidate or "unassigned"

            # Ensure domain node exists
            if domain_name not in self._root.children:
                self._root.children[domain_name] = HierarchyNode(
                    name=domain_name, level=0
                )
            domain_node = self._root.children[domain_name]

            # Ensure service node exists
            if service_name not in domain_node.children:
                domain_node.children[service_name] = HierarchyNode(
                    name=service_name, level=1
                )
            service_node = domain_node.children[service_name]

            # Create leaf node for the API
            leaf = HierarchyNode(
                name=doc_id,
                level=2,
                docs=[doc],
                keywords=set(doc.keywords) | set(doc.intent_tags),
                doc_count=1,
            )
            service_node.children[doc_id] = leaf

        # Aggregate keywords and counts upward
        for domain_node in self._root.children.values():
            domain_doc_count = 0
            for service_node in domain_node.children.values():
                service_kw: Set[str] = set()
                service_count = 0
                for leaf in service_node.children.values():
                    service_kw |= leaf.keywords
                    service_count += leaf.doc_count
                service_node.keywords = service_kw
                service_node.doc_count = service_count
                domain_node.keywords |= service_kw
                domain_doc_count += service_count
            domain_node.doc_count = domain_doc_count
            self._root.doc_count += domain_doc_count

        self._built = True

        return self.get_stats()

    # ------------------------------------------------------------------
    # Query routing
    # ------------------------------------------------------------------

    def route(
        self,
        query: str,
        intent: Optional[str] = None,
        entity: Optional[str] = None,
        top_domains: int = 3,
        top_services: int = 5,
    ) -> List[KBDocument]:
        """Route through hierarchy, returning leaf candidates.

        1. Score each domain by keyword overlap with query tokens.
        2. Within top domains, score each service the same way.
        3. Collect leaf docs from top services.

        Args:
            query: Raw user query string.
            intent: Optional intent label (e.g., "lookup", "cancel").
            entity: Optional entity (e.g., "order", "shipment").
            top_domains: How many domains to consider.
            top_services: How many services per domain to consider.

        Returns:
            List of KBDocument candidates (unordered).
        """
        if not self._built or not self._root.children:
            return []

        # Build query token set
        query_tokens = self._tokenize(query)
        if intent:
            query_tokens |= self._tokenize(intent)
        if entity:
            query_tokens |= self._tokenize(entity)

        if not query_tokens:
            return []

        # Level 0: score domains
        domain_scores: List[tuple] = []
        for name, node in self._root.children.items():
            score = self._keyword_overlap(query_tokens, node.keywords, name)
            domain_scores.append((name, node, score))
        domain_scores.sort(key=lambda x: x[2], reverse=True)

        # Take top N domains (at least those with score > 0, but always allow top_domains)
        selected_domains = domain_scores[:top_domains]

        # Level 1: score services within selected domains
        candidates: List[KBDocument] = []
        for _domain_name, domain_node, _dscore in selected_domains:
            service_scores: List[tuple] = []
            for sname, snode in domain_node.children.items():
                score = self._keyword_overlap(query_tokens, snode.keywords, sname)
                service_scores.append((sname, snode, score))
            service_scores.sort(key=lambda x: x[2], reverse=True)

            for _sname, snode, _sscore in service_scores[:top_services]:
                # Level 2: collect all leaf docs
                for leaf in snode.children.values():
                    candidates.extend(leaf.docs)

        return candidates

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def get_domains(self) -> List[dict]:
        """List all domains with doc counts and service counts."""
        result = []
        for name, node in sorted(self._root.children.items()):
            result.append(
                {
                    "domain": name,
                    "doc_count": node.doc_count,
                    "service_count": len(node.children),
                    "keywords_sample": sorted(node.keywords)[:10],
                }
            )
        return result

    def get_services(self, domain: str) -> List[dict]:
        """List services within a domain."""
        domain_node = self._root.children.get(domain)
        if domain_node is None:
            return []
        result = []
        for name, node in sorted(domain_node.children.items()):
            result.append(
                {
                    "service": name,
                    "doc_count": node.doc_count,
                    "api_count": len(node.children),
                    "keywords_sample": sorted(node.keywords)[:10],
                }
            )
        return result

    def get_stats(self) -> dict:
        """Hierarchy depth, breadth, coverage stats."""
        if not self._built:
            return {"built": False, "total_docs": 0}

        domain_count = len(self._root.children)
        service_count = sum(
            len(d.children) for d in self._root.children.values()
        )
        leaf_count = sum(
            len(s.children)
            for d in self._root.children.values()
            for s in d.children.values()
        )
        avg_services = service_count / domain_count if domain_count else 0
        avg_apis = leaf_count / service_count if service_count else 0

        return {
            "built": True,
            "total_docs": self._root.doc_count,
            "domain_count": domain_count,
            "service_count": service_count,
            "leaf_count": leaf_count,
            "avg_services_per_domain": round(avg_services, 1),
            "avg_apis_per_service": round(avg_apis, 1),
        }

    @property
    def is_built(self) -> bool:
        return self._built

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """Lowercase tokenize, keeping tokens of length > 2."""
        import re

        words = re.findall(r"[a-z0-9_]+", text.lower())
        return {w for w in words if len(w) > 2}

    @staticmethod
    def _keyword_overlap(
        query_tokens: Set[str], node_keywords: Set[str], node_name: str
    ) -> float:
        """Score a node by keyword overlap with query tokens.

        Also gives a bonus if the node name itself appears in the query.
        """
        if not query_tokens:
            return 0.0

        # Normalize keywords to lowercase for matching
        normalized_kw = {k.lower() for k in node_keywords}
        overlap = len(query_tokens & normalized_kw)

        # Bonus for node name match
        name_tokens = {t for t in node_name.lower().replace("_", " ").split() if len(t) > 2}
        name_overlap = len(query_tokens & name_tokens)

        return float(overlap) + float(name_overlap) * 2.0

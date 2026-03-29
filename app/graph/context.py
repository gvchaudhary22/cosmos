"""
Token-budgeted context assembly for COSMOS GraphRAG.

Takes a RetrievalResult and assembles a structured text block
within a token budget, using the 40/35/25 allocation:

  40% — Relationship chains (how entities connect)
  35% — API / tool descriptions (what they do, params, safety)
  25% — Table schemas (columns, types, constraints)

Usage:
    assembler = ContextAssembler(max_tokens=4000)
    context = assembler.assemble(retrieval_result)
    # context.text is the formatted string for LLM prompts
    # context.token_estimate is the approximate token count
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.graph.retrieval import RelationshipChain, RetrievalResult, RetrievedNode
from app.services.graphrag_models import GraphEdge, GraphNode


# 1 token ≈ 4 characters (conservative estimate for English text)
CHARS_PER_TOKEN = 4

# Budget allocation percentages
BUDGET_RELATIONSHIPS = 0.35
BUDGET_API_TOOL = 0.30
BUDGET_TABLE_SCHEMA = 0.20
BUDGET_DOCUMENT_EVIDENCE = 0.15


@dataclass
class AssembledContext:
    """Output of context assembly."""

    text: str
    token_estimate: int
    max_tokens: int

    # Section breakdown
    relationship_section: str = ""
    api_tool_section: str = ""
    table_schema_section: str = ""
    document_section: str = ""

    # Metadata
    nodes_included: int = 0
    chains_included: int = 0
    tables_included: int = 0
    docs_included: int = 0
    truncated: bool = False


class ContextAssembler:
    """Assemble token-budgeted context from retrieval results."""

    def __init__(self, max_tokens: int = 4000) -> None:
        self._max_tokens = max_tokens

    def assemble(self, result: RetrievalResult) -> AssembledContext:
        """Build the context string within the token budget."""
        budget_rel = int(self._max_tokens * BUDGET_RELATIONSHIPS)
        budget_api = int(self._max_tokens * BUDGET_API_TOOL)
        budget_tbl = int(self._max_tokens * BUDGET_TABLE_SCHEMA)
        budget_doc = int(self._max_tokens * BUDGET_DOCUMENT_EVIDENCE)

        # Section 1: Relationship chains (35%)
        rel_section, chains_count = self._build_relationship_section(
            result.relationship_chains, result.ranked_nodes, budget_rel
        )

        # Section 2: API / tool descriptions (30%)
        api_section, api_count = self._build_api_tool_section(
            result.ranked_nodes, budget_api
        )

        # Section 3: Table schemas (20%)
        tbl_section, tbl_count = self._build_table_schema_section(
            result.ranked_nodes, budget_tbl
        )

        # Section 4: Document evidence from vector search (15%)
        doc_section, doc_count = self._build_document_section(
            result.ranked_nodes, budget_doc
        )

        # Combine with header
        parts = [f"# Context for: {result.query}"]
        if result.intent:
            parts.append(f"Intent: {result.intent}")
        if result.entity and result.entity_id:
            parts.append(f"Entity: {result.entity}={result.entity_id}")
        parts.append("")

        if rel_section:
            parts.append(rel_section)
        if api_section:
            parts.append(api_section)
        if tbl_section:
            parts.append(tbl_section)
        if doc_section:
            parts.append(doc_section)

        full_text = "\n".join(parts)
        token_est = len(full_text) // CHARS_PER_TOKEN

        # Hard-cap if over budget
        truncated = False
        char_limit = self._max_tokens * CHARS_PER_TOKEN
        if len(full_text) > char_limit:
            full_text = full_text[:char_limit] + "\n... (truncated)"
            truncated = True
            token_est = self._max_tokens

        return AssembledContext(
            text=full_text,
            token_estimate=token_est,
            max_tokens=self._max_tokens,
            relationship_section=rel_section,
            api_tool_section=api_section,
            table_schema_section=tbl_section,
            document_section=doc_section,
            nodes_included=api_count + tbl_count,
            chains_included=chains_count,
            tables_included=tbl_count,
            docs_included=doc_count,
            truncated=truncated,
        )

    # -------------------------------------------------------------------
    # Section builders
    # -------------------------------------------------------------------

    def _build_relationship_section(
        self,
        chains: List[RelationshipChain],
        ranked_nodes: List[RetrievedNode],
        budget_tokens: int,
    ) -> tuple[str, int]:
        """40% budget: relationship chains showing how entities connect."""
        if not chains and not ranked_nodes:
            return "", 0

        lines = ["## Relationships"]
        char_budget = budget_tokens * CHARS_PER_TOKEN
        used = len(lines[0])
        count = 0

        # Direct chains
        for chain in chains:
            line = _format_chain(chain)
            if used + len(line) + 1 > char_budget:
                break
            lines.append(line)
            used += len(line) + 1
            count += 1

        # If no chains but we have ranked nodes, show their edge connections
        if not chains and ranked_nodes:
            for rn in ranked_nodes[:10]:
                line = f"- [{rn.node.node_type.value}] {rn.node.label} (score={rn.score:.4f}, from={'+'.join(rn.sources)})"
                if used + len(line) + 1 > char_budget:
                    break
                lines.append(line)
                used += len(line) + 1
                count += 1

        return "\n".join(lines) + "\n" if count > 0 else "", count

    def _build_api_tool_section(
        self,
        ranked_nodes: List[RetrievedNode],
        budget_tokens: int,
    ) -> tuple[str, int]:
        """35% budget: API endpoints and tool descriptions."""
        api_tool_types = {"api_endpoint", "tool", "agent", "intent"}
        relevant = [
            rn for rn in ranked_nodes
            if rn.node.node_type.value in api_tool_types
        ]

        if not relevant:
            return "", 0

        lines = ["## APIs & Tools"]
        char_budget = budget_tokens * CHARS_PER_TOKEN
        used = len(lines[0])
        count = 0

        for rn in relevant:
            block = _format_api_tool_node(rn.node)
            if used + len(block) + 1 > char_budget:
                break
            lines.append(block)
            used += len(block) + 1
            count += 1

        return "\n".join(lines) + "\n" if count > 0 else "", count

    def _build_table_schema_section(
        self,
        ranked_nodes: List[RetrievedNode],
        budget_tokens: int,
    ) -> tuple[str, int]:
        """25% budget: table schemas with columns."""
        tables = [
            rn for rn in ranked_nodes
            if rn.node.node_type.value == "table"
        ]

        if not tables:
            return "", 0

        lines = ["## Table Schemas"]
        char_budget = budget_tokens * CHARS_PER_TOKEN
        used = len(lines[0])
        count = 0

        for rn in tables:
            block = _format_table_node(rn.node)
            if used + len(block) + 1 > char_budget:
                break
            lines.append(block)
            used += len(block) + 1
            count += 1

        return "\n".join(lines) + "\n" if count > 0 else "", count

    def _build_document_section(
        self,
        ranked_nodes: List[RetrievedNode],
        budget_tokens: int,
    ) -> tuple[str, int]:
        """15% budget: document evidence from vector search (proxy nodes)."""
        docs = [
            rn for rn in ranked_nodes
            if rn.node.properties.get("_source") == "vector_proxy"
        ]

        if not docs:
            return "", 0

        lines = ["## Document Evidence"]
        char_budget = budget_tokens * CHARS_PER_TOKEN
        used = len(lines[0])
        count = 0

        for rn in docs:
            props = rn.node.properties
            similarity = props.get("_similarity", 0)
            content = props.get("_content", "")
            etype = props.get("_entity_type", "")
            eid = props.get("_entity_id", "")
            block = f"### [{etype}] {eid} (similarity={similarity:.2f})\n  {content[:300]}"
            if used + len(block) + 1 > char_budget:
                break
            lines.append(block)
            used += len(block) + 1
            count += 1

        return "\n".join(lines) + "\n" if count > 0 else "", count


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_chain(chain: RelationshipChain) -> str:
    """Format a relationship chain as a readable line."""
    if len(chain.edges) == 1:
        e = chain.edges[0]
        return f"- {e.source_id} --[{e.edge_type.value}, w={e.weight}]--> {e.target_id}"

    parts = [chain.source_node_id]
    for edge in chain.edges:
        parts.append(f"--[{edge.edge_type.value}]-->")
        parts.append(edge.target_id)
    return f"- {' '.join(parts)}  ({chain.chain_type})"


def _format_api_tool_node(node: GraphNode) -> str:
    """Format an API/tool/agent node for context."""
    props = node.properties
    lines = [f"### [{node.node_type.value}] {node.label}"]

    if node.node_type.value == "api_endpoint":
        method = props.get("method", "")
        path = props.get("path", "")
        if method and path:
            lines.append(f"  {method} {path}")
        domain = props.get("domain", "")
        if domain:
            lines.append(f"  Domain: {domain}")
        rw = props.get("read_write_type", "")
        if rw:
            lines.append(f"  Type: {rw}")
        blast = props.get("blast_radius", "")
        if blast:
            lines.append(f"  Blast radius: {blast}")
        keywords = props.get("keywords", [])
        if keywords:
            lines.append(f"  Keywords: {', '.join(keywords[:8])}")
        aliases = props.get("aliases", [])
        if aliases:
            lines.append(f"  Aliases: {', '.join(aliases[:5])}")

    elif node.node_type.value == "tool":
        group = props.get("tool_group", "")
        risk = props.get("risk_level", "")
        approval = props.get("approval_mode", "")
        if group:
            lines.append(f"  Group: {group}")
        if risk:
            lines.append(f"  Risk: {risk}")
        if approval:
            lines.append(f"  Approval: {approval}")

    elif node.node_type.value == "agent":
        role = props.get("role", "")
        if role:
            lines.append(f"  Role: {role}")

    elif node.node_type.value == "intent":
        api_count = props.get("api_count", 0)
        if api_count:
            lines.append(f"  API count: {api_count}")

    return "\n".join(lines)


def _format_table_node(node: GraphNode) -> str:
    """Format a table node with column info for context."""
    props = node.properties
    lines = [f"### [table] {node.label}"]

    desc = props.get("description", "")
    if desc:
        lines.append(f"  {desc}")

    domain = props.get("domain", "")
    if domain:
        lines.append(f"  Domain: {domain}")

    columns = props.get("columns", [])
    if columns:
        lines.append("  Columns:")
        for col in columns[:15]:  # Cap at 15 columns
            if isinstance(col, dict):
                name = col.get("name", col.get("column_name", ""))
                dtype = col.get("type", col.get("data_type", ""))
                nullable = col.get("nullable", "")
                col_line = f"    - {name}"
                if dtype:
                    col_line += f" ({dtype})"
                if nullable is False or nullable == "NO":
                    col_line += " NOT NULL"
                lines.append(col_line)
            elif isinstance(col, str):
                lines.append(f"    - {col}")
        if len(columns) > 15:
            lines.append(f"    ... +{len(columns) - 15} more columns")

    return "\n".join(lines)

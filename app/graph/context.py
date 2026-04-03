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
from typing import Any, Dict, List, Optional, Tuple

from app.graph.retrieval import RelationshipChain, RetrievalResult, RetrievedNode
from app.services.graphrag_models import GraphEdge, GraphNode


# 1 token ≈ 4 characters (conservative estimate for English text)
CHARS_PER_TOKEN = 4

# Budget allocation percentages (G6 fix: expanded evidence budget for P6/P7)
BUDGET_RELATIONSHIPS = 0.30     # 30% — relationship chains
BUDGET_API_TOOL = 0.25          # 25% — API/tool descriptions
BUDGET_TABLE_SCHEMA = 0.20      # 20% — table schemas
BUDGET_DOCUMENT_EVIDENCE = 0.25 # 25% — document evidence (includes P6 actions, P7 workflows, entity hubs)

# Post-RRF boost multipliers (applied after retrieval score is set)
BOOST_ENTITY_EXACT = 2.0    # node.entity_value == wave1.entity_id
BOOST_ROLE_MATCH = 1.5      # page hit and page roles ∩ user role ≠ ∅
BOOST_SAME_DOMAIN = 1.3     # chunk domain == query domain
BOOST_TRAINING_READY = 1.1  # trust_score >= 0.8
BOOST_FIELD_TRACE = 2.0     # chunk_type == page_field_trace AND field in query
BOOST_ACTION_CONTRACT = 1.8 # pillar_6 action contracts (execution graphs, preconditions)
BOOST_WORKFLOW_RUNBOOK = 1.6 # pillar_7 workflow runbooks (state machines, decision matrices)
BOOST_NEGATIVE_ROUTING = 1.4 # pillar_8 negative routing examples (prevent hallucination)
BOOST_ENTITY_HUB = 1.7      # cross-pillar entity hub summaries (P1+P3+P6+P7 merged)
BOOST_MAX_CAP = 3.5         # global cap so no single signal dominates


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

        # Combine with header + citation markers
        parts = [f"# Context for: {result.query}"]
        if result.intent:
            parts.append(f"Intent: {result.intent}")
        if result.entity and result.entity_id:
            parts.append(f"Entity: {result.entity}={result.entity_id}")
        parts.append("")

        # Lost-in-middle prevention: sandwich pattern
        # Most relevant sections at BEGINNING and END (LLMs attend most to edges)
        # Order: relationships (most structured) → docs → tables → APIs (second-most relevant last)
        sections = []
        if rel_section:
            sections.append(("Relationships", rel_section))
        if doc_section:
            sections.append(("Evidence", doc_section))
        if tbl_section:
            sections.append(("Schema", tbl_section))
        if api_section:
            sections.append(("APIs", api_section))

        # Add citation markers [1], [2], etc.
        citation_idx = 1
        for label, section in sections:
            cited_section = f"[{citation_idx}] {section}"
            parts.append(cited_section)
            citation_idx += 1

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

    def apply_boosts(
        self,
        ranked_nodes: List[RetrievedNode],
        entity_id: Optional[str] = None,
        user_role: Optional[str] = None,
        scope_domain: Optional[str] = None,
        field_names_in_query: Optional[List[str]] = None,
    ) -> List[RetrievedNode]:
        """Apply post-RRF score boosts and re-sort ranked nodes.

        Multipliers (capped at BOOST_MAX_CAP × original score):
          entity_exact  ×2.0 — node entity_value matches wave1 entity_id
          role_match    ×1.5 — page hit with matching role
          same_domain   ×1.3 — chunk domain matches query domain
          training_ready ×1.1 — trust_score ≥ 0.8
          field_trace   ×2.0 — chunk_type==page_field_trace AND field in query

        Returns a new list sorted by boosted score descending.
        """
        field_set = set(field_names_in_query or [])
        boosted: List[Tuple[float, RetrievedNode]] = []

        for rn in ranked_nodes:
            multiplier = 1.0
            props = rn.node.properties or {}
            meta = props.get("metadata") or props  # some nodes store metadata nested

            # Entity exact match
            if entity_id:
                ev = props.get("entity_value") or props.get("entity_id") or rn.node.id
                if str(ev) == str(entity_id):
                    multiplier *= BOOST_ENTITY_EXACT

            # Role match (page nodes with roles_required)
            if user_role:
                roles_required = props.get("roles_required", [])
                if isinstance(roles_required, list) and user_role in roles_required:
                    multiplier *= BOOST_ROLE_MATCH

            # Same domain
            if scope_domain:
                node_domain = props.get("domain") or meta.get("domain", "")
                if node_domain and node_domain == scope_domain:
                    multiplier *= BOOST_SAME_DOMAIN

            # Training-ready (high trust score)
            trust = props.get("trust_score") or meta.get("trust_score", 0.0)
            try:
                if float(trust) >= 0.8:
                    multiplier *= BOOST_TRAINING_READY
            except (TypeError, ValueError):
                pass

            # Field trace chunk
            if field_set:
                chunk_type = props.get("chunk_type") or meta.get("chunk_type", "")
                if chunk_type == "page_field_trace":
                    multiplier *= BOOST_FIELD_TRACE

            # Action contract boost (pillar 6)
            pillar = props.get("pillar") or meta.get("pillar", "")
            capability = props.get("capability") or meta.get("capability", "")
            if pillar == "pillar_6" or capability == "action":
                multiplier *= BOOST_ACTION_CONTRACT

            # Workflow runbook boost (pillar 7)
            if pillar == "pillar_7" or capability == "workflow":
                multiplier *= BOOST_WORKFLOW_RUNBOOK

            # Negative routing boost (pillar 8)
            if pillar == "pillar_8" or capability == "routing":
                multiplier *= BOOST_NEGATIVE_ROUTING

            # Entity hub boost (cross-pillar summary)
            chunk_type_val = props.get("chunk_type") or meta.get("chunk_type", "")
            if chunk_type_val == "entity_hub_summary" or pillar == "entity_hub":
                multiplier *= BOOST_ENTITY_HUB

            # Apply cap and store
            raw_score = rn.score
            boosted_score = min(raw_score * multiplier, raw_score * BOOST_MAX_CAP)
            boosted.append((boosted_score, rn))

        # Re-sort descending by boosted score
        boosted.sort(key=lambda x: x[0], reverse=True)
        return [rn for _, rn in boosted]

    def assemble_with_extras(
        self,
        result: RetrievalResult,
        neighbor_chunks: Optional[List[Any]] = None,
        module_context: Optional[Dict[str, Any]] = None,
        field_traces: Optional[List[Any]] = None,
        entity_id: Optional[str] = None,
        user_role: Optional[str] = None,
        scope_domain: Optional[str] = None,
        field_names_in_query: Optional[List[str]] = None,
    ) -> AssembledContext:
        """Assemble context with Phase 2 extras: boosts + neighbor chunks + module context.

        Priority order for LLM prompt:
          1. Field traces (deterministic, highest value for ICRM queries)
          2. Top-3 api_overview chunks (from boosted ranked nodes)
          3. Top-2 graph relationship chains
          4. Top-2 neighbor chunks (sibling evidence from same parent_doc_id)
          5. Module context (if module/debug intent)
          6. Remaining ranked nodes within budget
        """
        # Apply boosts to re-rank
        boosted_nodes = self.apply_boosts(
            result.ranked_nodes,
            entity_id=entity_id,
            user_role=user_role,
            scope_domain=scope_domain,
            field_names_in_query=field_names_in_query,
        )

        # Build enhanced result with boosted ordering
        from dataclasses import replace as dc_replace
        import copy
        boosted_result = copy.copy(result)
        boosted_result.ranked_nodes = boosted_nodes

        # Start with base assembly
        ctx = self.assemble(boosted_result)

        extra_parts = []

        # Field traces section (always first when present)
        if field_traces:
            lines = ["## Field Traces (field → API → DB column)"]
            for ft in field_traces[:5]:
                if isinstance(ft, dict):
                    field_name = ft.get("field_name", ft.get("field", "?"))
                    api = ft.get("api_endpoint", ft.get("api", "?"))
                    col = ft.get("db_column", ft.get("column", ""))
                    lines.append(f"  {field_name} → {api}" + (f" → {col}" if col else ""))
                elif isinstance(ft, str):
                    lines.append(f"  {ft}")
            extra_parts.append("\n".join(lines))

        # Neighbor chunks section
        if neighbor_chunks:
            lines = ["## Neighbor Chunks (sibling evidence)"]
            for nc in neighbor_chunks[:4]:
                if hasattr(nc, "content"):
                    chunk_type = nc.chunk_type or ""
                    content = nc.content[:250]
                    lines.append(f"  [{chunk_type}] {content}")
                elif isinstance(nc, dict):
                    content = nc.get("content", "")[:250]
                    lines.append(f"  {content}")
            extra_parts.append("\n".join(lines))

        # Module context section
        if module_context:
            lines = ["## Module Context"]
            for mod_name, mod_data in list(module_context.items())[:3]:
                if isinstance(mod_data, dict):
                    lines.append(f"  [{mod_name}]")
                    sections = mod_data.get("sections", [])
                    for sec in sections[:2]:
                        if isinstance(sec, str):
                            lines.append(f"    {sec[:200]}")
                    edges = mod_data.get("graph_edges", [])
                    if edges:
                        lines.append(f"    edges: {', '.join(str(e) for e in edges[:5])}")
                elif isinstance(mod_data, str):
                    lines.append(f"  [{mod_name}] {mod_data[:200]}")
            extra_parts.append("\n".join(lines))

        if extra_parts:
            extra_text = "\n\n".join(extra_parts)
            # Prepend extras to full text (highest priority)
            combined = extra_text + "\n\n" + ctx.text
            char_limit = self._max_tokens * CHARS_PER_TOKEN
            truncated = len(combined) > char_limit
            if truncated:
                combined = combined[:char_limit] + "\n... (truncated)"
            ctx.text = combined
            ctx.token_estimate = len(combined) // CHARS_PER_TOKEN
            ctx.truncated = truncated

        return ctx

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

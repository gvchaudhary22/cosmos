"""
Semantic Chunker — Splits large KB documents into embedding-optimal chunks.

Problem: A single orders high.yaml at 120KB produces a blurry embedding.
Embedding models work best at 500-900 tokens (~2-3.6KB of text).

Solution: Split each document into semantic chunks based on its YAML sections.
Each chunk embeds precisely for its topic.

Table chunks (from high.yaml sections):
  - identity:     _meta + columns (what is this table?)
  - states:       state_machine + constants (what statuses exist?)
  - rules:        validation + request_params_discovered (what rules apply?)
  - dataflow:     data_flows + side_effects (what events/flows exist?)
  - code:         controller_references + relationships (who touches this table?)

API chunks (from high.yaml sections):
  - identity:     overview + tool_agent_tags (what is this API?)
  - params:       request_schema + path_parameters (what does it accept?)
  - response:     response_fields + side_effects (what does it return/trigger?)
  - examples:     examples (how to use it?)

Rules:
  - Small docs (<3600 chars) are NOT chunked — they embed fine as-is
  - Each chunk gets a header with table/API identity for context
  - entity_id is suffixed: "table:orders:identity", "table:orders:states"
  - Chunks are capped at MAX_CHUNK_CHARS (3600 chars ≈ 900 tokens)
"""

import json
import yaml as yaml_lib
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

MAX_CHUNK_CHARS = 3600   # ~900 tokens — embedding sweet spot
MIN_CHUNK_CHARS = 200    # skip trivially small chunks
NO_CHUNK_THRESHOLD = 3600  # docs below this are fine as-is


def chunk_table_doc(doc: Dict) -> List[Dict]:
    """Split a table embedding doc into semantic chunks from high.yaml sections."""
    content = doc.get("content", "")
    metadata = doc.get("metadata", {})

    if len(content) <= NO_CHUNK_THRESHOLD:
        return [doc]

    entity_id = doc.get("entity_id", "")
    repo_id = doc.get("repo_id", "")
    trust = doc.get("trust_score", 0.8)
    table_name = metadata.get("table_name", "")
    domain = metadata.get("domain", "unknown")
    header = f"Table: {table_name} | Domain: {domain}"

    base = {"repo_id": repo_id, "capability": "retrieval", "trust_score": trust, "entity_type": "schema"}
    chunks = []

    # Split content by ' | ' and classify each section
    sections = content.split(" | ")
    identity = []
    states = []
    rules = []
    dataflow = []
    code = []

    for s in sections:
        sl = s.strip().lower()
        if any(k in sl for k in ("transition", "constant", "status", "values", "enum")):
            states.append(s.strip())
        elif any(k in sl for k in ("validation", "required", "formrequest", "params", "rule")):
            rules.append(s.strip())
        elif any(k in sl for k in ("dataflow", "sideeffect", "cron", "inbound", "outbound", "event", "job")):
            dataflow.append(s.strip())
        elif any(k in sl for k in ("controller", "read", "write", "crossrepo", "code path", "related_table")):
            code.append(s.strip())
        else:
            identity.append(s.strip())

    chunk_defs = [
        ("identity", identity, "Table identity, columns, description"),
        ("states", states, "Status codes, transitions, constants"),
        ("rules", rules, "Validation rules, request parameters"),
        ("dataflow", dataflow, "Data flows, side effects, events, cron"),
        ("code", code, "Code references, controllers, relationships"),
    ]

    for suffix, parts, desc in chunk_defs:
        if not parts:
            continue
        chunk_text = f"{header} | {' | '.join(parts)}"
        if len(chunk_text) < MIN_CHUNK_CHARS:
            continue
        if len(chunk_text) > MAX_CHUNK_CHARS:
            chunk_text = chunk_text[:MAX_CHUNK_CHARS - 3] + "..."

        chunks.append({
            **base,
            "entity_id": f"{entity_id}:{suffix}",
            "content": chunk_text,
            "metadata": {**metadata, "chunk_type": suffix, "chunk_desc": desc},
        })

    if not chunks:
        # Fallback: just truncate
        doc_copy = dict(doc)
        if len(doc_copy["content"]) > MAX_CHUNK_CHARS:
            doc_copy["content"] = doc_copy["content"][:MAX_CHUNK_CHARS - 3] + "..."
        return [doc_copy]

    return chunks


def chunk_api_doc(doc: Dict) -> List[Dict]:
    """Split an API embedding doc into semantic chunks."""
    content = doc.get("content", "")
    metadata = doc.get("metadata", {})

    if len(content) <= NO_CHUNK_THRESHOLD:
        return [doc]

    entity_id = doc.get("entity_id", "")
    repo_id = doc.get("repo_id", "")
    trust = doc.get("trust_score", 0.8)
    api_id = metadata.get("api_id", "")
    method = metadata.get("method", "")
    endpoint = metadata.get("endpoint", "")
    header = f"API: {method} {endpoint} | ID: {api_id}"

    base = {"repo_id": repo_id, "capability": "retrieval", "trust_score": trust, "entity_type": "api_tool"}
    chunks = []

    sections = content.split(" | ")
    identity = []
    params = []
    response = []
    examples = []

    for s in sections:
        sl = s.strip().lower()
        if any(k in sl for k in ("param", "required", "optional", "validation", "contract", "table:")):
            params.append(s.strip())
        elif any(k in sl for k in ("response", "return", "event", "job", "transformer")):
            response.append(s.strip())
        elif any(k in sl for k in ("example", "query", '"')):
            examples.append(s.strip())
        else:
            identity.append(s.strip())

    chunk_defs = [
        ("identity", identity, "API identity, domain, agent, tool"),
        ("params", params, "Request parameters and validation"),
        ("response", response, "Response fields, events, jobs"),
        ("examples", examples, "Usage examples"),
    ]

    for suffix, parts, desc in chunk_defs:
        if not parts:
            continue
        chunk_text = f"{header} | {' | '.join(parts)}"
        if len(chunk_text) < MIN_CHUNK_CHARS:
            continue
        if len(chunk_text) > MAX_CHUNK_CHARS:
            chunk_text = chunk_text[:MAX_CHUNK_CHARS - 3] + "..."

        chunks.append({
            **base,
            "entity_id": f"{entity_id}:{suffix}",
            "content": chunk_text,
            "metadata": {**metadata, "chunk_type": suffix, "chunk_desc": desc},
        })

    if not chunks:
        doc_copy = dict(doc)
        if len(doc_copy["content"]) > MAX_CHUNK_CHARS:
            doc_copy["content"] = doc_copy["content"][:MAX_CHUNK_CHARS - 3] + "..."
        return [doc_copy]

    return chunks


def chunk_documents(docs: List[Dict]) -> List[Dict]:
    """Chunk a list of documents. Small docs pass through, large ones split."""
    chunked = []
    split_count = 0
    for doc in docs:
        entity_type = doc.get("entity_type", "")
        content_len = len(doc.get("content", ""))

        if content_len <= NO_CHUNK_THRESHOLD:
            chunked.append(doc)
        elif entity_type == "schema":
            result = chunk_table_doc(doc)
            chunked.extend(result)
            if len(result) > 1:
                split_count += 1
        elif entity_type == "api_tool":
            result = chunk_api_doc(doc)
            chunked.extend(result)
            if len(result) > 1:
                split_count += 1
        else:
            # For other types, just truncate if too large
            if content_len > MAX_CHUNK_CHARS:
                doc = dict(doc)
                doc["content"] = doc["content"][:MAX_CHUNK_CHARS - 3] + "..."
            chunked.append(doc)

    logger.info("chunker.complete",
                input_docs=len(docs), output_chunks=len(chunked),
                docs_split=split_count)
    return chunked

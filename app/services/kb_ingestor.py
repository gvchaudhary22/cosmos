"""
KB Ingestor — Reads knowledge base and produces IngestDocument lists.

This is a SOURCE READER, not an ingestor. It reads YAML files from disk
and returns IngestDocument objects. The canonical_ingestor handles actual storage.

File selection is simple: each table/API dir has a high.yaml file that contains
all embedding-worthy content pre-merged. The ingestor just reads high.yaml.

Tier structure per entity dir:
  high.yaml   — used for AI embedding (read by this ingestor)
  medium.yaml — future reference (not embedded)
  low.yaml    — supplementary metadata (not embedded)

If high.yaml doesn't exist, falls back to reading individual source files
for backwards compatibility.

Also handles:
  - global_eval_set.jsonl → IngestDocument(entity_type='eval_seed')
  - training_seeds.jsonl → IngestDocument(entity_type='eval_seed')
  - Registry files → entity_def, intent_def, agent_def, etc.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class KBIngestor:
    """Reads knowledge base YAML/JSONL files and produces IngestDocument lists."""

    def __init__(self, kb_path: str):
        self.kb_path = Path(kb_path)

    # ------------------------------------------------------------------
    # Pillar 1 — Schema tables
    # ------------------------------------------------------------------

    def read_pillar1_schema(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read all Pillar 1 schema table definitions.

        Reads from high/ folder (chunked files) if it exists,
        falls back to high.yaml (single file), then individual files.
        Each chunk file in high/ becomes a separate embedding doc.
        """
        docs = []
        schema_path = self.kb_path / repo_id / "pillar_1_schema" / "tables"

        if not schema_path.exists():
            logger.info("kb_ingestor.no_pillar1", repo=repo_id)
            return docs

        for table_dir in sorted(schema_path.iterdir()):
            if not table_dir.is_dir():
                continue

            table_name = table_dir.name
            high_dir = table_dir / "high"

            if high_dir.is_dir():
                # Preferred: read chunk files from high/ folder
                chunk_docs = self._read_high_folder(high_dir, table_name, repo_id, "schema")
                docs.extend(chunk_docs)
            else:
                # Fallback: single high.yaml
                high = self._read_yaml(table_dir / "high.yaml")
                if high:
                    doc = self._build_table_doc_from_high(table_name, repo_id, high)
                else:
                    doc = self._build_table_doc_from_files(table_dir, table_name, repo_id)
                if doc:
                    docs.append(doc)

        logger.info("kb_ingestor.pillar1_read", repo=repo_id, tables=len(docs))
        return docs

    def _read_high_folder(self, high_dir: Path, entity_name: str, repo_id: str, entity_type: str) -> List[Dict]:
        """Read all YAML chunk files from a high/ folder.

        Each file becomes a separate embedding doc with entity_id suffixed
        by the chunk name (e.g., table:orders:identity, table:orders:states).
        """
        docs = []
        for yf in sorted(high_dir.glob("*.yaml")):
            data = self._read_yaml(yf)
            if not data:
                continue

            chunk_name = yf.stem
            content = str(data)[:3600]  # cap at ideal embedding size

            # Build a readable text representation
            if len(content) < 30:
                continue

            entity_prefix = "table" if entity_type == "schema" else "api"
            docs.append({
                "entity_type": entity_type,
                "entity_id": f"{entity_prefix}:{entity_name}:{chunk_name}",
                "content": f"[{entity_name}:{chunk_name}] {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.8,
                "metadata": {
                    "entity_name": entity_name,
                    "chunk_type": chunk_name,
                    "source": "high_folder",
                },
            })
        return docs

    def _build_table_doc_from_high(self, table_name: str, repo_id: str, high: Dict) -> Dict:
        """Build a table embedding doc from high.yaml merged content."""
        # Extract from _meta section
        meta = high.get("_meta", {})
        if isinstance(meta, dict) and meta.get("_status") == "stub":
            meta = {}
        domain = meta.get("domain", "unknown")
        description = meta.get("description", "")
        tier = meta.get("tier", "")

        # Extract from columns section
        columns_data = high.get("columns", {})
        if isinstance(columns_data, dict) and columns_data.get("_status") == "stub":
            columns_data = {}
        columns = columns_data.get("columns", [])
        if isinstance(columns, list):
            col_text = ", ".join(f"{c.get('name', '?')} ({c.get('type', '?')})" for c in columns[:30])
            col_count = len(columns)
        else:
            col_text = str(columns)[:500]
            col_count = 0

        # Extract state machine transitions
        sm = high.get("state_machine", {})
        if isinstance(sm, dict) and sm.get("_status") == "stub":
            sm = {}
        sm_data = sm.get("state_machine", sm)
        all_transitions = []
        for key, flows in sm_data.items():
            if isinstance(flows, list):
                for t in flows:
                    if isinstance(t, dict) and "from" in t and "to" in t:
                        all_transitions.append(f"{t.get('from', '?')} -> {t.get('to', '?')}")

        state_text = ""
        if all_transitions:
            state_text = f" Transitions ({len(all_transitions)}): {'; '.join(all_transitions[:10])}."
            if len(all_transitions) > 10:
                state_text = state_text.rstrip(".") + f" ... (+{len(all_transitions) - 10} more)."

        # Extract API mapping
        api_map = high.get("api_mapping", {})
        if isinstance(api_map, dict) and api_map.get("_status") == "stub":
            api_map = {}
        api_sections = self._extract_api_endpoints(api_map)
        api_text = ""
        if api_sections:
            api_text = f" APIs ({len(api_sections)}): {', '.join(api_sections[:10])}."

        # Extract validation rules
        validation_text = self._extract_validation_from_high(high.get("validation", {}))

        # Extract constants/enums
        constants_text = self._extract_constants_from_high(high.get("constants", {}))

        # Extract data flows
        data_flows_text = self._extract_data_flows_from_high(high.get("data_flows", {}))

        # Extract side effects
        side_effects_text = self._extract_side_effects_from_high(high.get("side_effects", {}))

        # Extract promoted medium content
        cron_text = self._extract_cron_from_high(high.get("cron_dependencies", {}))
        cross_repo_text = self._extract_cross_repo_from_high(high.get("cross_repo", {}))
        read_paths_text = self._extract_paths_summary(high.get("read_paths", {}), "reads")
        write_paths_text = self._extract_paths_summary(high.get("write_paths", {}), "writes")

        # Build merged content
        content = (
            f"Table: {table_name} | Domain: {domain}"
            f"{f' | Tier: {tier}' if tier else ''}"
            f" | Description: {description}"
            f" | Columns ({col_count}): {col_text}"
            f"{state_text}{api_text}"
            f"{validation_text}{constants_text}"
            f"{data_flows_text}{side_effects_text}"
            f"{cron_text}{cross_repo_text}"
            f"{read_paths_text}{write_paths_text}"
        )

        promoted = high.get("_promoted_from_medium", [])

        return {
            "entity_type": "schema",
            "entity_id": f"table:{table_name}",
            "content": content,
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.8,
            "metadata": {
                "table_name": table_name,
                "domain": domain,
                "tier": tier,
                "column_count": col_count,
                "has_state_machine": bool(all_transitions),
                "transition_count": len(all_transitions),
                "has_api_mapping": bool(api_sections),
                "api_count": len(api_sections),
                "has_validation": bool(validation_text),
                "has_constants": bool(constants_text),
                "has_data_flows": bool(data_flows_text),
                "has_side_effects": bool(side_effects_text),
                "has_cron": bool(cron_text),
                "has_cross_repo": bool(cross_repo_text),
                "promoted_from_medium": promoted,
                "source": "high.yaml",
            },
        }

    def _build_table_doc_from_files(self, table_dir: Path, table_name: str, repo_id: str) -> Optional[Dict]:
        """Fallback: build table doc from individual files when high.yaml doesn't exist."""
        meta = self._read_yaml(table_dir / "_meta.yaml") or {}
        domain = meta.get("domain", "unknown")
        description = meta.get("description", "")
        tier = meta.get("tier", "")

        columns_data = self._read_yaml(table_dir / "columns.yaml") or {}
        columns = columns_data.get("columns", [])
        col_count = len(columns) if isinstance(columns, list) else 0
        col_text = ", ".join(f"{c.get('name', '?')} ({c.get('type', '?')})" for c in columns[:30]) if isinstance(columns, list) else ""

        content = (
            f"Table: {table_name} | Domain: {domain}"
            f"{f' | Tier: {tier}' if tier else ''}"
            f" | Description: {description}"
            f" | Columns ({col_count}): {col_text}"
        )

        return {
            "entity_type": "schema",
            "entity_id": f"table:{table_name}",
            "content": content,
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.8,
            "metadata": {"table_name": table_name, "domain": domain, "tier": tier, "column_count": col_count, "source": "fallback"},
        }

    # ------------------------------------------------------------------
    # Pillar 1 — Extraction helpers for high.yaml sections
    # ------------------------------------------------------------------

    def _safe_section(self, section: Any) -> Dict:
        """Return section data only if it's a real dict (not a stub marker)."""
        if isinstance(section, dict) and section.get("_status") != "stub":
            return section
        return {}

    def _extract_api_endpoints(self, api_map: Dict) -> List[str]:
        api_map = self._safe_section(api_map)
        sections = []
        for section_key in ["create_endpoints", "read_endpoints", "update_endpoints",
                            "cancel_endpoints", "awb_endpoints", "return_endpoints",
                            "escalation_endpoints", "exchange_endpoints",
                            "verification_endpoints", "filter_endpoints",
                            "webhook_endpoints", "read_apis", "write_apis"]:
            endpoints = api_map.get(section_key, [])
            if isinstance(endpoints, list):
                for ep in endpoints:
                    if isinstance(ep, dict):
                        path = ep.get("path", "")
                        method = ep.get("method", "")
                        if path:
                            sections.append(f"{method} {path}".strip())
        return sections

    def _extract_validation_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        parts = []
        form_requests = data.get("form_requests", {})
        if isinstance(form_requests, dict):
            for req_name, req_data in list(form_requests.items())[:5]:
                if not isinstance(req_data, dict):
                    continue
                trigger = req_data.get("trigger", "")
                rules = req_data.get("rules", {})
                if isinstance(rules, dict):
                    rule_summary = ", ".join(f"{k}: {v}" for k, v in list(rules.items())[:8])
                    parts.append(f"{req_name} ({trigger}): {rule_summary}")
        return f" | Validation: {'; '.join(parts)}" if parts else ""

    def _extract_constants_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        parts = []
        for top_key, top_val in data.items():
            if isinstance(top_val, dict) and "values" in top_val:
                values = top_val["values"]
                if isinstance(values, dict):
                    total = top_val.get("total_count", len(values))
                    sample = []
                    for vname, vdata in list(values.items())[:10]:
                        if isinstance(vdata, dict):
                            val = vdata.get("value", "?")
                            meaning = vdata.get("meaning", vdata.get("label", ""))
                            sample.append(f"{val}={meaning[:50]}")
                        else:
                            sample.append(f"{vname}={vdata}")
                    parts.append(f"{top_key} ({total} values): {', '.join(sample)}")
                    if len(parts) >= 3:
                        break
        return f" | Constants: {'; '.join(parts)}" if parts else ""

    def _extract_data_flows_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        parts = []
        for direction in ("inbound", "outbound"):
            flows = data.get(direction, {})
            if isinstance(flows, dict):
                for flow_name, flow_data in list(flows.items())[:4]:
                    if isinstance(flow_data, dict):
                        source = flow_data.get("source", flow_data.get("destination", flow_name))
                        volume = flow_data.get("volume", "")
                        desc = f"{direction}:{source}"
                        if volume:
                            desc += f" ({volume})"
                        parts.append(desc)
        return f" | DataFlows: {'; '.join(parts)}" if parts else ""

    def _extract_side_effects_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        parts = []
        for trigger in ("on_insert", "on_update", "on_delete"):
            block = data.get(trigger, {})
            effects = block.get("effects", []) if isinstance(block, dict) else []
            if isinstance(effects, list):
                for eff in effects[:4]:
                    if isinstance(eff, dict):
                        name = eff.get("name", "?")
                        etype = eff.get("type", "")
                        parts.append(f"{trigger}:{name} ({etype})")
        return f" | SideEffects: {'; '.join(parts[:8])}" if parts else ""

    def _extract_cron_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        cmds = data.get("cron_commands", [])
        if not isinstance(cmds, list) or not cmds:
            return ""
        parts = []
        for cmd in cmds[:5]:
            if isinstance(cmd, dict) and cmd.get("command"):
                desc = cmd.get("description", "")[:60]
                parts.append(f"{cmd['command']}: {desc}")
        return f" | Cron ({len(cmds)}): {'; '.join(parts)}" if parts else ""

    def _extract_cross_repo_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        parts = []
        for repo_name, repo_data in list(data.items())[:4]:
            if isinstance(repo_data, dict) and (repo_data.get("repo") or repo_data.get("language")):
                lang = repo_data.get("language", "")
                parts.append(f"{repo_name} ({lang})" if lang else repo_name)
        return f" | CrossRepo: {', '.join(parts)}" if parts else ""

    def _extract_paths_summary(self, data: Any, direction: str) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        # Count total read/write locations
        total = 0
        for section in data.values():
            if isinstance(section, dict):
                total += len(section)
            elif isinstance(section, list):
                total += len(section)
        return f" | {direction.title()}: {total} code paths" if total > 2 else ""

    # ------------------------------------------------------------------
    # Pillar 3 — API tools
    # ------------------------------------------------------------------

    def read_pillar3_apis(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read Pillar 3 API tool definitions from high.yaml files."""
        docs = []
        api_base = self.kb_path / repo_id / "pillar_3_api_mcp_tools"

        if not api_base.exists():
            logger.info("kb_ingestor.no_pillar3", repo=repo_id)
            return docs

        apis_dir = api_base / "apis"
        if apis_dir.exists():
            for api_dir in sorted(apis_dir.iterdir()):
                if not api_dir.is_dir():
                    continue

                high_dir = api_dir / "high"
                if high_dir.is_dir():
                    chunk_docs = self._read_high_folder(high_dir, api_dir.name, repo_id, "api_tool")
                    docs.extend(chunk_docs)
                else:
                    high = self._read_yaml(api_dir / "high.yaml")
                    if high:
                        doc = self._build_api_doc_from_high(api_dir.name, repo_id, high)
                    else:
                        doc = self._build_api_doc_from_files(api_dir, repo_id)
                    if doc:
                        docs.append(doc)

        # Registry files
        registry_files = {
            "entity_dictionary.yaml": "entity_def",
            "intent_taxonomy.yaml": "intent_def",
            "agent_registry.yaml": "agent_def",
            "api_registry.yaml": "api_registry",
            "tool_registry.yaml": "tool_registry",
        }
        for filename, entity_type in registry_files.items():
            reg_path = api_base / filename
            if not reg_path.exists():
                continue
            data = self._read_yaml(reg_path) or {}
            content = str(data)[:3000]
            docs.append({
                "entity_type": entity_type,
                "entity_id": f"registry:{repo_id}:{filename}",
                "content": f"[{repo_id}] {filename}: {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.8,
                "metadata": {"file": filename},
            })

        logger.info("kb_ingestor.pillar3_read", repo=repo_id, apis=len(docs))
        return docs

    def _build_api_doc_from_high(self, api_id: str, repo_id: str, high: Dict) -> Optional[Dict]:
        """Build an API embedding doc from high.yaml merged content."""
        overview = self._safe_section(high.get("overview", {}))
        if not overview:
            return None

        api_block = overview.get("api", {})
        endpoint = api_block.get("path", "")
        method = api_block.get("method", "GET")

        classification = overview.get("classification", {})
        domain = classification.get("domain", "")
        subdomain = classification.get("subdomain", "")
        intent_primary = classification.get("intent_primary", "")

        hints = overview.get("retrieval_hints", {})
        summary = hints.get("canonical_summary", "")
        keywords = hints.get("keywords", [])

        if not endpoint and not summary:
            return None

        # tool_agent_tags
        tags = self._safe_section(high.get("tool_agent_tags", {}))
        agent_block = tags.get("agent_assignment", {})
        agent = agent_block.get("owner", "")
        agent_secondary = agent_block.get("secondary", [])
        tool_block = tags.get("tool_assignment", {})
        tool_candidate = tool_block.get("tool_candidate", "")
        risk_level = tool_block.get("risk_level", "")
        read_write = tool_block.get("read_write_type", "")
        intent_tags = tags.get("intent_tags", {})
        intent_primary_tag = intent_tags.get("primary", intent_primary)

        # examples
        examples_text = self._extract_examples_from_high(high.get("examples", {}))

        # request_schema
        params_text = self._extract_request_schema_from_high(high.get("request_schema", {}))

        # response_fields (from transformer scan)
        response_text = ""
        resp = high.get("response_fields", {})
        if isinstance(resp, dict) and resp.get("fields"):
            fields = resp["fields"][:15]
            response_text = f" | Response: [{', '.join(fields)}]"

        content = (
            f"API: {method} {endpoint} | ID: {api_id}"
            f" | Domain: {domain}/{subdomain}"
            f" | Intent: {intent_primary_tag}"
            f" | Summary: {summary}"
            f"{f' | Agent: {agent}' if agent else ''}"
            f"{f' | Tool: {tool_candidate}' if tool_candidate else ''}"
            f"{f' | Risk: {risk_level}' if risk_level else ''}"
            f"{f' | RW: {read_write}' if read_write else ''}"
            f"{f' | Keywords: {', '.join(str(k) for k in keywords[:5])}' if keywords else ''}"
            f"{examples_text}{params_text}{response_text}"
        )

        return {
            "entity_type": "api_tool",
            "entity_id": f"api:{repo_id}:{api_id}",
            "content": content,
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.8,
            "metadata": {
                "api_id": api_id,
                "method": method,
                "endpoint": endpoint,
                "domain": domain,
                "subdomain": subdomain,
                "intent_primary": intent_primary_tag,
                "agent": agent,
                "agent_secondary": agent_secondary,
                "tool_candidate": tool_candidate,
                "risk_level": risk_level,
                "read_write_type": read_write,
                "has_examples": bool(examples_text),
                "has_request_schema": bool(params_text),
                "has_response_fields": bool(response_text),
                "source": "high.yaml",
            },
        }

    def _build_api_doc_from_files(self, api_dir: Path, repo_id: str) -> Optional[Dict]:
        """Fallback: build API doc from individual files."""
        overview = self._read_yaml(api_dir / "overview.yaml") or {}
        api_block = overview.get("api", {})
        endpoint = api_block.get("path", "")
        method = api_block.get("method", "GET")
        classification = overview.get("classification", {})
        domain = classification.get("domain", "")
        hints = overview.get("retrieval_hints", {})
        summary = hints.get("canonical_summary", "")

        if not endpoint and not summary:
            return None

        content = f"API: {method} {endpoint} | ID: {api_dir.name} | Domain: {domain} | Summary: {summary}"

        return {
            "entity_type": "api_tool",
            "entity_id": f"api:{repo_id}:{api_dir.name}",
            "content": content,
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": 0.8,
            "metadata": {"api_id": api_dir.name, "method": method, "endpoint": endpoint, "domain": domain, "source": "fallback"},
        }

    # ------------------------------------------------------------------
    # Pillar 3 — Extraction helpers
    # ------------------------------------------------------------------

    def _extract_examples_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        pairs = data.get("param_extraction_pairs", [])
        if not isinstance(pairs, list) or not pairs:
            return ""
        parts = []
        for pair in pairs[:5]:
            if isinstance(pair, dict):
                query = pair.get("query", "")
                params = pair.get("params", {})
                if query:
                    parts.append(f'"{query}" -> {json.dumps(params)}')
        return f" | Examples: {'; '.join(parts)}" if parts else ""

    def _extract_request_schema_from_high(self, data: Any) -> str:
        data = self._safe_section(data)
        if not data:
            return ""
        contract = data.get("contract", {})
        if not isinstance(contract, dict):
            return ""
        required = contract.get("required", [])
        optional = contract.get("optional", [])
        if not required and not optional:
            return ""

        parts = []
        table = data.get("source_table", data.get("table", ""))
        validation = data.get("validation_class", "")
        if table:
            parts.append(f"table:{table}")
        if validation:
            parts.append(f"validation:{validation}")

        req_names = []
        for p in (required if isinstance(required, list) else [])[:12]:
            if isinstance(p, dict):
                req_names.append(f"{p.get('name', '')}({p.get('type', '')})")
        if req_names:
            parts.append(f"required:[{', '.join(req_names)}]")

        opt_names = [p.get("name", "") for p in (optional if isinstance(optional, list) else [])[:8] if isinstance(p, dict)]
        if opt_names:
            parts.append(f"optional:[{', '.join(opt_names)}]")

        return f" | Params: {'; '.join(parts)}" if parts else ""

    # ------------------------------------------------------------------
    # Pillar 1 — Non-table knowledge (catalog, access patterns, indexes)
    # ------------------------------------------------------------------

    def read_pillar1_extras(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read non-table Pillar 1 knowledge: catalog, access_patterns,
        connections, relationships, all_tables_index, json_keys."""
        docs = []
        pillar_path = self.kb_path / repo_id / "pillar_1_schema"
        if not pillar_path.exists():
            return docs

        # --- catalog/ (domain-grouped table overviews) ---
        catalog_dir = pillar_path / "catalog"
        if catalog_dir.exists():
            for yf in sorted(catalog_dir.glob("*.yaml")):
                data = self._read_yaml(yf)
                if not data:
                    continue
                table = data.get("table", yf.stem)
                content = str(data)[:4000]
                docs.append({
                    "entity_type": "catalog",
                    "entity_id": f"catalog:{repo_id}:{yf.stem}",
                    "content": f"[Catalog] {content}",
                    "repo_id": repo_id,
                    "capability": "retrieval",
                    "trust_score": 0.85,
                    "metadata": {"file": yf.name, "source": "pillar1_catalog"},
                })

        # --- access_patterns/ (column-level write paths) ---
        ap_dir = pillar_path / "access_patterns"
        if ap_dir.exists():
            for yf in sorted(ap_dir.glob("*.yaml")):
                data = self._read_yaml(yf)
                if not data:
                    continue
                content = str(data)[:4000]
                docs.append({
                    "entity_type": "access_pattern",
                    "entity_id": f"access:{repo_id}:{yf.stem}",
                    "content": f"[AccessPattern] {content}",
                    "repo_id": repo_id,
                    "capability": "retrieval",
                    "trust_score": 0.85,
                    "metadata": {"file": yf.name, "source": "pillar1_access_patterns"},
                })

        # --- json_keys/ (JSON column key definitions) ---
        jk_dir = pillar_path / "json_keys"
        if jk_dir.exists():
            for yf in sorted(jk_dir.glob("*.yaml")):
                data = self._read_yaml(yf)
                if not data:
                    continue
                content = str(data)[:3000]
                docs.append({
                    "entity_type": "json_keys",
                    "entity_id": f"jsonkeys:{repo_id}:{yf.stem}",
                    "content": f"[JSONKeys] {content}",
                    "repo_id": repo_id,
                    "capability": "retrieval",
                    "trust_score": 0.8,
                    "metadata": {"file": yf.name, "source": "pillar1_json_keys"},
                })

        # --- Top-level files ---
        for filename, entity_type, trust in [
            ("connections.yaml", "db_connections", 0.9),
            ("relationships.yaml", "entity_relationships", 0.9),
            ("all_tables_index.yaml", "table_index", 0.85),
        ]:
            fpath = pillar_path / filename
            if not fpath.exists():
                continue
            data = self._read_yaml(fpath)
            if not data:
                continue
            content = str(data)[:5000]
            docs.append({
                "entity_type": entity_type,
                "entity_id": f"p1:{repo_id}:{filename.replace('.yaml', '')}",
                "content": f"[{entity_type}] {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": trust,
                "metadata": {"file": filename, "source": "pillar1_top_level"},
            })

        logger.info("kb_ingestor.pillar1_extras_read", repo=repo_id, docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 3 — API classification (deep evidence-based)
    # ------------------------------------------------------------------

    def read_pillar3_extras(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read non-API Pillar 3 knowledge: api_classification."""
        docs = []
        api_base = self.kb_path / repo_id / "pillar_3_api_mcp_tools"
        if not api_base.exists():
            return docs

        class_dir = api_base / "api_classification"
        if class_dir.exists():
            for yf in sorted(class_dir.glob("*.yaml")):
                data = self._read_yaml(yf)
                if not data:
                    continue
                content = str(data)[:4000]
                docs.append({
                    "entity_type": "api_classification",
                    "entity_id": f"apiclass:{repo_id}:{yf.stem}",
                    "content": f"[APIClassification] {content}",
                    "repo_id": repo_id,
                    "capability": "retrieval",
                    "trust_score": 0.9,
                    "metadata": {"file": yf.name, "source": "pillar3_classification"},
                })

        logger.info("kb_ingestor.pillar3_extras_read", repo=repo_id, docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 4 — Page/Role Intelligence
    # ------------------------------------------------------------------

    def read_pillar4_pages(self, repo_id: str = "SR_Web") -> List[Dict]:
        """Read Pillar 4 page intelligence: actions, fields, API bindings, roles."""
        docs = []
        p4_path = self.kb_path / repo_id / "pillar_4_page_role_intelligence"
        if not p4_path.exists():
            return docs

        # --- Pages ---
        pages_dir = p4_path / "pages"
        if pages_dir.exists():
            for page_dir in sorted(pages_dir.iterdir()):
                if not page_dir.is_dir():
                    continue
                page_id = page_dir.name

                # Read key files
                parts = [f"Page: {page_id}"]

                page_meta = self._read_yaml(page_dir / "page_meta.yaml")
                if page_meta and isinstance(page_meta, dict):
                    parts.append(f"Title: {page_meta.get('title', page_id)}")

                actions = self._read_yaml(page_dir / "actions.yaml")
                if actions and isinstance(actions, (list, dict)):
                    action_list = actions if isinstance(actions, list) else actions.get("actions", [])
                    action_names = [a.get("label", a.get("id", "")) for a in action_list[:10] if isinstance(a, dict)]
                    if action_names:
                        parts.append(f"Actions: {', '.join(action_names)}")

                fields = self._read_yaml(page_dir / "fields.yaml")
                if fields and isinstance(fields, (list, dict)):
                    field_list = fields if isinstance(fields, list) else fields.get("fields", [])
                    field_names = [f.get("name", f.get("label", "")) for f in field_list[:15] if isinstance(f, dict)]
                    if field_names:
                        parts.append(f"Fields: {', '.join(field_names)}")

                api_bindings = self._read_yaml(page_dir / "api_bindings.yaml")
                if api_bindings and isinstance(api_bindings, (list, dict)):
                    binding_list = api_bindings if isinstance(api_bindings, list) else api_bindings.get("bindings", [])
                    apis = [b.get("api_id", "") for b in binding_list[:10] if isinstance(b, dict)]
                    if apis:
                        parts.append(f"APIs: {', '.join(apis)}")

                roles = self._read_yaml(page_dir / "role_permissions.yaml")
                if roles and isinstance(roles, dict):
                    role_names = list(roles.keys())[:5]
                    parts.append(f"Roles: {', '.join(role_names)}")

                content = " | ".join(parts)
                docs.append({
                    "entity_type": "page_intelligence",
                    "entity_id": f"page:{repo_id}:{page_id}",
                    "content": content,
                    "repo_id": repo_id,
                    "capability": "retrieval",
                    "trust_score": 0.85,
                    "metadata": {"page_id": page_id, "source": "pillar4"},
                })

        # --- role_matrix.yaml ---
        rm = self._read_yaml(p4_path / "role_matrix.yaml")
        if rm:
            content = str(rm)[:4000]
            docs.append({
                "entity_type": "role_matrix",
                "entity_id": f"role_matrix:{repo_id}",
                "content": f"[RoleMatrix:{repo_id}] {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.9,
                "metadata": {"source": "pillar4_role_matrix"},
            })

        # --- cross_repo_mapping.yaml ---
        crm = self._read_yaml(p4_path / "cross_repo_mapping.yaml")
        if crm:
            content = str(crm)[:3000]
            docs.append({
                "entity_type": "cross_repo_page_mapping",
                "entity_id": f"cross_page_map:{repo_id}",
                "content": f"[CrossRepoPageMapping:{repo_id}] {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.85,
                "metadata": {"source": "pillar4_cross_repo"},
            })

        logger.info("kb_ingestor.pillar4_read", repo=repo_id, docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 5 — Module docs
    # ------------------------------------------------------------------

    def read_pillar5_modules(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read Pillar 5 module documentation: business rules, API docs, debugging.

        Reads from high/ folder (chunked files) if it exists,
        falls back to reading source YAML files directly.
        Each chunk becomes a separate embedding doc.
        """
        docs = []
        p5_path = self.kb_path / repo_id / "pillar_5_module_docs"
        if not p5_path.exists():
            return docs

        modules_dir = p5_path / "modules"
        if not modules_dir.exists():
            return docs

        for mod_dir in sorted(modules_dir.iterdir()):
            if not mod_dir.is_dir():
                continue
            module_name = mod_dir.name
            high_dir = mod_dir / "high"

            if high_dir.is_dir():
                # Preferred: read chunk files from high/ folder
                for yf in sorted(high_dir.glob("*.yaml")):
                    data = self._read_yaml(yf)
                    if not data:
                        continue
                    content = data.get("content", "")
                    if not content or len(content) < 50:
                        continue
                    quality = data.get("quality_score", 50)
                    section = data.get("section", yf.stem)

                    docs.append({
                        "entity_type": "module_doc",
                        "entity_id": f"module:{repo_id}:{module_name}:{section}",
                        "content": f"[{module_name}:{section}] {content[:3600]}",
                        "repo_id": repo_id,
                        "capability": "retrieval",
                        "trust_score": min(quality / 100, 0.95),
                        "metadata": {
                            "module": module_name,
                            "section": section,
                            "quality_score": quality,
                            "source": "pillar5_chunked",
                        },
                    })
            else:
                # Fallback: read source YAML files
                for yf in sorted(mod_dir.glob("*.yaml")):
                    if yf.name == "index.yaml":
                        continue
                    data = self._read_yaml(yf)
                    if not data:
                        continue
                    content = data.get("content", "")
                    if not content or not isinstance(content, str) or len(content) < 50:
                        continue
                    quality = data.get("quality_score", 50)

                    docs.append({
                        "entity_type": "module_doc",
                        "entity_id": f"module:{repo_id}:{module_name}:{yf.stem}",
                        "content": f"[{module_name}:{yf.stem}] {content[:3600]}",
                        "repo_id": repo_id,
                        "capability": "retrieval",
                        "trust_score": min(quality / 100, 0.95),
                        "metadata": {"module": module_name, "source": "pillar5"},
                    })

        logger.info("kb_ingestor.pillar5_read", repo=repo_id, docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Eval seeds — global_eval_set.jsonl / training_seeds.jsonl
    # ------------------------------------------------------------------

    def read_eval_seeds(self, repo_id: Optional[str] = None) -> List[Dict]:
        """Read global_eval_set.jsonl and training_seeds.jsonl files."""
        docs = []
        for repo_dir in sorted(self.kb_path.iterdir()):
            if not repo_dir.is_dir():
                continue
            if repo_id and repo_dir.name != repo_id:
                continue
            for eval_file in repo_dir.glob("**/global_eval_set.jsonl"):
                docs.extend(self._read_jsonl_seeds(eval_file, repo_dir.name, "eval", "expected_tool"))
            for seed_file in repo_dir.glob("**/training_seeds.jsonl"):
                docs.extend(self._read_jsonl_seeds(seed_file, repo_dir.name, "seed", "intent"))
        logger.info("kb_ingestor.eval_seeds_read", docs=len(docs))
        return docs

    def _read_jsonl_seeds(self, path: Path, repo_name: str, prefix: str, label_key: str, max_entries: int = 10000) -> List[Dict]:
        docs = []
        try:
            with open(path) as f:
                for i, line in enumerate(f):
                    if i >= max_entries:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    query = entry.get("query", "") or entry.get("input", "")
                    label = entry.get(label_key, "")
                    if not query:
                        continue
                    docs.append({
                        "entity_type": "eval_seed",
                        "entity_id": f"{prefix}:{repo_name}:{path.stem}:{i}",
                        "content": f"Query: {query} | {label_key}: {label}",
                        "repo_id": repo_name,
                        "capability": "intent_seed",
                        "trust_score": 0.9,
                        "metadata": entry,
                    })
        except Exception as e:
            logger.warning("kb_ingestor.jsonl_read_error", file=str(path), error=str(e))
        return docs

    # ------------------------------------------------------------------
    # Generated artifacts
    # ------------------------------------------------------------------

    def read_generated_artifacts(self) -> List[Dict]:
        docs = []
        gen_path = self.kb_path / "generated"
        if not gen_path.exists():
            return docs
        for repo_dir in sorted(gen_path.iterdir()):
            if not repo_dir.is_dir():
                continue
            for module_dir in sorted(repo_dir.iterdir()):
                if not module_dir.is_dir():
                    continue
                manifest = self._read_yaml(module_dir / "generated_manifest.yaml") or {}
                trust = manifest.get("trust_score", 0.65)
                for artifact in module_dir.glob("*.yaml"):
                    if artifact.name == "generated_manifest.yaml":
                        continue
                    data = self._read_yaml(artifact)
                    if not data:
                        continue
                    docs.append({
                        "entity_type": artifact.stem,
                        "entity_id": f"gen:{repo_dir.name}:{module_dir.name}:{artifact.stem}",
                        "content": f"[{repo_dir.name}/{module_dir.name}] {str(data)[:3000]}",
                        "repo_id": repo_dir.name,
                        "capability": "retrieval",
                        "trust_score": trust,
                        "metadata": {"module": module_dir.name, "artifact_type": artifact.stem, "generated": True},
                    })
        logger.info("kb_ingestor.generated_read", docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Convenience: read everything
    # ------------------------------------------------------------------

    def read_all(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        docs = []
        # Pillar 1: tables + extras (catalog, connections, relationships, etc.)
        docs.extend(self.read_pillar1_schema(repo_id))
        docs.extend(self.read_pillar1_extras(repo_id))
        # Pillar 3: APIs + extras (api_classification)
        docs.extend(self.read_pillar3_apis(repo_id))
        docs.extend(self.read_pillar3_extras(repo_id))
        # Pillar 4: page/role intelligence (SR_Web, MultiChannel_Web)
        for p4_repo in ["SR_Web", "MultiChannel_Web"]:
            docs.extend(self.read_pillar4_pages(p4_repo))
        # Pillar 5: module docs (all repos)
        all_repos = [d.name for d in self.kb_path.iterdir()
                     if d.is_dir() and (d / "pillar_5_module_docs").exists()]
        for p5_repo in sorted(all_repos):
            docs.extend(self.read_pillar5_modules(p5_repo))
        # Eval seeds + generated artifacts
        docs.extend(self.read_eval_seeds(repo_id))
        docs.extend(self.read_generated_artifacts())
        logger.info("kb_ingestor.read_all_complete", total=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_yaml(self, path: Path) -> Optional[Dict]:
        if not path.exists():
            return None
        try:
            import yaml
            with open(path) as f:
                return yaml.safe_load(f)
        except Exception:
            return None

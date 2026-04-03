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

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()


class KBIngestor:
    """Reads knowledge base YAML/JSONL files and produces IngestDocument lists."""

    def __init__(self, kb_path: str):
        self.kb_path = Path(kb_path)

    # ------------------------------------------------------------------
    # Manifest integrity helpers
    # ------------------------------------------------------------------

    _MANIFEST_FILENAME = ".cosmos_manifest.json"

    def _compute_manifest(self, directory: str) -> Dict[str, str]:
        """Compute SHA-256 checksums for all .yaml/.yml files in directory.

        Returns:
            {filename: sha256_hex} mapping (filenames are relative to directory).
        """
        manifest: Dict[str, str] = {}
        base = Path(directory)
        for ext in ("*.yaml", "*.yml"):
            for path in sorted(base.rglob(ext)):
                try:
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    rel = str(path.relative_to(base))
                    manifest[rel] = digest
                except OSError as exc:
                    logger.warning(
                        "kb_ingestor.manifest.read_error",
                        path=str(path),
                        error=str(exc),
                    )
        return manifest

    def _verify_manifest(
        self, directory: str, manifest: Dict[str, str]
    ) -> List[str]:
        """Compare current files against a previously computed manifest.

        Returns:
            List of change descriptions (changed, new, or missing files).
            Empty list means everything matches.
        """
        current = self._compute_manifest(directory)
        changes: List[str] = []

        for filename, saved_hash in manifest.items():
            current_hash = current.get(filename)
            if current_hash is None:
                changes.append(f"MISSING: {filename}")
            elif current_hash != saved_hash:
                changes.append(f"CHANGED: {filename}")

        for filename in current:
            if filename not in manifest:
                changes.append(f"NEW: {filename}")

        return changes

    def save_manifest(self, directory: str) -> Dict[str, str]:
        """Compute checksums and persist them to {directory}/.cosmos_manifest.json.

        Returns:
            The manifest dict that was saved.
        """
        manifest = self._compute_manifest(directory)
        manifest_path = Path(directory) / self._MANIFEST_FILENAME
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2))
            logger.info(
                "kb_ingestor.manifest.saved",
                directory=directory,
                files=len(manifest),
            )
        except OSError as exc:
            logger.warning(
                "kb_ingestor.manifest.save_error",
                directory=directory,
                error=str(exc),
            )
        return manifest

    def verify_against_saved_manifest(
        self, directory: str
    ) -> Tuple[bool, List[str]]:
        """Load the saved manifest and verify current files against it.

        Returns:
            (True, [])           — all files match the saved manifest.
            (False, [changes])   — list of CHANGED/NEW/MISSING entries.
            (True, [])           — no manifest found (treated as clean first run).
        """
        manifest_path = Path(directory) / self._MANIFEST_FILENAME
        if not manifest_path.exists():
            logger.info(
                "kb_ingestor.manifest.not_found",
                directory=directory,
            )
            return True, []

        try:
            saved: Dict[str, str] = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "kb_ingestor.manifest.load_error",
                directory=directory,
                error=str(exc),
            )
            return True, []

        changes = self._verify_manifest(directory, saved)
        if changes:
            return False, changes
        return True, []

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

            # Prefer high.yaml (merged) over high/ dir. Skip stubs.
            high_yaml = table_dir / "high.yaml"
            if high_yaml.exists():
                high = self._read_yaml(high_yaml)
                if high:
                    if high.get("_status") == "stub" or high.get("_tier") == "stub":
                        continue  # Skip stub files entirely
                    doc = self._build_table_doc_from_high(table_name, repo_id, high)
                else:
                    doc = None
            elif high_dir.is_dir():
                chunk_docs = self._read_high_folder(high_dir, table_name, repo_id, "schema")
                docs.extend(chunk_docs)
                continue
            else:
                doc = self._build_table_doc_from_files(table_dir, table_name, repo_id)
            if doc:
                docs.append(doc)

        logger.info("kb_ingestor.pillar1_read", repo=repo_id, tables=len(docs))
        return docs

    # Maps high/ folder filenames to typed chunk_type values for Pillar 3 APIs
    _P3_CHUNK_TYPE_MAP = {
        "overview": "api_overview",
        "tool_agent_tags": "api_tool_tags",
        "examples": "api_example",
        "db_mapping": "api_db_mapping",
        "request_schema": "api_schema",
        "response_fields": "api_schema",
        "index": "api_overview",
    }

    def _read_high_folder(self, high_dir: Path, entity_name: str, repo_id: str, entity_type: str) -> List[Dict]:
        """Read all YAML chunk files from a high/ folder.

        Each file becomes a separate embedding doc with entity_id suffixed
        by the chunk name (e.g., table:orders:identity, table:orders:states).
        Adds typed metadata (chunk_type, parent_doc_id, section, pillar, chunk_index)
        to the metadata JSON for post-retrieval filtering.
        """
        docs = []
        is_api = entity_type == "api_tool"
        is_schema = entity_type == "schema"
        entity_prefix = "table" if is_schema else "api"
        pillar = "pillar_3" if is_api else "pillar_1"
        parent_doc_id = f"{'api_endpoint' if is_api else 'schema'}:{repo_id}:{entity_name}"

        for chunk_index, yf in enumerate(sorted(high_dir.glob("*.yaml"))):
            data = self._read_yaml(yf)
            if not data:
                continue

            chunk_name = yf.stem

            if len(str(data)) < 30:
                continue

            # Determine typed chunk_type
            if is_api:
                chunk_type = self._P3_CHUNK_TYPE_MAP.get(chunk_name, "api_doc")
            else:
                chunk_type = f"schema_{chunk_name}"

            # Phase 4a: split examples.yaml into one chunk per param_extraction_pair
            # One blob scores ~0.70 similarity; one chunk per pair scores ~0.95
            if is_api and chunk_name == "examples" and isinstance(data, dict):
                pairs = data.get("param_extraction_pairs", [])
                if pairs:
                    for pair_idx, pair in enumerate(pairs):
                        if not isinstance(pair, dict):
                            continue
                        input_text = pair.get("input", pair.get("query", ""))
                        expected_tool = pair.get("expected_tool", pair.get("tool", ""))
                        entity_type_val = pair.get("entity_type", "")
                        entity_val = pair.get("entity_value", "")
                        params = pair.get("params", {})

                        if not input_text:
                            continue

                        parts = [f"Example: {input_text}"]
                        if expected_tool:
                            parts.append(f"tool: {expected_tool}")
                        if entity_type_val:
                            parts.append(f"entity_type: {entity_type_val}")
                        if entity_val:
                            parts.append(f"entity_value: {entity_val}")
                        if params:
                            parts.append(f"params: {params}")
                        pair_content = " | ".join(parts)

                        docs.append({
                            "entity_type": entity_type,
                            "entity_id": f"{entity_prefix}:{entity_name}:example:{pair_idx}",
                            "content": f"[{entity_name}:example:{pair_idx}] {pair_content}",
                            "repo_id": repo_id,
                            "capability": "retrieval",
                            "trust_score": 0.85,  # examples are highly reliable signal
                            "metadata": {
                                "entity_name": entity_name,
                                "chunk_type": "api_example",
                                "section": "examples",
                                "parent_doc_id": parent_doc_id,
                                "chunk_index": chunk_index * 1000 + pair_idx,  # stable ordering
                                "pillar": pillar,
                                "source": "high_folder",
                                "expected_tool": expected_tool,
                                "entity_type_val": entity_type_val,
                            },
                        })
                    continue  # skip the blob — per-pair chunks replace it

            content = self._render_chunk(data, chunk_name, entity_name, is_api)

            docs.append({
                "entity_type": entity_type,
                "entity_id": f"{entity_prefix}:{entity_name}:{chunk_name}",
                "content": f"[{entity_name}:{chunk_name}] {content}",
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.8,
                "metadata": {
                    "entity_name": entity_name,
                    "chunk_type": chunk_type,
                    "section": chunk_name,
                    "parent_doc_id": parent_doc_id,
                    "chunk_index": chunk_index,
                    "pillar": pillar,
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

                # Prefer high.yaml (merged) over high/ dir (split) to avoid double-embedding.
                # Skip entire file if it's a stub (_status: stub or _tier: stub).
                high_yaml_path = api_dir / "high.yaml"
                if high_yaml_path.exists():
                    high = self._read_yaml(high_yaml_path)
                    if high:
                        # Skip stubs at file level
                        if high.get("_status") == "stub" or high.get("_tier") == "stub":
                            continue
                        doc = self._build_api_doc_from_high(api_dir.name, repo_id, high)
                    else:
                        doc = None
                elif (api_dir / "high").is_dir():
                    chunk_docs = self._read_high_folder(api_dir / "high", api_dir.name, repo_id, "api_tool")
                    docs.extend(chunk_docs)
                    continue
                else:
                    doc = self._build_api_doc_from_files(api_dir, repo_id)
                if doc:
                    docs.append(doc)

        # Registry files — ingest per-record instead of one blob
        registry_files = {
            "entity_dictionary.yaml": ("entity_def", "entities"),
            "intent_taxonomy.yaml": ("intent_def", "intents"),
            "agent_registry.yaml": ("agent_def", "agents"),
            "api_registry.yaml": ("api_registry", "apis"),
            "tool_registry.yaml": ("tool_registry", "tools"),
        }
        for filename, (entity_type, list_key) in registry_files.items():
            reg_path = api_base / filename
            if not reg_path.exists():
                continue
            data = self._read_yaml(reg_path) or {}
            records = data.get(list_key, [])
            if isinstance(records, list) and records:
                # Per-record ingestion: each API/tool/agent gets its own doc
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    rec_id = rec.get("api_id") or rec.get("tool_id") or rec.get("agent_id") or rec.get("entity_id") or rec.get("id", "")
                    if not rec_id:
                        continue
                    parts = [f"{k}: {v}" for k, v in rec.items() if isinstance(v, (str, int, float, bool)) and v]
                    content = " | ".join(parts)
                    docs.append({
                        "entity_type": entity_type,
                        "entity_id": f"registry:{repo_id}:{rec_id}",
                        "content": f"[{repo_id}:{filename}] {content}",
                        "repo_id": repo_id,
                        "capability": "retrieval",
                        "trust_score": 0.8,
                        "metadata": {
                            "file": filename,
                            "domain": rec.get("domain", ""),
                            "training_ready": rec.get("training_ready", False),
                        },
                    })
            else:
                # Fallback for registries without a list key
                parts = [f"{k}: {v}" for k, v in data.items()
                         if isinstance(v, (str, int, float, bool)) and not k.startswith("_")]
                docs.append({
                    "entity_type": entity_type,
                    "entity_id": f"registry:{repo_id}:{filename}",
                    "content": f"[{repo_id}] {filename}: {' | '.join(parts)}",
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
            response_text = f" | Response: [{', '.join(str(f) for f in fields)}]"

        # Check if this API was enriched by Claude (source code reading)
        is_enriched = high.get("_enriched_by_claude", False)

        # For enriched APIs: include business_logic description (much richer content)
        business_logic_text = ""
        if is_enriched:
            bl = high.get("overview", {}).get("business_logic", {})
            if isinstance(bl, dict):
                desc = bl.get("description", "")
                if desc:
                    business_logic_text = f" | Business Logic: {desc[:500]}"
                # Include database reads
                db_reads = bl.get("database_reads", [])
                if db_reads and isinstance(db_reads, list):
                    tables = [r.get("table", "") for r in db_reads if isinstance(r, dict)]
                    if tables:
                        business_logic_text += f" | Tables: {', '.join(tables[:10])}"
                # Include side effects
                side_effects = bl.get("side_effects", [])
                if side_effects and isinstance(side_effects, list):
                    business_logic_text += f" | Side Effects: {'; '.join(str(s) for s in side_effects[:5])}"

        content = (
            f"API: {method} {endpoint} | ID: {api_id}"
            f" | Domain: {domain}/{subdomain}"
            f" | Intent: {intent_primary_tag}"
            f" | Summary: {summary}"
            f"{f' | Agent: {agent}' if agent else ''}"
            f"{f' | Tool: {tool_candidate}' if tool_candidate else ''}"
            f"{f' | Risk: {risk_level}' if risk_level else ''}"
            f"{f' | RW: {read_write}' if read_write else ''}"
            f"{f' | Keywords: {", ".join(str(k) for k in keywords[:5])}' if keywords else ''}"
            f"{business_logic_text}"
            f"{examples_text}{params_text}{response_text}"
        )

        # Enriched APIs get higher trust score (rank higher in retrieval)
        trust = 0.95 if is_enriched else 0.5

        return {
            "entity_type": "api_tool",
            "entity_id": f"api:{repo_id}:{api_id}",
            "content": content,
            "repo_id": repo_id,
            "capability": "retrieval",
            "trust_score": trust,
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
                "enriched": is_enriched,
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
                content = self._render_chunk(data, "identity", yf.stem, False)
                docs.append({
                    "entity_type": "catalog",
                    "entity_id": f"catalog:{repo_id}:{yf.stem}",
                    "content": f"[Catalog:{table}] {content}",
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
                content = self._render_chunk(data, "rules", yf.stem, False)
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
                content = self._render_chunk(data, "identity", yf.stem, False)
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
            content = self._render_chunk(data, "identity", filename.replace('.yaml', ''), False)
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
                content = self._render_chunk(data, "overview", yf.stem, True)
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
        """Read Pillar 4 page intelligence as typed chunks.

        Each page produces up to 4 typed embedding docs:
          page_summary    — route, domain, page_type, roles_required
          page_fields     — all field names and descriptions
          page_api_bindings — endpoint + method + params
          page_field_trace  — field→api→column traces (highest value for ICRM queries)

        Each chunk includes chunk_type, parent_doc_id, pillar in metadata JSON
        for post-retrieval filtering in Wave 2 context assembly.
        """
        docs = []
        p4_path = self.kb_path / repo_id / "pillar_4_page_role_intelligence"
        if not p4_path.exists():
            return docs

        # --- Pages: emit typed chunks ---
        pages_dir = p4_path / "pages"
        if pages_dir.exists():
            for page_dir in sorted(pages_dir.iterdir()):
                if not page_dir.is_dir():
                    continue
                page_id = page_dir.name
                parent_doc_id = f"page:{repo_id}:{page_id}"

                # trust_score from index.yaml confidence field
                index_data = self._read_yaml(page_dir / "index.yaml") or {}
                confidence_str = index_data.get("confidence", "medium") if isinstance(index_data, dict) else "medium"
                trust = {"high": 0.9, "medium": 0.8, "low": 0.7}.get(str(confidence_str).lower(), 0.8)

                base_meta = {
                    "page_id": page_id,
                    "parent_doc_id": parent_doc_id,
                    "pillar": "pillar_4",
                    "repo_id": repo_id,
                    "source": "pillar4",
                }

                # ── Chunk 1: page_summary ─────────────────────────────────
                page_meta = self._read_yaml(page_dir / "page_meta.yaml")
                if page_meta and isinstance(page_meta, dict):
                    route = page_meta.get("route", page_meta.get("path", ""))
                    domain = page_meta.get("domain", "")
                    page_type = page_meta.get("page_type", page_meta.get("type", ""))
                    title = page_meta.get("title", page_id)
                    roles = page_meta.get("roles_required", page_meta.get("roles", []))
                    roles_str = ", ".join(roles) if isinstance(roles, list) else str(roles)

                    summary_content = (
                        f"Page: {title} | ID: {page_id}"
                        f"{f' | Route: {route}' if route else ''}"
                        f"{f' | Domain: {domain}' if domain else ''}"
                        f"{f' | Type: {page_type}' if page_type else ''}"
                        f"{f' | Roles: {roles_str}' if roles_str else ''}"
                    )
                    docs.append({
                        "entity_type": "page_intel",
                        "entity_id": f"page:{repo_id}:{page_id}:summary",
                        "content": summary_content,
                        "repo_id": repo_id,
                        "capability": "retrieval",
                        "trust_score": trust,
                        "metadata": {**base_meta, "chunk_type": "page_summary", "chunk_index": 0},
                    })

                # ── Chunk 2: page_fields ──────────────────────────────────
                fields_data = self._read_yaml(page_dir / "fields.yaml")
                if fields_data:
                    field_list = fields_data if isinstance(fields_data, list) else (
                        fields_data.get("fields", []) if isinstance(fields_data, dict) else []
                    )
                    if field_list:
                        field_lines = []
                        for f in field_list[:30]:
                            if isinstance(f, dict):
                                name = f.get("name", f.get("label", ""))
                                desc = f.get("description", f.get("desc", ""))
                                field_lines.append(f"{name}: {desc}" if desc else name)
                        if field_lines:
                            fields_content = (
                                f"Page fields for {page_id}:\n" + "\n".join(field_lines)
                            )
                            docs.append({
                                "entity_type": "page_intel",
                                "entity_id": f"page:{repo_id}:{page_id}:fields",
                                "content": fields_content,
                                "repo_id": repo_id,
                                "capability": "retrieval",
                                "trust_score": trust,
                                "metadata": {**base_meta, "chunk_type": "page_fields", "chunk_index": 1},
                            })

                # ── Chunk 3: page_api_bindings ────────────────────────────
                api_bindings_data = self._read_yaml(page_dir / "api_bindings.yaml")
                if api_bindings_data:
                    binding_list = api_bindings_data if isinstance(api_bindings_data, list) else (
                        api_bindings_data.get("bindings", []) if isinstance(api_bindings_data, dict) else []
                    )
                    if binding_list:
                        binding_lines = []
                        for b in binding_list[:15]:
                            if isinstance(b, dict):
                                api_id = b.get("api_id", b.get("endpoint", ""))
                                method = b.get("method", "")
                                action = b.get("action", b.get("trigger", ""))
                                line = f"{method} {api_id}".strip()
                                if action:
                                    line += f" (triggered by: {action})"
                                binding_lines.append(line)
                        if binding_lines:
                            bindings_content = (
                                f"API bindings for {page_id}:\n" + "\n".join(binding_lines)
                            )
                            docs.append({
                                "entity_type": "page_intel",
                                "entity_id": f"page:{repo_id}:{page_id}:api_bindings",
                                "content": bindings_content,
                                "repo_id": repo_id,
                                "capability": "retrieval",
                                "trust_score": trust,
                                "metadata": {**base_meta, "chunk_type": "page_api_bindings", "chunk_index": 2},
                            })

                # ── Chunk 4: page_field_trace (highest value) ─────────────
                field_trace_data = self._read_yaml(page_dir / "field_trace_chain.yaml")
                if field_trace_data:
                    trace_list = field_trace_data if isinstance(field_trace_data, list) else (
                        field_trace_data.get("traces", field_trace_data.get("fields", [])) if isinstance(field_trace_data, dict) else []
                    )
                    if trace_list:
                        trace_lines = []
                        for t in trace_list[:20]:
                            if isinstance(t, dict):
                                field = t.get("field_name", t.get("field", t.get("name", "")))
                                api = t.get("api", t.get("api_id", t.get("endpoint", "")))
                                col = t.get("db_column", t.get("column", t.get("table_column", "")))
                                table = t.get("table", t.get("db_table", ""))
                                line = f"{field}"
                                if api:
                                    line += f" → API: {api}"
                                if table and col:
                                    line += f" → {table}.{col}"
                                elif col:
                                    line += f" → column: {col}"
                                trace_lines.append(line)
                        if trace_lines:
                            trace_content = (
                                f"Field trace chains for {page_id}:\n" + "\n".join(trace_lines)
                            )
                            docs.append({
                                "entity_type": "page_intel",
                                "entity_id": f"page:{repo_id}:{page_id}:field_trace",
                                "content": trace_content,
                                "repo_id": repo_id,
                                "capability": "retrieval",
                                "trust_score": min(trust + 0.05, 0.95),  # slight boost — highest value
                                "metadata": {**base_meta, "chunk_type": "page_field_trace", "chunk_index": 3},
                            })

        # --- role_matrix.yaml ---
        rm = self._read_yaml(p4_path / "role_matrix.yaml")
        if rm:
            content = self._render_chunk(rm if isinstance(rm, dict) else {"data": rm}, "rules", "role_matrix", False)
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
            content = self._render_chunk(crm if isinstance(crm, dict) else {"data": crm}, "identity", "cross_repo_mapping", False)
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

        # Maps section names to typed chunk_type values for Pillar 5
        _P5_CHUNK_TYPE_MAP = {
            "api_routes": "module_routes",
            "routes": "module_routes",
            "cross_links": "module_cross_links",
            "dependencies": "module_cross_links",
        }

        for mod_dir in sorted(modules_dir.iterdir()):
            if not mod_dir.is_dir():
                continue
            module_name = mod_dir.name
            high_dir = mod_dir / "high"
            parent_doc_id = f"module:{repo_id}:{module_name}"

            if high_dir.is_dir():
                # Preferred: read chunk files from high/ folder
                for chunk_index, yf in enumerate(sorted(high_dir.glob("*.yaml"))):
                    data = self._read_yaml(yf)
                    if not data:
                        continue
                    content = data.get("content", "")
                    if not content or len(content) < 50:
                        continue
                    quality = data.get("quality_score", 50)
                    section = data.get("section", yf.stem)
                    chunk_type = _P5_CHUNK_TYPE_MAP.get(section, "module_summary")

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
                            "chunk_type": chunk_type,
                            "parent_doc_id": parent_doc_id,
                            "chunk_index": chunk_index,
                            "pillar": "pillar_5",
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
                    section = yf.stem
                    chunk_type = _P5_CHUNK_TYPE_MAP.get(section, "module_summary")

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
                            "chunk_type": chunk_type,
                            "parent_doc_id": parent_doc_id,
                            "pillar": "pillar_5",
                            "quality_score": quality,
                            "source": "pillar5",
                        },
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
                        "content": f"[{repo_dir.name}/{module_dir.name}] {self._render_chunk(data, artifact.stem, module_dir.name, False)}",
                        "repo_id": repo_dir.name,
                        "capability": "retrieval",
                        "trust_score": trust,
                        "metadata": {"module": module_dir.name, "artifact_type": artifact.stem, "generated": True},
                    })
        logger.info("kb_ingestor.generated_read", docs=len(docs))
        return docs

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Pillar 6: Action contracts (multi-file: domains/{domain}/{action}/{file}.yaml)
    # ------------------------------------------------------------------

    # File types and how to build content for each
    _ACTION_FILE_RENDERERS = {
        "index": lambda d: f"Action: {d.get('title','')} | Domain: {d.get('domain','')} | Kind: {d.get('kind','')} | Owner: {d.get('owner_module','')}",
        "intent_map": lambda d: (
            "Positive: " + "; ".join(d.get("positive_phrases", {}).get("english", [])[:5]) +
            "\nHinglish: " + "; ".join(d.get("positive_phrases", {}).get("hinglish", [])[:3]) +
            "\nNegative: " + "; ".join(n.get("query","") for n in d.get("negative_phrases", [])[:3])
        ),
        "contract": lambda d: f"Purpose: {d.get('purpose','')}\nInputs: {', '.join(i.get('name','') for i in d.get('required_inputs',[]))}\nSync/Async: {d.get('sync_async','')}\nIdempotent: {d.get('idempotent','')}",
        "permissions": lambda d: f"Roles: {', '.join(d.get('allowed_roles',[]))}\nApproval: {d.get('approval_mode','')}\nRisk: {d.get('customer_impact_level','')}",
        "data_access": lambda d: (
            "Reads: " + ", ".join(t.get("table","") for t in d.get("tables_read",[])) +
            "\nWrites: " + ", ".join(t.get("table","") for t in d.get("tables_written",[])) +
            "\nQueues: " + ", ".join(q.get("name","") for q in d.get("queues_used",[]))
        ),
        "execution_graph": lambda d: "\n".join(f"Step {s.get('step','')}: {s.get('action','')}" for s in d.get("steps", [])[:6]),
        "failure_modes": lambda d: "\n".join(f"Fail: {f.get('condition','')}" for f in d.get("validation_failures", [])[:3] + d.get("external_api_failure", [])[:2]),
        "observability": lambda d: f"Logs: {', '.join(d.get('log_tags',[]))}\nQueue: {d.get('queue_name','')}\nJob: {d.get('job_class','')}",
        "examples": lambda d: "\n".join(f"Q: {e.get('query','')}" for e in d.get("preview", [])[:2] + d.get("execute", [])[:2]),
        "eval_cases": lambda d: "\n".join(f"Eval: {e.get('query','')}" for e in d.get("positive", [])[:3] + d.get("regression", [])[:2]),
        "rollback": lambda d: "\n".join(f"Rollback: {s.get('action','')}" for s in d.get("rollback_steps", [])[:3]),
    }

    def read_pillar6_actions(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read multi-file action contracts from pillar_6_action_contracts/domains/."""
        p6_path = self.kb_path / repo_id / "pillar_6_action_contracts" / "domains"
        if not p6_path.exists():
            return []
        docs = []
        for domain_dir in sorted(p6_path.iterdir()):
            if not domain_dir.is_dir():
                continue
            for action_dir in sorted(domain_dir.iterdir()):
                if not action_dir.is_dir():
                    continue
                # Read index.yaml for metadata
                index = self._read_yaml(action_dir / "index.yaml")
                if not index:
                    continue
                action_id = index.get("action_id", f"action.{domain_dir.name}.{action_dir.name}")
                domain = index.get("domain", domain_dir.name)

                # Each YAML file becomes one embedding doc
                for fname, renderer in self._ACTION_FILE_RENDERERS.items():
                    fpath = action_dir / f"{fname}.yaml"
                    data = self._read_yaml(fpath)
                    if not data:
                        continue
                    try:
                        content = renderer(data)
                    except Exception:
                        content = str(data)[:500]

                    docs.append({
                        "source_id": f"{action_id}.{fname}",
                        "source_type": f"action_{fname}",
                        "content": content,
                        "metadata": {
                            "repo_id": repo_id,
                            "pillar": "pillar_6",
                            "domain": domain,
                            "action_id": action_id,
                            "file_type": fname,
                            "kind": index.get("kind", ""),
                            "training_ready": index.get("training_ready", False),
                        },
                        "entity_type": "action",
                        "entity_id": action_id,
                        "capability": "action",
                    })
        logger.info("kb_ingestor.pillar6_action_contracts", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 7: Workflow runbooks (multi-file: domains/{domain}/{workflow}/{file}.yaml)
    # ------------------------------------------------------------------

    _WORKFLOW_FILE_RENDERERS = {
        "index": lambda d: f"Workflow: {d.get('title','')} | Domain: {d.get('domain','')} | Entry: {', '.join(d.get('entry_entities',[]))}",
        "overview": lambda d: f"Starts: {d.get('what_starts_the_workflow','')}\nEnds: {d.get('what_ends_it','')}\nActors: {', '.join(d.get('main_actors',[]))}",
        "state_machine": lambda d: "\n".join(f"{t.get('from','')} → {t.get('to','')} ({t.get('trigger','')})" for t in d.get("allowed_transitions", [])[:8]),
        "entrypoints": lambda d: "\n".join(f"Entry: {e.get('page','') or e.get('api','') or e.get('name','')}" for e in d.get("ui_pages", [])[:3] + d.get("apis", [])[:3]),
        "decision_matrix": lambda d: "\n".join(f"Decision: {dec.get('name','')}" for dec in d.get("decisions", [])[:5]),
        "action_map": lambda d: "\n".join(f"Action: {a.get('action_name','')} → {a.get('linked_action_contract','')}" for a in d.get("actions", [])[:5]),
        "data_flow": lambda d: "\n".join(f"Step: {s.get('step','')} → writes: {s.get('writes_to','')}" for s in d.get("evidence_chain", [])[:5]),
        "ui_map": lambda d: "\n".join(f"Page: {p.get('page_id','')} ({p.get('path','')})" for p in d.get("pages", [])[:3]),
        "async_map": lambda d: "\n".join(f"Queue: {q.get('name','')} → {', '.join(q.get('jobs',[]))}" for q in d.get("queues", [])[:3]),
        "operator_playbook": lambda d: "\n".join(f"Diagnose: {s.get('action','')}" for s in d.get("diagnose", [])[:4]),
        "user_language": lambda d: (
            "Seller: " + "; ".join(d.get("seller_wording", [])[:4]) +
            "\nHinglish: " + "; ".join(d.get("hinglish_variants", [])[:4])
        ),
        "exception_paths": lambda d: "\n".join(f"Exception: {e.get('name','')}: {e.get('handling','')[:100]}" for e in d.get("exceptions", [])[:4]),
        "eval_cases": lambda d: "\n".join(f"Eval: {e.get('query','')}" for e in d.get("eval_cases", [])[:5]),
    }

    def read_pillar7_runbooks(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read multi-file workflow runbooks from pillar_7_workflow_runbooks/domains/."""
        p7_path = self.kb_path / repo_id / "pillar_7_workflow_runbooks" / "domains"
        if not p7_path.exists():
            return []
        docs = []
        for domain_dir in sorted(p7_path.iterdir()):
            if not domain_dir.is_dir():
                continue
            for wf_dir in sorted(domain_dir.iterdir()):
                if not wf_dir.is_dir():
                    continue
                index = self._read_yaml(wf_dir / "index.yaml")
                if not index:
                    continue
                wf_id = index.get("workflow_id", f"workflow.{domain_dir.name}.{wf_dir.name}")
                domain = index.get("domain", domain_dir.name)

                for fname, renderer in self._WORKFLOW_FILE_RENDERERS.items():
                    fpath = wf_dir / f"{fname}.yaml"
                    data = self._read_yaml(fpath)
                    if not data:
                        continue
                    try:
                        content = renderer(data)
                    except Exception:
                        content = str(data)[:500]

                    docs.append({
                        "source_id": f"{wf_id}.{fname}",
                        "source_type": f"workflow_{fname}",
                        "content": content,
                        "metadata": {
                            "repo_id": repo_id,
                            "pillar": "pillar_7",
                            "domain": domain,
                            "workflow_id": wf_id,
                            "file_type": fname,
                            "training_ready": index.get("training_ready", False),
                        },
                        "entity_type": "workflow",
                        "entity_id": wf_id,
                        "capability": "workflow",
                    })
        logger.info("kb_ingestor.pillar7_workflow_runbooks", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 8: Negative routing examples
    # ------------------------------------------------------------------

    def read_pillar8_negative_routing(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read cross-domain negative routing examples from pillar_8_negative_routing/."""
        p8_path = self.kb_path / repo_id / "pillar_8_negative_routing"
        if not p8_path.exists():
            return []
        docs = []
        for f in sorted(p8_path.glob("*.yaml")):
            data = self._read_yaml(f)
            if not data:
                continue
            examples = data.get("examples", [])
            # Bundle negatives into chunks of 10 for embedding efficiency
            for i in range(0, len(examples), 10):
                chunk = examples[i:i+10]
                content_parts = []
                for ex in chunk:
                    content_parts.append(
                        f"Query: {ex.get('user_query','')}\n"
                        f"  Looks like: {ex.get('looks_like','')}\n"
                        f"  Do NOT use: {ex.get('should_not_use','')}\n"
                        f"  Use instead: {ex.get('correct_tool','')}\n"
                        f"  Reason: {ex.get('reason','')}"
                    )
                docs.append({
                    "source_id": f"negative_routing.{f.stem}.chunk_{i//10}",
                    "source_type": "negative_routing",
                    "content": "\n---\n".join(content_parts),
                    "metadata": {
                        "repo_id": repo_id,
                        "pillar": "pillar_8",
                        "example_count": len(chunk),
                    },
                    "entity_type": "negative_routing",
                    "entity_id": f"negative_routing.{f.stem}",
                    "capability": "routing",
                })
        logger.info("kb_ingestor.pillar8_negatives", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Entity Hubs: Cross-pillar summaries
    # ------------------------------------------------------------------

    def read_entity_hubs(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read Entity Hub YAML docs from entity_hubs/."""
        hub_path = self.kb_path / repo_id / "entity_hubs"
        if not hub_path.exists():
            return []
        docs = []
        for f in sorted(hub_path.glob("*.yaml")):
            data = self._read_yaml(f)
            if not data or not data.get("content"):
                continue
            docs.append({
                "source_id": f"entity_hub:{f.stem}",
                "source_type": "entity_hub",
                "content": data["content"],
                "entity_type": "entity_hub",
                "entity_id": f"entity_hub:{f.stem}",
                "capability": "retrieval",
                "trust_score": 0.95,
                "metadata": {
                    "repo_id": repo_id,
                    "pillar": "entity_hub",
                    "domain": f.stem,
                    "chunk_type": "entity_hub_summary",
                    "query_mode": "lookup",
                },
            })
        logger.info("kb_ingestor.entity_hubs", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 9: Agent Definitions
    # ------------------------------------------------------------------

    def read_pillar9_agents(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read agent definition YAMLs from pillar_9_agents/."""
        p9_path = self.kb_path / repo_id / "pillar_9_agents"
        if not p9_path.exists():
            return []
        docs = []
        for f in sorted(p9_path.glob("*.yaml")):
            data = self._read_yaml(f)
            if not data:
                continue
            agent_name = data.get("agent_name", f.stem)
            # Build rich content for embedding
            parts = [
                f"Agent: {data.get('display_name', agent_name)}",
                f"Domain: {data.get('domain', '')}",
                f"Tier: {data.get('tier', '')}",
                f"Description: {data.get('description', '')}",
            ]
            if data.get("system_prompt"):
                parts.append(f"Instructions: {data['system_prompt']}")
            if data.get("tools_allowed"):
                tools = data["tools_allowed"]
                if isinstance(tools, list):
                    parts.append(f"Tools: {', '.join(str(t) for t in tools)}")
                elif isinstance(tools, dict):
                    parts.append(f"Tools: {', '.join(f'{k}: {v}' for k, v in tools.items())}")
            if data.get("skills"):
                skills = data["skills"]
                if isinstance(skills, list):
                    parts.append(f"Skills: {', '.join(str(s) for s in skills)}")
                elif isinstance(skills, dict):
                    parts.append(f"Skills: {', '.join(f'{k}: {v}' for k, v in skills.items())}")
            if data.get("handoff_rules"):
                parts.append(f"Handoffs: {', '.join(f'{k}: {v}' for k, v in data['handoff_rules'].items())}")
            if data.get("anti_patterns"):
                parts.append(f"Never do: {'; '.join(data['anti_patterns'])}")
            if data.get("example_queries"):
                parts.append(f"Example queries: {'; '.join(data['example_queries'][:5])}")

            docs.append({
                "entity_type": "agent_definition",
                "entity_id": f"agent:{agent_name}",
                "content": "\n".join(parts),
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.95,
                "metadata": {
                    "pillar": "pillar_9_agents",
                    "domain": data.get("domain", ""),
                    "agent_name": agent_name,
                    "tier": data.get("tier", ""),
                    "query_mode": "explain",
                },
            })
        logger.info("kb_ingestor.pillar9_agents", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 10: Skill Definitions
    # ------------------------------------------------------------------

    def read_pillar10_skills(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read skill definition YAMLs from pillar_10_skills/."""
        p10_path = self.kb_path / repo_id / "pillar_10_skills"
        if not p10_path.exists():
            return []
        docs = []
        for f in sorted(p10_path.glob("*.yaml")):
            data = self._read_yaml(f)
            if not data:
                continue
            skill_name = data.get("skill_name", f.stem)
            parts = [
                f"Skill: {data.get('display_name', skill_name)}",
                f"Domain: {data.get('domain', '')}",
                f"Description: {data.get('description', '')}",
            ]
            if data.get("triggers"):
                parts.append(f"Triggers: {', '.join(data['triggers'][:10])}")
            if data.get("steps"):
                for i, step in enumerate(data["steps"]):
                    if isinstance(step, dict):
                        parts.append(f"Step {i+1}: {step.get('type', '')} — {step.get('description', step.get('tool', ''))}")
                    else:
                        parts.append(f"Step {i+1}: {step}")
            if data.get("required_params"):
                parts.append(f"Required params: {', '.join(str(p) for p in data['required_params'])}")
            docs.append({
                "entity_type": "skill_definition",
                "entity_id": f"skill:{skill_name}",
                "content": "\n".join(parts),
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.95,
                "metadata": {
                    "pillar": "pillar_10_skills",
                    "domain": data.get("domain", ""),
                    "skill_name": skill_name,
                    "query_mode": "explain",
                },
            })
        logger.info("kb_ingestor.pillar10_skills", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Pillar 11: Tool Definitions
    # ------------------------------------------------------------------

    def read_pillar11_tools(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        """Read tool definition YAMLs from pillar_11_tools/."""
        p11_path = self.kb_path / repo_id / "pillar_11_tools"
        if not p11_path.exists():
            return []
        docs = []
        for f in sorted(p11_path.glob("*.yaml")):
            data = self._read_yaml(f)
            if not data:
                continue
            tool_name = data.get("tool_name", f.stem)
            parts = [
                f"Tool: {data.get('display_name', tool_name)}",
                f"Category: {data.get('category', 'read')}",
                f"Risk: {data.get('risk_level', 'low')}",
                f"Description: {data.get('description', '')}",
            ]
            if data.get("parameters"):
                for param in data["parameters"][:10]:
                    if isinstance(param, dict):
                        parts.append(f"Param: {param.get('name', '')} ({param.get('type', '')}) — {param.get('description', '')}")
            if data.get("data_source"):
                parts.append(f"Data source: {data['data_source']}")
            if data.get("endpoints"):
                for ep in data["endpoints"][:5]:
                    if isinstance(ep, dict):
                        parts.append(f"Endpoint: {ep.get('method', '')} {ep.get('path', '')}")
            docs.append({
                "entity_type": "tool_definition",
                "entity_id": f"tool_def:{tool_name}",
                "content": "\n".join(parts),
                "repo_id": repo_id,
                "capability": "retrieval",
                "trust_score": 0.95,
                "metadata": {
                    "pillar": "pillar_11_tools",
                    "domain": data.get("domain", ""),
                    "tool_name": tool_name,
                    "query_mode": "explain",
                },
            })
        logger.info("kb_ingestor.pillar11_tools", repo_id=repo_id, count=len(docs))
        return docs

    # ------------------------------------------------------------------
    # Convenience: read everything
    # ------------------------------------------------------------------

    def read_all(self, repo_id: str = "MultiChannel_API") -> List[Dict]:
        docs = []

        # Manifest integrity check — warn on any YAML file changes since last run.
        kb_dir = str(self.kb_path)
        manifest_ok, manifest_changes = self.verify_against_saved_manifest(kb_dir)
        if not manifest_ok:
            for change in manifest_changes:
                logger.warning("kb_ingestor.manifest.change_detected", change=change)
            logger.warning(
                "kb_ingestor.manifest.changes_summary",
                total_changes=len(manifest_changes),
                directory=kb_dir,
            )

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
        # Pillar 6: action contracts (multi-file)
        for p6_repo in [d.name for d in self.kb_path.iterdir()
                        if d.is_dir() and (d / "pillar_6_action_contracts").exists()]:
            docs.extend(self.read_pillar6_actions(p6_repo))
        # Pillar 7: workflow runbooks (multi-file)
        for p7_repo in [d.name for d in self.kb_path.iterdir()
                        if d.is_dir() and (d / "pillar_7_workflow_runbooks").exists()]:
            docs.extend(self.read_pillar7_runbooks(p7_repo))
        # Pillar 8: negative routing
        for p8_repo in [d.name for d in self.kb_path.iterdir()
                        if d.is_dir() and (d / "pillar_8_negative_routing").exists()]:
            docs.extend(self.read_pillar8_negative_routing(p8_repo))
        # Entity Hubs: cross-pillar summaries
        for hub_repo in [d.name for d in self.kb_path.iterdir()
                         if d.is_dir() and (d / "entity_hubs").exists()]:
            docs.extend(self.read_entity_hubs(hub_repo))
        # Eval seeds + generated artifacts
        docs.extend(self.read_eval_seeds(repo_id))
        docs.extend(self.read_generated_artifacts())

        # Tag every doc with query_mode for retrieval routing
        self._tag_query_modes(docs)

        logger.info("kb_ingestor.read_all_complete", total=len(docs))
        return docs

    # Mode mapping: which doc types help which query modes
    _QUERY_MODE_MAP = {
        # lookup: "what exists?" / "find X"
        "schema": "lookup", "schema_identity": "lookup", "schema_states": "lookup",
        "catalog": "lookup", "table_index": "lookup", "json_keys": "lookup",
        "api_overview": "lookup", "api_registry": "lookup", "tool_registry": "lookup",
        "entity_def": "lookup", "intent_def": "lookup", "agent_def": "lookup",
        # diagnose: "why did X happen?" / "what went wrong?"
        "workflow_state_machine": "diagnose", "workflow_exception_paths": "diagnose",
        "workflow_decision_matrix": "diagnose", "action_failure_modes": "diagnose",
        "workflow_data_flow": "diagnose", "workflow_eval_cases": "diagnose",
        "operator_runbook": "diagnose",
        # act: "what should I do?" / "trigger X"
        "action_contract": "act", "action_execution_graph": "act",
        "action_permissions": "act", "action_rollback": "act",
        "action_data_access": "act", "workflow_action_map": "act",
        "workflow_operator_playbook": "act",
        # explain: "how does X work?" / "what will happen?"
        "action_intent_map": "explain", "action_examples": "explain",
        "action_observability": "explain", "workflow_overview": "explain",
        "workflow_async_map": "explain", "workflow_user_language": "explain",
        "workflow_ui_map": "explain", "workflow_entrypoints": "explain",
        # routing: prevent hallucination
        "negative_routing": "routing", "action_eval_cases": "routing",
        # page: field questions
        "page_field_trace": "lookup", "page_intelligence": "lookup",
        "access_pattern": "lookup", "cross_repo_page_mapping": "lookup",
    }

    @classmethod
    def _tag_query_modes(cls, docs: List[Dict]) -> None:
        """Tag each doc with query_mode (lookup/diagnose/act/explain/routing)."""
        for doc in docs:
            meta = doc.get("metadata", {})
            source_type = doc.get("source_type", "")
            chunk_type = meta.get("chunk_type", "")
            file_type = meta.get("file_type", "")
            capability = doc.get("capability", "")

            # Try source_type first, then chunk_type, then file_type
            mode = (
                cls._QUERY_MODE_MAP.get(source_type)
                or cls._QUERY_MODE_MAP.get(chunk_type)
                or cls._QUERY_MODE_MAP.get(f"{capability}_{file_type}" if file_type else "")
                or cls._QUERY_MODE_MAP.get(file_type)
            )

            # Fallback by capability
            if not mode:
                if capability == "action":
                    mode = "act"
                elif capability == "workflow":
                    mode = "diagnose"
                elif capability == "routing":
                    mode = "routing"
                else:
                    mode = "lookup"

            meta["query_mode"] = mode
            doc["metadata"] = meta

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

    @staticmethod
    def _render_chunk(data: Dict, chunk_name: str, entity_name: str, is_api: bool) -> str:
        """Render YAML data into retrieval-optimized text instead of raw str(data).

        Each chunk type gets a custom renderer that extracts the most
        retrieval-relevant fields as clean text.
        """
        if not isinstance(data, dict):
            return str(data)[:3600]

        try:
            if chunk_name == "overview":
                api = data.get("overview", data).get("api", data.get("api", {})) if isinstance(data.get("overview", data), dict) else {}
                cls = data.get("overview", data).get("classification", data.get("classification", {})) if isinstance(data.get("overview", data), dict) else {}
                parts = []
                if api.get("path"):
                    parts.append(f"{api.get('method', '?')} {api.get('path', '')}")
                if api.get("controller"):
                    parts.append(f"Controller: {api['controller']}")
                if cls.get("domain"):
                    parts.append(f"Domain: {cls['domain']}")
                if cls.get("intent_primary"):
                    parts.append(f"Intent: {cls['intent_primary']}")
                hints = data.get("overview", data).get("retrieval_hints", {}) if isinstance(data.get("overview", data), dict) else {}
                if hints.get("canonical_summary"):
                    parts.append(hints["canonical_summary"])
                if hints.get("aliases"):
                    parts.append(f"Aliases: {', '.join(hints['aliases'][:5])}")
                return " | ".join(parts) if parts else str(data)[:3600]

            elif chunk_name == "tool_agent_tags":
                ta = data.get("tool_agent_tags", data) if isinstance(data.get("tool_agent_tags"), dict) else data
                tool = ta.get("tool_assignment", {})
                agent = ta.get("agent_assignment", {})
                routing = ta.get("routing_hints", {})
                parts = []
                if tool.get("tool_candidate"):
                    parts.append(f"Tool: {tool['tool_candidate']}")
                if tool.get("read_write_type"):
                    parts.append(f"Type: {tool['read_write_type']}")
                if tool.get("risk_level"):
                    parts.append(f"Risk: {tool['risk_level']}")
                if agent.get("owner"):
                    parts.append(f"Agent: {agent['owner']}")
                if routing.get("prefer_when"):
                    parts.append(f"Use when: {'; '.join(routing['prefer_when'][:3])}")
                if routing.get("avoid_when"):
                    parts.append(f"Avoid when: {'; '.join(routing['avoid_when'][:3])}")
                neg = ta.get("negative_routing_examples", [])
                for n in neg[:2]:
                    if isinstance(n, dict):
                        parts.append(f"NOT for: {n.get('user_query', '')}")
                return " | ".join(parts) if parts else str(data)[:3600]

            elif chunk_name == "request_schema":
                rs = data.get("request_schema", data) if isinstance(data.get("request_schema"), dict) else data
                parts = [f"{rs.get('method', '?')} {rs.get('path', '')}"]
                contract = rs.get("contract", {})
                req = contract.get("required", [])
                if req:
                    fields = [f.get("name", "") for f in req[:8] if isinstance(f, dict)]
                    parts.append(f"Required: {', '.join(fields)}")
                opt = contract.get("optional", [])
                if opt:
                    fields = [f.get("name", "") for f in opt[:5] if isinstance(f, dict)]
                    parts.append(f"Optional: {', '.join(fields)}")
                if rs.get("source_table"):
                    parts.append(f"Table: {rs['source_table']}")
                resp = rs.get("response_fields", {})
                if resp.get("fields"):
                    parts.append(f"Response: {', '.join(str(f) for f in resp['fields'][:8])}")
                se = rs.get("side_effects", {})
                if se.get("jobs"):
                    parts.append(f"Jobs: {', '.join(se['jobs'][:3])}")
                return " | ".join(parts) if parts else str(data)[:3600]

            elif chunk_name in ("identity", "states", "rules"):
                # Schema chunks — extract table-level semantic info
                parts = [f"Section: {chunk_name}"]
                if isinstance(data, dict):
                    for key in ("table_name", "purpose", "description", "columns", "status_values",
                                "validation_rules", "business_rules", "relationships"):
                        val = data.get(key)
                        if val:
                            if isinstance(val, list):
                                parts.append(f"{key}: {', '.join(str(v) for v in val[:10])}")
                            elif isinstance(val, str):
                                parts.append(f"{key}: {val[:200]}")
                            elif isinstance(val, dict):
                                parts.append(f"{key}: {', '.join(f'{k}={v}' for k, v in list(val.items())[:8])}")
                return " | ".join(parts) if parts else str(data)[:3600]

            else:
                # Generic fallback — extract top-level keys as structured text
                parts = []
                for key, val in list(data.items())[:15]:
                    if key.startswith("_"):
                        continue
                    if isinstance(val, str) and len(val) < 200:
                        parts.append(f"{key}: {val}")
                    elif isinstance(val, list) and len(val) <= 10:
                        parts.append(f"{key}: {', '.join(str(v)[:50] for v in val[:5])}")
                    elif isinstance(val, (int, float, bool)):
                        parts.append(f"{key}: {val}")
                return " | ".join(parts) if parts else str(data)[:3600]

        except Exception:
            return str(data)[:3600]

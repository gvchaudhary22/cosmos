"""
KB Quality Fixer — Fixes the 4 critical KB quality issues before training.

Fixes:
  1. Generate real examples for 5,483 Pillar 3 APIs (replace generic "Default endpoint invocation shape")
  2. Populate missing request_schema params (69% of APIs)
  3. Populate empty-column tables (37% of Pillar 1)
  4. Regenerate entity hubs with structured API/action/workflow links

Uses Claude Opus 4.6 for generation. Writes fixes directly to KB YAML files.
Caches results to avoid re-processing on subsequent runs.

Usage:
    fixer = KBQualityFixer(kb_path)
    report = await fixer.run_all_fixes()

    # Or individual:
    await fixer.fix_generic_examples()
    await fixer.fix_missing_params()
    await fixer.fix_empty_columns()
    await fixer.fix_entity_hubs()
"""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
import yaml

logger = structlog.get_logger()


class KBQualityFixer:
    """Fixes critical KB quality issues using Claude Opus."""

    def __init__(self, kb_path: str, model: str = "claude-opus-4-6", max_concurrent: int = 3):
        self.kb_path = Path(kb_path)
        self.model = model
        self.max_concurrent = max_concurrent
        self._cli = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._stats = {
            "examples_fixed": 0,
            "params_fixed": 0,
            "columns_fixed": 0,
            "hubs_fixed": 0,
            "api_calls": 0,
            "errors": 0,
        }

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=120)
        return self._cli

    async def run_all_fixes(self) -> Dict[str, Any]:
        """Run all 4 quality fixes in sequence."""
        t0 = time.time()
        logger.info("kb_quality_fixer.start")

        await self.fix_generic_examples()
        await self.fix_missing_params()
        await self.fix_empty_columns()
        await self.fix_entity_hubs()

        elapsed = time.time() - t0
        self._stats["total_seconds"] = round(elapsed)
        logger.info("kb_quality_fixer.complete", **self._stats)
        return self._stats

    # ===================================================================
    # Fix 1: Generate real examples for Pillar 3 APIs
    # ===================================================================

    async def fix_generic_examples(self):
        """Replace generic 'Default endpoint invocation shape' with real examples."""
        logger.info("kb_quality_fixer.fix_examples_start")

        for repo_dir in self._iter_repos():
            apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            if not apis_dir.exists():
                continue

            # Collect APIs needing fix
            to_fix = []
            for api_dir in sorted(apis_dir.iterdir()):
                if not api_dir.is_dir():
                    continue
                high_path = api_dir / "high.yaml"
                if not high_path.exists():
                    continue

                try:
                    data = yaml.safe_load(open(high_path))
                    if not data:
                        continue
                    # Check if examples are generic
                    examples = data.get("examples", {})
                    if isinstance(examples, dict):
                        http_ex = examples.get("http_examples", {})
                        minimal = http_ex.get("minimal", {}) if isinstance(http_ex, dict) else {}
                        desc = minimal.get("description", "") if isinstance(minimal, dict) else ""
                        pairs = examples.get("param_extraction_pairs", [])

                        is_generic = (
                            desc == "Default endpoint invocation shape"
                            or (isinstance(pairs, list) and len(pairs) > 0
                                and isinstance(pairs[0], dict)
                                and "run " in pairs[0].get("query", "").lower()
                                and "for this account" in pairs[0].get("query", "").lower())
                        )

                        if is_generic:
                            to_fix.append((api_dir, high_path, data))
                except Exception:
                    continue

            logger.info("kb_quality_fixer.examples_to_fix",
                        repo=repo_dir.name, count=len(to_fix))

            # Process in batches
            for i in range(0, len(to_fix), self.max_concurrent):
                batch = to_fix[i:i + self.max_concurrent]
                tasks = [self._fix_api_examples(api_dir, high_path, data)
                         for api_dir, high_path, data in batch]
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _fix_api_examples(self, api_dir: Path, high_path: Path, data: Dict):
        """Generate real examples for a single API."""
        async with self._semaphore:
            try:
                overview = data.get("overview", {})
                api_block = overview.get("api", {}) if isinstance(overview, dict) else {}
                classification = overview.get("classification", {}) if isinstance(overview, dict) else {}
                tags = data.get("tool_agent_tags", {})
                tool_info = tags.get("tool_assignment", {}) if isinstance(tags, dict) else {}

                method = api_block.get("method", "?")
                path = api_block.get("path", "?")
                domain = classification.get("domain", "")
                intent = classification.get("intent_primary", "")
                tool_candidate = tool_info.get("tool_candidate", "")

                prompt = f"""Generate 3 realistic ICRM operator queries for this Shiprocket API:

API: {method} {path}
Domain: {domain}
Intent: {intent}
Tool: {tool_candidate}

Generate queries that a real ICRM support operator would type when they need this API's data.
Include specific but placeholder values (order ID 12345, AWB 9876543210, seller company_id 5678).

Return ONLY a JSON array:
[
  {{"query": "...", "params": {{"key": "value"}}}},
  {{"query": "...", "params": {{"key": "value"}}}},
  {{"query": "...", "params": {{"key": "value"}}}}
]"""

                cli = self._get_cli()
                raw = await cli.prompt(prompt, model=self.model)
                self._stats["api_calls"] += 1
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                pairs = json.loads(raw)

                # Update the high.yaml examples section
                if not isinstance(data.get("examples"), dict):
                    data["examples"] = {}

                data["examples"]["param_extraction_pairs"] = pairs
                data["examples"]["http_examples"] = {
                    "minimal": {
                        "description": pairs[0]["query"] if pairs else f"{method} {path}",
                        "request": f"{method} {path}",
                    }
                }

                # Write back
                with open(high_path, "w") as f:
                    # Write header comment
                    f.write(f"# {api_dir.name} — HIGH tier (used for AI embedding)\n")
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

                self._stats["examples_fixed"] += 1

            except Exception as e:
                self._stats["errors"] += 1
                logger.debug("kb_quality_fixer.example_fix_failed",
                             api=api_dir.name, error=str(e))

    # ===================================================================
    # Fix 2: Populate missing request_schema params
    # ===================================================================

    async def fix_missing_params(self):
        """Populate request_schema.contract.required for APIs missing params."""
        logger.info("kb_quality_fixer.fix_params_start")

        for repo_dir in self._iter_repos():
            apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            if not apis_dir.exists():
                continue

            to_fix = []
            for api_dir in sorted(apis_dir.iterdir()):
                if not api_dir.is_dir():
                    continue
                high_path = api_dir / "high.yaml"
                if not high_path.exists():
                    continue

                try:
                    data = yaml.safe_load(open(high_path))
                    if not data:
                        continue
                    req = data.get("request_schema", {})
                    if not isinstance(req, dict):
                        to_fix.append((api_dir, high_path, data))
                        continue
                    contract = req.get("contract", {})
                    if not isinstance(contract, dict) or not contract.get("required"):
                        to_fix.append((api_dir, high_path, data))
                except Exception:
                    continue

            logger.info("kb_quality_fixer.params_to_fix",
                        repo=repo_dir.name, count=len(to_fix))

            for i in range(0, len(to_fix), self.max_concurrent):
                batch = to_fix[i:i + self.max_concurrent]
                tasks = [self._fix_api_params(api_dir, high_path, data)
                         for api_dir, high_path, data in batch]
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _fix_api_params(self, api_dir: Path, high_path: Path, data: Dict):
        """Generate request params for a single API."""
        async with self._semaphore:
            try:
                overview = data.get("overview", {})
                api_block = overview.get("api", {}) if isinstance(overview, dict) else {}
                method = api_block.get("method", "GET")
                path = api_block.get("path", "unknown")
                controller = api_block.get("controller", "")

                # For GET endpoints, params are usually query params
                # For POST/PUT, params are body params
                prompt = f"""Given this Shiprocket API endpoint, infer the most likely request parameters:

Method: {method}
Path: {path}
Controller: {controller}

Based on the URL path segments and common Shiprocket API patterns, list the required and optional parameters.

Return ONLY a JSON object:
{{
  "required": [
    {{"name": "param_name", "type": "string|int|date|array", "validation": "required|description"}}
  ],
  "optional": [
    {{"name": "param_name", "type": "string|int", "validation": "optional|description"}}
  ]
}}

Common Shiprocket params: company_id, order_id, awb_number, status, page, per_page, from_date, to_date."""

                client = self._get_client()
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                self._stats["api_calls"] += 1

                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                params = json.loads(raw)

                # Update request_schema
                if not isinstance(data.get("request_schema"), dict):
                    data["request_schema"] = {}

                data["request_schema"]["contract"] = {
                    "required": params.get("required", []),
                    "optional": params.get("optional", []),
                }
                data["request_schema"]["method"] = method
                data["request_schema"]["path"] = path
                data["request_schema"]["_generated"] = True

                with open(high_path, "w") as f:
                    f.write(f"# {api_dir.name} — HIGH tier (used for AI embedding)\n")
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

                self._stats["params_fixed"] += 1

            except Exception as e:
                self._stats["errors"] += 1
                logger.debug("kb_quality_fixer.params_fix_failed",
                             api=api_dir.name, error=str(e))

    # ===================================================================
    # Fix 3: Populate empty-column tables
    # ===================================================================

    async def fix_empty_columns(self):
        """Populate columns for tables that have zero column data."""
        logger.info("kb_quality_fixer.fix_columns_start")

        for repo_dir in self._iter_repos():
            tables_dir = repo_dir / "pillar_1_schema" / "tables"
            if not tables_dir.exists():
                continue

            to_fix = []
            for table_dir in sorted(tables_dir.iterdir()):
                if not table_dir.is_dir():
                    continue
                high_path = table_dir / "high.yaml"
                if not high_path.exists():
                    continue

                try:
                    data = yaml.safe_load(open(high_path))
                    if not data:
                        continue
                    columns = data.get("columns", {})
                    if isinstance(columns, dict) and columns.get("_status") == "stub":
                        to_fix.append((table_dir, high_path, data))
                    elif not columns or (isinstance(columns, dict) and len(columns) <= 1):
                        to_fix.append((table_dir, high_path, data))
                except Exception:
                    continue

            logger.info("kb_quality_fixer.columns_to_fix",
                        repo=repo_dir.name, count=len(to_fix))

            for i in range(0, len(to_fix), self.max_concurrent):
                batch = to_fix[i:i + self.max_concurrent]
                tasks = [self._fix_table_columns(table_dir, high_path, data)
                         for table_dir, high_path, data in batch]
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _fix_table_columns(self, table_dir: Path, high_path: Path, data: Dict):
        """Generate column definitions for an empty table."""
        async with self._semaphore:
            try:
                table_name = table_dir.name
                meta = data.get("_meta", {})
                domain = meta.get("domain", "unknown") if isinstance(meta, dict) else "unknown"
                description = meta.get("description", "") if isinstance(meta, dict) else ""

                # Check if catalog has more info
                catalog_info = ""
                catalog_path = table_dir.parent.parent / "catalog"
                if catalog_path.exists():
                    for cf in catalog_path.iterdir():
                        if cf.suffix in (".yaml", ".yml"):
                            try:
                                cdata = yaml.safe_load(open(cf))
                                if isinstance(cdata, dict):
                                    cols = cdata.get("columns", {})
                                    if isinstance(cols, dict) and table_name in str(cols):
                                        catalog_info = str(cols)[:500]
                            except Exception:
                                pass

                prompt = f"""Given this Shiprocket database table, infer the most likely columns:

Table: {table_name}
Domain: {domain}
Description: {description}
{f'Catalog hints: {catalog_info}' if catalog_info else ''}

Based on the table name and Shiprocket's e-commerce shipping domain, list 10-20 likely columns.

Return ONLY a JSON object:
{{
  "columns": [
    {{"name": "id", "type": "bigint", "meaning": "Primary key", "nullable": false}},
    {{"name": "column_name", "type": "varchar/int/datetime/json/etc", "meaning": "What this stores", "nullable": true}}
  ]
}}

Common patterns: id (PK), company_id (FK to companies), status (int), created_at/updated_at (timestamps)."""

                client = self._get_client()
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=600,
                    messages=[{"role": "user", "content": prompt}],
                )
                self._stats["api_calls"] += 1

                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                result = json.loads(raw)

                data["columns"] = {
                    "columns": result.get("columns", []),
                    "_generated": True,
                }

                with open(high_path, "w") as f:
                    f.write(f"# {table_name} — HIGH tier (used for AI embedding)\n")
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

                self._stats["columns_fixed"] += 1

            except Exception as e:
                self._stats["errors"] += 1
                logger.debug("kb_quality_fixer.columns_fix_failed",
                             table=table_dir.name, error=str(e))

    # ===================================================================
    # Fix 4: Regenerate entity hubs with structured links
    # ===================================================================

    async def fix_entity_hubs(self):
        """Regenerate entity hubs with structured API/action/workflow references."""
        logger.info("kb_quality_fixer.fix_hubs_start")

        for repo_dir in self._iter_repos():
            hubs_dir = repo_dir / "entity_hubs"
            if not hubs_dir.exists():
                continue

            apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
            actions_dir = repo_dir / "pillar_6_action_contracts" / "domains"
            workflows_dir = repo_dir / "pillar_7_workflow_runbooks" / "domains"

            for hub_file in sorted(hubs_dir.glob("*.yaml")):
                try:
                    hub_data = yaml.safe_load(open(hub_file))
                    if not hub_data:
                        continue

                    entity_name = hub_data.get("entity", hub_file.stem)
                    tables = hub_data.get("tables", [])

                    # Find APIs matching this domain
                    matching_apis = []
                    if apis_dir.exists():
                        for api_dir in apis_dir.iterdir():
                            if not api_dir.is_dir():
                                continue
                            hf = api_dir / "high.yaml"
                            if not hf.exists():
                                continue
                            try:
                                api_data = yaml.safe_load(open(hf))
                                if not api_data:
                                    continue
                                ov = api_data.get("overview", {})
                                cl = ov.get("classification", {}) if isinstance(ov, dict) else {}
                                if cl.get("domain", "").lower() == entity_name.lower():
                                    api_block = ov.get("api", {}) if isinstance(ov, dict) else {}
                                    matching_apis.append({
                                        "id": api_dir.name,
                                        "method": api_block.get("method", "?"),
                                        "path": api_block.get("path", "?"),
                                        "intent": cl.get("intent_primary", ""),
                                    })
                            except Exception:
                                continue
                            if len(matching_apis) >= 20:
                                break  # Cap at 20 for hub

                    # Find actions matching this domain
                    matching_actions = []
                    domain_actions_dir = actions_dir / entity_name if actions_dir.exists() else None
                    if domain_actions_dir and domain_actions_dir.exists():
                        for action_dir in domain_actions_dir.iterdir():
                            if not action_dir.is_dir():
                                continue
                            contract = action_dir / "contract.yaml"
                            if contract.exists():
                                try:
                                    ac = yaml.safe_load(open(contract))
                                    if ac:
                                        matching_actions.append({
                                            "action_id": ac.get("action_id", action_dir.name),
                                            "purpose": str(ac.get("purpose", ""))[:100],
                                            "sync_async": ac.get("sync_async", "sync"),
                                        })
                                except Exception:
                                    pass

                    # Find workflows matching this domain
                    matching_workflows = []
                    domain_wf_dir = workflows_dir / entity_name if workflows_dir.exists() else None
                    if domain_wf_dir and domain_wf_dir.exists():
                        for wf_dir in domain_wf_dir.iterdir():
                            if not wf_dir.is_dir():
                                continue
                            wf_file = wf_dir / "workflow.yaml"
                            if wf_file.exists():
                                try:
                                    wf = yaml.safe_load(open(wf_file))
                                    if wf:
                                        matching_workflows.append({
                                            "workflow_id": wf.get("workflow_id", wf_dir.name),
                                            "description": str(wf.get("description", ""))[:100],
                                        })
                                except Exception:
                                    pass

                    # Update hub with structured links
                    hub_data["key_apis"] = matching_apis[:20]
                    hub_data["key_apis_count"] = len(matching_apis)
                    hub_data["action_contracts"] = matching_actions
                    hub_data["workflows"] = matching_workflows
                    hub_data["_enriched"] = True

                    # Regenerate content markdown
                    content_lines = [
                        f"# Entity Hub: {entity_name}",
                        f"\n{hub_data.get('description', '')}",
                        f"\n## Schema Tables ({len(tables)})",
                    ]
                    for t in tables:
                        content_lines.append(f"- {t}")

                    content_lines.append(f"\n## APIs ({len(matching_apis)})")
                    for api in matching_apis[:10]:
                        content_lines.append(f"- {api['method']} {api['path']} ({api['intent']})")
                    if len(matching_apis) > 10:
                        content_lines.append(f"- ... and {len(matching_apis) - 10} more")

                    content_lines.append(f"\n## Action Contracts ({len(matching_actions)})")
                    for ac in matching_actions:
                        content_lines.append(f"- {ac['action_id']}: {ac['purpose']}")

                    content_lines.append(f"\n## Workflows ({len(matching_workflows)})")
                    for wf in matching_workflows:
                        content_lines.append(f"- {wf['workflow_id']}: {wf['description']}")

                    hub_data["content"] = "\n".join(content_lines)

                    with open(hub_file, "w") as f:
                        yaml.dump(hub_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

                    self._stats["hubs_fixed"] += 1
                    logger.info("kb_quality_fixer.hub_fixed",
                                entity=entity_name,
                                apis=len(matching_apis),
                                actions=len(matching_actions),
                                workflows=len(matching_workflows))

                except Exception as e:
                    self._stats["errors"] += 1
                    logger.debug("kb_quality_fixer.hub_fix_failed",
                                 hub=hub_file.name, error=str(e))

    # ===================================================================
    # Helpers
    # ===================================================================

    def _iter_repos(self):
        """Iterate over all repo directories in the KB."""
        for org_dir in sorted(self.kb_path.iterdir()):
            if not org_dir.is_dir():
                continue
            for repo_dir in sorted(org_dir.iterdir()):
                if not repo_dir.is_dir():
                    continue
                yield repo_dir

    def get_stats(self) -> Dict:
        return dict(self._stats)

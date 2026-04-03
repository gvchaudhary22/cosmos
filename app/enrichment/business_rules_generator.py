"""
Business Rules Generator (Pillar 2) — Extracts and centralizes business rules
from scattered KB sources into structured rule YAML files.

Business rules are constraints, limits, and policies that the LLM needs to know
but are currently scattered across API docs, action contracts, and code comments.

Examples:
  - COD limit: Rs 50,000 per order
  - Weight dispute threshold: auto-flag if difference > 500g
  - RTO charges apply after 7 days
  - Pickup rescheduling: max 3 attempts
  - Seller wallet minimum: Rs 100 to ship

This generator:
1. Reads existing KB docs (Pillar 3 APIs, Pillar 6 Actions, Pillar 1 Schema)
2. Uses Claude Opus to extract implicit business rules
3. Writes structured rule YAML files to pillar_2_business_rules/
4. These get ingested by the training pipeline alongside other pillars

Usage:
    generator = BusinessRulesGenerator(kb_path)
    stats = await generator.generate_all()
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import structlog
import yaml

logger = structlog.get_logger()

DOMAINS = [
    "orders", "shipments", "billing", "courier", "ndr",
    "settings", "returns", "pickup", "support", "catalog",
]

EXTRACT_RULES_PROMPT = """You are extracting business rules from Shiprocket's API and schema documentation.

Business rules are constraints, limits, thresholds, policies, and conditions that govern how the system works. These are NOT code patterns — they are business-level rules that an ICRM operator needs to know.

<documents>
{documents_text}
</documents>

<domain>{domain}</domain>

Extract ALL business rules you can find. For each rule, provide:
- name: short descriptive name
- rule: the actual rule statement (clear, specific, with numbers/values)
- source: which document/field you found it in
- type: one of: limit, threshold, policy, constraint, eligibility, sla, fee
- impact: what happens if the rule is violated or not met

Return ONLY a JSON array:
[
  {{
    "name": "cod_order_limit",
    "rule": "Maximum COD order value is Rs 50,000. Orders above this must be prepaid.",
    "source": "orders.payment_method validation",
    "type": "limit",
    "impact": "Order creation fails with error: COD limit exceeded"
  }}
]

If no rules are found, return an empty array: []"""


class BusinessRulesGenerator:
    """Generates Pillar 2 business rule YAML files from existing KB documents."""

    def __init__(self, kb_path: str, model: str = "claude-opus-4-6"):
        self.kb_path = Path(kb_path)
        self.model = model
        self._cli = None

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=120)
        return self._cli

    async def generate_all(self) -> Dict[str, Any]:
        """Generate business rules for all domains."""
        stats = {"domains_processed": 0, "rules_extracted": 0, "files_written": 0}

        for repo_dir in sorted(self.kb_path.iterdir()):
            if not repo_dir.is_dir():
                continue
            for project_dir in sorted(repo_dir.iterdir()):
                if not project_dir.is_dir():
                    continue

                # Create pillar_2 directory
                pillar2_dir = project_dir / "pillar_2_business_rules"
                pillar2_dir.mkdir(exist_ok=True)

                for domain in DOMAINS:
                    domain_docs = self._collect_domain_docs(project_dir, domain)
                    if not domain_docs:
                        continue

                    rules = await self._extract_rules(domain, domain_docs)
                    if rules:
                        output_file = pillar2_dir / f"{domain}_rules.yaml"
                        self._write_rules_yaml(output_file, domain, rules)
                        stats["rules_extracted"] += len(rules)
                        stats["files_written"] += 1

                    stats["domains_processed"] += 1

        logger.info("business_rules.generation_complete", **stats)
        return stats

    def _collect_domain_docs(self, project_dir: Path, domain: str) -> str:
        """Collect relevant docs for a domain from Pillar 1, 3, and 6."""
        texts = []

        # Pillar 1: Schema docs mentioning this domain
        schema_dir = project_dir / "pillar_1_schema" / "tables"
        if schema_dir.exists():
            for table_dir in schema_dir.iterdir():
                if not table_dir.is_dir():
                    continue
                high = table_dir / "high.yaml"
                if high.exists():
                    try:
                        data = yaml.safe_load(open(high))
                        meta = data.get("_meta", {})
                        if domain in meta.get("domain", "").lower() or domain in meta.get("table", "").lower():
                            texts.append(f"[Schema: {meta.get('table', '')}]\n{open(high).read()[:2000]}")
                    except Exception:
                        pass

        # Pillar 6: Action contracts for this domain
        actions_dir = project_dir / "pillar_6_action_contracts" / "domains" / domain
        if actions_dir.exists():
            for action_dir in actions_dir.iterdir():
                if not action_dir.is_dir():
                    continue
                contract = action_dir / "contract.yaml"
                if contract.exists():
                    texts.append(f"[Action: {action_dir.name}]\n{open(contract).read()[:2000]}")

        # Pillar 3: Sample API docs for this domain (first 5)
        apis_dir = project_dir / "pillar_3_api_mcp_tools" / "apis"
        if apis_dir.exists():
            count = 0
            for api_dir in apis_dir.iterdir():
                if count >= 5:
                    break
                if not api_dir.is_dir():
                    continue
                high = api_dir / "high.yaml"
                if high.exists():
                    try:
                        data = yaml.safe_load(open(high))
                        classification = data.get("overview", {}).get("classification", {})
                        if domain in classification.get("domain", "").lower():
                            texts.append(f"[API: {api_dir.name}]\n{open(high).read()[:1500]}")
                            count += 1
                    except Exception:
                        pass

        return "\n\n---\n\n".join(texts) if texts else ""

    async def _extract_rules(self, domain: str, docs_text: str) -> List[Dict]:
        """Use Claude Opus to extract business rules from domain docs."""
        try:
            cli = self._get_cli()
            raw = await cli.prompt(
                EXTRACT_RULES_PROMPT.format(
                    documents_text=docs_text[:6000],
                    domain=domain,
                ),
                model=self.model,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            logger.warning("business_rules.extraction_failed", domain=domain, error=str(e))
            return []

    def _write_rules_yaml(self, path: Path, domain: str, rules: List[Dict]):
        """Write extracted rules to a YAML file."""
        output = {
            "domain": domain,
            "pillar": "pillar_2_business_rules",
            "rules_count": len(rules),
            "rules": rules,
        }
        with open(path, "w") as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info("business_rules.file_written", path=str(path), rules=len(rules))

"""
Negative Examples Generator (Pillar 8) — Generates domain-specific anti-patterns
from action contracts and business rules.

Negative examples teach COSMOS what NOT to do. Each negative defines:
- A user query that sounds reasonable
- The WRONG action the agent might take
- The CORRECT action to take instead
- Why the wrong action is dangerous

Sources for generating negatives:
1. Action contract preconditions → what happens if preconditions are violated
2. Business rules → what happens if limits/thresholds are ignored
3. Risk levels → high-risk tools need explicit warnings

Usage:
    generator = NegativeExamplesGenerator(kb_path)
    stats = await generator.generate_all()
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import structlog
import yaml

logger = structlog.get_logger()

DOMAINS = [
    "orders", "shipments", "billing", "courier", "ndr",
    "settings", "returns", "pickup", "support",
]

GENERATE_NEGATIVES_PROMPT = """You are generating NEGATIVE EXAMPLES for COSMOS, Shiprocket's ICRM AI assistant. Negative examples teach the AI what NOT to do.

<action_contracts>
{action_contracts_text}
</action_contracts>

<business_rules>
{business_rules_text}
</business_rules>

<domain>{domain}</domain>

Generate 8-10 negative examples for this domain. For each, provide:
- query: A realistic operator query that could lead to a wrong action
- should_not: The wrong/dangerous action the AI might take
- correct_action: What the AI SHOULD do instead
- risk: why the wrong action is dangerous (financial impact, data loss, etc.)
- category: one of: precondition_violation, authorization_bypass, data_mutation_risk, limit_exceeded, process_order_violation

Focus on REAL dangers in e-commerce shipping:
- Cancelling shipped orders (refund fraud)
- Modifying COD amounts (financial loss)
- Approving refunds without verification
- Updating shipment status backwards (data integrity)
- Bypassing weight dispute process
- Issuing wallet credits without authorization

Return ONLY a JSON array:
[
  {{
    "query": "Cancel order 12345 and process immediate refund",
    "should_not": "Execute cancel + refund without checking shipment status",
    "correct_action": "First check if order is shipped. If shipped, inform operator that cancellation requires RTO process. If not shipped, cancel order but refund requires separate approval.",
    "risk": "Financial loss — refund issued but product already delivered",
    "category": "precondition_violation"
  }}
]"""


class NegativeExamplesGenerator:
    """Generates Pillar 8 negative example YAML files from action contracts and rules."""

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
        """Generate negative examples for all domains."""
        stats = {"domains_processed": 0, "negatives_generated": 0, "files_written": 0}

        for repo_dir in sorted(self.kb_path.iterdir()):
            if not repo_dir.is_dir():
                continue
            for project_dir in sorted(repo_dir.iterdir()):
                if not project_dir.is_dir():
                    continue

                pillar8_dir = project_dir / "pillar_8_negative_routing"
                pillar8_dir.mkdir(exist_ok=True)

                for domain in DOMAINS:
                    contracts_text = self._collect_action_contracts(project_dir, domain)
                    rules_text = self._collect_business_rules(project_dir, domain)

                    if not contracts_text and not rules_text:
                        continue

                    negatives = await self._generate_negatives(domain, contracts_text, rules_text)
                    if negatives:
                        output_file = pillar8_dir / f"{domain}_negatives.yaml"
                        self._write_negatives_yaml(output_file, domain, negatives)
                        stats["negatives_generated"] += len(negatives)
                        stats["files_written"] += 1

                    stats["domains_processed"] += 1

        logger.info("negative_examples.generation_complete", **stats)
        return stats

    def _collect_action_contracts(self, project_dir: Path, domain: str) -> str:
        """Collect action contracts for a domain."""
        texts = []
        actions_dir = project_dir / "pillar_6_action_contracts" / "domains" / domain
        if actions_dir.exists():
            for action_dir in actions_dir.iterdir():
                if not action_dir.is_dir():
                    continue
                contract = action_dir / "contract.yaml"
                if contract.exists():
                    texts.append(open(contract).read()[:2000])
        return "\n---\n".join(texts) if texts else ""

    def _collect_business_rules(self, project_dir: Path, domain: str) -> str:
        """Collect business rules for a domain (if Pillar 2 exists)."""
        rules_file = project_dir / "pillar_2_business_rules" / f"{domain}_rules.yaml"
        if rules_file.exists():
            return open(rules_file).read()[:2000]
        return ""

    async def _generate_negatives(self, domain: str, contracts: str, rules: str) -> List[Dict]:
        """Use Claude Opus to generate negative examples."""
        try:
            cli = self._get_cli()
            raw = await cli.prompt(
                GENERATE_NEGATIVES_PROMPT.format(
                    action_contracts_text=contracts or "No action contracts available for this domain.",
                    business_rules_text=rules or "No business rules available for this domain.",
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
            logger.warning("negative_examples.generation_failed", domain=domain, error=str(e))
            return []

    def _write_negatives_yaml(self, path: Path, domain: str, negatives: List[Dict]):
        """Write negative examples to YAML file."""
        output = {
            "domain": domain,
            "pillar": "pillar_8_negative_routing",
            "negatives_count": len(negatives),
            "negatives": negatives,
        }
        with open(path, "w") as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info("negative_examples.file_written", path=str(path), negatives=len(negatives))

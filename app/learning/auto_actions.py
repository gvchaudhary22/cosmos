"""
Auto-Action Generator — Goal 5: Continuous Learning.

Analyzes eval failures and low-confidence traces to propose:
  1. missing_action_candidate — query matched no action in KB → propose new P6 action
  2. add_negative_example    — query confused two domains → add P8 disambiguation
  3. add_clarification_rule  — query too ambiguous → add clarification trigger

Uses Claude Opus 4.6 with adaptive thinking for high-quality proposals.
Max 10 proposals per run to control cost.
"""

import json
import re
from dataclasses import dataclass, field

import anthropic
import structlog

logger = structlog.get_logger(__name__)

MAX_PROPOSALS_PER_RUN = 10
MODEL = "claude-opus-4-6"


@dataclass
class AutoActionProposal:
    action_type: str  # missing_action_candidate | add_negative_example | add_clarification_rule
    source_query: str
    source_confidence: float
    source_domain: str
    proposed_entity_id: str
    proposed_pillar: str
    proposed_content: dict  # JSON-serializable
    rationale: str
    eval_domain: str = ""
    eval_recall_before: float = 0.0


class AutoActionGenerator:
    """Generate auto-action proposals from eval failures and low-confidence traces."""

    def __init__(self, anthropic_api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)

    async def generate_from_eval(
        self,
        eval_report,  # EvalReport from kb_eval.py
        max_proposals: int = MAX_PROPOSALS_PER_RUN,
    ) -> list:
        """
        Analyze weak domains from an EvalReport and propose KB improvements.
        For each weak domain (recall@5 < 50%), ask Opus 4.6 to propose:
        - What action is missing (missing_action_candidate)
        - What disambiguation is needed (add_negative_example)
        """
        proposals = []

        # Only process weak domains (recall@5 < 50%, at least 5 seeds)
        weak = eval_report.weak_domains[:5]  # cap at 5 domains

        for domain in weak:
            if len(proposals) >= max_proposals:
                break

            ds = eval_report.by_domain.get(domain)
            if not ds:
                continue

            recall5 = ds.recall_at_5 / max(ds.total, 1)

            # Get sample failing queries for this domain
            failing_queries = [
                r.query for r in eval_report.results
                if not r.recall_at_5 and domain in r.expected_tool
            ][:5]

            if not failing_queries:
                continue

            new_proposals = await self._propose_for_domain(
                domain=domain,
                recall5=recall5,
                failing_queries=failing_queries,
                total_seeds=ds.total,
            )
            proposals.extend(new_proposals)

        return proposals[:max_proposals]

    async def generate_from_traces(
        self,
        low_conf_traces: list,  # list of {query, confidence, intent, entity, tools_used}
        max_proposals: int = MAX_PROPOSALS_PER_RUN,
    ) -> list:
        """
        Analyze low-confidence traces (confidence < 0.4) and propose KB improvements.
        Groups traces by entity type, then asks Opus to propose clarification rules
        for systematically ambiguous queries.
        """
        if not low_conf_traces:
            return []

        # Group by entity
        by_entity: dict = {}
        for t in low_conf_traces:
            entity = t.get("entity", "unknown")
            by_entity.setdefault(entity, []).append(t)

        proposals = []
        for entity, traces in list(by_entity.items())[:3]:  # max 3 entity groups
            if len(proposals) >= max_proposals:
                break

            queries = [t["query"] for t in traces[:5]]
            avg_conf = sum(t.get("confidence", 0) for t in traces) / len(traces)

            new_props = await self._propose_clarification(
                entity=entity,
                queries=queries,
                avg_confidence=avg_conf,
            )
            proposals.extend(new_props)

        return proposals[:max_proposals]

    async def _propose_for_domain(
        self,
        domain: str,
        recall5: float,
        failing_queries: list,
        total_seeds: int,
    ) -> list:
        """Ask Opus 4.6 to propose KB improvements for a weak domain."""

        queries_text = "\n".join(f"  - {q}" for q in failing_queries)

        prompt = f"""You are improving a knowledge base for a logistics/e-commerce ICRM system (Shiprocket).

Domain: {domain}
Current recall@5: {recall5:.0%} (out of {total_seeds} eval queries)
Sample failing queries (not being retrieved correctly):
{queries_text}

Analyze why these queries might be failing to retrieve the correct KB documents, and propose specific KB improvements.

Respond with JSON (array of up to 2 proposals):
{{
  "proposals": [
    {{
      "type": "missing_action_candidate",
      "entity_id": "suggested_doc_id (e.g., {domain}_cancel_action)",
      "pillar": "pillar_6",
      "content": {{
        "title": "...",
        "description": "...",
        "example_queries": ["...", "..."],
        "key_terms": ["...", "..."],
        "what_it_handles": "..."
      }},
      "rationale": "Why this KB document would fix the recall gap"
    }}
  ]
}}

Valid types: "missing_action_candidate" or "add_negative_example"
Valid pillars: "pillar_6", "pillar_8", "pillar_3"
Return only valid JSON with no extra commentary."""

        try:
            response = await self.client.messages.create(
                model=MODEL,
                max_tokens=2000,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract text from response (skip thinking blocks)
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text
                    break

            # Parse JSON
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not json_match:
                return []

            data = json.loads(json_match.group())
            proposals = []

            for p in data.get("proposals", [])[:2]:
                proposals.append(AutoActionProposal(
                    action_type=p.get("type", "missing_action_candidate"),
                    source_query=failing_queries[0] if failing_queries else "",
                    source_confidence=recall5,
                    source_domain=domain,
                    proposed_entity_id=p.get("entity_id", f"{domain}_improvement"),
                    proposed_pillar=p.get("pillar", "pillar_6"),
                    proposed_content=p.get("content", {}),
                    rationale=p.get("rationale", ""),
                    eval_domain=domain,
                    eval_recall_before=recall5,
                ))

            return proposals

        except Exception as e:
            logger.warning("auto_action.propose_failed", domain=domain, error=str(e))
            return []

    async def _propose_clarification(
        self,
        entity: str,
        queries: list,
        avg_confidence: float,
    ) -> list:
        """Ask Opus 4.6 to propose clarification rules for ambiguous queries."""

        queries_text = "\n".join(f"  - {q}" for q in queries)

        prompt = f"""You are improving a logistics ICRM knowledge base (Shiprocket).

Entity type: {entity}
Average confidence: {avg_confidence:.0%} (low = ambiguous)
Sample low-confidence queries:
{queries_text}

These queries are being answered with low confidence. Propose a clarification rule that the system should ask users when it receives such ambiguous queries.

Respond with JSON:
{{
  "entity_id": "clarification_{entity}_rule_1",
  "trigger_patterns": ["pattern1", "pattern2"],
  "clarification_question": "What should the system ask?",
  "options": ["Option A: ...", "Option B: ..."],
  "rationale": "Why this clarification helps"
}}

Return only valid JSON with no extra commentary."""

        try:
            response = await self.client.messages.create(
                model=MODEL,
                max_tokens=1000,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )

            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text
                    break

            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not json_match:
                return []

            data = json.loads(json_match.group())

            return [AutoActionProposal(
                action_type="add_clarification_rule",
                source_query=queries[0] if queries else "",
                source_confidence=avg_confidence,
                source_domain=entity,
                proposed_entity_id=data.get("entity_id", f"clarification_{entity}"),
                proposed_pillar="pillar_8",
                proposed_content=data,
                rationale=data.get("rationale", ""),
            )]

        except Exception as e:
            logger.warning("auto_action.clarification_failed", entity=entity, error=str(e))
            return []

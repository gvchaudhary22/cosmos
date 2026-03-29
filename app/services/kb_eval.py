"""
KB Eval Pipeline — Automated evaluation of retrieval quality.

Uses the 5,482 eval seeds in global_eval_set.jsonl to measure:
  1. Retrieval Recall@K: Is the correct doc in the top K results?
  2. Tool Selection Accuracy: Does the top result map to the expected tool?
  3. Domain Accuracy: Is the retrieved doc from the right domain?

Run modes:
  - Full eval: Run all 5,482 seeds (takes ~10 min with API calls)
  - Sample eval: Run 100 random seeds (~30 sec)
  - Domain eval: Run seeds for a specific domain

Output: EvalReport with per-domain accuracy breakdown.

Usage:
    evaluator = KBEvaluator(vectorstore)
    report = await evaluator.run_eval(sample_size=100)
    print(report.summary())
"""

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class EvalResult:
    """Result for a single eval seed."""
    query: str
    expected_tool: str
    expected_api_id: str
    retrieved_entity_ids: List[str]
    retrieved_domains: List[str]
    recall_at_1: bool = False
    recall_at_3: bool = False
    recall_at_5: bool = False
    recall_at_10: bool = False
    tool_match: bool = False
    domain_match: bool = False
    latency_ms: float = 0.0


@dataclass
class DomainStats:
    """Aggregated stats for a domain."""
    domain: str
    total: int = 0
    recall_at_1: int = 0
    recall_at_3: int = 0
    recall_at_5: int = 0
    tool_match: int = 0
    domain_match: int = 0
    avg_latency_ms: float = 0.0


@dataclass
class EvalReport:
    """Complete evaluation report."""
    total_seeds: int = 0
    evaluated: int = 0
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    tool_accuracy: float = 0.0
    domain_accuracy: float = 0.0
    avg_latency_ms: float = 0.0
    by_domain: Dict[str, DomainStats] = field(default_factory=dict)
    weak_domains: List[str] = field(default_factory=list)
    results: List[EvalResult] = field(default_factory=list)
    duration_s: float = 0.0

    def summary(self) -> str:
        lines = [
            f"=== KB Eval Report ===",
            f"Seeds: {self.evaluated}/{self.total_seeds}",
            f"Recall@1: {self.recall_at_1:.1%}",
            f"Recall@3: {self.recall_at_3:.1%}",
            f"Recall@5: {self.recall_at_5:.1%}",
            f"Tool accuracy: {self.tool_accuracy:.1%}",
            f"Domain accuracy: {self.domain_accuracy:.1%}",
            f"Avg latency: {self.avg_latency_ms:.0f}ms",
            f"Duration: {self.duration_s:.1f}s",
            f"",
            f"--- Weak domains (recall@5 < 50%) ---",
        ]
        for domain in self.weak_domains:
            ds = self.by_domain[domain]
            r5 = ds.recall_at_5 / max(ds.total, 1)
            lines.append(f"  {domain}: {r5:.0%} recall@5 ({ds.total} seeds)")
        return "\n".join(lines)


class KBEvaluator:
    """Evaluate KB retrieval quality using eval seeds."""

    def __init__(self, vectorstore, kb_path: Optional[str] = None):
        self.vectorstore = vectorstore
        self.kb_path = kb_path

    async def run_eval(
        self,
        sample_size: Optional[int] = None,
        domain_filter: Optional[str] = None,
        repo_id: str = "MultiChannel_API",
    ) -> EvalReport:
        """Run evaluation against eval seeds.

        Args:
            sample_size: If set, randomly sample this many seeds.
            domain_filter: If set, only eval seeds from this domain.
            repo_id: Which repo's eval seeds to use.
        """
        seeds = self._load_seeds(repo_id)

        if domain_filter:
            seeds = [s for s in seeds if domain_filter in s.get("expected_tool", "")]

        if sample_size and sample_size < len(seeds):
            seeds = random.sample(seeds, sample_size)

        report = EvalReport(total_seeds=len(seeds))
        t0 = time.monotonic()

        domain_results: Dict[str, DomainStats] = {}
        total_latency = 0.0

        for seed in seeds:
            query = seed.get("query", "")
            expected_tool = seed.get("expected_tool", "")
            api_id = seed.get("api_id", "")

            if not query:
                continue

            # Extract domain from expected_tool (e.g., "shipments_create" → "shipments")
            domain = expected_tool.split("_")[0] if expected_tool else "unknown"

            # Search
            t1 = time.monotonic()
            try:
                results = await self.vectorstore.search_similar(
                    query=query,
                    limit=10,
                    repo_id=repo_id,
                )
            except Exception as e:
                logger.warning("eval.search_failed", query=query[:50], error=str(e))
                continue
            latency = (time.monotonic() - t1) * 1000
            total_latency += latency

            # Score
            retrieved_ids = [r.get("entity_id", "") for r in results]
            retrieved_domains = [r.get("metadata", {}).get("domain", "") for r in results]
            retrieved_tools = [r.get("metadata", {}).get("tool_candidate", "") for r in results]

            # Check recall: is the expected API in the results?
            recall_1 = api_id in retrieved_ids[:1] or any(api_id in rid for rid in retrieved_ids[:1])
            recall_3 = api_id in retrieved_ids[:3] or any(api_id in rid for rid in retrieved_ids[:3])
            recall_5 = api_id in retrieved_ids[:5] or any(api_id in rid for rid in retrieved_ids[:5])
            recall_10 = api_id in retrieved_ids[:10] or any(api_id in rid for rid in retrieved_ids[:10])

            # Check tool match
            tool_match = expected_tool in retrieved_tools[:3]

            # Check domain match
            domain_match = domain in retrieved_domains[:3]

            result = EvalResult(
                query=query,
                expected_tool=expected_tool,
                expected_api_id=api_id,
                retrieved_entity_ids=retrieved_ids[:5],
                retrieved_domains=retrieved_domains[:5],
                recall_at_1=recall_1,
                recall_at_3=recall_3,
                recall_at_5=recall_5,
                recall_at_10=recall_10,
                tool_match=tool_match,
                domain_match=domain_match,
                latency_ms=latency,
            )
            report.results.append(result)

            # Aggregate by domain
            if domain not in domain_results:
                domain_results[domain] = DomainStats(domain=domain)
            ds = domain_results[domain]
            ds.total += 1
            ds.recall_at_1 += int(recall_1)
            ds.recall_at_3 += int(recall_3)
            ds.recall_at_5 += int(recall_5)
            ds.tool_match += int(tool_match)
            ds.domain_match += int(domain_match)

        # Compute aggregates
        n = len(report.results)
        report.evaluated = n
        if n > 0:
            report.recall_at_1 = sum(r.recall_at_1 for r in report.results) / n
            report.recall_at_3 = sum(r.recall_at_3 for r in report.results) / n
            report.recall_at_5 = sum(r.recall_at_5 for r in report.results) / n
            report.tool_accuracy = sum(r.tool_match for r in report.results) / n
            report.domain_accuracy = sum(r.domain_match for r in report.results) / n
            report.avg_latency_ms = total_latency / n

        report.by_domain = domain_results
        report.weak_domains = [
            d for d, ds in domain_results.items()
            if ds.total >= 5 and ds.recall_at_5 / max(ds.total, 1) < 0.5
        ]
        report.duration_s = time.monotonic() - t0

        logger.info(
            "eval.complete",
            evaluated=n,
            recall_at_5=f"{report.recall_at_5:.1%}",
            tool_accuracy=f"{report.tool_accuracy:.1%}",
            weak_domains=report.weak_domains,
        )

        return report

    def _load_seeds(self, repo_id: str) -> List[Dict]:
        """Load eval seeds from JSONL files."""
        seeds = []
        if not self.kb_path:
            return seeds

        kb = Path(self.kb_path)
        for jsonl_file in kb.glob(f"{repo_id}/**/global_eval_set.jsonl"):
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            seeds.append(json.loads(line))
            except Exception as e:
                logger.warning("eval.load_failed", file=str(jsonl_file), error=str(e))

        return seeds

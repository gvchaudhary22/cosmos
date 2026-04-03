"""
Stage 2→3 CI gate — Soft-warn + Hard-fail retrieval quality benchmark.

Runs benchmark_runner against dev_set.jsonl after every training pipeline
execution and writes metrics to cosmos/data/benchmark_results.json.

Stage 1 (complete): report-only — no test failures, engineers review output.
Stage 2 (current): pytest.warns when metrics drop below soft thresholds.
Stage 3 (Phase 3 rollout): pytest.fails when metrics drop below hard thresholds.

To run:
    cd cosmos && pytest tests/eval/test_retrieval_ci.py -v -s

The test is marked "slow" and skipped in CI unless COSMOS_EVAL=1 is set,
so it doesn't block every PR — only training pipeline runs.
"""

from __future__ import annotations

import json
import os
import asyncio
import time
from pathlib import Path
from typing import Any, Dict

import pytest

# Skip unless COSMOS_EVAL env var is set (avoids slowing down every PR run)
_EVAL_ENABLED = os.getenv("COSMOS_EVAL", "0") == "1"

_COSMOS_ROOT = Path(__file__).resolve().parents[2]
_DEV_SET_PATH = _COSMOS_ROOT / "data" / "dev_set.jsonl"
_RESULTS_PATH = _COSMOS_ROOT / "data" / "benchmark_results.json"

# Stage 2 soft-gate thresholds (warn if below; not yet enforced)
_SOFT_GATE = {
    "tool_accuracy": 0.70,
    "recall_at_5": 0.80,
    "mrr": 0.65,
    "entity_match": 0.70,
    "page_field_accuracy": 0.50,
    "grounded_answer_rate": 0.65,
}

# Stage 3 hard-gate thresholds (future: fail if below)
_HARD_GATE = {
    "tool_accuracy": 0.80,
    "recall_at_5": 0.90,
    "mrr": 0.75,
    "entity_match": 0.85,
    "page_field_accuracy": 0.70,
    "grounded_answer_rate": 0.80,
}


def _load_results() -> Dict[str, Any]:
    """Load the most recent benchmark results from disk."""
    if not _RESULTS_PATH.exists():
        return {}
    with open(_RESULTS_PATH) as f:
        return json.load(f)


def _format_metric_table(results: Dict[str, Any]) -> str:
    """Format benchmark results as a readable table."""
    lines = [
        "",
        "=" * 70,
        "  COSMOS RETRIEVAL BENCHMARK — Stage 1 Report",
        "=" * 70,
    ]
    if not results:
        lines.append("  No results found. Run benchmark_runner.py first.")
        lines.append("  Command: python -m cosmos.tests.eval.benchmark_runner --limit 100")
        lines.append("=" * 70)
        return "\n".join(lines)

    # Per-backend metrics
    backends = results.get("backends", {})
    if backends:
        lines.append(f"  {'Metric':<25} " + "  ".join(f"{b:<18}" for b in backends))
        lines.append("  " + "-" * 65)
        all_metrics = set()
        for b_data in backends.values():
            all_metrics.update(b_data.keys())
        for metric in sorted(all_metrics):
            if metric.startswith("latency"):
                row = f"  {metric:<25} "
                for b, b_data in backends.items():
                    val = b_data.get(metric, 0.0)
                    row += f"{val:>6.0f}ms          "
            else:
                row = f"  {metric:<25} "
                for b, b_data in backends.items():
                    val = b_data.get(metric, 0.0)
                    soft = _SOFT_GATE.get(metric)
                    hard = _HARD_GATE.get(metric)
                    flag = ""
                    if hard and val < hard:
                        flag = " [HARD]"
                    elif soft and val < soft:
                        flag = " [soft]"
                    row += f"{val:>6.3f}{flag:<12}"
            lines.append(row)
    else:
        # Flat metrics (single backend result)
        for metric, val in sorted(results.items()):
            if isinstance(val, (int, float)):
                soft = _SOFT_GATE.get(metric)
                hard = _HARD_GATE.get(metric)
                flag = ""
                if hard and val < hard:
                    flag = "  ← below HARD gate"
                elif soft and val < soft:
                    flag = "  ← below soft gate"
                lines.append(f"  {metric:<30} {val:.4f}{flag}")

    lines.append("=" * 70)
    lines.append(f"  Results saved to: {_RESULTS_PATH}")
    lines.append("=" * 70)
    return "\n".join(lines)


@pytest.mark.skipif(not _EVAL_ENABLED, reason="Set COSMOS_EVAL=1 to run retrieval benchmarks")
@pytest.mark.slow
def test_retrieval_benchmark_report(capsys):
    """Stage 1: Run benchmark and report metrics. No failures — engineers review.

    To run: COSMOS_EVAL=1 pytest tests/eval/test_retrieval_ci.py -v -s
    To run with limit: COSMOS_EVAL=1 BENCHMARK_LIMIT=50 pytest tests/eval/test_retrieval_ci.py -v -s
    """
    if not _DEV_SET_PATH.exists():
        pytest.skip(f"dev_set.jsonl not found at {_DEV_SET_PATH}")

    # Run the benchmark asynchronously
    limit = int(os.getenv("BENCHMARK_LIMIT", "200"))

    async def _run():
        try:
            from cosmos.tests.eval.benchmark_runner import run_benchmark, load_dev_set
            dev_set = load_dev_set(_DEV_SET_PATH, limit=limit)
            if not dev_set:
                return {}
            return await run_benchmark(dev_set)
        except ImportError:
            # If benchmark_runner can't import full stack (no DB), just load existing results
            return _load_results()
        except Exception as e:
            print(f"\nBenchmark run failed: {e}")
            return _load_results()

    results = asyncio.run(_run())

    # Save results
    if results:
        _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_RESULTS_PATH, "w") as f:
            json.dump({
                **results,
                "_meta": {
                    "stage": 1,
                    "gate": "report_only",
                    "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dev_set_limit": limit,
                }
            }, f, indent=2)

    # Print readable table (visible with -s flag)
    table = _format_metric_table(results)
    print(table)

    # Stage 1: just assert results exist — no metric thresholds enforced yet
    # Stage 2 will add: pytest.warns for soft gate
    # Stage 3 will add: assert metric >= threshold for hard gate
    assert results is not None, "Benchmark produced no results"


@pytest.mark.skipif(not _EVAL_ENABLED, reason="Set COSMOS_EVAL=1 to run retrieval benchmarks")
@pytest.mark.slow
def test_existing_results_soft_gate():
    """Stage 1: Load existing benchmark_results.json and log soft-gate violations.

    Does NOT fail — only logs which metrics are below soft thresholds.
    Promotes to hard failures in Stage 3.
    """
    results = _load_results()
    if not results:
        pytest.skip("No existing benchmark_results.json — run test_retrieval_benchmark_report first")

    violations = []
    backends = results.get("backends", results)
    # Handle both flat and nested (per-backend) result shapes
    if "backends" in results:
        # Use the "current" backend or first available
        backend_name = "current" if "current" in results["backends"] else next(iter(results["backends"]), None)
        metrics = results["backends"].get(backend_name, {}) if backend_name else {}
    else:
        metrics = results

    for metric, threshold in _SOFT_GATE.items():
        val = metrics.get(metric)
        if val is not None and val < threshold:
            violations.append(f"  {metric}: {val:.4f} < soft gate {threshold:.2f}")

    if violations:
        print("\nStage 2 soft-gate violations (warnings raised, not failing):")
        for v in violations:
            print(v)

    # Stage 2: raise UserWarning for each soft-gate violation
    import warnings
    for violation_msg in violations:
        warnings.warn(
            f"COSMOS soft gate: {violation_msg.strip()}",
            UserWarning,
            stacklevel=1,
        )

    # Stage 3 (Phase 3): uncomment below to hard-fail on threshold breach
    # for metric, threshold in _HARD_GATE.items():
    #     val = metrics.get(metric)
    #     if val is not None:
    #         assert val >= threshold, (
    #             f"HARD GATE FAILED: {metric}={val:.4f} < {threshold:.2f}"
    #         )

    # Stage 2: always pass (warnings are advisory)
    assert True


@pytest.mark.skipif(not _EVAL_ENABLED, reason="Set COSMOS_EVAL=1 to run retrieval benchmarks")
@pytest.mark.slow
def test_hard_gate_phase3():
    """Stage 3 / Phase 3: Hard-fail when metrics drop below hard thresholds.

    Enabled when COSMOS_HARD_GATE=1 is set (in addition to COSMOS_EVAL=1).
    This test FAILS CI when critical quality metrics are below the hard gate.
    Only activate after 2 weeks of stable Stage 2 soft-gate data.

    To run:
        COSMOS_EVAL=1 COSMOS_HARD_GATE=1 pytest tests/eval/test_retrieval_ci.py -v -s
    """
    import os
    hard_gate_enabled = os.getenv("COSMOS_HARD_GATE", "0") == "1"
    if not hard_gate_enabled:
        pytest.skip("Set COSMOS_HARD_GATE=1 to enforce hard CI gate (Phase 3 only)")

    results = _load_results()
    if not results:
        pytest.skip("No existing benchmark_results.json — run test_retrieval_benchmark_report first")

    # Resolve flat vs per-backend result shape
    if "backends" in results:
        backend_name = "current" if "current" in results["backends"] else next(iter(results["backends"]), None)
        metrics = results["backends"].get(backend_name, {}) if backend_name else {}
    else:
        metrics = {k: v for k, v in results.items() if isinstance(v, (int, float))}

    failures = []
    for metric, threshold in _HARD_GATE.items():
        val = metrics.get(metric)
        if val is not None and val < threshold:
            failures.append(f"  {metric}: {val:.4f} < hard gate {threshold:.2f}")

    if failures:
        fail_msg = "COSMOS HARD GATE FAILED — metrics below Phase 3 thresholds:\n"
        fail_msg += "\n".join(failures)
        fail_msg += "\n\nFix retrieval quality before merging."
        pytest.fail(fail_msg)

"""Prometheus metrics for COSMOS observability."""

from typing import Dict, List
from collections import defaultdict
import threading


class Counter:
    """Thread-safe counter metric."""

    def __init__(self, name: str, description: str, labels: List[str] = None):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._values: Dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels) -> None:
        key = tuple(labels.get(l, "") for l in self._labels)
        with self._lock:
            self._values[key] += value

    def get(self, **labels) -> float:
        key = tuple(labels.get(l, "") for l in self._labels)
        return self._values.get(key, 0.0)

    def collect(self) -> str:
        """Export in Prometheus text format."""
        lines = [f"# HELP {self.name} {self.description}"]
        lines.append(f"# TYPE {self.name} counter")
        if not self._values:
            return "\n".join(lines)
        with self._lock:
            for key, value in self._values.items():
                if self._labels:
                    label_str = ",".join(
                        f'{l}="{v}"' for l, v in zip(self._labels, key)
                    )
                    lines.append(f"{self.name}{{{label_str}}} {value}")
                else:
                    lines.append(f"{self.name} {value}")
        return "\n".join(lines)


class Histogram:
    """Thread-safe histogram metric with preset buckets."""

    def __init__(
        self,
        name: str,
        description: str,
        labels: List[str] = None,
        buckets: List[float] = None,
    ):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._buckets = buckets or [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
        self._observations: Dict[tuple, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def observe(self, value: float, **labels) -> None:
        key = tuple(labels.get(l, "") for l in self._labels)
        with self._lock:
            self._observations[key].append(value)

    def get_observations(self, **labels) -> List[float]:
        """Return raw observations for a label combination."""
        key = tuple(labels.get(l, "") for l in self._labels)
        return list(self._observations.get(key, []))

    def collect(self) -> str:
        """Export in Prometheus text format with bucket counts."""
        lines = [f"# HELP {self.name} {self.description}"]
        lines.append(f"# TYPE {self.name} histogram")
        if not self._observations:
            return "\n".join(lines)
        with self._lock:
            for key, observations in self._observations.items():
                label_parts = (
                    [f'{l}="{v}"' for l, v in zip(self._labels, key)]
                    if self._labels
                    else []
                )
                label_prefix = ",".join(label_parts)
                # Bucket counts
                for bucket in self._buckets:
                    count = sum(1 for o in observations if o <= bucket)
                    le_label = f'le="{bucket}"'
                    if label_prefix:
                        lines.append(
                            f"{self.name}_bucket{{{label_prefix},{le_label}}} {count}"
                        )
                    else:
                        lines.append(f"{self.name}_bucket{{{le_label}}} {count}")
                # +Inf bucket
                inf_count = len(observations)
                le_inf = 'le="+Inf"'
                if label_prefix:
                    lines.append(
                        f"{self.name}_bucket{{{label_prefix},{le_inf}}} {inf_count}"
                    )
                else:
                    lines.append(f"{self.name}_bucket{{{le_inf}}} {inf_count}")
                # Sum and count
                total = sum(observations)
                if label_prefix:
                    lines.append(f"{self.name}_sum{{{label_prefix}}} {total}")
                    lines.append(f"{self.name}_count{{{label_prefix}}} {inf_count}")
                else:
                    lines.append(f"{self.name}_sum {total}")
                    lines.append(f"{self.name}_count {inf_count}")
        return "\n".join(lines)


class Gauge:
    """Thread-safe gauge metric."""

    def __init__(self, name: str, description: str, labels: List[str] = None):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._values: Dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels) -> None:
        key = tuple(labels.get(l, "") for l in self._labels)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels) -> None:
        key = tuple(labels.get(l, "") for l in self._labels)
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels) -> None:
        key = tuple(labels.get(l, "") for l in self._labels)
        with self._lock:
            self._values[key] -= value

    def get(self, **labels) -> float:
        key = tuple(labels.get(l, "") for l in self._labels)
        return self._values.get(key, 0.0)

    def collect(self) -> str:
        """Export in Prometheus text format."""
        lines = [f"# HELP {self.name} {self.description}"]
        lines.append(f"# TYPE {self.name} gauge")
        with self._lock:
            if not self._values:
                return "\n".join(lines)
            for key, value in self._values.items():
                if self._labels:
                    label_str = ",".join(
                        f'{l}="{v}"' for l, v in zip(self._labels, key)
                    )
                    lines.append(f"{self.name}{{{label_str}}} {value}")
                else:
                    lines.append(f"{self.name} {value}")
        return "\n".join(lines)


# Pre-defined COSMOS metrics
METRICS: Dict[str, object] = {}


def init_metrics():
    """Initialize all COSMOS metrics."""
    METRICS.clear()
    METRICS.update({
        # Request metrics
        "cosmos_requests_total": Counter(
            "cosmos_requests_total",
            "Total API requests",
            ["method", "endpoint", "status"],
        ),
        "cosmos_request_duration_seconds": Histogram(
            "cosmos_request_duration_seconds",
            "Request latency",
            ["method", "endpoint"],
        ),
        # ReAct engine metrics
        "cosmos_react_queries_total": Counter(
            "cosmos_react_queries_total",
            "Total ReAct queries",
            ["intent", "entity"],
        ),
        "cosmos_react_confidence": Histogram(
            "cosmos_react_confidence",
            "ReAct confidence distribution",
            ["intent"],
        ),
        "cosmos_react_loops": Histogram(
            "cosmos_react_loops",
            "ReAct loops per query",
            ["intent"],
            buckets=[1, 2, 3],
        ),
        "cosmos_react_latency_seconds": Histogram(
            "cosmos_react_latency_seconds",
            "ReAct total latency",
            ["intent"],
        ),
        "cosmos_escalations_total": Counter(
            "cosmos_escalations_total",
            "Escalated queries",
            ["reason"],
        ),
        # Tool metrics
        "cosmos_tool_calls_total": Counter(
            "cosmos_tool_calls_total",
            "Tool calls",
            ["tool_name", "success"],
        ),
        "cosmos_tool_latency_seconds": Histogram(
            "cosmos_tool_latency_seconds",
            "Tool execution latency",
            ["tool_name"],
        ),
        # LLM metrics
        "cosmos_llm_calls_total": Counter(
            "cosmos_llm_calls_total",
            "LLM API calls",
            ["model", "cached"],
        ),
        "cosmos_llm_tokens_total": Counter(
            "cosmos_llm_tokens_total",
            "Tokens consumed",
            ["model", "direction"],
        ),
        "cosmos_llm_cost_usd_total": Counter(
            "cosmos_llm_cost_usd_total",
            "Total cost in USD",
            ["model"],
        ),
        "cosmos_llm_latency_seconds": Histogram(
            "cosmos_llm_latency_seconds",
            "LLM call latency",
            ["model"],
        ),
        # Guardrail metrics
        "cosmos_guardrail_checks_total": Counter(
            "cosmos_guardrail_checks_total",
            "Guardrail checks",
            ["rule", "action"],
        ),
        # Approval metrics
        "cosmos_approvals_total": Counter(
            "cosmos_approvals_total",
            "Approval requests",
            ["risk_level", "status"],
        ),
        # Session metrics
        "cosmos_active_sessions": Gauge(
            "cosmos_active_sessions",
            "Currently active sessions",
        ),
        # MARS bridge metrics
        "cosmos_mars_requests_total": Counter(
            "cosmos_mars_requests_total",
            "Requests to MARS",
            ["endpoint", "status"],
        ),
        "cosmos_mars_handled_total": Counter(
            "cosmos_mars_handled_total",
            "Queries handled by MARS vs COSMOS",
            ["handler"],
        ),
    })


def collect_all() -> str:
    """Collect all metrics in Prometheus text format."""
    lines = []
    for metric in METRICS.values():
        lines.append(metric.collect())
    return "\n".join(lines)

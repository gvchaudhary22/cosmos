"""
Circuit Breaker — Graceful degradation when MARS is unavailable.

States:
  CLOSED:    MARS is healthy, all calls go through normally
  OPEN:      MARS is down (3+ failures), skip MARS-dependent tiers
  HALF_OPEN: After cooldown, try one probe call to check if MARS recovered

When OPEN:
  - Tier 1 tools (MCAPI via MARS) → skipped, KB-only answers
  - Tier 3 safe DB → skipped entirely
  - Response includes freshness_mode = "kb_only"
  - User informed: "Live data temporarily unavailable"
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


class CircuitState(str, Enum):
    CLOSED = "closed"         # Healthy
    OPEN = "open"             # MARS down, skip dependent calls
    HALF_OPEN = "half_open"   # Testing if MARS recovered


@dataclass
class CircuitStats:
    state: str
    consecutive_failures: int
    last_failure_time: Optional[float]
    last_success_time: Optional[float]
    total_failures: int
    total_successes: int
    total_short_circuits: int


class CircuitBreaker:
    """
    Tracks MARS health. Opens circuit after consecutive failures.
    Auto-recovers by probing after cooldown period.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        name: str = "mars",
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.name = name

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: Optional[float] = None
        self._last_success_time: Optional[float] = None
        self._total_failures = 0
        self._total_successes = 0
        self._total_short_circuits = 0

    @property
    def state(self) -> CircuitState:
        """Current circuit state, with automatic HALF_OPEN transition."""
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and (
                time.time() - self._last_failure_time > self.cooldown_seconds
            ):
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    "circuit_breaker.half_open",
                    name=self.name,
                    cooldown=self.cooldown_seconds,
                )
        return self._state

    @property
    def is_available(self) -> bool:
        """Should we attempt MARS calls?"""
        s = self.state
        return s in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self):
        """Record a successful MARS call."""
        self._consecutive_failures = 0
        self._last_success_time = time.time()
        self._total_successes += 1

        if self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            self._state = CircuitState.CLOSED
            logger.info("circuit_breaker.closed", name=self.name, reason="success after recovery")

    def record_failure(self):
        """Record a failed MARS call."""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        self._total_failures += 1

        if self._consecutive_failures >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker.opened",
                    name=self.name,
                    failures=self._consecutive_failures,
                )

    def record_short_circuit(self):
        """Record a call that was skipped due to open circuit."""
        self._total_short_circuits += 1

    def get_stats(self) -> CircuitStats:
        return CircuitStats(
            state=self.state.value,
            consecutive_failures=self._consecutive_failures,
            last_failure_time=self._last_failure_time,
            last_success_time=self._last_success_time,
            total_failures=self._total_failures,
            total_successes=self._total_successes,
            total_short_circuits=self._total_short_circuits,
        )

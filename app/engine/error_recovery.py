"""
Error Recovery — RALPH-inspired error recovery pattern.

Reflect -> Analyze -> Learn -> Plan -> Halt
Max 3 attempts before escalation.

Reference: mars/.claude/rules/ralph-error-loop.md
"""

import asyncio
import structlog
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = structlog.get_logger()


@dataclass
class RecoveryAttempt:
    attempt: int
    strategy: str
    error_type: str
    success: bool
    result: Any = None
    error: Optional[str] = None


@dataclass
class RecoveryResult:
    recovered: bool
    attempts: List[RecoveryAttempt]
    final_result: Any = None
    escalation_reason: Optional[str] = None


class ErrorRecovery:
    """RALPH-inspired error recovery: Reflect -> Analyze -> Learn -> Plan -> Halt.

    Max 3 attempts before escalation.
    """

    MAX_ATTEMPTS = 3

    # Error types that should never be retried
    NON_RETRYABLE = {"auth", "permanent", "validation"}

    async def attempt_recovery(
        self,
        error: Exception,
        context: dict,
        retry_fn: Callable,
    ) -> RecoveryResult:
        """Try to recover from an error with up to 3 attempts.

        Attempt 1 (REFLECT): Simple retry with minor adjustment
        Attempt 2 (ANALYZE): Broaden approach, try different tool
        Attempt 3 (LEARN): Fundamentally different strategy
        HALT: Return escalation response
        """
        error_type = self.classify_error(error)
        attempts: List[RecoveryAttempt] = []

        if not self.should_retry(error_type):
            attempts.append(RecoveryAttempt(
                attempt=0,
                strategy="no_retry",
                error_type=error_type,
                success=False,
                error=f"Non-retryable error type: {error_type}",
            ))
            return RecoveryResult(
                recovered=False,
                attempts=attempts,
                escalation_reason=f"Non-retryable error: {error_type} — {str(error)}",
            )

        strategies = [
            ("reflect", self._strategy_reflect),
            ("analyze", self._strategy_analyze),
            ("learn", self._strategy_learn),
        ]

        for attempt_num, (strategy_name, strategy_fn) in enumerate(strategies, 1):
            logger.info(
                "error_recovery.attempt",
                attempt=attempt_num,
                strategy=strategy_name,
                error_type=error_type,
            )

            adjusted_context = strategy_fn(context, error, error_type, attempt_num)

            try:
                result = await retry_fn(adjusted_context)
                attempts.append(RecoveryAttempt(
                    attempt=attempt_num,
                    strategy=strategy_name,
                    error_type=error_type,
                    success=True,
                    result=result,
                ))
                return RecoveryResult(
                    recovered=True,
                    attempts=attempts,
                    final_result=result,
                )
            except Exception as retry_error:
                logger.warning(
                    "error_recovery.attempt_failed",
                    attempt=attempt_num,
                    strategy=strategy_name,
                    error=str(retry_error),
                )
                attempts.append(RecoveryAttempt(
                    attempt=attempt_num,
                    strategy=strategy_name,
                    error_type=error_type,
                    success=False,
                    error=str(retry_error),
                ))
                # Update error for next attempt's classification
                error = retry_error
                error_type = self.classify_error(error)

                if not self.should_retry(error_type):
                    break

        # HALT — all attempts exhausted
        return RecoveryResult(
            recovered=False,
            attempts=attempts,
            escalation_reason=(
                f"Recovery failed after {len(attempts)} attempt(s). "
                f"Last error: {str(error)}"
            ),
        )

    def classify_error(self, error: Exception) -> str:
        """Classify error type: transient/permanent/auth/rate_limit/data/validation."""
        error_str = str(error).lower()
        error_type_name = type(error).__name__.lower()

        # Auth errors
        if any(kw in error_str for kw in ("unauthorized", "403", "401", "forbidden", "authentication")):
            return "auth"

        # Rate limiting
        if any(kw in error_str for kw in ("429", "rate limit", "too many requests", "throttl")):
            return "rate_limit"

        # Validation / bad input
        if any(kw in error_str for kw in ("validation", "invalid", "bad request", "400")):
            return "validation"

        # Timeouts (transient)
        if any(kw in error_str for kw in ("timeout", "timed out", "deadline")):
            return "transient"

        # Connection errors (transient)
        if any(kw in error_str for kw in ("connection", "refused", "reset", "503", "502", "504")):
            return "transient"

        # Data errors
        if any(kw in error_str for kw in ("not found", "404", "no data", "empty")):
            return "data"

        # Known permanent error types
        if any(kw in error_type_name for kw in ("permission", "notimplemented")):
            return "permanent"

        # Default to transient (optimistic)
        return "transient"

    def should_retry(self, error_type: str) -> bool:
        """Some errors should never retry (auth, permanent, validation)."""
        return error_type not in self.NON_RETRYABLE

    # ------------------------------------------------------------------
    # Recovery strategies
    # ------------------------------------------------------------------

    def _strategy_reflect(self, context: dict, error: Exception,
                          error_type: str, attempt: int) -> dict:
        """Attempt 1: Simple retry with minor adjustment.

        - For rate_limit: add a delay hint
        - For transient: retry as-is
        - For data: broaden search params
        """
        adjusted = dict(context)
        adjusted["_recovery_attempt"] = attempt
        adjusted["_recovery_strategy"] = "reflect"

        if error_type == "rate_limit":
            adjusted["_delay_seconds"] = 2
        elif error_type == "data":
            # Broaden: remove restrictive filters
            params = adjusted.get("params", {})
            if "date_from" in params:
                del params["date_from"]
            adjusted["params"] = params

        return adjusted

    def _strategy_analyze(self, context: dict, error: Exception,
                          error_type: str, attempt: int) -> dict:
        """Attempt 2: Broaden approach, try different tool."""
        adjusted = dict(context)
        adjusted["_recovery_attempt"] = attempt
        adjusted["_recovery_strategy"] = "analyze"

        # Suggest trying an alternative tool
        adjusted["_try_alternative_tool"] = True

        if error_type == "rate_limit":
            adjusted["_delay_seconds"] = 5
        elif error_type == "data":
            # Try searching by different field
            adjusted["_search_strategy"] = "broad"

        return adjusted

    def _strategy_learn(self, context: dict, error: Exception,
                        error_type: str, attempt: int) -> dict:
        """Attempt 3: Fundamentally different strategy."""
        adjusted = dict(context)
        adjusted["_recovery_attempt"] = attempt
        adjusted["_recovery_strategy"] = "learn"

        # Use fallback data source or simplified query
        adjusted["_use_fallback"] = True
        adjusted["_simplified"] = True

        if error_type == "rate_limit":
            adjusted["_delay_seconds"] = 10

        return adjusted

"""
E-Commerce Patterns — MARS ecommerce patterns.

Provides:
  - IdempotencyManager: ensures write operations are idempotent
  - OrderStateMachine: enforces strict order state transitions

Reference: mars/docs/ecommerce.md
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


@dataclass
class IdempotencyRecord:
    key: str
    result: dict
    created_at: float  # epoch
    expires_at: float  # epoch


class IdempotencyManager:
    """Ensures write operations are idempotent (from MARS ecommerce patterns).

    For every write action, a deterministic key is computed from (action, entity_id, params).
    If the same key is seen again before expiry, the cached result is returned
    instead of re-executing.
    """

    def __init__(self):
        self._records: Dict[str, IdempotencyRecord] = {}

    def generate_key(self, action: str, entity_id: str, params: dict) -> str:
        """Generate idempotency key: SHA-256 hash of action+entity+sorted-params."""
        raw = f"{action}:{entity_id}:{json.dumps(params, sort_keys=True, default=str)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def check(self, key: str) -> Optional[dict]:
        """Check if this operation was already executed.

        Returns cached result or None if not found / expired.
        """
        record = self._records.get(key)
        if record is None:
            return None
        if time.time() > record.expires_at:
            del self._records[key]
            return None
        return record.result

    def record(self, key: str, result: dict, ttl_seconds: int = 3600) -> None:
        """Record operation result for idempotency."""
        now = time.time()
        self._records[key] = IdempotencyRecord(
            key=key,
            result=result,
            created_at=now,
            expires_at=now + ttl_seconds,
        )

    def cleanup_expired(self) -> int:
        """Remove expired records. Returns count of removed entries."""
        now = time.time()
        expired_keys = [k for k, v in self._records.items() if now > v.expires_at]
        for k in expired_keys:
            del self._records[k]
        return len(expired_keys)


# --------------------------------------------------------------------------- #
# Order State Machine
# --------------------------------------------------------------------------- #


class OrderStateMachine:
    """Enforces strict order state transitions (from MARS ecommerce patterns).

    States: pending -> processing -> shipped -> delivered -> completed
                    -> cancelled (from pending/processing only)
                    -> returned (from delivered only)
                    -> refunded (from returned only)
    """

    TRANSITIONS: Dict[str, List[str]] = {
        "pending": ["processing", "cancelled"],
        "processing": ["shipped", "cancelled"],
        "shipped": ["delivered"],
        "delivered": ["completed", "returned"],
        "cancelled": [],    # terminal
        "completed": [],    # terminal
        "returned": ["refunded"],
        "refunded": [],     # terminal
    }

    # Action-to-required-status mapping
    ACTION_STATUS_MAP: Dict[str, List[str]] = {
        "cancel_order": ["pending", "processing"],
        "initiate_refund": ["delivered", "returned"],
        "reattempt_delivery": ["shipped"],   # NDR state
        "update_address": ["pending", "processing"],
    }

    def can_transition(self, current: str, target: str) -> bool:
        """Check if transition is valid."""
        allowed = self.TRANSITIONS.get(current, [])
        return target in allowed

    def get_allowed_transitions(self, current: str) -> List[str]:
        """Return list of valid next states from current state."""
        return list(self.TRANSITIONS.get(current, []))

    def validate_action(self, order_status: str, action: str) -> dict:
        """Validate if an action is allowed given current order status.

        Returns:
            {
                "allowed": bool,
                "reason": str,
                "required_status": List[str],
                "current_status": str,
            }
        """
        required = self.ACTION_STATUS_MAP.get(action)

        if required is None:
            return {
                "allowed": False,
                "reason": f"Unknown action '{action}'",
                "required_status": [],
                "current_status": order_status,
            }

        if order_status in required:
            return {
                "allowed": True,
                "reason": f"Action '{action}' is allowed for status '{order_status}'",
                "required_status": required,
                "current_status": order_status,
            }

        return {
            "allowed": False,
            "reason": (
                f"Action '{action}' requires order status to be one of {required}, "
                f"but current status is '{order_status}'"
            ),
            "required_status": required,
            "current_status": order_status,
        }

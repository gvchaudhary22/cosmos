"""MARS Integration Bridge -- Orchestrates MARS<->COSMOS 80/20 rule enforcement.

Before COSMOS processes any query:
1. Check MARS prompt safety gate
2. Ask MARS for rule-based classification
3. If MARS handled it (needs_cosmos=false) -> return MARS response directly
4. If needs_cosmos=true -> let COSMOS ReAct engine process
5. After processing -> sync results back to MARS
"""

import structlog
from typing import Optional

from app.clients.mars import MarsClient

logger = structlog.get_logger()


class MarsBridge:
    """Orchestrates MARS<->COSMOS 80/20 rule enforcement.

    Before COSMOS processes any query:
    1. Check MARS prompt safety gate
    2. Ask MARS for rule-based classification
    3. If MARS handled it (needs_cosmos=false) -> return MARS response directly
    4. If needs_cosmos=true -> let COSMOS ReAct engine process
    5. After processing -> sync results back to MARS
    """

    def __init__(self, mars_client: MarsClient, enabled: bool = True):
        self._mars = mars_client
        self._enabled = enabled
        self._stats = {"mars_handled": 0, "cosmos_handled": 0, "mars_unavailable": 0}

    async def pre_process(self, message: str, company_id: str, session_id: str) -> dict:
        """Pre-process through MARS.

        Returns: {"handled_by": "mars"|"cosmos", "mars_response": dict|None, "safety_check": dict}

        If MARS is unavailable, gracefully fall back to COSMOS-only mode.
        """
        result = {
            "handled_by": "cosmos",
            "mars_response": None,
            "safety_check": {"safe": True, "score": 0.0, "flags": []},
        }

        if not self._enabled:
            self._stats["cosmos_handled"] += 1
            return result

        # Step 1: Check MARS health
        try:
            healthy = await self._mars.health_check()
        except Exception:
            healthy = False

        if not healthy:
            logger.warning("mars_bridge.mars_unavailable", session_id=session_id)
            self._stats["mars_unavailable"] += 1
            self._stats["cosmos_handled"] += 1
            return result

        # Step 2: Prompt safety check
        try:
            safety = await self._mars.check_prompt_safety(message)
            result["safety_check"] = safety
            if not safety.get("safe", True):
                logger.warning(
                    "mars_bridge.unsafe_prompt",
                    session_id=session_id,
                    flags=safety.get("flags", []),
                )
                result["handled_by"] = "mars"
                result["mars_response"] = {
                    "blocked": True,
                    "reason": "Prompt flagged as unsafe",
                    "flags": safety.get("flags", []),
                }
                self._stats["mars_handled"] += 1
                return result
        except Exception as exc:
            logger.warning("mars_bridge.safety_check_failed", error=str(exc))
            # Continue to COSMOS on safety check failure

        # Step 3: Ask MARS for rule-based classification
        try:
            classification = await self._mars.classify_intent(message, company_id)
            needs_cosmos = classification.get("needs_cosmos", True)

            if not needs_cosmos:
                logger.info(
                    "mars_bridge.mars_handled",
                    session_id=session_id,
                    intent=classification.get("intent"),
                )
                result["handled_by"] = "mars"
                result["mars_response"] = classification
                self._stats["mars_handled"] += 1
                return result
        except Exception as exc:
            logger.warning("mars_bridge.classify_failed", error=str(exc))
            # Fall through to COSMOS

        # Step 4: COSMOS handles it
        self._stats["cosmos_handled"] += 1

        # Try to resume state from MARS
        try:
            state = await self._mars.resume_state(session_id)
            if state:
                result["resumed_state"] = state
        except Exception:
            pass

        return result

    async def post_process(self, session_id: str, result: dict) -> None:
        """Post-process: sync results back to MARS (state, learning)."""
        if not self._enabled:
            return

        # Save state to MARS for cross-session recovery
        try:
            state = {
                "confidence": result.get("confidence", 0.0),
                "tools_used": result.get("tools_used", []),
                "escalated": result.get("escalated", False),
            }
            await self._mars.save_state(session_id, state)
        except Exception as exc:
            logger.warning("mars_bridge.save_state_failed", error=str(exc))

        # Sync learning records if available
        try:
            learning = result.get("learning_records")
            if learning:
                await self._mars.sync_learning(learning)
        except Exception as exc:
            logger.warning("mars_bridge.sync_learning_failed", error=str(exc))

        # Create escalation ticket if needed
        if result.get("escalated"):
            try:
                await self._mars.create_escalation_ticket(
                    session_id=session_id,
                    reason=result.get("escalation_reason", "Low confidence"),
                    context={
                        "confidence": result.get("confidence", 0.0),
                        "tools_used": result.get("tools_used", []),
                    },
                )
            except Exception as exc:
                logger.warning("mars_bridge.escalation_failed", error=str(exc))

    def get_stats(self) -> dict:
        """Return bridge stats: mars_handled, cosmos_handled, mars_unavailable."""
        total = self._stats["mars_handled"] + self._stats["cosmos_handled"]
        return {
            **self._stats,
            "total": total,
            "mars_ratio": round(self._stats["mars_handled"] / total, 2) if total > 0 else 0.0,
            "cosmos_ratio": round(self._stats["cosmos_handled"] / total, 2) if total > 0 else 0.0,
        }

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional
from enum import Enum


class GuardrailAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    WARN = "warn"
    MASK = "mask"


@dataclass
class GuardrailResult:
    action: GuardrailAction
    reason: Optional[str] = None
    modified_data: Any = None  # For MASK action, contains masked version


class Guardrail(ABC):
    """Base class for all guardrails."""
    name: str

    @abstractmethod
    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        pass


class GuardrailPipeline:
    """Runs multiple guardrails in sequence. Stops on first BLOCK."""

    def __init__(self):
        self.pre_guards: list[Guardrail] = []
        self.post_guards: list[Guardrail] = []

    def add_pre(self, guard: Guardrail):
        self.pre_guards.append(guard)

    def add_post(self, guard: Guardrail):
        self.post_guards.append(guard)

    async def run_pre(self, context: Dict) -> GuardrailResult:
        """Run pre-execution guardrails. Returns first BLOCK or final ALLOW."""
        for guard in self.pre_guards:
            result = await guard.check(context)
            if result.action == GuardrailAction.BLOCK:
                return result
        return GuardrailResult(action=GuardrailAction.ALLOW)

    async def run_post(self, context: Dict) -> GuardrailResult:
        """Run post-execution guardrails. Can MASK or BLOCK response."""
        for guard in self.post_guards:
            result = await guard.check(context)
            if result.action == GuardrailAction.BLOCK:
                return result
            if result.action == GuardrailAction.MASK and result.modified_data:
                context["response"] = result.modified_data
        return GuardrailResult(action=GuardrailAction.ALLOW)

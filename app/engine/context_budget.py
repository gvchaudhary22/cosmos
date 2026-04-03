"""
Context Budgeter for COSMOS Phase 4.

Enforces token limits per query to prevent context-window rot.
Allocates budget by model tier and truncates content in priority order.
"""

import structlog
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.engine.model_router import ModelTier

logger = structlog.get_logger()


@dataclass
class TokenBudget:
    max_input_tokens: int
    max_output_tokens: int
    max_tool_results_tokens: int
    max_context_tokens: int


BUDGETS: Dict[ModelTier, TokenBudget] = {
    ModelTier.HAIKU: TokenBudget(4000, 2000, 1000, 1000),
    ModelTier.SONNET: TokenBudget(16000, 4000, 4000, 4000),
    ModelTier.OPUS: TokenBudget(32000, 8000, 8000, 8000),
}


class ContextBudgeter:
    """Enforces token budgets and truncates/summarizes when needed."""

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token for English."""
        if not text:
            return 0
        return max(1, len(text) // 4)

    def fit_within_budget(self, content: str, budget: int) -> str:
        """Truncate content to fit within token budget, preserving structure."""
        if not content:
            return content

        estimated = self.estimate_tokens(content)
        if estimated <= budget:
            return content

        # Truncate to approximate char count (budget * 4 chars/token)
        max_chars = budget * 4
        if max_chars <= 0:
            return ""

        # Try to truncate at a sentence boundary
        truncated = content[:max_chars]
        last_period = truncated.rfind(".")
        last_newline = truncated.rfind("\n")
        break_point = max(last_period, last_newline)

        if break_point > max_chars * 0.5:
            truncated = truncated[: break_point + 1]

        return truncated + "\n[...truncated to fit token budget]"

    def build_context_window(
        self,
        system_prompt: str,
        tools: List[Dict],
        session_history: List[Dict],
        user_message: str,
        tier: ModelTier,
    ) -> Dict:
        """
        Build context window that fits within the tier's budget.

        Priority order (what gets truncated first):
        1. Session history (oldest messages first)
        2. Tool result details (summarize large payloads)
        3. System prompt tools (remove least-relevant tool schemas)
        4. Never truncate: user message, safety instructions
        """
        budget = self.get_budget_for_tier(tier)
        remaining = budget.max_input_tokens

        # Reserve space for user message (never truncated)
        user_tokens = self.estimate_tokens(user_message)
        remaining -= user_tokens

        # Reserve space for system prompt (high priority)
        system_tokens = self.estimate_tokens(system_prompt)
        if system_tokens > remaining * 0.4:
            system_prompt = self.fit_within_budget(
                system_prompt, int(remaining * 0.4)
            )
            system_tokens = self.estimate_tokens(system_prompt)
        remaining -= system_tokens

        # Fit tools within budget
        fitted_tools = self._fit_tools(tools, min(remaining // 2, budget.max_tool_results_tokens))
        tools_tokens = sum(self.estimate_tokens(str(t)) for t in fitted_tools)
        remaining -= tools_tokens

        # Fit session history (oldest truncated first)
        fitted_history = self._fit_history(
            session_history, min(remaining, budget.max_context_tokens)
        )

        return {
            "system_prompt": system_prompt,
            "tools": fitted_tools,
            "session_history": fitted_history,
            "user_message": user_message,
            "budget_used": {
                "system_tokens": system_tokens,
                "tools_tokens": tools_tokens,
                "history_tokens": sum(
                    self.estimate_tokens(str(m)) for m in fitted_history
                ),
                "user_tokens": user_tokens,
                "tier": tier.value,
                "max_input_tokens": budget.max_input_tokens,
            },
        }

    def get_budget_for_tier(self, tier: ModelTier) -> TokenBudget:
        """Get budget config for a model tier."""
        return BUDGETS[tier]

    def _fit_tools(self, tools: List[Dict], budget_tokens: int) -> List[Dict]:
        """Fit tool definitions within budget, dropping least important last."""
        if not tools:
            return []

        fitted = []
        used = 0
        for tool in tools:
            tool_tokens = self.estimate_tokens(str(tool))
            if used + tool_tokens > budget_tokens:
                break
            fitted.append(tool)
            used += tool_tokens

        return fitted

    def _fit_history(self, history: List[Dict], budget_tokens: int) -> List[Dict]:
        """Fit session history within budget, keeping most recent messages."""
        if not history:
            return []

        # Work backwards (newest first) to keep the most recent context
        fitted = []
        used = 0
        for msg in reversed(history):
            msg_tokens = self.estimate_tokens(str(msg))
            if used + msg_tokens > budget_tokens:
                break
            fitted.append(msg)
            used += msg_tokens

        # Reverse back to chronological order
        fitted.reverse()
        return fitted

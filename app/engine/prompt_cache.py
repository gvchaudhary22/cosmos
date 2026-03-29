"""
Prompt Cache Manager for COSMOS Phase 4.

Manages cacheable prompt segments for Anthropic's prompt caching,
targeting ~90% cost reduction on repeated system-prompt prefixes.
"""

import hashlib
import structlog
from typing import Any, Dict, List, Optional

logger = structlog.get_logger()

# Default system prompt for COSMOS ICRM assistant
_DEFAULT_SYSTEM_PROMPT = (
    "You are COSMOS, an AI assistant for Shiprocket's internal CRM platform. "
    "You help customer support agents look up orders, shipments, returns, NDRs, "
    "payments, billing, wallets, and customer information. "
    "Always be concise, accurate, and reference specific data when available. "
    "If you are uncertain, say so clearly. Never fabricate data."
)


class PromptCacheManager:
    """Manages cacheable prompt segments for cost reduction on repeated prefixes."""

    def __init__(self) -> None:
        self._system_prompt_cache: Dict[str, Dict] = {}
        self._context_cache: Dict[str, Dict] = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0

    def get_system_prompt(self, role: str = "icrm_agent", tools: List[str] = None) -> Dict:
        """
        Return system prompt with cache_control markers.

        Returns dict in Anthropic format:
        {"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}
        """
        cache_key = self._make_key(role, tools or [])

        if cache_key in self._system_prompt_cache:
            self._cache_hits += 1
            return self._system_prompt_cache[cache_key]

        self._cache_misses += 1

        # Build tool context string
        tool_text = ""
        if tools:
            tool_text = (
                "\n\nAvailable tools: " + ", ".join(tools) + ". "
                "Use them to fetch data before answering. "
                "Never guess when a tool can provide the answer."
            )

        prompt_text = _DEFAULT_SYSTEM_PROMPT + tool_text

        cached_prompt = {
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }

        self._system_prompt_cache[cache_key] = cached_prompt
        return cached_prompt

    def build_cached_message(
        self,
        system_prompt: Dict,
        user_message: str,
        context: Optional[Dict] = None,
    ) -> Dict:
        """
        Build full message with cache-optimized structure.

        Order matters for caching:
        1. System prompt (stable, cached) — ~2-4k tokens
        2. Tool definitions (stable, cached) — ~3-5k tokens
        3. Session context (semi-stable) — ~1-2k tokens
        4. User message (always fresh) — variable
        """
        message = {
            "system": [system_prompt],
            "messages": [],
        }

        # Add session context if available
        if context:
            context_text = self._format_context(context)
            message["messages"].append({
                "role": "user",
                "content": f"[Context] {context_text}",
            })
            message["messages"].append({
                "role": "assistant",
                "content": "Understood. I have the session context.",
            })

        # Add user message (always fresh, never cached)
        message["messages"].append({
            "role": "user",
            "content": user_message,
        })

        return message

    def get_cache_stats(self) -> Dict:
        """Return hit rate, estimated savings."""
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0.0

        # Anthropic caches reduce cost by ~90% on cache hits
        estimated_savings_pct = hit_rate * 0.9

        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "total_lookups": total,
            "hit_rate_pct": round(hit_rate, 2),
            "estimated_savings_pct": round(estimated_savings_pct, 2),
        }

    def invalidate(self, role: str = None) -> int:
        """Invalidate cached prompts. Returns number of entries cleared."""
        if role is None:
            count = len(self._system_prompt_cache)
            self._system_prompt_cache.clear()
            return count

        to_remove = [k for k in self._system_prompt_cache if k.startswith(f"{role}:")]
        for k in to_remove:
            del self._system_prompt_cache[k]
        return len(to_remove)

    @staticmethod
    def _make_key(role: str, tools: List[str]) -> str:
        tools_str = ",".join(sorted(tools))
        return f"{role}:{hashlib.md5(tools_str.encode()).hexdigest()}"

    @staticmethod
    def _format_context(context: Dict) -> str:
        parts = []
        for k, v in context.items():
            parts.append(f"{k}: {v}")
        return "; ".join(parts)

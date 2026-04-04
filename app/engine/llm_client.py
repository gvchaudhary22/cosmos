"""
Unified LLM Client for COSMOS Phase 4.

Wraps model routing, prompt caching, context budgeting, and cost tracking
into a single interface that is backward-compatible with the existing
MockLLMClient.complete(prompt, max_tokens) signature used by ReActEngine.

Supports three backends:
  1. Anthropic API (ANTHROPIC_API_KEY) — production, fast, pay-per-token
  2. CLI (Max plan) — development/testing, $20/month flat
  3. Hybrid (CLI for cheap tasks, API for critical) — cost-optimized

Backend selected via LLM_MODE env var: "api" | "cli" | "hybrid"
"""

import asyncio
import json
import os
import structlog
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from app.engine.classifier import Intent
from app.engine.model_router import ModelRouter, ModelTier, PROFILES
from app.engine.prompt_cache import PromptCacheManager
from app.engine.context_budget import ContextBudgeter
from app.engine.cost_tracker import CostTracker

logger = structlog.get_logger()


@dataclass
class ToolUseResult:
    """Result from a multi-turn call_with_tools() conversation."""
    final_text: str
    tool_calls_made: List[Dict] = field(default_factory=list)
    pending_approval: Optional[Dict] = None  # set when a HIGH-risk tool needs approval
    turns: int = 0


def _build_anthropic_client(api_key: Optional[str]) -> Any:
    """Lazily build an AsyncAnthropic client if an API key is provided."""
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=api_key)
    except ImportError:
        logger.warning("llm_client.anthropic_not_installed")
        return None


class CLIBackend:
    """
    CLI backend — uses Max plan subscription via subprocess.

    Each call spawns: claude -p "prompt" --output-format json
    Each call is stateless (no --session-id / --continue) — COSMOS queries
    are single-shot retrieval tasks that don't require multi-turn context.
    """

    def __init__(self, model: str = "sonnet"):
        self.model = model

    async def complete(
        self,
        prompt: str,
        max_tokens: int = 500,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call CLI and return structured result.

        Returns: {text, input_tokens, output_tokens, session_id, latency_ms}
        """
        t0 = time.monotonic()

        # Build the full prompt with system context
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"[System: {system_prompt}]\n\n{prompt}"

        cmd = [
            "claude", "-p", full_prompt,
            "--output-format", "json",
            "--model", self.model,
            "--max-turns", "1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=60.0,  # 60s timeout for CLI
            )

            latency_ms = (time.monotonic() - t0) * 1000

            if proc.returncode != 0:
                error = stderr.decode("utf-8", errors="ignore")[:500]
                logger.warning("cli_backend.error", returncode=proc.returncode, error=error)
                raise CLIBackendError(f"CLI exited {proc.returncode}: {error}")

            # Parse JSON output
            output = stdout.decode("utf-8", errors="ignore")
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                # CLI might return plain text if --output-format json fails
                data = {"result": output.strip()}

            # Extract response text
            text = ""
            if isinstance(data, dict):
                text = data.get("result", data.get("content", data.get("text", "")))
                if isinstance(text, list):
                    # Handle content blocks format
                    text = "".join(
                        block.get("text", "") for block in text
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
            elif isinstance(data, str):
                text = data

            # Extract usage if available
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            input_tokens = usage.get("input_tokens", len(full_prompt.split()) * 2)  # estimate
            output_tokens = usage.get("output_tokens", len(text.split()) * 2)

            logger.info(
                "cli_backend.complete",
                latency_ms=round(latency_ms, 1),
                tokens_in=input_tokens,
                tokens_out=output_tokens,
            )

            return {
                "text": text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "session_id": cli_session,
                "latency_ms": latency_ms,
            }

        except asyncio.TimeoutError:
            logger.error("cli_backend.timeout", timeout=60)
            raise CLIBackendError("CLI timed out after 60 seconds")
        except FileNotFoundError:
            raise CLIBackendError(
                "CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            )

    async def stream(
        self,
        prompt: str,
        max_tokens: int = 500,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Stream response from CLI (reads stdout line by line)."""
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"[System: {system_prompt}]\n\n{prompt}"

        cmd = [
            "claude", "-p", full_prompt,
            "--output-format", "stream-json",
            "--model", self.model,
            "--max-turns", "1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async for line in proc.stdout:
                decoded = line.decode("utf-8", errors="ignore").strip()
                if not decoded:
                    continue
                try:
                    event = json.loads(decoded)
                    if event.get("type") == "assistant" and "content" in event:
                        for block in event["content"]:
                            if block.get("type") == "text":
                                yield block["text"]
                    elif isinstance(event, dict) and "text" in event:
                        yield event["text"]
                except json.JSONDecodeError:
                    yield decoded

            await proc.wait()

        except FileNotFoundError:
            raise CLIBackendError("CLI not found")


class CLIBackendError(Exception):
    """Raised when CLI backend fails."""
    pass


class LLMClient:
    """
    Unified LLM client with model routing, caching, and cost tracking.

    Drop-in compatible with the existing llm.complete(prompt, max_tokens)
    interface used by ReActEngine.

    When *anthropic_client* is explicitly provided it is used as-is (useful
    for testing with mocks).  Otherwise, if *api_key* is set, a real
    ``anthropic.AsyncAnthropic`` instance is created automatically.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_router: Optional[ModelRouter] = None,
        cache_manager: Optional[PromptCacheManager] = None,
        cost_tracker: Optional[CostTracker] = None,
        context_budgeter: Optional[ContextBudgeter] = None,
        anthropic_client: Any = None,
        llm_mode: Optional[str] = None,
    ) -> None:
        self._api_key = api_key
        self._router = model_router or ModelRouter()
        self._cache = cache_manager or PromptCacheManager()
        self._costs = cost_tracker or CostTracker()
        self._budgeter = context_budgeter or ContextBudgeter()
        self._default_session_id = "default"

        # Backend selection: "api" | "cli" | "hybrid"
        self._mode = llm_mode or os.environ.get("LLM_MODE", "api")

        # Build backends based on mode
        self._client = None      # Anthropic API client
        self._cli = None         # CLI backend

        if self._mode in ("api", "hybrid"):
            self._client = anthropic_client if anthropic_client is not None else _build_anthropic_client(api_key)

        if self._mode in ("cli", "hybrid"):
            cli_model = os.environ.get("CLI_MODEL", "sonnet")
            self._cli = CLIBackend(model=cli_model)

        # Log which backend(s) are active
        backends = []
        if self._client:
            backends.append("api")
        if self._cli:
            backends.append("cli")
        logger.info("llm_client.init", mode=self._mode, backends=backends)

    # ------------------------------------------------------------------
    # complete — non-streaming request
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        max_tokens: int = 500,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
        session_id: Optional[str] = None,
        complexity_signals: Optional[Dict] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """
        Complete with automatic model routing and cost tracking.

        Backward-compatible: can be called as complete(prompt, max_tokens)
        just like the old MockLLMClient.

        Full flow:
        1. Route to appropriate model tier
        2. Check budget
        3. Apply prompt caching (or use caller-supplied system_prompt)
        4. Fit within context budget
        5. Call Anthropic API (or raise if no client)
        6. Record costs
        7. Return response
        """
        sid = session_id or self._default_session_id
        intent_enum = self._resolve_intent(intent)
        conf = confidence if confidence is not None else 0.7

        # 1. Route to model
        profile = self._router.route(
            intent_enum, conf, complexity_signals or {}
        )

        # 2. Check budget
        budget_check = self._costs.check_budget(sid)
        if not budget_check["allowed"]:
            logger.warning(
                "llm_client.budget_exceeded",
                session_id=sid,
                warning=budget_check["warning"],
            )
            raise BudgetExceededError(budget_check["warning"])

        # 3. Build system prompt — prefer caller-supplied, else cache
        if system_prompt is not None:
            sys_block = {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        else:
            sys_block = self._cache.get_system_prompt()

        # 4. Fit within context budget
        tier_budget = self._budgeter.get_budget_for_tier(profile.tier)
        fitted_prompt = self._budgeter.fit_within_budget(
            prompt, tier_budget.max_input_tokens
        )

        # 5. Call backend (API or CLI based on mode + priority)
        effective_max_tokens = min(max_tokens, profile.max_tokens)
        sys_text = sys_block.get("text", "") if isinstance(sys_block, dict) else str(sys_block)
        priority = complexity_signals.get("priority", "normal") if complexity_signals else "normal"

        response_text, input_tokens, output_tokens = await self._call_backend(
            profile, sys_block, sys_text, fitted_prompt, effective_max_tokens, sid, priority
        )

        # 6. Record costs
        cached = sys_block.get("cache_control") is not None
        self._costs.record(
            session_id=sid,
            tier=profile.tier.value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            intent=intent or "unknown",
            cached=cached,
        )

        # 7. Return
        return response_text

    # ------------------------------------------------------------------
    # stream — SSE-friendly streaming request
    # ------------------------------------------------------------------

    async def stream(
        self,
        prompt: str,
        max_tokens: int = 500,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """
        Streaming variant of complete(). Yields text chunks as they arrive
        from the Anthropic API.

        Falls back to a single-yield of the full complete() result when the
        underlying client does not support ``messages.stream``.
        """
        sid = session_id or self._default_session_id
        intent_enum = self._resolve_intent(intent)
        conf = confidence if confidence is not None else 0.7

        # Route
        profile = self._router.route(intent_enum, conf, {})

        # Budget
        budget_check = self._costs.check_budget(sid)
        if not budget_check["allowed"]:
            raise BudgetExceededError(budget_check["warning"])

        # System prompt
        if system_prompt is not None:
            sys_block = {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        else:
            sys_block = self._cache.get_system_prompt()

        # Fit prompt
        tier_budget = self._budgeter.get_budget_for_tier(profile.tier)
        fitted_prompt = self._budgeter.fit_within_budget(
            prompt, tier_budget.max_input_tokens
        )
        effective_max_tokens = min(max_tokens, profile.max_tokens)

        if self._client is None:
            raise LLMClientError(
                "No Anthropic client configured. Set ANTHROPIC_API_KEY or provide anthropic_client."
            )

        # Try real streaming via client.messages.stream
        stream_fn = getattr(getattr(self._client, "messages", None), "stream", None)
        if stream_fn is not None:
            async for chunk in self._stream_anthropic(
                profile, sys_block, fitted_prompt, effective_max_tokens, sid, intent
            ):
                yield chunk
        else:
            # Fallback: call complete() and yield the whole result at once
            result = await self.complete(
                prompt,
                max_tokens=max_tokens,
                intent=intent,
                confidence=confidence,
                session_id=session_id,
                system_prompt=system_prompt,
            )
            yield result

    # ------------------------------------------------------------------
    # classify / reason helpers
    # ------------------------------------------------------------------

    async def classify(self, text: str) -> str:
        """Always uses Haiku for pure classification tasks (no response generation).

        Uses complexity_signals to force Haiku via route_classify path.
        This is the only call site where Haiku is appropriate — it extracts
        intent/entity from text, not generating a response for an operator.
        """
        return await self.complete(
            text,
            max_tokens=200,
            intent="classify",
            confidence=1.0,
            complexity_signals={"_force_classify": True},
        )

    async def reason(
        self,
        prompt: str,
        max_tokens: int = 2000,
        session_id: Optional[str] = None,
    ) -> str:
        """Uses Sonnet by default, Opus for complex queries."""
        return await self.complete(
            prompt,
            max_tokens=max_tokens,
            intent="explain",
            confidence=0.6,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # call_with_tools — multi-turn Claude tool_use loop
    # ------------------------------------------------------------------

    async def call_with_tools(
        self,
        prompt: str,
        tools: List[Dict],
        system_prompt: str = "",
        tool_executor: Optional[Callable] = None,
        max_turns: int = 5,
        max_tokens: int = 1024,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
    ) -> "ToolUseResult":
        """
        Run a multi-turn Claude tool_use conversation.

        Flow per turn:
          1. Send messages + tool definitions to Claude
          2. If stop_reason == "tool_use": extract tool calls, execute via tool_executor, feed results back
          3. If stop_reason == "end_turn": return final text
          4. Repeat up to max_turns

        Args:
            prompt:        User message
            tools:         Anthropic-format tool definitions (from ToolExecutorService)
            system_prompt: KB context injected as system message
            tool_executor: async callable(tool_name, tool_input) -> ToolExecutionResult
                           If None, tool calls are returned unexecuted.
            max_turns:     Safety cap (default 5)
            max_tokens:    Per-turn token budget
            session_id:    For cost tracking
            intent:        For model routing (default "act" → Opus)

        Returns:
            ToolUseResult with final_text, tool_calls_made, pending_approval info
        """
        if self._client is None:
            raise LLMClientError(
                "call_with_tools() requires Anthropic API mode. "
                "Set ANTHROPIC_API_KEY and LLM_MODE=api."
            )

        sid = session_id or self._default_session_id
        profile = self._router.route(
            self._resolve_intent(intent or "act"), 0.8, {}
        )

        sys_block = {
            "type": "text",
            "text": system_prompt or "You are COSMOS, Shiprocket's AI assistant for ICRM operators.",
            "cache_control": {"type": "ephemeral"},
        }

        messages: List[Dict] = [{"role": "user", "content": prompt}]
        tool_calls_made: List[Dict] = []
        pending_approval: Optional[Dict] = None
        final_text = ""
        total_input_tokens = 0
        total_output_tokens = 0

        for turn in range(max_turns):
            logger.info("llm_client.tool_use_turn", turn=turn, tools=len(tools))

            response = await self._client.messages.create(
                model=profile.model_id,
                max_tokens=max_tokens,
                system=[sys_block],
                tools=tools,
                tool_choice={"type": "auto"},
                messages=messages,
            )

            total_input_tokens += getattr(response.usage, "input_tokens", 0)
            total_output_tokens += getattr(response.usage, "output_tokens", 0)

            # Collect text blocks
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text  # overwrite — last text wins

            if response.stop_reason != "tool_use":
                break

            # Process tool_use blocks
            tool_results_content = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tc = {"id": block.id, "name": block.name, "input": block.input}
                tool_calls_made.append(tc)
                logger.info("llm_client.tool_called", tool=block.name, turn=turn)

                if tool_executor is None:
                    # No executor — return the tool calls for caller to handle
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"error": "No tool executor configured"}),
                    })
                    continue

                result = await tool_executor(block.name, block.input)

                if result.status == "pending_approval":
                    pending_approval = result.data
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result.data),
                    })
                elif result.status == "error":
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": json.dumps({"error": result.error}),
                    })
                else:
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result.data),
                    })

            # Append assistant turn + tool results for next turn
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results_content})

            if pending_approval:
                # Stop loop — operator must confirm before we can continue
                break

        # Record costs
        self._costs.record(
            session_id=sid,
            tier=profile.tier.value,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            intent=intent or "act",
            cached=True,
        )

        return ToolUseResult(
            final_text=final_text,
            tool_calls_made=tool_calls_made,
            pending_approval=pending_approval,
            turns=min(turn + 1, max_turns),
        )

    # ------------------------------------------------------------------
    # Accessor helpers
    # ------------------------------------------------------------------

    def get_router(self) -> ModelRouter:
        """Access the model router for stats."""
        return self._router

    def get_cost_tracker(self) -> CostTracker:
        """Access the cost tracker for stats."""
        return self._costs

    def get_cache_manager(self) -> PromptCacheManager:
        """Access the prompt cache manager for stats."""
        return self._cache

    def has_client(self) -> bool:
        """Return True if any LLM backend is configured (API or CLI)."""
        return self._client is not None or self._cli is not None

    @property
    def mode(self) -> str:
        """Current LLM backend mode: api | cli | hybrid."""
        return self._mode

    # ------------------------------------------------------------------
    # Internal: backend routing (API vs CLI vs Hybrid)
    # ------------------------------------------------------------------

    async def _call_backend(
        self,
        profile,
        sys_block: Dict,
        sys_text: str,
        prompt: str,
        max_tokens: int,
        session_id: str,
        priority: str = "normal",
    ) -> tuple:
        """
        Route to the correct backend based on mode and priority.

        Modes:
          api:    Always use Anthropic API
          cli:    Always use CLI
          hybrid: Low priority → CLI, normal/high → API
        """
        use_cli = False

        if self._mode == "cli":
            use_cli = True
        elif self._mode == "hybrid":
            # Hybrid: use CLI for cheap tasks, API for critical
            use_cli = priority == "low"
            # Fallback: if API not available, use CLI
            if not use_cli and self._client is None:
                use_cli = True

        if use_cli and self._cli:
            return await self._call_cli(prompt, max_tokens, sys_text, session_id)
        elif self._client is not None:
            return await self._call_anthropic(profile, sys_block, prompt, max_tokens)
        elif self._cli:
            # API requested but not available — fallback to CLI
            logger.info("llm_client.fallback_to_cli", reason="api_not_configured")
            return await self._call_cli(prompt, max_tokens, sys_text, session_id)
        else:
            raise LLMClientError(
                "No LLM backend configured. Set ANTHROPIC_API_KEY for API mode "
                "or install CLI for CLI mode (LLM_MODE=cli)."
            )

    async def _call_cli(
        self,
        prompt: str,
        max_tokens: int,
        system_prompt: str,
        session_id: str,
    ) -> tuple:
        """Call CLI and return (text, input_tokens, output_tokens)."""
        try:
            result = await self._cli.complete(
                prompt=prompt,
                max_tokens=max_tokens,
                system_prompt=system_prompt if system_prompt else None,
                session_id=session_id,
            )
            return (
                result["text"],
                result["input_tokens"],
                result["output_tokens"],
            )
        except CLIBackendError as exc:
            raise LLMClientError(f"CLI backend failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal: non-streaming Anthropic call
    # ------------------------------------------------------------------

    async def _call_anthropic(
        self,
        profile,
        system_prompt: Dict,
        prompt: str,
        max_tokens: int,
    ) -> tuple:
        """Call the Anthropic API and return (text, input_tokens, output_tokens)."""
        try:
            response = await self._client.messages.create(
                model=profile.model_id,
                max_tokens=max_tokens,
                system=[system_prompt],
                messages=[{"role": "user", "content": prompt}],
            )

            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text

            input_tokens = getattr(response.usage, "input_tokens", 0)
            output_tokens = getattr(response.usage, "output_tokens", 0)

            return text, input_tokens, output_tokens

        except LLMClientError:
            raise
        except Exception as exc:
            logger.error("llm_client.anthropic_error", error=str(exc))
            raise LLMClientError(f"Anthropic API call failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal: streaming Anthropic call
    # ------------------------------------------------------------------

    async def _stream_anthropic(
        self,
        profile,
        system_prompt: Dict,
        prompt: str,
        max_tokens: int,
        session_id: str,
        intent: Optional[str],
    ) -> AsyncIterator[str]:
        """Stream text chunks from the Anthropic API and record costs afterward."""
        try:
            async with self._client.messages.stream(
                model=profile.model_id,
                max_tokens=max_tokens,
                system=[system_prompt],
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text

                # After stream completes, grab the final message for usage
                final_message = await stream.get_final_message()
                input_tokens = getattr(final_message.usage, "input_tokens", 0)
                output_tokens = getattr(final_message.usage, "output_tokens", 0)

                cached = system_prompt.get("cache_control") is not None
                self._costs.record(
                    session_id=session_id,
                    tier=profile.tier.value,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    intent=intent or "unknown",
                    cached=cached,
                )

        except LLMClientError:
            raise
        except Exception as exc:
            logger.error("llm_client.stream_error", error=str(exc))
            raise LLMClientError(f"Anthropic streaming failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_intent(intent: Optional[str]) -> Intent:
        """Convert string intent to Intent enum, defaulting to UNKNOWN."""
        if intent is None:
            return Intent.UNKNOWN
        try:
            return Intent(intent)
        except ValueError:
            return Intent.UNKNOWN


class LLMClientError(Exception):
    """Raised when the LLM client encounters an error."""
    pass


class BudgetExceededError(LLMClientError):
    """Raised when a budget limit is exceeded."""
    pass

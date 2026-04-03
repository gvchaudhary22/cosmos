"""
Tests for LLM integration: Anthropic API wiring, SSE streaming, and backward compatibility.

Covers:
  - LLMClient with no API key (mock fallback) — must still work
  - LLMClient.complete() routing logic
  - LLMClient.stream() yields chunks
  - SSE chat endpoint returns proper event format
  - Budget enforcement blocks when exceeded
  - System prompt passthrough
  - classify() always uses Haiku
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.engine.llm_client import (
    LLMClient,
    LLMClientError,
    BudgetExceededError,
)
from app.engine.model_router import ModelRouter, ModelTier, PROFILES
from app.engine.cost_tracker import CostTracker
from app.engine.prompt_cache import PromptCacheManager
from app.engine.context_budget import ContextBudgeter


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Helper: build a mock Anthropic client
# ------------------------------------------------------------------

def _mock_anthropic_client(text="Hello from Claude.", input_tokens=120, output_tokens=30):
    """Return a MagicMock that behaves like anthropic.AsyncAnthropic."""
    mock_block = MagicMock()
    mock_block.text = text

    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_response.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    # No .stream by default — tests that need it add it explicitly
    mock_client.messages.stream = None
    return mock_client


def _mock_streaming_client(chunks=None, input_tokens=80, output_tokens=25):
    """Return a mock client whose messages.stream yields text chunks."""
    if chunks is None:
        chunks = ["Hello", " from", " Claude", " streaming."]

    # Build the async context manager for stream
    mock_final = MagicMock()
    mock_final.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)

    class _FakeStream:
        def __init__(self):
            self.text_stream = _async_iter(chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_final_message(self):
            return mock_final

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock()  # fallback
    mock_client.messages.stream = MagicMock(return_value=_FakeStream())
    return mock_client


async def _async_iter(items):
    for item in items:
        yield item


# =====================================================================
# 1. No API key — backward compatibility
# =====================================================================


class TestNoApiKey:
    """When no API key / client is provided, existing behavior is preserved."""

    def test_no_client_raises_llm_error(self):
        client = LLMClient(llm_mode="api")
        with pytest.raises(LLMClientError, match="No LLM backend"):
            _run(client.complete("hello"))

    def test_has_client_false(self):
        client = LLMClient(llm_mode="api")
        assert client.has_client() is False

    def test_has_client_true_with_mock(self):
        client = LLMClient(anthropic_client=_mock_anthropic_client())
        assert client.has_client() is True

    def test_backward_compatible_signature(self):
        """complete() accepts (prompt, max_tokens) positionally."""
        import inspect
        sig = inspect.signature(LLMClient.complete)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "max_tokens" in params
        # system_prompt is new but optional
        assert "system_prompt" in params


# =====================================================================
# 2. complete() routing logic
# =====================================================================


class TestCompleteRouting:
    def test_lookup_high_confidence_uses_sonnet(self):
        """Quality-first: high-confidence lookups use Sonnet minimum, not Haiku."""
        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        _run(client.complete("show order 123", intent="lookup", confidence=0.95))
        model_used = mock.messages.create.call_args.kwargs["model"]
        assert "sonnet" in model_used

    def test_explain_uses_opus(self):
        """Quality-first: explain always uses Opus for causal depth."""
        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        _run(client.complete("why is order delayed", intent="explain", confidence=0.7))
        model_used = mock.messages.create.call_args.kwargs["model"]
        assert "opus" in model_used

    def test_low_confidence_uses_opus(self):
        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        _run(client.complete("some ambiguous thing", intent="lookup", confidence=0.3))
        model_used = mock.messages.create.call_args.kwargs["model"]
        assert "opus" in model_used

    def test_complete_returns_text(self):
        mock = _mock_anthropic_client(text="Order #123 was shipped yesterday.")
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        result = _run(client.complete("show order 123"))
        assert result == "Order #123 was shipped yesterday."

    def test_complete_records_cost(self):
        mock = _mock_anthropic_client(input_tokens=200, output_tokens=100)
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        _run(client.complete("hello", session_id="s1"))
        summary = client.get_cost_tracker().get_session_summary("s1")
        assert summary["query_count"] == 1
        assert summary["total_input_tokens"] == 200
        assert summary["total_output_tokens"] == 100

    def test_custom_system_prompt_passed_through(self):
        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        _run(client.complete("hello", system_prompt="You are a pirate."))
        call_kwargs = mock.messages.create.call_args.kwargs
        system_blocks = call_kwargs["system"]
        assert len(system_blocks) == 1
        assert system_blocks[0]["text"] == "You are a pirate."
        assert system_blocks[0]["cache_control"]["type"] == "ephemeral"


# =====================================================================
# 3. stream() yields chunks
# =====================================================================


class TestStream:
    def test_stream_yields_chunks(self):
        chunks = ["Hello", " world", "!"]
        mock = _mock_streaming_client(chunks=chunks)
        client = LLMClient(anthropic_client=mock, llm_mode="api")

        collected = []

        async def _collect():
            async for chunk in client.stream("say hello"):
                collected.append(chunk)

        _run(_collect())
        assert collected == chunks

    def test_stream_records_cost_after_completion(self):
        mock = _mock_streaming_client(chunks=["Hi"], input_tokens=50, output_tokens=10)
        client = LLMClient(anthropic_client=mock, llm_mode="api")

        async def _drain():
            async for _ in client.stream("hi", session_id="stream_s"):
                pass

        _run(_drain())
        summary = client.get_cost_tracker().get_session_summary("stream_s")
        assert summary["query_count"] == 1

    def test_stream_no_client_raises(self):
        client = LLMClient(llm_mode="api")

        async def _try():
            async for _ in client.stream("hello"):
                pass

        with pytest.raises(LLMClientError, match="No Anthropic client"):
            _run(_try())

    def test_stream_fallback_to_complete_when_no_stream_method(self):
        """If client.messages.stream is None, falls back to complete()."""
        mock = _mock_anthropic_client(text="Fallback complete.")
        # messages.stream is already None from _mock_anthropic_client
        client = LLMClient(anthropic_client=mock, llm_mode="api")

        collected = []

        async def _collect():
            async for chunk in client.stream("hello"):
                collected.append(chunk)

        _run(_collect())
        assert len(collected) == 1
        assert collected[0] == "Fallback complete."


# =====================================================================
# 4. Budget enforcement
# =====================================================================


class TestBudgetEnforcement:
    def test_budget_exceeded_blocks_complete(self):
        tracker = CostTracker(daily_budget_usd=0.001, per_session_budget_usd=0.001)
        # Burn through budget
        for _ in range(20):
            tracker.record("s1", "opus", 5000, 5000, "explain")

        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, cost_tracker=tracker, llm_mode="api")
        with pytest.raises(BudgetExceededError):
            _run(client.complete("hello", session_id="s1"))

    def test_budget_exceeded_blocks_stream(self):
        tracker = CostTracker(daily_budget_usd=0.001, per_session_budget_usd=0.001)
        for _ in range(20):
            tracker.record("s1", "opus", 5000, 5000, "explain")

        mock = _mock_streaming_client()
        client = LLMClient(anthropic_client=mock, cost_tracker=tracker, llm_mode="api")

        async def _try():
            async for _ in client.stream("hello", session_id="s1"):
                pass

        with pytest.raises(BudgetExceededError):
            _run(_try())

    def test_fresh_session_passes_budget(self):
        tracker = CostTracker(daily_budget_usd=50.0, per_session_budget_usd=1.0)
        mock = _mock_anthropic_client()
        client = LLMClient(anthropic_client=mock, cost_tracker=tracker, llm_mode="api")
        # Should not raise
        result = _run(client.complete("hello", session_id="fresh"))
        assert result is not None


# =====================================================================
# 5. classify() always uses Haiku
# =====================================================================


class TestClassify:
    def test_classify_routes_to_haiku(self):
        mock = _mock_anthropic_client(text='{"intent": "lookup"}')
        client = LLMClient(anthropic_client=mock, llm_mode="api")
        result = _run(client.classify("show order 12345"))
        model_used = mock.messages.create.call_args.kwargs["model"]
        assert "haiku" in model_used
        assert "lookup" in result


# =====================================================================
# 6. SSE chat endpoint format
# =====================================================================


class TestSSEChatEndpoint:
    """Test the /stream endpoint returns proper SSE format."""

    def test_stream_endpoint_returns_sse(self):
        from starlette.testclient import TestClient
        from app.api.endpoints.chat import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router, prefix="/chat")

        client = TestClient(app)
        resp = client.post(
            "/chat/stream",
            json={"message": "hello", "user_id": "u1"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Parse SSE events
        body = resp.text
        events = _parse_sse(body)
        assert len(events) >= 2  # at least phase + done/chunk

        # First event should be a phase event
        assert events[0]["event"] == "phase"
        phase_data = json.loads(events[0]["data"])
        assert phase_data["phase"] == "classifying"

        # Last event should be done (stub mode)
        last = events[-1]
        assert last["event"] == "done"
        done_data = json.loads(last["data"])
        assert "confidence" in done_data
        assert "tools_used" in done_data

    def test_stream_endpoint_has_correct_headers(self):
        from starlette.testclient import TestClient
        from app.api.endpoints.chat import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router, prefix="/chat")

        client = TestClient(app)
        resp = client.post(
            "/chat/stream",
            json={"message": "test", "user_id": "u1"},
        )
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"

    def test_stream_endpoint_chunk_events_present(self):
        from starlette.testclient import TestClient
        from app.api.endpoints.chat import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router, prefix="/chat")

        client = TestClient(app)
        resp = client.post(
            "/chat/stream",
            json={"message": "hello", "user_id": "u1"},
        )
        events = _parse_sse(resp.text)
        chunk_events = [e for e in events if e["event"] == "chunk"]
        # Stub mode should still emit at least one chunk
        assert len(chunk_events) >= 1


# =====================================================================
# 7. SSE helper format
# =====================================================================


class TestSSEHelper:
    def test_sse_format(self):
        from app.api.endpoints.chat import _sse
        result = _sse("phase", {"phase": "classifying"})
        assert result.startswith("event: phase\n")
        assert "data: " in result
        assert result.endswith("\n\n")
        data_line = result.split("\n")[1]
        payload = json.loads(data_line.replace("data: ", ""))
        assert payload["phase"] == "classifying"


# ------------------------------------------------------------------
# SSE parsing helper
# ------------------------------------------------------------------

def _parse_sse(text: str) -> list:
    """Parse SSE text into a list of {event, data} dicts."""
    events = []
    current_event = None
    current_data = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            current_data = line[6:]
        elif line == "" and current_event is not None:
            events.append({"event": current_event, "data": current_data or ""})
            current_event = None
            current_data = None
    return events

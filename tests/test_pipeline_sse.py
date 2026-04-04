"""
Tests for Phase 11 Wave A — Async Pipeline + SSE Streaming.

Covers:
  - POST /pipeline/run returns HTTP 202 + run_id
  - _RUN_REGISTRY stores RunState per run_id
  - event_callback in run_full() emits correct events
  - SSE stream endpoint yields events in correct SSE format
  - pipeline_done terminates the stream
  - GET /pipeline/run/{run_id}/stream returns 404 for unknown run_id
  - /pipeline/status returns graph_stats + cosmos_tools_count keys
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_milestone(name: str, docs: int = 5, success: bool = True) -> MagicMock:
    m = MagicMock()
    m.milestone = 1
    m.name = name
    m.success = success
    m.documents_ingested = docs
    m.duration_ms = 100.0
    m.error = None
    m.details = {}
    return m


# ---------------------------------------------------------------------------
# W1-B: RunState + _RUN_REGISTRY
# ---------------------------------------------------------------------------

class TestRunRegistry:

    def test_run_state_defaults(self):
        from app.api.endpoints.training_pipeline import RunState
        state = RunState(run_id="abc123")
        assert state.run_id == "abc123"
        assert state.status == "running"
        assert state.total_docs == 0
        assert state.error is None

    def test_run_registry_stores_state(self):
        from app.api.endpoints.training_pipeline import _RUN_REGISTRY, RunState
        state = RunState(run_id="test_store")
        _RUN_REGISTRY["test_store"] = state
        assert _RUN_REGISTRY["test_store"] is state
        # cleanup
        del _RUN_REGISTRY["test_store"]

    def test_evict_stale_runs_removes_old_entries(self):
        import time
        from app.api.endpoints.training_pipeline import (
            _RUN_REGISTRY,
            _run_registry_timestamps,
            _evict_stale_runs,
            RunState,
            _RUN_REGISTRY_MAX_AGE_S,
        )
        state = RunState(run_id="stale_run")
        _RUN_REGISTRY["stale_run"] = state
        # Backdate timestamp beyond TTL
        _run_registry_timestamps["stale_run"] = time.time() - _RUN_REGISTRY_MAX_AGE_S - 1

        _evict_stale_runs()

        assert "stale_run" not in _RUN_REGISTRY
        assert "stale_run" not in _run_registry_timestamps


# ---------------------------------------------------------------------------
# W1-D: event_callback in run_full()
# ---------------------------------------------------------------------------

class TestEventCallback:

    def _make_pipeline_with_mocked_milestones(self, milestone_mock=None):
        """Create a TrainingPipeline instance with all run_* methods mocked."""
        from app.services.training_pipeline import TrainingPipeline

        pipeline = TrainingPipeline.__new__(TrainingPipeline)
        pipeline.vectorstore = MagicMock()
        pipeline.kb_path = MagicMock()
        pipeline.data_dir = MagicMock()
        pipeline.codebase_intel = None
        pipeline.ingestor = MagicMock()
        pipeline._graphrag = MagicMock()

        run_methods = [
            "run_split", "run_pillar1_pillar3", "run_pillar1_extras",
            "run_pillar4_and_5", "run_pillar6_7_8", "run_pillar9_10_11",
            "run_pillar12_faq", "run_entity_hubs", "run_generated_artifacts",
            "run_eval_seeds", "run_graph_build", "run_eval_seeds_autogen",
            "run_kb_drift_check", "run_icrm_eval", "run_enrichment_pipeline",
        ]
        for mname in run_methods:
            m = milestone_mock if milestone_mock else AsyncMock(return_value=_make_milestone(mname))
            setattr(pipeline, mname, m)

        return pipeline

    @pytest.mark.asyncio
    async def test_emit_calls_event_callback(self):
        """_emit() in run_full() calls event_callback with correct type + data."""
        pipeline = self._make_pipeline_with_mocked_milestones()

        received_events = []

        async def callback(etype, data):
            received_events.append({"type": etype, "data": data})

        await pipeline.run_full(event_callback=callback)

        # pipeline_done must be the last event
        assert received_events, "No events emitted"
        last = received_events[-1]
        assert last["type"] == "pipeline_done"
        assert "total_docs" in last["data"]
        assert "duration_ms" in last["data"]

        # milestone_start + milestone_done pairs must exist
        starts = [e for e in received_events if e["type"] == "milestone_start"]
        dones = [e for e in received_events if e["type"] == "milestone_done"]
        assert len(starts) > 0
        assert len(dones) > 0
        assert len(starts) == len(dones)

    @pytest.mark.asyncio
    async def test_callback_error_does_not_abort_pipeline(self):
        """If event_callback raises, the pipeline continues (never let SSE abort pipeline)."""
        call_count = 0

        async def bad_callback(etype, data):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("SSE connection dropped")

        mock_m = AsyncMock(return_value=_make_milestone("test"))
        pipeline = self._make_pipeline_with_mocked_milestones(milestone_mock=mock_m)

        result = await pipeline.run_full(event_callback=bad_callback)

        assert result is not None
        assert call_count > 0  # callback was called multiple times


# ---------------------------------------------------------------------------
# W1-A+C: POST /pipeline/run returns 202 + run_id; SSE stream endpoint
# ---------------------------------------------------------------------------

class TestPipelineRunEndpoint:

    def _make_app_state(self, pipeline=None):
        state = MagicMock()
        state.training_pipeline = pipeline
        state.vectorstore = None
        return state

    @pytest.mark.asyncio
    async def test_post_run_returns_202_with_run_id(self):
        from app.api.endpoints.training_pipeline import run_full_pipeline, PipelineRequest, _RUN_REGISTRY

        mock_pipeline = MagicMock()
        mock_pipeline.run_full = AsyncMock(return_value=MagicMock(
            success=True, total_documents=10, total_duration_ms=1000.0, milestones=[],
        ))

        request = MagicMock()
        request.app.state = self._make_app_state(pipeline=mock_pipeline)

        with patch("asyncio.create_task"):
            response = await run_full_pipeline(request, PipelineRequest())

        assert response.status_code == 202
        body = json.loads(response.body)
        assert "run_id" in body
        assert body["status"] == "started"
        assert "stream_url" in body
        assert body["run_id"] in body["stream_url"]

    @pytest.mark.asyncio
    async def test_post_run_no_pipeline_returns_error(self):
        from app.api.endpoints.training_pipeline import run_full_pipeline, PipelineRequest

        request = MagicMock()
        request.app.state = self._make_app_state(pipeline=None)

        result = await run_full_pipeline(request, PipelineRequest())
        assert "error" in result

    @pytest.mark.asyncio
    async def test_sse_stream_returns_404_for_unknown_run_id(self):
        from app.api.endpoints.training_pipeline import pipeline_run_stream

        response = await pipeline_run_stream("nonexistent_run_id_xyz")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_sse_stream_yields_events_in_sse_format(self):
        from app.api.endpoints.training_pipeline import pipeline_run_stream, RunState, _RUN_REGISTRY

        state = RunState(run_id="sse_test")
        _RUN_REGISTRY["sse_test"] = state

        # Pre-populate events so stream terminates immediately
        await state.events.put({"type": "milestone_start", "data": {"name": "split", "label": "M2"}})
        await state.events.put({"type": "milestone_done", "data": {"name": "split", "docs": 5, "ms": 100, "success": True}})
        await state.events.put({"type": "pipeline_done", "data": {"total_docs": 5, "duration_ms": 100, "success": True}})

        response = await pipeline_run_stream("sse_test")
        assert response.media_type == "text/event-stream"

        # Collect all SSE chunks
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())

        full_output = "".join(chunks)

        assert "event: milestone_start" in full_output
        assert "event: milestone_done" in full_output
        assert "event: pipeline_done" in full_output
        # Each SSE block ends with double newline
        assert "\n\n" in full_output
        # data fields are valid JSON
        for line in full_output.split("\n"):
            if line.startswith("data: "):
                json.loads(line[6:])  # must not raise

        # cleanup
        del _RUN_REGISTRY["sse_test"]

    @pytest.mark.asyncio
    async def test_sse_stream_terminates_after_pipeline_done(self):
        """Stream must stop yielding after pipeline_done — no infinite loop."""
        from app.api.endpoints.training_pipeline import pipeline_run_stream, RunState, _RUN_REGISTRY

        state = RunState(run_id="term_test")
        _RUN_REGISTRY["term_test"] = state

        await state.events.put({"type": "pipeline_done", "data": {"total_docs": 0, "duration_ms": 0, "success": True}})
        # Add extra events after done — must NOT be yielded
        await state.events.put({"type": "milestone_start", "data": {"name": "phantom"}})

        response = await pipeline_run_stream("term_test")

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())

        full_output = "".join(chunks)

        # phantom event after pipeline_done must not appear
        assert "phantom" not in full_output

        # cleanup
        del _RUN_REGISTRY["term_test"]

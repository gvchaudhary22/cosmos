# CHANGELOG

## [v3.1.1] — 2026-04-05 — M3 Phase 1: UAT Verification + Phase 2 Prep

### Fixed
- `tests/test_action_approval.py` — 10 propose/consume tests called async methods without `await` after Phase 2 refactored `ActionApprovalGate.propose()` / `consume()` to `async`. Added `@pytest.mark.asyncio` + `await` to all affected tests.

### Added (Phase 2 prep — wired but not yet active in production paths)
- `app/brain/action_approval.py` — generic write action registry (`_WRITE_ACTION_REGISTRY`) with COD toggle + SRF feature entries; `detect_write_action()` replaces hardcoded keyword checks; async `propose()` / `consume()` with DB-backed persistence (best-effort); `WriteActionSignal` datatype
- `app/services/tool_executor.py` — `feature_cod_toggle`, `feature_srf_enable`, `analytics_ndr_count` tool registrations in fallback registry (approval_mode=manual on write tools)
- `docs/PHASE-1-UAT.md` — formal UAT report: 1057 tests pass, all M3-P1 in-scope criteria verified

### Docs
- `docs/PHASE-1-UAT.md` — UAT verdict: PASS

---

## [v3.1] — 2026-04-04 — M3 Phase 1: Agentic ICRM Copilot (Write Actions + Streaming)

### Added

#### Write Action Approval Gate (#22)
- `app/brain/action_approval.py` — `ActionApprovalGate` + `ActionProposal` (single-use tokens, 5-min TTL, replay-safe)
- `HybridChatRequest` — added `confirm_action: bool` + `confirm_token: Optional[str]` fields
- SSE streaming endpoint: `approval_required` event when cancel intent detected; `action_executing` / `action_executed` on confirm
- Session ownership validation on token consume (prevents cross-session token reuse)
- KB: `mcapi.v1.orders.cancel.post/high.yaml` — added `soft_required_context` (asks for order IDs) + `action_contract` block
- 23 new tests in `tests/test_action_approval.py`

#### SSE True Progressive Streaming (#20)
- `LLMClient._stream_anthropic()` — `messages.stream()` → `stream.text_stream` for token-level streaming
- `RIPEREngine.stream_final_response()` — async generator yielding `{event: "chunk", text: ...}` per token
- 4 bugs fixed in `hybrid_chat_stream()`:
  - B1: `classification` NameError in streaming scope
  - B2: `_merge_context` was not awaited
  - B3: `ParamClarificationEngine` not called on streaming path
  - B4: `request_classification` not populated before RIPER call
- 8 new tests in `tests/test_streaming_sse.py`

#### ParamClarificationEngine enhancements (#21 — NDR domain)
- `param_clarifier.py`: `_should_skip()` now checks `company_id` direct argument (was only checking session_context)
- `param_clarifier.py`: `_param_present()` now detects AWB from query text via regex
- 3 NDR `high.yaml` files enriched with `soft_required_context`:
  - `mcapi.v1.admin.ndr.senddemomessage.post`
  - `mcapi.v1.admin.ndr.get_call_center_recording.by_id.get`
  - `mcapi.v1.admin.ndr.upload_priority.post`
- 10 new tests in `tests/test_ndr_soft_required.py`

#### icrm-action-agent (forge)
- `.cosmos/extensions/agents/icrm-action-agent.md` — userland agent for write action domain knowledge
- `rocketmind.registry.json` — registered as agent #12

#### KB + Graph enrichment (#11, Neo4j sync)
- `app/services/query_orchestrator.py` — enrichment tier system (Wave 3/4 improvements)
- `scripts/enrich_p3_apis_batch.py` — new flags: `--soft-context-only`, `--enriched-only`, `--prefix`, `--api-ids`
- `scripts/audit_neo4j_sync.py` — new audit script: gap detection + sync for MySQL → Neo4j
- `app/services/neo4j_graph.py` — credentials fix (was hardcoded `"password"` at import time)
- `app/services/kb_ingestor.py` — agent→skill edge structured metadata fix

### Fixed
- Neo4j credential hardcode bug (`neo4j_graph.py` read from `settings` instead of module-level literal)
- Streaming path `_merge_context` not awaited (caused context to be a coroutine object)
- Streaming path `classification` NameError before RIPER call
- ParamClarifier `_should_skip` missed `company_id` passed as direct API argument

### Tests
- Total: **1057 passing** (was 1016 at M2 ship)
- New test files: `test_action_approval.py` (23), `test_streaming_sse.py` (8), `test_ndr_soft_required.py` (10), `test_param_clarifier.py`

### Docs
- `docs/PHASE-1-PLAN.md` — M3 Phase 1 plan
- `docs/PHASE-1-ISSUE22-UAT.md` — UAT report for issue #22 (21/21 PASS)
- `app.state.action_approval_gate` — initialized at startup via `main.py`

---

## [v2.1] — M2 Phase 6 (reference)

- ParamClarificationEngine (#18) — 13 tests
- ICRM token persistence / MARS→COSMOS wiring (#19)
- cosmos_tools seeded (27 tools from P11 YAMLs)
- KB file index: 34,481 indexed
- Neo4j synced: 11,083 nodes / 48,185 edges / 20,793 lookups

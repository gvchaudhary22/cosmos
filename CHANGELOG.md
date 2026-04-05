# CHANGELOG

## [v3.2] — 2026-04-05 — M3 Phase 2: Feature Flags + Analytics + Entity Extractor

### Added

#### Write Action: Seller Feature Flags (#23)
- `_WRITE_ACTION_REGISTRY` — generic write action detection: `feature_cod_toggle` + `feature_srf_enable` entries with entity_id patterns, keywords, intent patterns
- `detect_write_action()` — KB entity_id → intent → keyword priority; replaces hardcoded `is_cancel_order_intent()` in streaming path
- `WriteActionSignal` dataclass — carries `tool_name`, `action_input`, `summary`, `risk_level`, `entity_id`
- `hybrid_chat.py` — uses `detect_write_action()` generically; all feature flag + cancel order intents trigger `approval_required` SSE
- KB YAMLs enriched with `action_contract` + `soft_required_context`:
  - `mcapi.v1.admin.sellers.enablepartialcodtoggle.by_company_id.post/high.yaml`
  - `mcapi.v1.admin.sellers.srf_feature_enable.by_company_id.post/high.yaml`
- `_FALLBACK_TOOLS` — `feature_cod_toggle`, `feature_srf_enable` registered (approval_mode=manual, risk_level=high/medium)

#### Analytics: Live Counts (#24)
- `PipelineName.ANALYTICS` — new Stage 1 probe pipeline enum value
- `_probe_analytics()` — fires on count/total/how-many keywords + company_id; uses EntityExtractor for date range; maps metric keywords to tool names
- Analytics SSE probe in `hybrid_chat.py` — yields `{"type": "analytics", metric, value, label, from_date, to_date}` before LLM chunk; graceful degradation on API failure
- `_FALLBACK_TOOLS` — `analytics_ndr_count`, `analytics_shipment_count`, `analytics_order_count` registered (GET endpoints, approval_mode=auto)

#### Entity Extractor — Date Resolution
- `app/brain/entity_extractor.py` (new) — `EntityExtractor.extract(query)` resolves 9 date expressions to IST ISO from/to dates
  - Expressions: today, yesterday, this week, last week, last 7 days, last 30 days, last N days, this month, last month
  - Always IST (Asia/Kolkata, UTC+5:30) — no hardcoded dates
  - Also extracts: `company_id`, `awb`, `order_ids` via regex
- `ExtractedEntities` dataclass — typed container with `has_date_range()` / `has_company()` helpers

#### DB-Backed Approval Token Persistence
- `app/main.py` — injects `ApprovalRepository(AsyncSessionLocal)` into `ActionApprovalGate` at startup
- `propose()` / `consume()` — async; persist to `icrm_action_approvals` (best-effort); UUID v4 tokens for DB FK compatibility
- `consume()` L2 DB fallback — post-restart token recovery with `update_status` called before returning proposal
- DB update failure now logged as WARNING (was silently swallowed)

### Fixed
- CRITICAL security: `consume()` DB update failure was silent (`pass`) — token could be re-consumed post-restart via L2 DB path; now logs `action_approval.db_consumed_failed` warning
- Resource leak: analytics executor `aclose()` moved to `finally` block — always closes even if execute() raises
- `hybrid_chat.py` — `await gate.consume()` and `await gate.propose()` properly awaited
- `_summary_text` in confirm path now uses `proposal.summary` (generic) instead of hardcoded "Order cancellation executed successfully."

### Tests
- Total: **1089 passing** (was 1057 at v3.1 ship, +32)
- New: `tests/test_entity_extractor.py` (18 tests — all 9 date expressions, company/AWB extraction, helpers)
- Updated: `tests/test_action_approval.py` (37 tests — +14 feature flag + WriteActionSignal + UUID format tests)

### Docs
- `docs/PHASE-2-PLAN.md` — M3 Phase 2 full wave plan
- `docs/PHASE-2-UAT.md` — UAT report (52/52 PASS)

---

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

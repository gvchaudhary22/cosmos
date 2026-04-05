# PHASE-2-PLAN.md — M3: Feature Flags + Analytics + KB Completion

> Created: 2026-04-04
> Milestone: M3 — Agentic ICRM Copilot
> Builds on: Phase 1 (ActionApprovalGate, SSE streaming, cancel order, NDR clarifier)
> Status: `planning_complete`

---

## Phase Goal

**Complete the ICRM write action surface**: seller feature flags (COD, SRF) with the approval gate, live analytics with natural-language date resolution, and KB enrichment completion for remaining domains (shipments, billing, returns) — so COSMOS can answer every common ICRM operator query with grounded, live data.

---

## Actual State Coming In (Revised from Phase 1 ship)

| Component | Status |
|-----------|--------|
| `ActionApprovalGate` — cancel order | ✅ DONE (#22) |
| SSE streaming — 4 bugs fixed | ✅ DONE (#20) |
| NDR `soft_required_context` + clarifier | ✅ DONE (#21) |
| Feature flag write action (#23) | ❌ PENDING — APIs exist in KB, not yet wired |
| Analytics routing (#24) | ❌ PENDING — no analytics probe in orchestrator |
| `app/brain/entity_extractor.py` | ❌ MISSING — date resolution not built |
| KB enrichment: shipments domain | ❌ PENDING |
| KB enrichment: billing domain | ❌ PENDING |
| KB enrichment: returns domain | ❌ PENDING |
| Eval benchmark recall@5 | ❌ NOT RUN since enrichment updates |
| Token storage: `icrm_action_approvals` DB | ❌ In-memory only — Phase 1 gap, Phase 2 completes it |

---

## Scope

### IN — Phase 2

| Issue | Feature | Wave |
|-------|---------|------|
| #23 | Feature flag write action — COD toggle + SRF feature enable | W1 |
| — | ActionApprovalGate: generic write action detection (refactor) | W1 |
| — | ActionApprovalGate: DB-backed token persistence (close Phase 1 gap) | W1 |
| #24 | Analytics intent routing — live NDR/shipment/order counts | W2 |
| W3-B | `app/brain/entity_extractor.py` — date entity resolution | W2 |
| #11 | KB enrichment: shipments domain `soft_required_context` | W3 |
| #11 | KB enrichment: billing domain `soft_required_context` | W3 |
| #11 | KB enrichment: returns domain `soft_required_context` | W3 |
| #4 | Run eval benchmark → confirm recall@5 ≥ 0.85 | W4 |
| — | Re-embed updated KB files after enrichment | W4 |

### OUT — Phase 2

- Bulk actions ("cancel all pending orders for company X") — Phase 3
- Multi-company analytics dashboard (top 10 by NDR rate) — Phase 3
- Proactive alerts ("NDR rate 23% — above threshold") — Phase 3
- Seller self-service chat (non-admin users) — Phase 3
- LIME UI changes for `approval_required` event — LIME team's scope
- #2 Neo4j cross-pillar edges — separate issue, tracked independently
- #5 Create-order KB enrichment — lower priority, tracked independently

---

## Architecture: Generic Write Action Detection

Phase 1 wired cancel-order detection as a hardcoded keyword check. Phase 2 generalizes this
so adding a new write action requires only a KB YAML change, not a code change.

### Current (Phase 1 — hardcoded)
```python
# In hybrid_chat.py generate() — checks only cancel order
if ActionApprovalGate.is_cancel_order_intent(query, intents, chunks):
    _gate.propose(session_id, "orders_cancel", ...)
```

### Target (Phase 2 — KB-driven, generic)
```python
# In hybrid_chat.py generate() — reads action_contract from any KB chunk
write_action = ActionApprovalGate.detect_write_action(query, intents, chunks)
if write_action:
    _gate.propose(session_id, write_action.tool_name, write_action.action_input, ...)
```

### `WriteActionSignal` returned by `detect_write_action()`
```python
@dataclass
class WriteActionSignal:
    tool_name: str           # "orders_cancel", "feature_cod_toggle"
    action_input: Dict       # extracted params {ids: [...], company_id: ...}
    summary: str             # human-readable for approval card
    risk_level: str          # from KB action_contract.risk_level
    entity_id: str           # KB entity_id of the matched API
```

### Detection priority (first match wins)
1. **KB chunk `action_contract.type == "write"` + similarity ≥ 0.75** — most reliable
2. **Intent list contains known write action strings** — fallback
3. **Keyword sets per action** — last resort

This means any new write action = edit `high.yaml` with `action_contract`, re-embed — no code change.

---

## Architecture: Analytics with Date Resolution

```
"how many NDRs for company 25149 this week?"
  ↓
Stage 1 Probe: PipelineName.ANALYTICS fires (new probe)
  → EntityExtractor.extract(query) → {company_id: "25149", from: "2026-03-31", to: "2026-04-04"}
  → Tool: analytics_ndr_count
  → GET /api/v1/admin/ndr?client_id=25149&from=2026-03-31&to=2026-04-04 (via icrm_token)
  ↓
AnalyticsResult(value=47, label="NDRs this week", comparison={last_week: 32, trend: "up"})
  ↓
SSE: {"type": "analytics", "metric": "ndr_count", "value": 47, ...}
SSE: chunk "Company 25149 has 47 NDRs this week (up from 32 last week, +47%)."
```

### Date Entity Extraction (`app/brain/entity_extractor.py`)
```python
class EntityExtractor:
    def extract(self, query: str) -> ExtractedEntities:
        """
        Returns:
          company_id: str | None  — "25149" from "company 25149"
          from_date: str | None   — ISO "2026-03-31" from "this week"
          to_date: str | None     — ISO "2026-04-04" from "this week"
          awb: str | None
          order_id: int | None
        """
```

Date expressions to handle:
| Expression | Resolves to (from today 2026-04-04) |
|-----------|--------------------------------------|
| "today" | from=2026-04-04, to=2026-04-04 |
| "yesterday" | from=2026-04-03, to=2026-04-03 |
| "this week" | from=2026-03-30 (Mon), to=2026-04-04 |
| "last week" | from=2026-03-23, to=2026-03-29 |
| "last 7 days" | from=2026-03-28, to=2026-04-04 |
| "this month" | from=2026-04-01, to=2026-04-04 |
| "last month" | from=2026-03-01, to=2026-03-31 |
| "last 30 days" | from=2026-03-05, to=2026-04-04 |

Timezone: Asia/Kolkata (IST, UTC+5:30) — all date boundaries computed in IST.

### Analytics Probe (new `PipelineName.ANALYTICS`)
- Fires when: query contains count/how many/total/stats keywords + company_id detected
- Tool execution: calls MCAPI via `icrm_token`
- Falls back to KB text if API call fails (graceful degradation)
- Yields SSE `analytics` event before `chunk` events

---

## Architecture: DB-Backed Approval Token Persistence

Phase 1 stores tokens in `ActionApprovalGate._pending` (in-memory dict).
Phase 2 persists them in `icrm_action_approvals` table via existing `ApprovalRepository`.

```python
# Phase 2 ActionApprovalGate uses DB for persistence:
class ActionApprovalGate:
    def __init__(self, approval_repo: Optional[ApprovalRepository] = None):
        self._repo = approval_repo  # None → fallback to in-memory
        self._pending: Dict[str, ActionProposal] = {}  # fallback + cache

    async def propose(self, ...) -> ActionProposal:
        proposal = ActionProposal(...)
        self._pending[token] = proposal
        if self._repo:
            await self._repo.create({
                "id": token,
                "action_type": action_type,
                "risk_level": risk_level,
                "metadata": {"action_input": action_input, "summary": summary, "session_id": session_id},
            })
        return proposal

    async def consume(self, token: str) -> Optional[ActionProposal]:
        # Check memory first, then DB
        ...
```

---

## Wave-Structured Task List

### Wave 1 — Feature Flags + Gate Generalization (parallel)

#### W1-A: ActionApprovalGate — generic write action detection
- **File**: `app/brain/action_approval.py`
- **Change**: Add `WriteActionSignal` dataclass + `detect_write_action(query, intents, chunks)` static method
  - Reads `action_contract.type == "write"` from KB chunks (from `chunk.get("action_contract")` or `chunk.get("metadata", {}).get("action_contract")`)
  - Fallback: per-action keyword sets for cancel, feature toggle
  - Returns `WriteActionSignal | None`
- **Change**: Remove `is_cancel_order_intent()` from streaming endpoint; replace with generic `detect_write_action()`
- **Acceptance**: Existing cancel tests still pass; new feature flag test passes with same detection path

#### W1-B: ActionApprovalGate — DB token persistence (close Phase 1 gap)
- **File**: `app/brain/action_approval.py`
- **Change**: `propose()` and `consume()` become `async def`; persist to `icrm_action_approvals` via `ApprovalRepository` when available
- **File**: `app/main.py` — inject `ApprovalRepository` into `ActionApprovalGate` at startup
- **File**: `app/api/endpoints/hybrid_chat.py` — `await gate.propose(...)` / `await gate.consume()`
- **Acceptance**: On server restart, pending tokens from DB are still valid

#### W1-C: Write action — seller feature flags — Issue #23
- **KB**: Enrich 2 APIs with `action_contract` blocks:
  - `mcapi.v1.admin.sellers.enablepartialcodtoggle.by_company_id.post/high.yaml`
  - `mcapi.v1.admin.sellers.srf_feature_enable.by_company_id.post/high.yaml`
  - Add `soft_required_context`: ask for `company_id` and `enabled` flag
  - Add `action_contract.type: write`, `risk_level: high`, `side_effects`, `confirm_prompt`
- **Tool executor**: Register `feature_cod_toggle` and `feature_srf_enable` tools in `_FALLBACK_TOOLS` list
- **Intent keywords**: "disable COD", "enable COD", "toggle COD", "enable feature", "disable prepaid", "SRF feature"
- **Acceptance**:
  - "disable COD for company 25149" → `approval_required` event + `action_input: {company_id: 25149, enabled: false}`
  - "enable SRF feature for seller 25149" → `approval_required` event
  - Confirm → `action_executed` with API result

---

### Wave 2 — Analytics + Date Entity Extraction (parallel)

#### W2-A: `app/brain/entity_extractor.py` — date entity resolution
- **File**: `app/brain/entity_extractor.py` (NEW)
- **Class**: `EntityExtractor`
  - `extract(query: str) -> ExtractedEntities` (sync, pure function)
  - Resolves all 8 date expressions to ISO from/to dates in IST timezone
  - Extracts: `company_id`, `from_date`, `to_date`, `awb`, `order_id`
  - Uses `datetime.now(timezone(timedelta(hours=5, minutes=30)))` for IST
- **Acceptance**: All 8 date expressions resolve correctly in unit tests; no hardcoded dates; timezone-aware (IST)

#### W2-B: Analytics probe + routing — Issue #24
- **File**: `app/services/query_orchestrator.py`
  - Add `PipelineName.ANALYTICS` to the enum
  - Add `_probe_analytics(query, session_context)` method:
    - Fires when query matches analytics keywords (count, how many, total, stats) + company_id detected
    - Calls `EntityExtractor.extract(query)` → gets company_id + date range
    - Dispatches to appropriate analytics tool via `ToolExecutorService`
    - Returns `AnalyticsProbeResult(value, metric, comparison, label)`
  - Wire into `_stage1_parallel_probe()` as a new parallel leg
- **Tool definitions** (add to `_FALLBACK_TOOLS` in `tool_executor.py`):
  - `analytics_ndr_count` → `GET /api/v1/admin/ndr` with `client_id`, `from`, `to`
  - `analytics_shipment_count` → `GET /api/v1/admin/shipments` with `client_id`, `from`, `to`
  - `analytics_order_count` → `GET /api/v1/admin/orders` with `client_id`, `from`, `to`
- **SSE event**: `yield _sse("analytics", {metric, value, label, comparison})`
- **Graceful degradation**: if API call fails, fall through to KB text answer
- **Acceptance**:
  - "how many NDRs for company 25149 this week?" → `analytics` SSE event + live count in chunk
  - "total orders for 25149 last 7 days" → `analytics` SSE event with `order_count`
  - "how many shipments last month for company 12345?" → correct from/to dates

---

### Wave 3 — KB Enrichment Completion (sequential per domain)

#### W3-A: Shipments domain enrichment — Issue #11
- **Script**: `python3 scripts/enrich_p3_apis_batch.py --apply --domain shipments --soft-context-only --force-update --enriched-only --workers 2`
- **Target**: All enriched shipment APIs get `soft_required_context` asking for AWB, company_id, date range as appropriate
- **Count**: ~200 shipment APIs expected (estimate)
- **Acceptance**: 0 enriched shipment APIs with empty `soft_required_context`

#### W3-B: Billing domain enrichment — Issue #11
- **Script**: `python3 scripts/enrich_p3_apis_batch.py --apply --domain billing --soft-context-only --force-update --enriched-only --workers 2`
- **Target**: Billing APIs requiring company_id or date range get `soft_required_context`
- **Acceptance**: All enriched billing APIs have `soft_required_context` populated

#### W3-C: Returns domain enrichment — Issue #11
- **Script**: `python3 scripts/enrich_p3_apis_batch.py --apply --domain returns --soft-context-only --force-update --enriched-only --workers 2`
- **Acceptance**: All enriched returns APIs have `soft_required_context` populated

#### W3-D: Close #11 — re-embed all updated domains
- **Command**: `curl -X POST http://localhost:10001/cosmos/api/v1/pipeline/schema -H "Content-Type: application/json" -d '{"repo_id": "MultiChannel_API"}'`
- **Acceptance**: Pipeline run shows updated vector count; content-hash diff shows updated docs

---

### Wave 4 — Verification + Ship Gate (sequential)

| Step | Action | Target |
|------|--------|--------|
| 1 | `python3 -m pytest tests/ -x -q` | ≥ 1080 tests (23 new for #23, 15 new for analytics, 12 new for entity_extractor) |
| 2 | `POST /pipeline/eval` — recall@5 benchmark | ≥ 0.85 (save EVAL-REPORT.md) |
| 3 | Security review: DB-backed tokens — no token from session A usable in session B | zero CRITICAL |
| 4 | E2E #23: "disable COD 25149" → approval → confirm → API called | pass |
| 5 | E2E #24: "how many NDRs this week for 25149?" → live count | pass |
| 6 | Re-embed KB: verify Qdrant count increases after enrichment | count > 22,464 |
| 7 | Update STATE.md, tag `v3.2` | done |

---

## Acceptance Criteria (Phase 2 Ship Gate)

| Criterion | Target | Test |
|-----------|--------|------|
| Feature flag intent → `approval_required` SSE | Fires on "disable COD 25149" | `test_feature_flag_approval` |
| Feature flag confirm → API executed | COD toggle API called | integration test |
| Generic write action detection (KB-driven) | Same behavior for cancel + feature flags | `test_generic_write_action` |
| DB-backed tokens survive restart | Token from pre-restart still valid | `test_token_persistence` |
| "yesterday" → correct ISO from/to | from=2026-04-03 to=2026-04-03 | `test_date_entity_yesterday` |
| "this week" → correct Mon→today | from=2026-03-30 to=2026-04-04 | `test_date_entity_this_week` |
| "last 7 days" → correct rolling window | from=2026-03-28 to=2026-04-04 | `test_date_entity_last_7_days` |
| IST timezone used | Monday = Asia/Kolkata boundary | `test_date_entity_timezone` |
| Analytics probe fires for count query | `PipelineName.ANALYTICS` in probe results | `test_analytics_probe` |
| Live NDR count returned in SSE `analytics` event | Numeric value, metric label, trend | `test_analytics_ndr_sse` |
| Graceful degradation on API failure | Falls back to KB text | `test_analytics_api_failure` |
| All tests pass | ≥ 1080 | `pytest -x -q` |
| recall@5 | ≥ 0.85 | `POST /pipeline/eval` |
| Shipments/billing/returns enrichment | 0 enriched APIs with empty soft_required_context | script run |

---

## Dependencies

| Dependency | Required For | Status |
|------------|-------------|--------|
| `ActionApprovalGate` (Phase 1) | W1-A generic refactor | ✅ Done |
| `ApprovalRepository` DB | W1-B token persistence | ✅ Exists in repositories.py |
| Feature flag API YAMLs in KB | W1-C | ✅ Found: COD toggle + SRF enable |
| `_FALLBACK_TOOLS` list in tool_executor | W1-C tool registration | ✅ Extensible |
| `ToolExecutorService` | W2-B analytics | ✅ Exists |
| `icrm_token` on chat request | W2-B live API calls | ✅ Done (Phase 1 wired) |
| enrichment script flags | W3-A/B/C | ✅ Done (Phase 1) |
| Qdrant running | W3-D re-embed | ✅ Up at port 6333 |
| LIME renders `analytics` SSE event | W2-B UX | ⚠️ LIME team — same contract as PHASE-1-PLAN.md |

---

## Risk Register

| Risk | Probability | Mitigation |
|------|------------|-----------|
| DB-backed `async propose/consume` breaks streaming generator | HIGH | Keep in-memory fallback; test both paths; never make `consume` awaitable in the SSE gen without try/except |
| COD toggle API path differs from what KB says | MEDIUM | Read actual controller source + high.yaml before coding; fallback to mock in tests |
| Analytics API requires different auth than assumed | MEDIUM | Use `icrm_token` for all /admin/* analytics; test with live MCAPI sandbox before wiring |
| Date extraction gets timezone wrong | LOW | Compute IST explicitly via `timedelta(hours=5, minutes=30)`; unit test all 8 expressions |
| Enrichment script overwrites correct `soft_required_context` | LOW | Use `--force-update` flag only on empty/missing fields; audit before/after counts |

---

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| Generic `detect_write_action()` instead of per-action methods | KB-driven: add new write action by editing YAML only |
| Analytics as Stage 1 probe (not RIPER path) | Live counts need to bypass text generation; probe fires before LLM |
| IST timezone for all dates | Shiprocket is India-only; IST is the operational timezone |
| DB-backed tokens for Phase 2 | Single server restart = all pending approvals lost in Phase 1; DB fixes this |
| `ApprovalRepository` reuse (no new table) | `icrm_action_approvals` already exists and maps perfectly to `ActionProposal` fields |
| Keep in-memory dict as L1 cache | DB lookup adds ~5ms; in-memory is instant for hot path |

---

## Issue Mapping

| Issue | Title | Wave | Status after Phase 2 |
|-------|-------|------|----------------------|
| [#23](https://github.com/gvchaudhary22/cosmos/issues/23) | Feature flag write action (COD, SRF) | W1 | ✅ Closes |
| [#24](https://github.com/gvchaudhary22/cosmos/issues/24) | Analytics live NDR/shipment counts | W2 | ✅ Closes |
| [#11](https://github.com/gvchaudhary22/cosmos/issues/11) | KB enrichment: all remaining domains | W3 | ✅ Closes (shipments+billing+returns done) |
| [#4](https://github.com/gvchaudhary22/cosmos/issues/4) | Eval benchmark recall@5 ≥ 0.85 | W4 | ✅ Closes |

---

## What Phase 3 Unlocks (Preview)

Once Phase 2 ships:
- **Bulk actions**: "cancel all pending orders for company 25149" — needs batch tool
- **Proactive alerts**: "company 25149 NDR rate is 23% — above threshold" — needs cron/monitor
- **Multi-company dashboard**: top 10 by NDR rate — analytics aggregation
- **Seller self-service**: sellers check their own status (role-gated queries)
- **#2** Neo4j cross-pillar edges — PPR quality improvement
- **#5** Complete create-order KB enrichment — coverage gap

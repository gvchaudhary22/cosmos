# PHASE-1-PLAN.md — M3: Write Actions + Streaming + Feature Control

> Created: 2026-04-04 | **Re-planned: 2026-04-04** (ground-truth audit — Waves 1+2 verified done)
> Milestone: M3 — Agentic ICRM Copilot
> Status: `in_progress` — Waves 1+2 complete, Wave 3 pending

---

## Phase Goal

**From read-only copilot to action-taking agent**: ICRM operators can cancel orders, enable/disable
seller features, and get live analytics — all via natural language chat.
Every write action requires explicit user confirmation before COSMOS executes.
Streaming responses render progressively in LIME like ChatGPT.

---

## Ground-Truth State (Re-planned 2026-04-04)

| Component | Status | Evidence |
|-----------|--------|----------|
| SSE streaming (`/hybrid/chat/stream`) | ✅ DONE | Full wave-by-wave + token streaming via `riper.stream_final_response()` |
| ParamClarificationEngine wired in stream | ✅ DONE | `hybrid_chat.py:1063` — clarification SSE event emitted |
| `ActionApprovalGate` | ✅ DONE | `app/brain/action_approval.py` — 206 lines, single-use token, TTL |
| Cancel order approval flow | ✅ DONE | `hybrid_chat.py:1099` — wired; confirm path at line 901 |
| soft_required_context: orders 657/657 | ✅ DONE | Re-embedded in run 9151516d |
| soft_required_context: admin 80/80 | ✅ DONE | Re-embedded in run 9151516d |
| soft_required_context: NDR | ✅ DONE | 81 NDR files confirmed |
| Security fixes C1–C5, H1 | ✅ DONE | Token logging + raw exc + hardcoded creds fixed |
| Feature flag approval (#23) | ❌ PENDING | Not in ActionApprovalGate or hybrid_chat |
| Analytics routing (#24) | ❌ PENDING | No analytics branch in orchestrator |
| `app/brain/entity_extractor.py` | ❌ MISSING | Date resolution not built |
| Tests | ✅ 1055 passing | Up from 1024 at M2 ship |

---

## Remaining Work — Wave 3 Only

### W3-A: Write action — seller feature flags — Issue #23

**What's needed:**

1. Extend `ActionApprovalGate` to detect feature-flag intents:
   - "disable COD for 25149", "enable prepaid seller 25149", "block pickup for this company"
   - Add `is_feature_flag_intent(message, intents, chunks)` method (mirrors `is_cancel_order_intent`)
   - Extract: `feature` (cod/prepaid/pickup), `action` (enable/disable), `company_id`

2. Wire detection in `hybrid_chat.py` (after cancel-order check, same pattern):
   ```python
   elif ActionApprovalGate.is_feature_flag_intent(chat_req.message, orch_result.intents, _kc_for_gate):
       _proposal = _gate.propose(action_type="update_seller_feature", ...)
       yield _sse("approval_required", {...})
       return
   ```

3. Execution path: on `confirm_action=True`:
   - POST `/api/v1/admin/sellers/features` via `icrm_token` with `{company_id, feature, enabled}`

**Files:**
- `app/brain/action_approval.py` — add feature-flag methods
- `app/api/endpoints/hybrid_chat.py` — wire detection + execution

**Acceptance:** "disable COD for 25149" → `approval_required` SSE → confirm → feature flag API called → streamed result

---

### W3-B: Analytics intent routing — Issue #24

**What's needed:**

Add analytics routing branch in `app/services/query_orchestrator.py` (or directly in `hybrid_chat.py`):
- Detect intents: `analytics_ndr_count`, `analytics_shipment_count`, `analytics_order_count`
- Extract company_id + date range (relative → absolute)
- Call live API: `GET /api/v1/admin/ndr?client_id=X&from=Y&to=Z` via `icrm_token`
- Emit SSE: `{"type":"analytics","metric":"ndr_count","value":47,...}`

**Response format:** "Company 25149 has 47 NDRs this week (32 last week, ↑47%)"

**Files:**
- `app/brain/entity_extractor.py` (new) — date resolution
- `app/services/query_orchestrator.py` or `hybrid_chat.py` — analytics routing

**Acceptance:** "how many NDRs for company 25149 this week?" → live count from API

---

### W3-C: Date entity extractor

**File:** `app/brain/entity_extractor.py` (new)

Resolve relative dates → absolute `from/to` params before API call:
- "yesterday" → `from=2026-04-03&to=2026-04-03`
- "this week" → `from=2026-03-28&to=2026-04-04` (Mon–today)
- "last 7 days" → `from=2026-03-28&to=2026-04-04`
- "last month" → `from=2026-03-01&to=2026-03-31`

Always use `Asia/Kolkata` timezone. No hardcoded dates.

**Acceptance:** All 4 expressions resolve correctly in unit tests.

---

### Wave 4 — Verification + Ship Gate (sequential, after Wave 3)

| Step | Action | Target |
|------|--------|--------|
| 1 | `python3 -m pytest tests/ -x -q` | ≥ 1080 tests |
| 2 | Security review: feature flag approval token replay impossible | 0 regressions |
| 3 | E2E: "disable COD 25149" → approval → confirm → API called → streamed | Integration test |
| 4 | E2E: "how many NDRs for 25149 this week?" → live count | Integration test |
| 5 | Update STATE.md, tag `v2.1` | Committed |

---

## Acceptance Criteria (Phase 1 Ship Gate)

| Criterion | Target | Status |
|-----------|--------|--------|
| SSE chunks arrive progressively | First chunk < 500ms | ✅ Done |
| Existing `/hybrid/chat` unbroken | Same JSON as before | ✅ Done |
| "cancel order X" → approval card | `approval_required` SSE | ✅ Done |
| Confirmed cancel → API executes | Order status changes | ✅ Done |
| Replayed confirm_token rejected | 4xx error | ✅ Done (TTL + pop()) |
| "disable COD 25149" → approval | Approval event | ❌ Pending W3-A |
| "how many NDRs this week" → live count | Numeric answer | ❌ Pending W3-B |
| "yesterday" date resolved | Correct from/to | ❌ Pending W3-C |
| All tests pass | ≥ 1080 | ❌ Currently 1055 |

---

## Architecture: Feature Flag Flow (W3-A target)

```
User: "disable COD for seller 25149"
  ↓
hybrid_chat_stream → ActionApprovalGate.is_feature_flag_intent() → True
  ↓
propose(action_type="update_seller_feature", params={feature:"cod", enabled:false, company_id:25149})
  ↓
SSE: {"type":"approval_required", "proposal": {
    "description": "Disable COD for seller 25149",
    "side_effects": ["Prevents all future COD orders for this seller"],
    "risk_level": "high",
    "reversible": true,
    "confirm_token": "tok_xyz"
}}
  ↓
User confirms → POST /api/v1/admin/sellers/features {company_id:25149, feature:"cod", enabled:false}
  ↓
SSE: {"type":"chunk","text":"COD disabled for seller 25149. Change takes effect immediately."}
```

---

## Architecture: Analytics Flow (W3-B target)

```
User: "how many NDRs for company 25149 this week?"
  ↓
entity_extractor.py: "this week" → from=2026-03-28, to=2026-04-04
  ↓
analytics routing: intent=analytics_ndr_count, company_id=25149
  ↓
GET /api/v1/admin/ndr?client_id=25149&from=2026-03-28&to=2026-04-04  (via icrm_token)
  ↓
SSE: {"type":"analytics","metric":"ndr_count","value":47,"comparison":{...}}
SSE: {"type":"chunk","text":"Company 25149 has 47 NDRs this week (32 last week, ↑47%)."}
```

---

## Risk Register

| Risk | Probability | Mitigation |
|------|------------|-----------|
| Feature flag API endpoint path unknown | MEDIUM | Check mcapi.py client for existing feature flag method; probe KB |
| Analytics API returns paginated results | LOW | Use `count` endpoint or `total` field from first page only |
| Date edge case: "last month" on 1st of month | LOW | Use calendar.monthrange for last-month boundary; test explicitly |

---

## Dependencies

| Dependency | Required For | Status |
|------------|-------------|--------|
| ActionApprovalGate (cancel) | Feature flag reuse | ✅ Done |
| ICRM token in session_context | Analytics + feature flag API calls | ✅ Done |
| entity_extractor.py | Analytics date resolution | ❌ W3-C must precede W3-B |

---

## What Phase 2 Unlocks (Preview)

- Bulk actions: "cancel all pending orders for company 25149"
- Multi-company analytics dashboard: top 10 by NDR rate
- Proactive alerts: "company 25149 NDR rate is 23% — above threshold"
- Seller self-service: sellers check their own status (not just admins)

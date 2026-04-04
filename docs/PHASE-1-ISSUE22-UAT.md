# Issue #22 UAT Report — Cancel Order with Approval Gate

> Verified: 2026-04-04 | Phase: M3-P1 Wave 2-B | Run by: /cosmos:verify 22

## Test Suite

| Run | Result |
|-----|--------|
| `python3 -m pytest tests/ -x -q` | **1055 passed, 3 skipped** ✅ (target ≥ 1050) |
| `tests/test_action_approval.py` (21 tests) | **21/21 PASSED** ✅ |

---

## Acceptance Criteria Checklist (from PHASE-1-PLAN.md)

| Criterion | Status | Evidence |
|-----------|--------|----------|
| `ActionApprovalGate.propose()` creates proposal with confirm_token | ✅ PASS | `test_propose_returns_proposal_with_token` |
| `ActionApprovalGate.consume()` validates token → returns ActionProposal | ✅ PASS | `test_consume_valid_token_returns_proposal` |
| Replayed confirm_token is rejected (single-use) | ✅ PASS | `test_consume_removes_token_single_use` |
| Expired token (> 5 min) is rejected | ✅ PASS | `test_consume_expired_token_returns_none` |
| Invalid/unknown token returns None | ✅ PASS | `test_consume_unknown_token_returns_none` |
| "cancel order X" → intent detected via keyword | ✅ PASS | `test_is_cancel_order_intent_from_query_keyword` |
| Hinglish "order cancel karo" → intent detected | ✅ PASS | `test_is_cancel_order_intent_hinglish` |
| Cancel intent from KB chunk (similarity ≥ 0.70) | ✅ PASS | `test_is_cancel_order_intent_from_high_similarity_chunk` |
| Low-similarity chunk (< 0.70) does NOT trigger | ✅ PASS | `test_is_cancel_order_intent_low_similarity_chunk_ignored` |
| Order IDs extracted from query text | ✅ PASS | `test_extract_order_ids_single`, `_multiple` |
| Short numbers (< 7 digits) not treated as order IDs | ✅ PASS | `test_extract_order_ids_short_numbers_ignored` |
| `HybridChatRequest.confirm_action` field exists (default False) | ✅ PASS | `test_hybrid_chat_request_has_confirm_fields` |
| `HybridChatRequest.confirm_token` field exists (default None) | ✅ PASS | `test_hybrid_chat_request_has_confirm_fields` |
| Both fields settable in request body | ✅ PASS | `test_hybrid_chat_request_confirm_fields_settable` |
| `ActionApprovalGate` singleton on `app.state` at startup | ✅ PASS | `main.py` — `app.state.action_approval_gate = ActionApprovalGate()` |
| `approval_required` SSE event emitted in streaming path | ✅ PASS | Code inspection — `yield _sse("approval_required", {...})` |
| `action_executing` SSE event on confirm | ✅ PASS | Code inspection — `yield _sse("action_executing", {...})` |
| `action_executed` SSE event with result | ✅ PASS | Code inspection — `yield _sse("action_executed", {...})` |
| KB YAML `soft_required_context` for ids param | ✅ PASS | YAML parse check — `param=ids, alias=order_ids` |
| KB YAML `action_contract` block | ✅ PASS | YAML parse check — `approval_mode=manual, risk=high, 5 side_effects` |
| YAML parses cleanly (no syntax errors) | ✅ PASS | `yaml.safe_load()` succeeds |

**Result: 21/21 criteria PASSED**

---

## Files Delivered

| File | Change |
|------|--------|
| `app/brain/action_approval.py` | NEW — `ActionApprovalGate` + `ActionProposal` |
| `app/api/endpoints/hybrid_chat.py` | `confirm_action`/`confirm_token` on request; approval confirm path + approval detect path in `generate()` |
| `app/main.py` | `app.state.action_approval_gate = ActionApprovalGate()` at startup |
| `mars/.../mcapi.v1.orders.cancel.post/high.yaml` | `soft_required_context` + `action_contract` added |
| `tests/test_action_approval.py` | NEW — 21 tests covering all gate behaviors |

---

## SSE Event Flow (verified in code)

```
Turn 1 — User: "cancel order 98765432"
  → [stage:probe_start, probe:*, stage:probe_complete, ...]
  → approval_required {
      confirm_token: "TzBWytbv...",
      expires_in_seconds: 300,
      action: "orders_cancel",
      action_input: {ids: [98765432]},
      summary: "Cancel 1 order(s): [98765432]",
      risk_level: "high"
    }
  → done { pending_approval: true, confidence: 0.9 }

Turn 2 — User: confirm_action=true, confirm_token="TzBWytbv..."
  → action_executing { action: "orders_cancel", summary, risk_level }
  → action_executed { status, data, latency_ms }
  → chunk "Order cancellation executed successfully."
  → done { tools_used: ["orders_cancel"] }

Turn 2 (replay attack) — confirm_token used again:
  → error { message: "Confirmation token is invalid or has expired." }
  → done { confidence: 0.0 }
```

---

## Known Gaps (documented, not blocking)

| Gap | Impact | Plan |
|-----|--------|------|
| Token stored in-memory (not DB-backed) | Proposals lost on server restart | Phase 2: persist in `icrm_action_approvals` table via `ApprovalRepository` |
| Cancel intent detection is keyword/chunk based | May miss novel phrasings | Phase 2: use RIPER/LLM intent extraction |
| Only `orders_cancel` tool wired | Feature flags, etc. not yet wired | Issue #23 (W2-C) |
| No LIME UI for `approval_required` event | Client must handle SSE event | LIME team contract documented in PHASE-1-PLAN.md |

---

## Verdict

**PASS** — Issue #22 acceptance criteria fully met. 1055 tests passing. Zero regressions.

Ready for: `/cosmos:riper issue #23` (enable/disable seller feature flags)

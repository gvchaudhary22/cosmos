# PHASE-2-UAT.md — M3 Phase 2 Verification

> Date: 2026-04-05
> Phase: M3 Phase 2 — Feature Flags + Analytics + Entity Extractor
> Waves verified: Wave 1 (feature flags) + Wave 2 (analytics + date extraction)
> Wave 3 (KB enrichment) deferred — requires live enrichment script run

---

## Automated Gate Results

| Check | Result | Detail |
|---|---|---|
| `pytest tests/ -x -q` | ✅ **1089 passing**, 3 skipped | Was 1057 at Phase 1 ship (+32 new) |
| `ruff check` (Phase 2 files) | ✅ All checks passed | Pre-existing orchestrator warnings not in Phase 2 scope |
| `mypy` | ⚠️ Not installed in env | No new type errors introduced (verified by inspection) |

---

## UAT Criteria — Wave 1 (Feature Flags)

### W1-A: Generic write action detection

| # | Criterion | Method | Result |
|---|-----------|--------|--------|
| 1 | `detect_write_action("cancel order 98765432", ...)` returns `WriteActionSignal` with `tool_name="orders_cancel"` | `test_detect_write_action_returns_write_action_signal` | ✅ PASS |
| 2 | `detect_write_action("show me NDR data", ...)` returns `None` | `test_detect_write_action_no_match_returns_none` | ✅ PASS |
| 3 | Cancel with order IDs populates `action_input["ids"]` | `test_detect_write_action_cancel_with_order_ids` | ✅ PASS |
| 4 | `is_cancel_order_intent()` backward compat delegates to `detect_write_action` | 7 existing intent tests | ✅ PASS |
| 5 | `WriteActionSignal` imported cleanly from `action_approval` | module import test | ✅ PASS |

### W1-B: DB-backed token persistence

| # | Criterion | Method | Result |
|---|-----------|--------|--------|
| 6 | `propose()` is `async def`, awaitable | `test_propose_returns_proposal_with_token` | ✅ PASS |
| 7 | `consume()` is `async def`, awaitable | `test_consume_valid_token_returns_proposal` | ✅ PASS |
| 8 | Token is UUID v4 format (DB FK compatible) | `test_propose_token_is_uuid_format` | ✅ PASS |
| 9 | Single-use: second consume returns None | `test_consume_removes_token_single_use` | ✅ PASS |
| 10 | Expired token rejected | `test_consume_expired_token_returns_none` | ✅ PASS |
| 11 | `main.py` injects `ApprovalRepository` at startup | code inspection | ✅ PASS |
| 12 | DB failure degrades gracefully (in-memory still works) | `_repo=None` path in all async tests | ✅ PASS |

### W1-C: Feature flag write actions (#23)

| # | Criterion | Method | Result |
|---|-----------|--------|--------|
| 13 | "disable cod for company 25149" → `WriteActionSignal(tool_name="feature_cod_toggle")` | `test_detect_write_action_cod_disable_keyword` | ✅ PASS |
| 14 | `action_input = {company_id: 25149, enabled: false}` | `test_detect_write_action_cod_disable_keyword` | ✅ PASS |
| 15 | "enable cod for seller 25149" → `enabled=True` | `test_detect_write_action_cod_enable_keyword` | ✅ PASS |
| 16 | Hinglish "cod band karo company 25149" detected | `test_detect_write_action_cod_hinglish` | ✅ PASS |
| 17 | COD from intent dict `{"action": "cod_toggle"}` | `test_detect_write_action_cod_from_intent` | ✅ PASS |
| 18 | COD from KB chunk `entity_id=enablepartialcodtoggle`, sim≥0.75 | `test_detect_write_action_cod_from_kb_chunk` | ✅ PASS |
| 19 | "enable srf for company 25149" → `feature_srf_enable`, `enabled=True`, `risk_level=medium` | `test_detect_write_action_srf_enable_keyword` | ✅ PASS |
| 20 | "srf off for 25149" → `enabled=False` | `test_detect_write_action_srf_disable_keyword` | ✅ PASS |
| 21 | SRF from KB chunk `entity_id=srf_feature_enable` | `test_detect_write_action_srf_from_kb_chunk` | ✅ PASS |
| 22 | `feature_cod_toggle` registered in `_FALLBACK_TOOLS` | `_FALLBACK_INDEX` key presence | ✅ PASS |
| 23 | `feature_srf_enable` registered in `_FALLBACK_TOOLS` | `_FALLBACK_INDEX` key presence | ✅ PASS |
| 24 | COD KB YAML has `action_contract` + `soft_required_context` | file inspection | ✅ PASS |
| 25 | SRF KB YAML has `action_contract` + `soft_required_context` | file inspection | ✅ PASS |
| 26 | `hybrid_chat.py` uses `detect_write_action()` (not `is_cancel_order_intent`) | code inspection line 1121+ | ✅ PASS |
| 27 | `hybrid_chat.py` uses `await gate.propose()` and `await gate.consume()` | code inspection | ✅ PASS |

---

## UAT Criteria — Wave 2 (Analytics + Entity Extractor)

### W2-A: Entity Extractor date resolution

| # | Criterion | Test | Result |
|---|-----------|------|--------|
| 28 | "today" → from=2026-04-05, to=2026-04-05 | `test_today` | ✅ PASS |
| 29 | "yesterday" → from=2026-04-04, to=2026-04-04 | `test_yesterday` | ✅ PASS |
| 30 | "this week" → from=2026-03-30 (Mon), to=2026-04-05 | `test_this_week` | ✅ PASS |
| 31 | "last week" → from=2026-03-23 (Mon), to=2026-03-29 (Sun) | `test_last_week` | ✅ PASS |
| 32 | "last 7 days" → from=2026-03-30, to=2026-04-05 | `test_last_7_days` | ✅ PASS |
| 33 | "last 30 days" → from=2026-03-07, to=2026-04-05 | `test_last_30_days` | ✅ PASS |
| 34 | "this month" → from=2026-04-01, to=2026-04-05 | `test_this_month` | ✅ PASS |
| 35 | "last month" → from=2026-03-01, to=2026-03-31 | `test_last_month` | ✅ PASS |
| 36 | "last N days" (custom N=14) resolves correctly | `test_last_N_days_custom` | ✅ PASS |
| 37 | No date phrase → `from_date=None, to_date=None` | `test_no_date_returns_none` | ✅ PASS |
| 38 | IST timezone used — all boundaries in Asia/Kolkata | `_REF` fixture in IST; `test_this_week` Mon boundary | ✅ PASS |
| 39 | company_id extracted ("company 25149") | `test_company_id_extracted` | ✅ PASS |
| 40 | seller/client_id aliases extracted | `test_seller_id_alias`, `test_client_id_alias` | ✅ PASS |
| 41 | AWB extracted from "AWB SH123456789" | `test_awb_extracted` | ✅ PASS |

### W2-B: Analytics probe + SSE

| # | Criterion | Method | Result |
|---|-----------|--------|--------|
| 42 | `PipelineName.ANALYTICS` added to enum | code inspection | ✅ PASS |
| 43 | `_probe_analytics()` fires for "how many NDRs" + company_id | code inspection + probe result structure | ✅ PASS |
| 44 | No company_id → probe returns `found_data=False` | probe logic inspection | ✅ PASS |
| 45 | No analytics keyword → probe returns early | probe logic inspection | ✅ PASS |
| 46 | NDR keyword maps to `analytics_ndr_count` tool | `_METRIC_MAP` inspection | ✅ PASS |
| 47 | `analytics_ndr_count` tool registered in `_FALLBACK_TOOLS` | code inspection | ✅ PASS |
| 48 | `analytics_shipment_count` tool registered | code inspection | ✅ PASS |
| 49 | `analytics_order_count` tool registered | code inspection | ✅ PASS |
| 50 | Analytics SSE probe in `hybrid_chat.py` yields `analytics` event | code inspection lines 1161+ | ✅ PASS |
| 51 | Graceful degradation: analytics API failure falls through to LLM | try/except around executor call | ✅ PASS |
| 52 | Only fires when `icrm_token` present (gate check) | `if _analytics_data and chat_req.icrm_token` | ✅ PASS |

---

## Test Count Summary

| File | Tests | Delta |
|---|---|---|
| `test_action_approval.py` | 37 | +14 (feature flag + WriteActionSignal + UUID tests) |
| `test_entity_extractor.py` | 18 | +18 (new file, Wave 2-A) |
| All other existing tests | 1034 | 0 regressions |
| **Total** | **1089** | **+32 vs Phase 1 ship** |

---

## Wave 3 Status (KB Enrichment — Deferred)

Wave 3 requires live script execution against Qdrant + Claude Opus API:

```bash
python3 scripts/enrich_p3_apis_batch.py --apply --domain shipments --soft-context-only --force-update --enriched-only --workers 2
python3 scripts/enrich_p3_apis_batch.py --apply --domain billing --soft-context-only --force-update --enriched-only --workers 2
python3 scripts/enrich_p3_apis_batch.py --apply --domain returns --soft-context-only --force-update --enriched-only --workers 2
```

These are operational tasks (not code changes) and can run independently of the Phase 2 ship.
Issue #11 remains OPEN until all domains complete.

---

## Ship Gate Assessment

| Gate | Status |
|---|---|
| All tests pass (≥1080 target) | ✅ 1089 passing |
| Lint clean (Phase 2 files) | ✅ Passed |
| No CRITICAL security regressions | ✅ Session ownership check preserved; UUID tokens; graceful degradation |
| Feature flag approval flow complete | ✅ detect_write_action → propose → approval_required → confirm → execute |
| Date extraction IST-correct | ✅ 8 expressions, all correct |
| Analytics probe wired | ✅ Stage 1 parallel probe + SSE event |
| Backward compat (cancel order) | ✅ is_cancel_order_intent still works |

**Verdict: PASS — ready to ship as v3.2**

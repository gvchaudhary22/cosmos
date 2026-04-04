# PHASE-6-PLAN.md — ChatGPT-Like ICRM Copilot

> Created: 2026-04-04
> Milestone: M2 — Retrieval Quality & Live Execution
> Goal: ICRM operators get a ChatGPT-like copilot that answers questions, detects missing parameters, asks targeted follow-ups, and executes API calls with their SSO token.

---

## Phase Goal

Transform COSMOS from a KB lookup tool into a **ChatGPT-like ICRM copilot** where:
1. User asks naturally → COSMOS understands intent + identifies the right API
2. If required context is missing → COSMOS asks ONE targeted question (not generic escalation)
3. User answers → COSMOS re-executes with the provided params
4. Response streams progressively in LIME (like ChatGPT)
5. Token is generated from SSO login and persisted in DB for session reuse

---

## Current Issues (Discovered During Planning)

| # | Issue | Severity | Root Cause |
|---|-------|----------|-----------|
| I-1 | `client_id` not recognized as practically-required | HIGH | `required_params: []` in admin/shipments high.yaml — all params declared optional, but without `client_id` the API returns empty results |
| I-2 | No multi-turn clarification loop | HIGH | ReAct escalates at confidence 0.3-0.5 instead of asking targeted follow-up and re-executing |
| I-3 | SSO tokens not persisted to DB | MEDIUM | `SSOAuthClient` caches in-memory only — server restart loses all tokens |
| I-4 | `/chat/mcp` may not use SSE streaming endpoint | MEDIUM | LIME page fetches sessions but streaming integration with `/hybrid/chat/stream` not verified |
| I-5 | Company_id not linked from JWT on session create | MEDIUM | `icrm_sessions.company_id` is nullable — admin JWT has `cid: 1` but this isn't extracted on session creation |
| I-6 | `admin/shipments` KB says `required: []` but business logic requires `client_id` for useful results | HIGH | Documentation gap — need "soft_required" param concept |

---

## API Enrichment Status: `GET /api/v1/admin/shipments`

**Entity ID**: `mcapi.v1.admin.shipments.get`
**KB Location**: `MultiChannel_API/pillar_3_api_mcp_tools/apis/mcapi.v1.admin.shipments.get/`
**Status**: ✅ ENRICHED (Claude Opus 4.6 deep enrichment from ShipmentController.php:259-740)

**Key params from the curl you shared:**

| Param | Type | In KB? | Notes |
|-------|------|--------|-------|
| `client_id` | int | ✓ optional | Maps to `company_id` — WITHOUT this, query returns all shipments or empty set |
| `status` | int | ✓ optional | Shiprocket status code (1=NEW, 6=SHIPPED, etc.) |
| `from` / `to` | date | ✓ optional | Date range filter |
| `awb` | string | ✓ optional | Max 10 comma-separated AWBs |
| `per_page` | int | ✓ optional | Pagination (default 15) |
| `type` | string | ✓ optional | `sr` = standard, `DIRECT` = ownkey AWB |
| `courier_id` | int | ✓ optional | Requires `client_id` when used (validated in controller) |

**Gap (Issue I-1 + I-6)**: The KB documents `client_id` as optional, but the controller behavior is:
- With `client_id`: returns that company's shipments ✓
- Without `client_id`: returns ALL shipments (massive query) or filtered empty set for non-super-admins

**Fix**: Add `soft_required_context` field to high.yaml for behavioral params.

---

## Architecture Design

### The ChatGPT-Like Flow

```
User (LIME /chat/mcp) → types: "show me shipments for company 25149 from yesterday"
  ↓
COSMOS receives via POST /hybrid/chat/stream (SSE)
  ↓
[Stage 1: Intelligence]
  Intent: admin_shipments_list (confidence: 0.95)
  Entities: company_id=25149, date=yesterday→2026-04-03
  API match: mcapi.v1.admin.shipments.get
  ↓
[Stage 2: Param Check] ← NEW COMPONENT
  Required params: client_id (soft_required), from, to
  Present: client_id=25149 ✓, from/to=computed ✓
  Missing: status (not required) → no clarification needed
  ↓
[Stage 3: Token Resolve] ← ENHANCED
  Session has user_id → lookup icrm_tokens table → token valid? → use it
  If expired → refresh via SSOAuthClient → update DB
  ↓
[Stage 4: Execute]
  GET /api/v1/admin/shipments?client_id=25149&from=2026-04-03&to=2026-04-03&per_page=15
  → Returns JSON with shipment list
  ↓
[Stage 5: Stream Response]
  → SSE chunks → LIME renders progressively
  → Final: "Found 47 shipments for company 25149 on 2026-04-03. Here are the top results: [table]"
```

### The Clarification Flow (when params missing)

**This is NOT API-specific.** Any query — whether fetching shipments, cancelling an order, checking NDR status, or asking about a seller's settings — can trigger clarification if required context is missing.

```
User types: "show me shipments for today"
  ↓
Intent: admin_shipments_list ✓
Entities extracted: date=today ✓
Param check: client_id = MISSING (soft_required for this intent)
  ↓
COSMOS emits clarification (NOT escalation):
  "Which company's shipments? Provide company ID (client_id).
   Example: 'company 25149' or 'seller gaurav@example.com'"
  ↓
User responds: "company 25149"
  ↓
entity_extracted = company_id:25149 → resume → returns results ✓
```

```
User types: "NDR status dikhao"
  ↓
Intent: ndr_list ✓ | Entity: date=today assumed
Param check: company_id = MISSING
  ↓
COSMOS: "Kaunse seller ke NDRs? Company ID batao."
  ↓
User: "25149"  →  COSMOS executes ndr list for company 25149 ✓
```

```
User types: "how many orders processed today"  ← general query, no action
  ↓
Intent: analytics/lookup ✓ | Entity: date=today
No tool execution needed — KB answers with stats context
COSMOS streams answer from Qdrant KB + Opus 4.6 synthesis
No clarification needed ✓
```

**Key rule**: Clarification only triggers when:
1. A tool/API execution is the right action, AND
2. A `soft_required` param for that intent is missing
General knowledge questions (how does X work, what is NDR, explain COD flow) NEVER trigger clarification — answered directly from KB.

---

## Scope

### IN
- **Issue I-1/I-6**: Add `soft_required_context` concept to KB + param checker reads it
- **Issue I-2**: Build `ParamClarificationEngine` — detect missing soft-required params → targeted question → re-execute on answer
- **Issue I-3**: Add `icrm_tokens` table → `SSOAuthClient` persists tokens to DB (not just memory)
- **Issue I-4**: Verify/fix LIME `/chat/mcp` uses SSE streaming endpoint
- **Issue I-5**: Extract `cid` from JWT on session create → populate `company_id` in session
- **Admin Shipments API**: Enrich high.yaml with `soft_required_context` for `client_id`

### OUT
- Building new LIME components (LIME is a separate repo — provide API contract only)
- Full multi-repo SSO (one flow for ICRM admin first)
- Auto-approval UI for write actions (M3)
- Rate limiting per user (M3)

---

## Wave-Structured Task List

### Wave 1 — KB Enrichment (parallel)

#### W1-A: Add `soft_required_context` to admin/shipments high.yaml
- **File**: `mars/knowledge_base/.../mcapi.v1.admin.shipments.get/high.yaml`
- **Add field**:
  ```yaml
  soft_required_context:
    - param: client_id
      alias: company_id
      reason: "Without client_id, API returns empty or all-company results. Always ask for company context."
      ask_if_missing: "Which company's shipments? Provide company ID (client_id)."
      example_values: ["25149", "12345"]
  ```
- **Acceptance**: `grep soft_required_context high.yaml` returns match

#### W1-B: Add `soft_required_context` to other major admin list APIs
- **Files**: `mcapi.v1.admin.orders.get/high.yaml`, `mcapi.v1.admin.ndr.get/high.yaml`
- **Pattern**: Same structure as W1-A
- **Acceptance**: 3+ admin APIs have `soft_required_context`

---

### Wave 2 — Param Clarification Engine (sequential after W1)

#### W2-A: Build `ParamClarificationEngine`
- **File**: `app/brain/param_clarifier.py` (new)
- **Inputs**: `intent`, `api_entity_id`, `extracted_params`, `session_state`
- **Logic**:
  1. Load `high.yaml` for the matched API entity
  2. Read `soft_required_context` list
  3. For each `soft_required` param → check if present in extracted_params
  4. If missing → return `ClarificationRequest(question=ask_if_missing, pending_param=param)`
  5. If all present → return `None` (proceed to execution)
- **Session storage**: `session_state.pending_clarification = {param, question, api_id, current_params}`
- **Acceptance**: Unit test: `test_clarifier_returns_question_when_company_missing()`

#### W2-B: Wire clarification into ReAct loop
- **File**: `app/engine/react.py`
- **Where**: Between intent classification and tool execution
- **Logic**:
  ```python
  clarification = await param_clarifier.check(intent, api_id, extracted_params, session)
  if clarification:
      session.pending_clarification = clarification
      return clarification.question  # Stream this as the response
  ```
- **On next turn**: If `session.pending_clarification` exists:
  - Extract param value from user message
  - Merge into `extracted_params`
  - Clear `pending_clarification`
  - Re-execute
- **Acceptance**: Integration test: 2-turn conversation where turn 1 asks for company_id, turn 2 provides it and gets shipment results

---

### Wave 3 — Token Persistence (parallel with Wave 2)

#### W3-A: Add `icrm_tokens` table to DB migration
- **File**: `app/db/migrations/001_initial.sql`
- **SQL**:
  ```sql
  CREATE TABLE IF NOT EXISTS icrm_tokens (
    id VARCHAR(36) PRIMARY KEY DEFAULT (UUID()),
    user_id VARCHAR(255) NOT NULL,
    company_id VARCHAR(255) NOT NULL DEFAULT '1',
    token_type ENUM('icrm_admin', 'seller') NOT NULL DEFAULT 'icrm_admin',
    bearer_token TEXT NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_icrm_tokens_user (user_id),
    INDEX idx_icrm_tokens_expiry (expires_at),
    UNIQUE KEY uk_user_type (user_id, company_id, token_type)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
  ```
- **Acceptance**: `SHOW TABLES` shows `icrm_tokens`

#### W3-B: Update `SSOAuthClient` to read/write DB tokens
- **File**: `app/clients/sso_auth.py`
- **Logic**:
  1. Before generating new token → check `icrm_tokens` DB for valid (non-expired) entry
  2. If valid DB token → use it (skip SSO call)
  3. If missing or expired → generate via SSO → save to `icrm_tokens` + update in-memory cache
  4. `is_valid()`: `expires_at > now() + 5min` (5-min buffer)
- **Acceptance**: Test: token survives simulated server restart (read from DB on cold start)

#### W3-C: Extract `company_id` from JWT on session create
- **File**: `app/api/endpoints/chat.py` or `hybrid_chat.py`
- **Logic**: When session is created, if `authorization` header present:
  - Decode JWT (no verification needed — just read `cid` claim)
  - Set `session.company_id = jwt_cid` if not already set
- **Acceptance**: Admin user JWT with `cid: 1` → `icrm_sessions.company_id = "1"`

---

### Wave 4 — LIME Integration Verification

#### W4-A: Verify `/chat/mcp` uses SSE streaming endpoint
- **Check**: Does `lime/src/app/chat/mcp/page.tsx` call `/hybrid/chat/stream`?
- **Expected**: `EventSource` or `fetch` with streaming for message send
- **If missing**: Provide LIME team the exact SSE API contract to implement

#### W4-B: Document SSE event contract for LIME
- **File**: `docs/LIME-CHAT-SSE-CONTRACT.md`
- **Content**: Every event type, payload shape, rendering guidance
- **Events to document**:
  - `stage:probe_start` → show "searching..." indicator
  - `chunk` → append to message bubble (streaming text)
  - `clarification` → render as a focused question card (not generic chat bubble)
  - `tool_result` → render inline result table/card
  - `done` → hide loading indicator, show citations

---

## Acceptance Criteria (Phase 6 Ship Gate)

| Criterion | Target | Verification |
|-----------|--------|-------------|
| Shipments query with company_id | Returns actual data | `POST /hybrid/chat/stream` with "show shipments for company 25149 today" |
| Shipments query without company_id | COSMOS asks targeted question | Clarification response in < 2s |
| 2-turn clarification → execution | Resolves to API call | Integration test `test_clarification_loop` |
| Token survives restart | DB token found on cold start | Restart COSMOS, re-query |
| SSE chunks received | Progressive rendering works | `curl --no-buffer POST /hybrid/chat/stream` shows `chunk` events |
| `soft_required_context` in KB | 3+ admin APIs enriched | `grep -r soft_required_context knowledge_base/` |
| Tests passing | 994+ | `pytest -x -q` |

---

## Risk Register

| Risk | Probability | Mitigation |
|------|------------|-----------|
| SSO token generation requires ICRM prod credentials | HIGH | Use test account for development; token flow is the same |
| Multi-turn state is lost on parallel requests | MEDIUM | `session_state` is already keyed by session_id; ensure DB-backed session for multi-server |
| LIME `/chat/mcp` has different API contract | MEDIUM | Document SSE contract first (W4-B), LIME team implements |
| `soft_required_context` breaks existing KB readers | LOW | It's an additive field — `kb_ingestor.py` ignores unknown fields |
| `company_id` decode from JWT vs explicit param conflict | LOW | Explicit `company_id` in request always wins over JWT-decoded |

---

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| `soft_required_context` as KB field (not code) | KB-driven — behavior changes by editing YAML, not deploying code. Future APIs self-document their behavioral requirements. |
| `icrm_tokens` as separate table (not in icrm_sessions) | Multiple token types per user (icrm_admin + seller), separate lifecycle management, easier TTL queries |
| Clarification as session state (not new endpoint) | Multi-turn is natural conversation — same session_id flows through. LIME doesn't need to know it's a clarification vs answer. |
| JWT decode without verification | COSMOS trusts MARS to authenticate. Decoding only reads `cid` claim for company_id context — MARS already verified the token. |
| Opus 4.6 for all response generation | "I can compromise with cost but not the response" — Opus always for all LLM responses |

---

## How It Connects to Existing Architecture

```
LIME (/chat/mcp)
  POST /hybrid/chat/stream  (SSE)
  → Headers: Authorization: Bearer <icrm_jwt>
  ↓
COSMOS hybrid_chat.py
  1. Decode JWT → company_id (W3-C)
  2. Classify intent → API match (existing)
  3. ParamClarificationEngine.check() → targeted Q if missing (W2-A, W2-B)
  4. If clarification needed → stream question, save pending to session
  5. Next turn → extract answer → merge params → proceed
  6. SSOAuthClient.get_token(user_id, company_id) → DB first, SSO if needed (W3-B)
  7. MCAPIClient.call(endpoint, params, token) → real API call
  8. Stream response chunks (Opus 4.6) → LIME renders
```

---

## M2 Preview — What Comes After Phase 6

- Phase 7: Write actions (cancel order, update address) with approval flow in LIME
- Phase 8: Enable/disable feature flags via admin APIs (Seller feature management)
- Phase 9: Analytics dashboard — "how many NDRs this week for company X?"

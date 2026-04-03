# COSMOS Knowledge Base Upgrade Plan

## Vision
Transform COSMOS from a template-based KB (generic API descriptions) to a **source-code-derived KB** where every API, business rule, and workflow is documented from reading the actual PHP/Go source code. Claude Opus 4.6 reads every controller, model, config file, and validation class — no scripts, no guessing.

## Principle: Quality > Speed > Cost
- **Every API enriched by Claude Opus 4.6** reading actual source code
- **Every claim traceable** to source file + line number
- **Every validation rule** from the actual FormRequest class
- **Every side effect** documented (jobs, events, listeners)

---

## Phase Map (14 Phases across ~15 Sessions)

### PHASE 1: Orders Module ✅ DONE
**Session**: 1 (completed April 2, 2026)
**Controller**: `OrderController.php` (15,633 lines) + 5 related controllers
**APIs Enriched**: 41 from deep source code reading
**Also Created**: 6 agents, 4 skills, 4 tools, 16 business rules

### PHASE 2: Shipments + NDR Module 🔄 IN PROGRESS
**Session**: 1-2
**Controllers**:
- `ShipmentController.php` (6,748 lines) — shipment CRUD, NDR management
- `TrackingController.php` (3,259 lines) — tracking page, NDR forms
- `AssignAwbController.php` (10,386 lines) — AWB assignment, parallel AWBs
- `Admin/ShipmentController.php` — admin shipment operations
- `SecuredShipmentController.php` — secured shipments
**FormRequests**: `AssignRequest`, `WrapperRequest`, `UpdateShipmentStatusRequest`
**Target**: ~40 APIs enriched
**Also Create**:
- `pillar_2_business_rules/shipments_rules.yaml`
- `pillar_2_business_rules/ndr_rules.yaml`
- `pillar_10_skills/awb_tracking.yaml`
- `pillar_10_skills/ndr_resolution.yaml`
- `pillar_11_tools/shipment_track.yaml`
- `pillar_11_tools/ndr_action.yaml`

### PHASE 3: Courier Module
**Session**: 2-3
**Controllers**:
- `Courier/AssignAwbController.php` (10,386 lines) — the beast
- `Courier/CourierServiceabilityController.php` — serviceability check
- `Courier/CourierRateController.php` — rate cards
- `Courier/CourierController.php` — courier management
- `app/Couriers/CourierFactory.php` — 100+ courier partner implementations
**FormRequests**: `Courier/ShipmentLabelRequest`, `Courier/AssignRequest`
**Target**: ~40 APIs enriched
**Also Create**:
- `pillar_2_business_rules/courier_rules.yaml`
- `pillar_9_agents/courier_ops_agent.yaml` (enhance existing)
- `pillar_10_skills/courier_assignment.yaml`
- `pillar_11_tools/assign_awb.yaml`
- `pillar_11_tools/serviceability_check.yaml`

### PHASE 4: Billing + Wallet Module
**Session**: 3-4
**Controllers**:
- `Billing/BillingController.php` — freight billing, invoices
- `Billing/WalletController.php` — wallet balance, recharge, transactions
- `Billing/WeightDisputeController.php` — weight discrepancy management
- `Billing/CodRemittanceController.php` — COD collection and remittance
- `AwbRefund/AwbRefundController.php` — AWB refund processing
**Config Files**: `config/wallet.php`, `config/refundFreight.php`
**Target**: ~30 APIs enriched
**Also Create**:
- `pillar_2_business_rules/billing_rules.yaml`
- `pillar_2_business_rules/wallet_rules.yaml`
- `pillar_2_business_rules/cod_rules.yaml`
- `pillar_10_skills/wallet_check.yaml`
- `pillar_10_skills/weight_dispute.yaml`
- `pillar_10_skills/refund_processing.yaml`
- `pillar_11_tools/wallet_balance.yaml`
- `pillar_11_tools/billing_query.yaml`

### PHASE 5: Settings + Auth Module
**Session**: 4-5
**Controllers**:
- `Settings/CompanyController.php` — company profile, KYC
- `Settings/PlanController.php` — subscription plans
- `Settings/PickupController.php` — pickup address management
- `Auth/AuthController.php` — login, register, token
- `Auth/OTPController.php` — OTP verification
**FormRequests**: `Settings/KYCRequest`, `Settings/BankValidationRequest`, `Auth/LoginRequest`
**Config Files**: `config/otp_config.php`, `config/settings_modules.php`
**Target**: ~30 APIs enriched
**Also Create**:
- `pillar_2_business_rules/settings_rules.yaml`
- `pillar_2_business_rules/auth_rules.yaml`
- `pillar_10_skills/kyc_verification.yaml`
- `pillar_11_tools/seller_info.yaml`
- `pillar_11_tools/seller_plan.yaml`

### PHASE 6: Admin + Reports Module
**Session**: 5-6
**Controllers**:
- `Admin/AdminController.php` — admin operations
- `Admin/ShipmentController.php` — admin shipment management
- `Reports/Admin/OrderReportController.php` — order reports
- `Reports/Admin/ShipmentsController.php` — shipment reports
- `Cxo/CxoController.php` — CXO dashboard
**Target**: ~40 APIs enriched (admin + report endpoints)
**Also Create**:
- `pillar_9_agents/admin_agent.yaml` (enhance)
- `pillar_9_agents/analytics_agent.yaml`

### PHASE 7: Returns + Exchange Module
**Session**: 6-7
**Controllers**:
- `Orders/ReturnOrderController.php` — return processing
- `Exchange/ExchangeController.php` — exchange orders
- `Support/ReturnOrdersController.php` — support return handling
**FormRequests**: `Orders/ReturnRequest`, `Exchange/ExchangeBulkOrderRequest`
**Config**: `config/return_reasons.php`
**Target**: ~25 APIs enriched
**Also Create**:
- `pillar_2_business_rules/returns_rules.yaml`
- `pillar_10_skills/return_processing.yaml`
- `pillar_9_agents/return_exchange_agent.yaml`

### PHASE 8: Channels + Other Modules
**Session**: 7-8
**Controllers**:
- `Channels/ChannelController.php` — channel integration
- `Channels/ShopifyController.php` — Shopify-specific
- `Channels/AmazonController.php` — Amazon-specific
- `Dashboard/OrdersController.php` — dashboard
- `Dashboard/ShipmentsController.php` — dashboard
- `Hyperlocal/` — hyperlocal delivery
- `Insurance/` — shipment insurance
- `VAS/` — value added services
- `Quick/` — Shiprocket Quick
- `International/` — international shipping
**Target**: ~40 APIs enriched
**Also Create**:
- `pillar_2_business_rules/channel_rules.yaml`
- `pillar_9_agents/channel_sync_agent.yaml`
- `pillar_9_agents/international_ship_agent.yaml`

### PHASE 9: Business Rules (All Domains)
**Session**: 8
**Source**: All 134 `config/*.php` files
**Read each config file** and extract business rules into:
```
pillar_2_business_rules/
  orders_rules.yaml         ← DONE
  shipments_rules.yaml      ← Phase 2
  courier_rules.yaml        ← Phase 3
  billing_rules.yaml        ← Phase 4
  wallet_rules.yaml         ← Phase 4
  cod_rules.yaml            ← Phase 4
  settings_rules.yaml       ← Phase 5
  auth_rules.yaml           ← Phase 5
  returns_rules.yaml        ← Phase 7
  channel_rules.yaml        ← Phase 8
  pickup_rules.yaml         — NEW
  international_rules.yaml  — NEW
  insurance_rules.yaml      — NEW
  weight_rules.yaml         — NEW
```

### PHASE 10: Middleware + Auth Documentation
**Session**: 9
**Source**: All 95 `app/Http/Middleware/*.php` files
**Read each middleware** and document:
- Auth type (JWT, Basic, API key, webhook signature)
- Role requirements
- Rate limits
- IP restrictions
- Module access rules
**Output**: `pillar_9_auth_middleware/` YAML files

### PHASE 11: Jobs + Events Registry
**Session**: 9-10
**Source**: 1,200 `app/Jobs/*.php` + 53 `app/Events/*.php` + 36 `app/Listeners/*.php`
**For each job/event**, document:
- What triggers it (which API call)
- What it does (DB writes, external calls, notifications)
- Queue name and priority
- Retry policy
**Output**: Add `side_effects` section to each API's high.yaml that dispatches the job

### PHASE 12: Multi-Repo Coverage
**Session**: 10-11
**Repos to enrich**:
- `SR_Web` (452 files) — Pillar 3 APIs are 100% stubs → need full enrichment
- `MultiChannel_Web` (264 files) — 96% unknown APIs → need resolution
- `shiprocket-channels` (132 files) — needs P3 APIs
- `helpdesk` (77 files) — needs P3 APIs, P6 actions
**Approach**: Read actual source code from each repo's cloned directory

### PHASE 13: Training Pipeline Run
**Session**: 11
**After all enrichment is complete**:
1. Restart COSMOS
2. Run `POST /cosmos/api/v1/pipeline/run`
3. Expected: 36,798+ docs embedded with enriched content
4. Verify: Qdrant point count, entity type breakdown, retrieval quality

### PHASE 14: Lime UI Components
**Session**: 12
**Build**:
- `WaveExecutionViewer.tsx` — real-time 5-wave swimlane
- `GroundingPanel.tsx` — VERIFIED/UNVERIFIED claims with sources
- `ExecutionPlanPanel.tsx` — spec-driven plan display
- `RetrievalTrace.tsx` — retrieved chunks with confidence scores

---

## Per-Phase Enrichment Method (RIPER)

For each module phase, follow this exact process:

### 1. RESEARCH (Read Source Code)
```
1. Read the main controller file (e.g., ShipmentController.php)
2. List all public methods → these are the API endpoints
3. Read the route file to map methods → HTTP paths
4. Read FormRequest classes for validation rules
5. Read related models/repositories for database schema
6. Check for Jobs dispatched (side effects)
7. Check for Events fired
```

### 2. INNOVATE (Determine KB Structure)
```
1. Identify top 30-40 APIs by ICRM traffic importance
2. Group by: CRUD operations, dashboard, admin, reporting
3. Determine which need agent/skill/tool definitions
4. Identify business rules from config files
```

### 3. PLAN (List Changes)
```
For each API:
- Which high.yaml file to update
- What FormRequest to reference
- What side effects to document
- What response fields to list
```

### 4. EXECUTE (Write Enriched YAML)
```
For each API, update high.yaml with:
- _enriched_by_claude: true
- _source_lines: "ControllerFile.php:XXX-YYY"
- Rich canonical_summary (2-3 sentences from code understanding)
- Correct request_schema (from FormRequest, not template)
- Real param_extraction_pairs (3 ICRM operator queries)
- business_logic.description (what the method does step by step)
- business_logic.database_reads (tables with JOINs)
- business_logic.side_effects (jobs, events dispatched)
- business_logic.auth (middleware, tenant isolation)
- response_fields (from Transformer class)
```

### 5. REVIEW (Verify)
```
- Count enriched files
- Check for _enriched_by_claude: true marker
- Verify source lines are real
- Cross-reference key facts against code
```

---

## Quality Standards

### Every Enriched high.yaml MUST Have:
1. `_enriched_by_claude: true` — marks file as code-derived
2. `_source_lines` — exact file + line range
3. `canonical_summary` — 2-3 sentences explaining business purpose (not technical path)
4. `request_schema.contract` — from actual FormRequest validation (not template)
5. `param_extraction_pairs` — 3 realistic ICRM operator queries
6. `business_logic.description` — step-by-step controller logic
7. `business_logic.database_reads` — tables with filters and JOINs
8. `business_logic.auth` — middleware + tenant isolation check

### Every Domain MUST Have:
1. Agent definition in `pillar_9_agents/`
2. 2-4 skill definitions in `pillar_10_skills/`
3. 2-4 tool definitions in `pillar_11_tools/`
4. Business rules in `pillar_2_business_rules/`

---

## Estimated Timeline

| Sessions | Phases | APIs Enriched | Cumulative |
|:--------:|:------:|:------------:|:----------:|
| Session 1 | Phase 1 (Orders) | 41 | 41 |
| Session 2 | Phase 2 (Shipments/NDR) | ~40 | ~81 |
| Session 3 | Phase 3 (Courier/AWB) | ~40 | ~121 |
| Session 4 | Phase 4 (Billing) | ~30 | ~151 |
| Session 5 | Phase 5 (Settings/Auth) | ~30 | ~181 |
| Session 6 | Phase 6 (Admin/Reports) | ~40 | ~221 |
| Session 7 | Phase 7 (Returns) | ~25 | ~246 |
| Session 8 | Phase 8 (Channels) + Phase 9 (Rules) | ~40 | ~286 |
| Session 9 | Phase 10 (Middleware) + Phase 11 (Jobs) | N/A (infra) | ~286 |
| Session 10 | Phase 12 (Multi-Repo) | ~50 | ~336 |
| Session 11 | Phase 13 (Pipeline Run) | N/A (test) | ~336 |
| Session 12 | Phase 14 (Lime UI) | N/A (UI) | ~336 |

**~336 APIs deeply enriched from source code** covering all major ICRM operations.

The remaining ~5,100 APIs are lower-traffic (admin internal, deprecated, Samsung-specific, etc.) and will be enriched in subsequent sessions as needed.

---

## Success Criteria

After all 14 phases:
- **336+ APIs** enriched from actual source code (covers 80%+ ICRM traffic)
- **10+ agent definitions** with tools, skills, handoffs
- **15+ skill definitions** with triggers, steps, params
- **15+ tool definitions** with OpenAI-compatible schemas
- **14+ business rule files** from 134 config files
- **95 middleware documented** for auth/access rules
- **Qdrant embeddings**: 36,798+ high-quality vectors
- **Retrieval accuracy**: >95% on ICRM eval benchmark
- **Overall score**: 9.4/10

# PHASE-FAQ: shiprocket_faq.xlsx → KB Ingestion Plan

> Generated: 2026-04-04 | cosmos:plan
> Source: /Users/gauravchaudhary/Downloads/shiprocket_faq.xlsx

---

## Phase Goal

Ingest 1,855 seller-facing FAQ chunks from `shiprocket_faq.xlsx` (40 domain tabs)
into COSMOS as a new `pillar_12_faq/` — so when a seller or ICRM operator asks any
question about Shiprocket features, COSMOS retrieves the exact FAQ answer with
citations, embedding + graph both contributing to recall.

---

## File Audit

| Property | Value |
|---|---|
| File | shiprocket_faq.xlsx (463 KB) |
| Sheets | 40 (all named `faq_<domain>`) |
| Total chunks | 1,855 |
| Column structure | Single column: `chunk_content` (Q + A merged) |
| Usable after cleanup | ~1,591 (~86%) |
| Too short < 20 words | 217 (11%) — skip |
| Has image URLs | 47 (2%) — strip URL, keep surrounding text |
| Hinglish present | 8 chunks — keep as-is (already translated at LIME/MARS) |
| Source | SR_Web seller panel FAQ — seller-facing help content |

### Sheet Inventory (40 tabs)

| Tab | Rows | Domain |
|---|---|---|
| faq_orders | 195 | Order management, search, sync, unprocessable |
| faq_engage | 337 | Engage360 — campaigns, ROAS, WhatsApp chatbot |
| faq_checkout | 165 | Checkout widget, COD, courier selection |
| faq_finance | 84 | Delivery Boost, NDR, COD deductions |
| faq_SRX | 80 | Co-Pilot AI, weight limits, company facts |
| faq_setup_manage | 80 | Channel integrations (Prestashop, etc.) |
| faq_returns | 62 | Return pickup, refunds |
| faq_sense | 54 | Shiprocket Notify, analytics |
| faq_ndr_deliveryBoost | 54 | NDR escalation, Delivery Boost |
| faq_omuni | 54 | Omuni inventory, ROAS, Instagram chatbot |
| faq_settings | 57 | Settings, pickup address, label type |
| faq_courier | 39 | Courier serviceability, API |
| faq_quick | 38 | API credits, billing |
| faq_blogs | 43 | COD deductions, shipping rates |
| faq_RTO | 21 | RTO management |
| faq_API | 20 | API integration, hyperlocal, serviceability |
| faq_self_serve_labels | 20 | Label generation |
| faq_profile | 17 | Restricted goods, account settings |
| faq_zop | 29 | ZOP platform, SKU pricing |
| faq_brand_boost | 33 | Webhooks, brand boost |
| faq_cargo | 31 | ShipSure cargo |
| faq_AppStore | 21 | App store access |
| faq_dashboard | 21 | Multi-user, dashboard features |
| faq_buyer_experience | 18 | Buyer-facing features |
| faq_fulfillment | 47 | Fulfillment operations |
| faq_tools | 29 | Tools (rate calculator, etc.) |
| faq_weight | 15 | Weight discrepancy |
| faq_secure | 25 | Secure shipments |
| faq_credit_line | 25 | Credit line, EMI |
| faq_revprotect | 17 | Revenue protection |
| faq_instant_cod | 8 | Instant COD |
| faq_shipsure | 5 | ShipSure insurance |
| faq_home | 8 | Home page features |
| faq_navigation_bar | 6 | Quick actions, wallet, keyboard shortcuts |
| faq_credit_score | 6 | CRIF credit score |
| faq_buyer_protect | 8 | Buyer protection |
| faq_business_loan | 6 | Business loan |
| faq_promise | 15 | Delivery promise |
| faq_trends | 14 | Trends dashboard |
| faq_miscellaneous | 48 | Mixed topics |

---

## Scope

### IN
- All 40 tabs → `pillar_12_faq/<tab_name>/` directory per domain
- Each row → one YAML file: `faq_<domain>_<idx>.yaml`
- Strip embedded image URLs (`amazonaws.com` links)
- Skip rows < 20 words after stripping
- Add metadata: entity_id, domain, query_mode, trust_score
- New `read_pillar12_faq()` method in `kb_ingestor.py`
- Wire into `training_pipeline.run_full()` and add `POST /pipeline/faq` endpoint
- Graph nodes: one `faq_topic` node per chunk + `ANSWERS_QUESTION` edges to domain nodes

### OUT
- No image downloading or OCR (image URLs stripped only)
- No Hinglish translation (COSMOS receives clean English from LIME/MARS)
- No enrichment with Claude Opus on first pass (trust_score 0.7, can enrich later)
- No modification of existing pillars

---

## Architecture: How Embedding + Graph Both Help

### Embedding (Qdrant — vector similarity)

When a seller asks: *"how do I sync orders from Shopify?"*

```
Query → embed_text(query) → cosine similarity search in cosmos_embeddings
  → retrieves faq_orders chunk: "How does Sync order work? If your sales channel
    is integrated with Shiprocket, orders automatically sync..."
  → score: 0.87 (high — semantically identical question)
```

Embedding handles:
- Paraphrase matching: "order sync nahi ho raha" → same vector space as "Sync order"
- Partial queries: "wallet balance" → retrieves both `faq_navigation_bar` + `faq_finance`
- Cross-domain: "delivery failed" → retrieves NDR + RTO + Delivery Boost FAQs

### Graph (Neo4j — relationship traversal)

Each FAQ chunk becomes a graph node with cross-pillar edges:

```
faq_topic:orders_search
    ─── ANSWERS_QUESTION ──► domain:orders
    ─── RELATED_TO ──────► api:mcapi.v1.orders.index.get
    ─── RELATED_TO ──────► faq_topic:orders_sync
    ─── DESCRIBED_BY ────► pillar_6_actions:order_search
```

Graph (PPR/BFS) handles:
- "I can't find my order" → PPR from `orders` seed → retrieves faq_orders_search +
  faq_orders_sync + api:orders.index + action:order_search in one traversal
- Multi-hop: "billing for returned order" → billing node → edges to returns FAQ →
  edges to finance FAQ → edges to refund action contract
- Cluster discovery: operator asks about "Engage" → PPR finds all 337 Engage FAQs +
  related APIs + Omuni integration FAQs via shared domain edges

### Why Both Together Beat Either Alone

| Query type | Vector alone | Graph alone | Vector + Graph |
|---|---|---|---|
| Exact question match | ✅ Great | ❌ No signal | ✅✅ |
| "Why did X happen?" | ❌ Poor | ✅ Traverses causal edges | ✅✅ |
| Multi-domain: NDR + finance | Partial | ✅ PPR finds both | ✅✅✅ |
| Hinglish paraphrase | ✅ Semantic match | ❌ | ✅✅ |
| "What can I do about X?" | Partial | ✅ Action edges | ✅✅✅ |

---

## Wave Execution

### Wave 1 — XLSX Parser + YAML Generator (parallel: no deps)

**Task 1.1** — Write `scripts/ingest_faq_xlsx.py`
- Input: `shiprocket_faq.xlsx`
- For each sheet tab → for each row:
  - Split `chunk_content` into `question` (line 0) + `answer` (remaining lines)
  - Strip image URLs (regex: `https?://[^\s]+amazonaws[^\s]+`)
  - Skip if word_count < 20 after stripping
  - Detect `query_mode`: `lookup` if Q starts with "How to"/"What is", `diagnose` if "Why"/"issue"/"problem", `navigate` if "Where"/"How to access"
  - Write to `KB_ROOT/MultiChannel_API/pillar_12_faq/<tab_name>/faq_<idx>.yaml`
- Acceptance: all 40 tabs processed, ~1,591 YAML files written

**Task 1.2** — Define YAML schema for `pillar_12_faq/`

```yaml
# pillar_12_faq/faq_orders/faq_001.yaml
_tier: high
_source: shiprocket_faq.xlsx
_tab: faq_orders
entity_type: faq_chunk
entity_id: faq:orders:001
domain: orders
query_mode: lookup          # lookup / diagnose / navigate / explain
trust_score: 0.7
question: "How to use the Search feature on the orders page?"
answer: |
  To use the Search feature on the Orders page on your Shiprocket account,
  follow these steps:
  * Locate the Search bar at the top of the Orders page...
canonical_summary: |
  Explains how sellers use the search/filter feature on the Shiprocket Orders
  page. Covers search bar location, filter options, and navigation.
keywords:
  - order search
  - search orders
  - filter orders
  - find order
related_domains:
  - orders
  - navigation
```

### Wave 2 — KB Ingestor + Pipeline Wiring (after Wave 1)

**Task 2.1** — Add `read_pillar12_faq()` to `kb_ingestor.py`
- File: `cosmos/app/services/kb_ingestor.py`
- Reads `pillar_12_faq/<domain>/*.yaml` per repo
- Builds embedding content: `"FAQ: {question}\n{answer}\nDomain: {domain}\nKeywords: {keywords}"`
- entity_type: `faq_chunk`, entity_id: `faq:{domain}:{idx}`
- Metadata: `pillar: pillar_12_faq`, `domain`, `query_mode`, `tab`

**Task 2.2** — Add `run_pillar12_faq()` to `training_pipeline.py`
- Mirrors existing `run_pillar9_10_11()` pattern
- Call inside `run_full()` after `run_pillar9_10_11()`
- Write graph nodes: `faq_topic` NodeType per chunk

**Task 2.3** — Add `POST /pipeline/faq` REST endpoint
- File: `cosmos/app/api/endpoints/training_pipeline.py`
- Calls `pipeline.run_pillar12_faq()`

**Task 2.4** — Add `NodeType.faq_topic` + `EdgeType.answers_question` to `graphrag_models.py`

### Wave 3 — Graph Edges (after Wave 2 nodes exist)

**Task 3.1** — Add cross-pillar edges in `_build_faq_graph()`:
- `faq:orders:* ─ANSWERS_QUESTION→ domain:orders`
- `faq:API:* ─RELATED_TO→ pillar_3` API nodes (where FAQ mentions API endpoints)
- `faq:ndr_deliveryBoost:* ─RELATED_TO→ faq:finance:*` (shared NDR domain)

### Wave 4 — Run + Eval (after Wave 3)

**Task 4.1** — Run `POST /pipeline/faq` → embed 1,591 chunks into Qdrant
**Task 4.2** — Run `POST /pipeline/eval` → confirm recall@5 improves

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Sheet name ≠ actual content (e.g., `faq_RTO` has Omuni content) | MEDIUM | Infer domain from Q content, not just tab name — add `_detected_domain` field |
| Duplicate FAQs across tabs | LOW | Content-hash dedup in vectorstore handles this automatically |
| Image-only chunks (URL with no text) | LOW | After stripping URL, word_count < 20 → skip |
| faq_engage (337 rows) dilutes other domains | LOW | Per-domain node in graph clusters Engage separately; MMR diversity prevents 5 Engage results for non-Engage query |
| Trust score 0.7 may rank below P3/P6 docs | INTENDED | FAQs are user-facing explanations; action contracts (0.9) should rank higher for `act` intent |

---

## Dependencies

| Dependency | Status |
|---|---|
| openpyxl or xlrd (Python xlsx reader) | Need to verify: `pip show openpyxl` |
| `KB_ROOT/MultiChannel_API/` writable | ✅ Confirmed |
| `NodeType`, `EdgeType` in graphrag_models.py | Need `faq_topic` + `answers_question` |
| Qdrant running | ✅ Online (20,737 vectors) |
| `training_pipeline.py` `run_full()` | ✅ Will add `run_pillar12_faq()` call |

---

## Acceptance Criteria

1. `scripts/ingest_faq_xlsx.py` processes all 40 tabs, writes ≥ 1,500 YAML files
2. `read_pillar12_faq()` in `kb_ingestor.py` reads all YAML, builds correct embedding content
3. `POST /pipeline/faq` returns `success: true`, `documents > 1500`
4. Qdrant gains ~1,591 new `faq_chunk` vectors (confirm via count endpoint)
5. `pytest tests/ -x -q` still passes (no regressions)
6. Test query: "how to sync orders from Shopify" → retrieves `faq_orders` chunk with score > 0.7
7. Test query: "wallet balance kaise dekhe" → retrieves `faq_navigation_bar` chunk

---

## Estimated Output Improvement

| Metric | Before FAQ ingestion | After FAQ ingestion |
|---|---|---|
| Seller UI questions answered | Poor (P3/P6 not designed for this) | **Excellent** — direct Q&A match |
| "Why did X happen?" queries | Partial | Good (diagnose mode FAQs) |
| Value-added service questions (Engage, Checkout, SRX) | None | **Full coverage** (337 + 165 + 80 chunks) |
| Recall@5 (seller-facing queries) | ~0.65 estimated | **~0.82 estimated** |

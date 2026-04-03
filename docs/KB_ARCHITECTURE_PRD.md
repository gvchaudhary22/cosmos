# COSMOS Knowledge Base Architecture — PRD

## Product: COSMOS (Cognitive Operations System for Multi-channel Orchestration at Shiprocket)

## Version: 1.0 | Date: April 2026

---

## 1. Overview

COSMOS is an AI-powered ICRM assistant that helps Shiprocket operations teams manage orders, shipments, seller accounts, and customer escalations. It uses a structured knowledge base (KB) to understand Shiprocket's systems, then selects the right **agent**, executes the right **tools** with correct parameters, follows **skill** recipes for multi-step tasks, and triggers **actions** with proper approval gates.

This PRD defines how **Agents, Skills, Tools, and Actions** work together — explained through the **Orders** domain as the reference example.

---

## 2. The Four Primitives

```
┌─────────────────────────────────────────────────────────┐
│                    COSMOS Architecture                    │
│                                                          │
│  AGENT = WHO does the work                               │
│    └── has: tools, skills, instructions, handoff rules   │
│                                                          │
│  SKILL = HOW to do a specific task (recipe)              │
│    └── has: triggers, steps (ordered actions), params    │
│                                                          │
│  TOOL = WHAT API to call (single operation)              │
│    └── has: endpoint, parameters, risk level, schema     │
│                                                          │
│  ACTION = THE EXECUTABLE CONTRACT (with guardrails)      │
│    └── has: preconditions, inputs, outputs, approval     │
└─────────────────────────────────────────────────────────┘
```

### 2.1 Agent

An **Agent** is a specialized AI persona scoped to a specific business domain. It knows:
- Which **tools** it can call
- Which **skills** (recipes) it can execute
- When to **hand off** to another agent
- What it should **never do** (anti-patterns)

**An agent does NOT contain business logic.** It delegates to tools and skills.

### 2.2 Skill

A **Skill** is a reusable recipe that chains multiple steps to accomplish a task. It includes:
- **Triggers**: Keywords/intents that activate the skill
- **Steps**: Ordered sequence of actions (api_call → analyze → respond)
- **Required params**: What information is needed before execution
- **Response template**: How to format the output

**A skill is intent-based.** When a user says "check order status", the skill `orders_get` is triggered.

### 2.3 Tool

A **Tool** is a single API operation the agent can call. It maps to one or more Shiprocket API endpoints. It includes:
- **Endpoint**: HTTP method + path
- **Parameters**: Required and optional inputs with types and validation
- **Risk level**: low (read), medium (write), high (destructive)
- **Read/Write**: Whether it modifies data

**A tool is stateless.** It calls one API and returns the result.

### 2.4 Action

An **Action** is an executable contract from Pillar 6. It wraps a tool call with:
- **Preconditions**: What must be true before execution (e.g., order status < SHIPPED)
- **Required inputs**: Fully typed with validation rules
- **Outputs**: What the action produces
- **Approval gate**: Whether human confirmation is needed
- **Observability**: Metrics, alerts, trace keys

**An action is the safety layer.** It prevents dangerous operations.

---

## 3. Orders Domain — Complete Reference

### 3.1 The Agent: `order_ops_agent`

```yaml
name: order_ops_agent
display_name: Order Operations Agent
tier: CORE
domain: orders
api_count: 1,320

system_prompt: |
  You are the Order Operations agent for Shiprocket's ICRM.
  You handle all order-related queries: status checks, cancellations,
  address updates, order search, and bulk operations.

  Rules:
  - Always verify the order exists before any action
  - Check cancellation eligibility: if status >= SHIPPED (6), cannot cancel
  - For address updates, verify shipment hasn't been picked up
  - If refund is needed, HANDOFF to billing_wallet agent
  - Never process refunds directly
  - Never delete order records
  - Never modify pricing

tools:
  - orders_get       # Lookup single order
  - orders_list      # List/search orders
  - orders_create    # Create new orders
  - orders_cancel    # Cancel orders
  - orders_update    # Update order details

skills:
  - order_lookup     # Check order status
  - order_search     # Search orders by filters
  - order_cancel     # Cancel with eligibility check
  - address_update   # Update shipping address

handoff_rules:
  billing_wallet: "when refund is needed after cancellation"
  shipment_ops_agent: "when order is shipped, tracking needed"
  ndr_agent: "when delivery fails"
  support_agent: "when escalation is needed"

anti_patterns:
  - "Never process refunds directly"
  - "Never delete order records"
  - "Never modify pricing or COD amount"
```

### 3.2 The Tools

#### Tool: `orders_get` (READ)

```yaml
name: orders_get
display_name: Order Lookup
read_write: READ
risk_level: low
approval_mode: auto
api_count: 150

description: |
  Look up a single order by order ID, channel order ID, or AWB number.
  Returns: order status, items, payment info, shipping address, timeline.

endpoints:
  - method: GET
    path: /api/v1/app/orders/show/{id}
    controller: OrderController@show
  - method: GET
    path: /api/v1/app/orders/track
    controller: OrderController@track
  - method: GET
    path: /api/v1/app/orders/status
    controller: OrderController@status

parameters:
  required:
    - name: order_id
      type: string
      description: "Shiprocket order ID (numeric) or channel order ID"
  optional:
    - name: awb_number
      type: string
      description: "AWB tracking number (alternative lookup)"

response_fields:
  - id (int): Internal order ID
  - channel_order_id (string): External order reference
  - status (int): Order status code (0=NEW, 6=SHIPPED, 7=DELIVERED)
  - customer_name (string): Buyer's name
  - payment_method (string): COD or Prepaid
  - created_at (datetime): Order creation timestamp
```

#### Tool: `orders_create` (WRITE)

```yaml
name: orders_create
display_name: Order Creation
read_write: WRITE
risk_level: medium
approval_mode: confirm
api_count: 698

description: |
  Create new orders, import orders, cancel orders, manage bulk operations.
  WRITE operations — always confirm with operator before executing.

endpoints:
  - method: POST
    path: /api/v1/app/orders/create
    controller: CustomController@store
  - method: POST
    path: /api/v1/app/orders/cancel
    controller: OrderController@cancel
  - method: POST
    path: /api/v1/app/orders/import
    controller: OrderController@import

parameters:
  required:
    - name: order_date
      type: date
      validation: "required|date"
    - name: pickup_location
      type: string
      validation: "required|exists:pickup_locations"
    - name: billing_customer_name
      type: string
      validation: "required|max:100"
    - name: billing_address
      type: string
      validation: "required|max:256"
    - name: billing_pincode
      type: string
      validation: "required|digits:6"
    - name: billing_phone
      type: string
      validation: "required|digits:10"
    - name: order_items
      type: array
      validation: "required|array|min:1"
    - name: payment_method
      type: string
      validation: "required|in:Prepaid,COD"
    - name: sub_total
      type: float
      validation: "required|numeric|min:0"
```

#### Tool: `orders_list` (READ)

```yaml
name: orders_list
display_name: Order Search & Listing
read_write: READ
risk_level: low
api_count: 472

description: |
  Fetch orders for a company with filters: status, date range, channel.
  Used for reporting, batch operations, and operational dashboards.

endpoints:
  - method: GET
    path: /api/v1/app/orders
    controller: OrderController@index
  - method: GET
    path: /api/v1/app/orders/count
    controller: OrderController@count
  - method: GET
    path: /api/v1/app/orders/processing
    controller: OrderController@processing

parameters:
  required:
    - name: company_id
      type: int
      description: "Seller company ID"
  optional:
    - name: status
      type: string
      description: "Comma-separated status codes to filter"
    - name: from_date
      type: date
      description: "Start date (YYYY-MM-DD)"
    - name: to_date
      type: date
      description: "End date (YYYY-MM-DD)"
    - name: per_page
      type: int
      description: "Results per page (default 20)"
```

### 3.3 The Skills

#### Skill: `order_lookup`

```yaml
name: order_lookup
display_name: Order Status Lookup
domain: orders
triggers:
  - "order status"
  - "check order"
  - "find order"
  - "order details"
  - "show order"

steps:
  1. type: api_call
     tool: orders_get
     description: "Fetch order by ID"

  2. type: respond
     template: |
       Order #{id} — Status: {status_name}
       Customer: {customer_name}
       Payment: {payment_method}
       Channel: {channel_name}
       Created: {created_at}
       {if shipped: AWB: {awb_number}, Courier: {courier_name}}

required_params:
  - order_id (from user query or conversation context)

response_format: |
  Show: order ID, human-readable status, customer name,
  channel, payment method, creation date.
  If shipped: include AWB and courier.
```

#### Skill: `order_cancel`

```yaml
name: order_cancel
display_name: Order Cancellation
domain: orders
triggers:
  - "cancel order"
  - "cancel this order"
  - "I want to cancel"

steps:
  1. type: api_call
     tool: orders_get
     description: "Fetch order to check current status"

  2. type: internal
     description: "Check eligibility: status must be < SHIPPED (6)"
     precondition: "order.status < 6"
     on_fail: |
       "This order cannot be cancelled — it's already {status_name}.
        For shipped orders, you can initiate a return instead."

  3. type: confirm
     description: "Ask operator to confirm cancellation"
     message: "Cancel order #{id} ({customer_name}, {payment_method} Rs {amount})?"

  4. type: api_call
     tool: orders_create  # cancel endpoint is under orders_create tool
     action: cancel_order
     description: "Execute cancellation"

  5. type: respond
     template: "Order #{id} cancelled successfully. Reason: {reason}."

  6. type: handoff
     condition: "if payment_method == 'Prepaid'"
     to: billing_wallet
     context: "Process refund for cancelled prepaid order #{id}"

required_params:
  - order_id
  - reason (enum: customer_request, seller_request, fraud, duplicate, out_of_stock)
```

#### Skill: `address_update`

```yaml
name: address_update
display_name: Address Update
domain: orders
triggers:
  - "update address"
  - "change address"
  - "wrong address"
  - "fix delivery address"

steps:
  1. type: api_call
     tool: orders_get
     description: "Fetch order to check if pickup happened"

  2. type: internal
     description: "Verify shipment not picked up yet"
     precondition: "shipment.status != 'picked_up'"
     on_fail: "Address cannot be changed after pickup. Contact courier for redirection."

  3. type: api_call
     tool: orders_update
     description: "Update shipping address"

  4. type: respond
     template: "Address updated for order #{id}. New address: {new_address}"
```

### 3.4 The Actions (Pillar 6 Contracts)

#### Action: `action.orders.create_order`

```yaml
action_id: action.orders.create_order
purpose: |
  Create a new order in Shiprocket. Accepts buyer info, addresses,
  product items, payment method. Validates all inputs and persists
  to orders + order_products tables.

dispatch_point: "POST /v1/external/orders/create/adhoc"
source_file: app/Http/Controllers/API/OrderController.php
sync_async: sync
idempotent: false
dry_run_supported: false

preconditions:
  - type: status_check
    entity: company
    id_param: company_id
    check: "company.status = active"
    fail_message: "Seller account is not active"

  - type: balance_check
    entity: wallet
    id_param: company_id
    check: "wallet.balance >= minimum_shipping_cost"
    fail_message: "Insufficient wallet balance for shipping"

required_inputs:
  - name: order_id
    type: string
    validation: "required|unique:orders,channel_order_id"
  - name: order_date
    type: datetime
    validation: "required|date"
  - name: pickup_location
    type: string
    validation: "required|exists:pickup_locations"
  - name: billing_customer_name
    type: string
    validation: "required|max:100"
  - name: billing_pincode
    type: string
    validation: "required|digits:6"
  - name: billing_phone
    type: string
    validation: "required|digits:10"
  - name: order_items
    type: array
    validation: "required|array|min:1"
  - name: payment_method
    type: string
    validation: "required|in:Prepaid,COD"
  - name: sub_total
    type: float
    validation: "required|numeric|min:0"

output:
  - name: order_id
    type: integer
    destination: orders.id
  - name: status
    type: string
    value: "NEW"

approval_gate:
  risk_level: medium
  mode: confirm
  message: "Create order for {billing_customer_name} at {billing_pincode}?"

observability:
  metrics:
    - order_creation_latency_ms (histogram)
    - order_creation_success_rate (counter)
    - order_creation_by_channel (counter)
  alerts:
    - "Error rate > 5% in 5 min → warning"
    - "Latency p95 > 3s → warning"
  trace_keys: [order_id, company_id, channel_order_id, payment_method]
  dashboard: grafana.internal/d/order-creation
```

#### Action: `action.orders.cancel_order` (implied from tool)

```yaml
action_id: action.orders.cancel_order

preconditions:
  - type: status_check
    entity: order
    id_param: order_id
    check: "order.status < 6"  # Must be before SHIPPED
    allowed_values: [0, 1, 2, 3, 4, 5]  # NEW through PICKUP_PENDING
    fail_message: "Cannot cancel — order is already shipped (status {current_status})"

  - type: time_window
    check: "order.created_at > 24_hours_ago"
    fail_message: "Cancellation window expired (>24 hours)"

approval_gate:
  risk_level: high
  mode: confirm
  message: |
    Cancel order #{order_id}?
    Customer: {customer_name}
    Amount: Rs {sub_total} ({payment_method})
    This action is irreversible.
```

---

## 4. How They Work Together — End-to-End Flow

### Query: "I placed my order on 15 March but still not picked up"

```
┌─ Step 1: Intent Classification ─────────────────────────┐
│ Query → Intent: orders_get (order status lookup)         │
│ Entities: date="15 March", issue="not picked up"         │
│ Agent: order_ops_agent                                   │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 2: Skill Selection ───────────────────────────────┐
│ Trigger match: "order" + "placed" → skill: order_lookup  │
│ Missing param: order_id (not in query)                   │
│ → Ask operator: "What is the order ID?"                  │
│ Operator: "Order 12345"                                  │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 3: Tool Execution ────────────────────────────────┐
│ Skill step 1: api_call → tool: orders_get                │
│ Call: GET /api/v1/app/orders/show/12345                   │
│ Response: { status: 1, pickup_status: null,               │
│            created_at: "2026-03-15", awb: null }          │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 4: Process Engine Context ────────────────────────┐
│ Status 1 (CONFIRMED) → Process: order_to_delivery        │
│ Current step: order_confirmed (step 2/14)                 │
│ Next expected: invoice_generated                          │
│ ISSUE: Order stuck at step 2 for 16 days                 │
│ Risk: No courier assigned, no AWB generated               │
│ Valid actions: [invoice_generated, cancelled]              │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 5: Response Assembly (Claude Opus 4.6) ───────────┐
│ Context: order data + process position + KB knowledge     │
│                                                          │
│ Response:                                                │
│ "Order #12345 was placed on March 15 but is stuck at     │
│  CONFIRMED status (step 2 of 14 in the delivery process).│
│                                                          │
│  Issue: No courier has been assigned yet. The order has   │
│  not progressed to invoice generation or AWB assignment.  │
│                                                          │
│  Possible causes:                                        │
│  1. Seller hasn't processed the order                    │
│  2. No pickup location configured                        │
│  3. Wallet balance too low for shipping                  │
│                                                          │
│  Recommended actions:                                    │
│  - Check seller's pickup location settings               │
│  - Verify wallet balance                                 │
│  - Contact seller to process the order                   │
│                                                          │
│  Sources: orders DB (status=1), Process Engine            │
│  Confidence: HIGH (verified from database)"              │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 6: Grounding Verification ────────────────────────┐
│ Claims verified:                                         │
│ ✓ "placed on March 15" — verified (orders.created_at)    │
│ ✓ "CONFIRMED status" — verified (orders.status = 1)      │
│ ✓ "step 2 of 14" — verified (Process Engine)             │
│ ✓ "No courier assigned" — verified (awb = null)          │
│ Grounding score: 100% (4/4 claims verified)              │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 7: Learning Memory ───────────────────────────────┐
│ Record: operator asked about stuck order                 │
│ Pattern: order stuck at CONFIRMED → usually seller issue │
│ Episodic: this operator's frequent query type = lookup   │
└──────────────────────────────────────────────────────────┘
```

---

## 5. Knowledge Base Structure (Per Domain)

For each business domain (orders, shipments, billing, etc.), the KB provides:

| Pillar | What It Provides | Example (Orders) |
|--------|-----------------|-------------------|
| **P1: Schema** | Table structure, columns, types, state machine | `orders` table: 50+ columns, status values 0-105 |
| **P2: Business Rules** | Limits, thresholds, policies | COD limit Rs 50,000, cancel window 24h |
| **P3: API Tools** | 1,320 API endpoints with params, examples | GET /orders/show/{id}, POST /orders/cancel |
| **P4: Pages** | ICRM UI pages that display order data | /admin/orders, /admin/orders/{id} |
| **P5: Modules** | Code module documentation | OrderController, OrderRepository |
| **P6: Actions** | 4 executable contracts | create_order, cancel_order, bulk_ship, clone_order |
| **P7: Workflows** | Multi-step runbooks | Order Creation Pipeline (8 steps) |
| **P8: Negatives** | What NOT to do | Never cancel shipped orders, never modify COD amount |
| **Entity Hub** | Cross-pillar summary | orders hub: tables + APIs + actions + workflows linked |

---

## 6. Agent Hierarchy (All Domains)

```
TIER 1: CORE (highest volume)
├── order_ops_agent      — Orders: status, cancel, create, search
├── shipment_ops_agent   — Shipments: tracking, AWB, courier assignment
├── courier_ops_agent    — Couriers: serviceability, rates, performance
├── admin_agent          — Settings: company config, KYC, features
├── finance_agent        — Billing: wallet, COD, refunds, invoices
└── ndr_agent            — NDR: delivery failures, reattempts, RTO

TIER 2: SPECIALIZED
├── catalog_agent        — Products: SKU, inventory, catalogs
├── auth_agent           — Authentication: login, password, SSO
├── integrations_agent   — Channels: Shopify, Amazon, WooCommerce sync
├── analytics_agent      — Reports: dashboards, exports, metrics
└── support_agent        — Escalations: supervisor routing, ticketing

TIER 3: OPERATIONAL
├── general_ops_agent    — Catch-all for unclassified queries
└── (dynamic agents from Agent Forge when confidence < 60%)
```

---

## 7. How KB Feeds the Pipeline

```
Knowledge Base (YAML files on disk)
    │
    ▼
Training Pipeline (COSMOS)
    ├── M0:  KB Quality Fixes (examples, params, columns, hubs)
    ├── M2:  Split into train/dev/holdout
    ├── M5:  Embed Pillar 1 (schema) + Pillar 3 (APIs)
    ├── M5b: Embed Pillar 1 extras (catalog, access patterns)
    ├── M5c: Embed Pillar 4 (pages) + Pillar 5 (modules)
    ├── M6:  Embed Pillar 6 (actions) + P7 (workflows) + P8 (negatives)
    ├── M7:  Generate + embed Entity Hubs
    ├── M4:  Generate + embed artifacts
    ├── P2:  Generate Business Rules (Opus)
    ├── P8+: Expand Negatives (Opus)
    ├── Enrich: Contextual headers (Opus) + Synthetic Q&A (5 per chunk)
    ├── Link:  Cross-pillar connections
    └── Sync:  KB Registry → graph_nodes (agents/tools/skills enriched)
    │
    ▼
Qdrant Vector Store (embeddings)
    + Neo4j Graph (relationships)
    + MySQL graph_nodes (MARS agent registry)
    │
    ▼
Query Orchestrator (5-wave pipeline)
    ├── Wave 1: Probe (intent + entity + vector + page + cross-repo)
    ├── Wave 2: Deep (GraphRAG + cross-repo deep)
    ├── Wave 3: LangGraph (stateful reasoning)
    ├── Wave 4: Neo4j (graph traversal)
    └── Wave 5: RIPER + ReAct (LLM assembly with tools)
    │
    ▼
Claude Opus 4.6 (response generation)
    + Process Engine (lifecycle context)
    + Grounding Verifier (source citations)
    + Learning Memory (preferences + patterns)
```

---

## 8. Success Metrics

| Metric | Target | How Measured |
|--------|--------|-------------|
| Retrieval accuracy | >95% | Top result rerank score > 0.3 |
| Tool selection accuracy | >90% | Correct tool chosen for query intent |
| Action success rate | >98% | Actions complete without precondition failure |
| Response quality | >4.5/5 | Operator ratings |
| Grounding score | >80% | Verified claims / total claims |
| Resolution time | <30 seconds | Query to final response |
| Knowledge coverage | >80% of operator queries | Gap detector firing rate < 20% |

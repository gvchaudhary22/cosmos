# Eval Dataset — 201 ICRM Seed Queries

Representative queries for regression testing COSMOS retrieval quality. Used by `KBEval` to measure `recall@5`.

---

## Order Status Lookup (35 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| What is the status of AWB 1234567890? | P1 | schema/tables/shipments | easy |
| Show me order 5001234 status | P3 | apis_tools/endpoints/get_order | easy |
| Why is my order still in pending pickup? | P7 | workflow_runbooks/pickup_workflow | medium |
| What does status 104 mean? | P1 | schema/status_values/cancelled | easy |
| Order delivered but COD not received | P7 | workflow_runbooks/cod_remittance | hard |
| How many orders are in RTO state for company 42? | P1 | schema/tables/orders | medium |
| What is the difference between status 7 and status 8? | P8 | negative_routing/delivery_status | medium |
| Track shipment for AWB 9876543210 | P3 | apis_tools/endpoints/track_shipment | easy |
| Why did order auto-cancel? | P7 | workflow_runbooks/auto_cancel | hard |
| What columns does the orders table have? | P1 | schema/tables/orders | easy |

*(Full dataset of 201 seeds stored in `cosmos_eval_seeds` MySQL table)*

---

## NDR Diagnosis (28 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| What is an NDR? | Hub | entity_summaries/ndr | easy |
| How do I resolve an NDR? | P6 | action_contracts/resolve_ndr | medium |
| Customer not available NDR — what next? | P7 | workflow_runbooks/ndr_workflow | medium |
| NDR resolve kaise karein? (Hinglish) | P6 | action_contracts/resolve_ndr | medium |
| How many NDR attempts before RTO? | P7 | workflow_runbooks/ndr_workflow | hard |
| Wrong address NDR — can I update address? | P6 | action_contracts/update_address | hard |
| What is the difference between NDR and RTO? | P8 | negative_routing/ndr_vs_rto | medium |
| Fake delivery NDR — how to escalate? | P7 | workflow_runbooks/ndr_escalation | hard |

---

## API Contract Lookup (32 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| How do I cancel an order via API? | P3 | apis_tools/endpoints/cancel_order | easy |
| What is the endpoint to track a shipment? | P3 | apis_tools/endpoints/track_shipment | easy |
| What headers does the MCAPI require? | P3 | apis_tools/auth/headers | medium |
| How do I create a forward shipment? | P3 | apis_tools/endpoints/create_shipment | easy |
| What is the rate calculation API? | P3 | apis_tools/endpoints/get_rates | medium |
| How do I update pickup address via API? | P3 | apis_tools/endpoints/update_pickup | medium |
| What does POST /v1/orders/create return? | P3 | apis_tools/endpoints/create_order | easy |
| How to list all active shipments? | P3 | apis_tools/endpoints/list_shipments | medium |

---

## DB Schema Lookup (25 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| What table stores order data? | P1 | schema/tables/orders | easy |
| Which column has the AWB number? | P1 | schema/tables/shipments | easy |
| What are the possible values for channel_id? | P1 | schema/enums/channels | medium |
| Which table links orders to shipments? | P1 | schema/tables/order_shipments | medium |
| What is stored in the products table? | P1 | schema/tables/products | easy |
| How is COD amount stored? | P1 | schema/tables/cod | medium |
| Which tables have company_id column? | P1 | schema/tenant_columns | hard |

---

## Action Execution (30 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| How do I cancel an order? | P6 | action_contracts/cancel_order | easy |
| Steps to reattempt a delivery | P6 | action_contracts/reattempt_delivery | medium |
| How to initiate a return? | P6 | action_contracts/initiate_return | medium |
| Can I change the delivery address after dispatch? | P6 | action_contracts/update_address | hard |
| How to mark an order as self-shipped? | P6 | action_contracts/self_ship | medium |
| What approvals are needed to issue a refund? | P6 | action_contracts/issue_refund | hard |
| How to bulk cancel orders? | P6 | action_contracts/bulk_cancel | medium |
| Steps to escalate a stuck pickup | P6 | action_contracts/escalate_pickup | hard |

---

## Workflow Diagnosis (31 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| Why was my pickup not scheduled? | P7 | workflow_runbooks/pickup_workflow | medium |
| What is the order lifecycle? | P7 | workflow_runbooks/order_lifecycle | easy |
| Why did the shipment go to RTO? | P7 | workflow_runbooks/rto_workflow | medium |
| What triggers an auto-manifest? | P7 | workflow_runbooks/manifest_workflow | hard |
| Why is my channel sync failing? | P7 | workflow_runbooks/channel_sync | hard |
| What states can an order be in? | P7 | workflow_runbooks/order_states | easy |
| How does COD remittance work? | P7 | workflow_runbooks/cod_remittance | medium |
| What happens after delivery is marked? | P7 | workflow_runbooks/post_delivery | medium |

---

## Negative Routing (20 queries)

| Query | Expected pillar | Expected entity | Difficulty |
|-------|----------------|-----------------|-----------|
| Difference between cancel order and cancel shipment | P8 | negative_routing/cancel_disambiguation | medium |
| Is NDR the same as RTO? | P8 | negative_routing/ndr_vs_rto | easy |
| Weight dispute vs weight freeze — what's different? | P8 | negative_routing/weight_dispute | hard |
| Channel order vs marketplace order | P8 | negative_routing/channel_vs_marketplace | medium |
| Pickup scheduled vs pickup done | P8 | negative_routing/pickup_status | easy |
| Forward shipment vs reverse shipment | P8 | negative_routing/forward_vs_reverse | easy |

---

## Adding New Seeds

When an operator reports a wrong answer:
1. Note the exact query they asked
2. Identify the correct KB document that should answer it
3. Add to `cosmos_eval_seeds`:
   ```sql
   INSERT INTO cosmos_eval_seeds (id, query, expected_entity_id, expected_pillar, category, difficulty)
   VALUES (UUID(), 'the query', 'pillar_x/entity', 'PX', 'category', 'medium');
   ```
4. Verify the expected doc is in the KB (run ingestion if needed)
5. Run eval: `recall@5` should improve with the new seed added and fixed

"""
Business Process Engine — Shiprocket process state machines.

Defines end-to-end business processes as state machines with:
- Ordered steps with durations
- Valid state transitions
- Failure branches
- Valid actions per state

When the orchestrator resolves an entity (order, shipment, AWB),
the process engine provides lifecycle context to Claude:
- Where the entity is in its lifecycle
- What step comes next
- What can go wrong
- What valid actions exist at this state

Usage:
    engine = ProcessEngine()
    position = engine.get_process_position("order", current_status="in_transit")
    context = engine.get_context_for_llm("order", current_status="in_transit")
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class ProcessStep:
    name: str
    owner: str  # seller, system, courier, shiprocket, ops_team
    avg_duration: str = ""
    description: str = ""
    conditional: str = ""  # e.g., "payment_mode == COD"


@dataclass
class ProcessPosition:
    process_name: str
    current_step: str
    step_number: int
    total_steps: int
    next_step: Optional[str]
    previous_step: Optional[str]
    percent_complete: float
    valid_actions: List[str]
    risk_factors: List[str]
    failure_branches: List[Dict[str, str]]
    estimated_remaining: str


# ===================================================================
# Shiprocket Business Processes
# ===================================================================

ORDER_TO_DELIVERY = {
    "name": "order_to_delivery",
    "description": "Forward shipment lifecycle from order placement to delivery and COD remittance",
    "steps": [
        ProcessStep("order_placed", "seller", "instant", "Seller creates order via panel or API"),
        ProcessStep("order_confirmed", "seller", "0-2 hours", "Seller confirms order details"),
        ProcessStep("invoice_generated", "system", "instant", "System generates invoice automatically"),
        ProcessStep("courier_assigned", "system", "1-5 minutes", "Recommendation engine assigns courier"),
        ProcessStep("awb_generated", "system", "instant", "AWB number generated for the shipment"),
        ProcessStep("label_printed", "seller", "0-24 hours", "Seller prints shipping label"),
        ProcessStep("manifest_created", "seller", "0-24 hours", "Seller creates pickup manifest"),
        ProcessStep("pickup_scheduled", "system", "0-4 hours", "Pickup request sent to courier"),
        ProcessStep("picked_up", "courier", "24-48 hours", "Courier picks up package from seller"),
        ProcessStep("in_transit", "courier", "1-7 days", "Package moving through courier network"),
        ProcessStep("out_for_delivery", "courier", "4-8 hours", "Package out for final delivery"),
        ProcessStep("delivered", "courier", "instant", "Package delivered to buyer"),
        ProcessStep("cod_collected", "courier", "0-24 hours", "COD amount collected from buyer", "payment_mode == COD"),
        ProcessStep("cod_remitted", "shiprocket", "T+2 to T+8 days", "COD remitted to seller wallet", "payment_mode == COD"),
    ],
    "valid_transitions": {
        "order_placed": ["order_confirmed", "cancelled"],
        "order_confirmed": ["invoice_generated", "cancelled"],
        "invoice_generated": ["courier_assigned", "cancelled"],
        "courier_assigned": ["awb_generated", "cancelled"],
        "awb_generated": ["label_printed", "cancelled"],
        "label_printed": ["manifest_created", "cancelled"],
        "manifest_created": ["pickup_scheduled", "cancelled"],
        "pickup_scheduled": ["picked_up", "pickup_failed"],
        "picked_up": ["in_transit", "rto_initiated"],
        "in_transit": ["out_for_delivery", "rto_initiated", "lost_in_transit"],
        "out_for_delivery": ["delivered", "ndr"],
        "ndr": ["out_for_delivery", "rto_initiated"],  # reattempt or RTO
        "delivered": ["cod_collected", "return_requested"],
        "cod_collected": ["cod_remitted"],
    },
    "failure_branches": {
        "pickup_scheduled": {"pickup_failed": "Courier failed to pick up. Reschedule or change courier."},
        "in_transit": {"lost_in_transit": "Package lost. Initiate claim process."},
        "out_for_delivery": {"ndr": "Delivery failed (NDR). Reattempt or initiate RTO."},
        "delivered": {"return_requested": "Buyer requests return. Initiate reverse pickup."},
    },
}

WEIGHT_DISPUTE = {
    "name": "weight_dispute",
    "description": "Weight discrepancy resolution between seller-declared and courier-measured weight",
    "steps": [
        ProcessStep("dispute_detected", "system", "instant", "System detects weight mismatch > threshold"),
        ProcessStep("evidence_collected", "system", "0-1 hours", "Weight images and data collected"),
        ProcessStep("seller_notified", "system", "instant", "Seller notified of discrepancy"),
        ProcessStep("seller_response", "seller", "7 days deadline", "Seller accepts or disputes with evidence"),
        ProcessStep("review", "ops_team", "1-3 days", "Ops team reviews evidence"),
        ProcessStep("resolved", "system", "instant", "Dispute resolved — charges adjusted or upheld"),
    ],
    "valid_transitions": {
        "dispute_detected": ["evidence_collected"],
        "evidence_collected": ["seller_notified"],
        "seller_notified": ["seller_response", "auto_accepted"],
        "seller_response": ["review", "auto_resolved"],
        "review": ["resolved"],
    },
    "failure_branches": {
        "seller_notified": {"auto_accepted": "Seller didn't respond within 7 days — auto-accepted courier weight."},
    },
}

COD_REMITTANCE = {
    "name": "cod_remittance",
    "description": "COD collection from buyer and remittance to seller wallet",
    "steps": [
        ProcessStep("delivery_confirmed", "courier", "instant"),
        ProcessStep("cod_collected", "courier", "0-24 hours"),
        ProcessStep("cod_deposited", "courier", "1-3 days", "Courier deposits collected COD"),
        ProcessStep("reconciled", "shiprocket", "1-2 days", "Shiprocket reconciles COD receipts"),
        ProcessStep("deductions_applied", "shiprocket", "instant", "Shipping charges, COD fee deducted"),
        ProcessStep("remitted_to_seller", "shiprocket", "instant", "Net amount credited to seller wallet"),
    ],
    "valid_transitions": {
        "delivery_confirmed": ["cod_collected"],
        "cod_collected": ["cod_deposited"],
        "cod_deposited": ["reconciled"],
        "reconciled": ["deductions_applied"],
        "deductions_applied": ["remitted_to_seller"],
    },
    "failure_branches": {
        "cod_collected": {"collection_failed": "Buyer didn't pay COD. Mark as failed delivery."},
        "cod_deposited": {"deposit_delayed": "Courier delayed deposit. Escalate to carrier ops."},
    },
}

NDR_RESOLUTION = {
    "name": "ndr_resolution",
    "description": "Non-Delivery Report resolution — reattempt or return to origin",
    "steps": [
        ProcessStep("ndr_received", "courier", "instant", "Courier reports delivery failure"),
        ProcessStep("reason_classified", "system", "instant", "NDR reason auto-classified"),
        ProcessStep("buyer_contacted", "system", "0-2 hours", "Automated message sent to buyer"),
        ProcessStep("action_decided", "system", "24 hours", "System or operator decides: reattempt or RTO"),
        ProcessStep("reattempt_scheduled", "courier", "24-48 hours", "New delivery attempt scheduled"),
        ProcessStep("resolved", "courier", "instant", "Delivered on reattempt OR RTO initiated"),
    ],
    "valid_transitions": {
        "ndr_received": ["reason_classified"],
        "reason_classified": ["buyer_contacted"],
        "buyer_contacted": ["action_decided"],
        "action_decided": ["reattempt_scheduled", "rto_initiated"],
        "reattempt_scheduled": ["resolved", "ndr_received"],  # Can NDR again
    },
    "failure_branches": {
        "reattempt_scheduled": {"ndr_received": "Delivery failed again. Max 3 attempts before auto-RTO."},
    },
}

RETURN_PROCESS = {
    "name": "return_process",
    "description": "Buyer return request through reverse pickup and refund",
    "steps": [
        ProcessStep("return_requested", "buyer", "instant"),
        ProcessStep("return_approved", "seller", "0-48 hours", "Seller approves/rejects return"),
        ProcessStep("reverse_pickup_scheduled", "system", "0-4 hours"),
        ProcessStep("reverse_picked_up", "courier", "24-48 hours"),
        ProcessStep("reverse_in_transit", "courier", "1-5 days"),
        ProcessStep("return_delivered", "courier", "instant", "Package returned to seller"),
        ProcessStep("quality_check", "seller", "1-3 days", "Seller verifies returned item"),
        ProcessStep("refund_processed", "shiprocket", "3-7 days"),
    ],
    "valid_transitions": {
        "return_requested": ["return_approved", "return_rejected"],
        "return_approved": ["reverse_pickup_scheduled"],
        "reverse_pickup_scheduled": ["reverse_picked_up", "reverse_pickup_failed"],
        "reverse_picked_up": ["reverse_in_transit"],
        "reverse_in_transit": ["return_delivered"],
        "return_delivered": ["quality_check"],
        "quality_check": ["refund_processed", "refund_rejected"],
    },
    "failure_branches": {},
}

ALL_PROCESSES = {
    "order_to_delivery": ORDER_TO_DELIVERY,
    "weight_dispute": WEIGHT_DISPUTE,
    "cod_remittance": COD_REMITTANCE,
    "ndr_resolution": NDR_RESOLUTION,
    "return_process": RETURN_PROCESS,
}

# Status → process + step mapping (for automatic detection)
STATUS_TO_PROCESS = {
    # Order statuses
    "new": ("order_to_delivery", "order_placed"),
    "confirmed": ("order_to_delivery", "order_confirmed"),
    "invoiced": ("order_to_delivery", "invoice_generated"),
    "ready_to_ship": ("order_to_delivery", "label_printed"),
    "pickup_scheduled": ("order_to_delivery", "pickup_scheduled"),
    "pickup_pending": ("order_to_delivery", "pickup_scheduled"),
    "picked_up": ("order_to_delivery", "picked_up"),
    "in_transit": ("order_to_delivery", "in_transit"),
    "out_for_delivery": ("order_to_delivery", "out_for_delivery"),
    "ofd": ("order_to_delivery", "out_for_delivery"),
    "delivered": ("order_to_delivery", "delivered"),
    "cancelled": ("order_to_delivery", "cancelled"),
    "rto_initiated": ("order_to_delivery", "rto_initiated"),
    "rto_delivered": ("order_to_delivery", "rto_delivered"),
    # NDR statuses
    "ndr": ("ndr_resolution", "ndr_received"),
    "ndr_actionable": ("ndr_resolution", "action_decided"),
    # Weight dispute statuses
    "dispute_open": ("weight_dispute", "dispute_detected"),
    "dispute_pending": ("weight_dispute", "seller_response"),
    "dispute_resolved": ("weight_dispute", "resolved"),
    # Return statuses
    "return_requested": ("return_process", "return_requested"),
    "return_approved": ("return_process", "return_approved"),
    "return_picked_up": ("return_process", "reverse_picked_up"),
}


class ProcessEngine:
    """Provides business process context for any entity based on its current status."""

    def get_process_position(self, current_status: str) -> Optional[ProcessPosition]:
        """Given a status string, determine process position."""
        status_lower = current_status.lower().strip().replace(" ", "_")

        mapping = STATUS_TO_PROCESS.get(status_lower)
        if not mapping:
            return None

        process_name, step_name = mapping
        process = ALL_PROCESSES.get(process_name)
        if not process:
            return None

        steps = process["steps"]
        step_names = [s.name for s in steps]

        if step_name not in step_names:
            return None

        idx = step_names.index(step_name)
        total = len(steps)
        transitions = process.get("valid_transitions", {})
        failures = process.get("failure_branches", {})

        # Valid actions from current state
        valid_next = transitions.get(step_name, [])

        # Risk factors at current state
        risks = []
        if step_name in failures:
            for fail_state, desc in failures[step_name].items():
                risks.append(f"{fail_state}: {desc}")

        # Failure branches reachable from current state
        failure_list = []
        if step_name in failures:
            for fail_state, desc in failures[step_name].items():
                failure_list.append({"state": fail_state, "description": desc})

        return ProcessPosition(
            process_name=process_name,
            current_step=step_name,
            step_number=idx + 1,
            total_steps=total,
            next_step=step_names[idx + 1] if idx + 1 < total else None,
            previous_step=step_names[idx - 1] if idx > 0 else None,
            percent_complete=round((idx + 1) / total * 100, 1),
            valid_actions=valid_next,
            risk_factors=risks,
            failure_branches=failure_list,
            estimated_remaining=self._estimate_remaining(steps, idx),
        )

    def get_context_for_llm(self, current_status: str) -> str:
        """Generate a human-readable process context string for Claude's prompt."""
        pos = self.get_process_position(current_status)
        if not pos:
            return ""

        lines = [
            f"[Process: {pos.process_name.replace('_', ' ').title()}]",
            f"Current step: {pos.current_step} (step {pos.step_number}/{pos.total_steps}, {pos.percent_complete}% complete)",
        ]

        if pos.next_step:
            lines.append(f"Next expected step: {pos.next_step}")

        if pos.valid_actions:
            lines.append(f"Valid actions from here: {', '.join(pos.valid_actions)}")

        if pos.risk_factors:
            lines.append("Risk factors at this stage:")
            for risk in pos.risk_factors:
                lines.append(f"  - {risk}")

        if pos.estimated_remaining:
            lines.append(f"Estimated time remaining: {pos.estimated_remaining}")

        return "\n".join(lines)

    def get_valid_actions(self, current_status: str) -> List[str]:
        """Return list of valid actions from the current state."""
        pos = self.get_process_position(current_status)
        return pos.valid_actions if pos else []

    def is_transition_valid(self, from_status: str, to_status: str) -> bool:
        """Check if a state transition is valid."""
        pos = self.get_process_position(from_status)
        if not pos:
            return False
        return to_status in pos.valid_actions

    def _estimate_remaining(self, steps: List[ProcessStep], current_idx: int) -> str:
        """Estimate remaining time based on average step durations."""
        remaining_steps = steps[current_idx + 1:]
        if not remaining_steps:
            return "Complete"

        # Parse durations and sum
        total_hours = 0
        for step in remaining_steps:
            dur = step.avg_duration.lower()
            if "day" in dur:
                # Extract max days
                parts = dur.replace("days", "").replace("day", "").strip().split("-")
                max_days = float(parts[-1].strip().replace("t+", ""))
                total_hours += max_days * 24
            elif "hour" in dur:
                parts = dur.replace("hours", "").replace("hour", "").strip().split("-")
                total_hours += float(parts[-1].strip())
            elif "minute" in dur:
                parts = dur.replace("minutes", "").replace("minute", "").strip().split("-")
                total_hours += float(parts[-1].strip()) / 60

        if total_hours < 1:
            return "< 1 hour"
        elif total_hours < 24:
            return f"~{int(total_hours)} hours"
        else:
            return f"~{int(total_hours / 24)} days"

"""
Tournament A/B API — ICRM users see two responses and pick the better one.

POST /cosmos/api/v1/tournament/generate     — Generate A/B pair for a query
POST /cosmos/api/v1/tournament/preference   — Record user's choice
GET  /cosmos/api/v1/tournament/stats        — Win rates and training readiness
GET  /cosmos/api/v1/tournament/training     — Export DPO training triples
GET  /cosmos/api/v1/tournament/needs-annotation — Pairs needing more annotators
GET  /cosmos/api/v1/tournament/failures     — Failure report (both_bad domains)
"""

from typing import Optional

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = structlog.get_logger()
router = APIRouter()


class TournamentRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    user_id: str
    company_id: str = ""
    entity_type: Optional[str] = None


class PreferenceRequest(BaseModel):
    pair_id: str
    user_id: str
    preference: str = Field(..., pattern="^(answer_1|answer_2|both_good|both_bad)$")
    # answer_1 or answer_2 (blind — user doesn't know which is A/B)
    # Backend maps answer_1/answer_2 back to lane A/B using stored _mapping
    reason_tags: list = Field(default_factory=list)
    # Structured: ["more_accurate", "more_complete", "better_evidence"]
    reason_text: Optional[str] = None
    # Optional free text
    time_to_decide_ms: float = 0.0
    adopted_answer: Optional[str] = None
    # Which answer to put into case history: "answer_1" or "answer_2"
    # Only the adopted answer goes into the thread — prevents noisy history


def _get_tournament(request: Request):
    return getattr(request.app.state, "tournament_ab", None)


@router.post("/generate")
async def generate_pair(request: Request, req: TournamentRequest):
    """
    Generate two responses using different retrieval lanes.
    ICRM users only (company_id=1). Sellers get 403.
    """
    if req.company_id != "1":
        return {"error": "Tournament mode is only available for ICRM users"}

    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}

    pair = await tournament.generate_pair(
        query=req.message,
        entity_type=req.entity_type,
        user_id=req.user_id,
    )

    if not pair:
        return {"error": "Could not generate A/B pair (shadow lane may be disabled)"}

    # Store pair before showing
    await tournament.store_pair(pair, req.user_id)

    # BLIND A/B: no model names, no confidence, no lane labels
    # Randomized order to prevent position bias
    ordered = []
    for label in pair.display_order:
        resp = pair.response_a if label == "A" else pair.response_b
        ordered.append({
            "content": resp.content,
            # Internal tracking only (not shown to user)
            "_lane": label,
        })

    return {
        "pair_id": pair.pair_id,
        "query": pair.query,
        "answer_1": {"content": ordered[0]["content"]},
        "answer_2": {"content": ordered[1]["content"]},
        # Internal mapping (frontend stores but does NOT display)
        "_mapping": {
            "answer_1": ordered[0]["_lane"],
            "answer_2": ordered[1]["_lane"],
        },
        "reason_tags": [
            "more_accurate",
            "more_complete",
            "more_actionable",
            "easier_to_understand",
            "better_evidence",
            "safer",
            "faster_to_use",
        ],
        "instructions": "Pick the better answer. Optionally select reason tags.",
    }


@router.post("/preference")
async def record_preference(request: Request, req: PreferenceRequest):
    """
    Record which response the ICRM user preferred.
    User picks answer_1 or answer_2 (blind). Backend maps to lane A/B.
    Only the adopted answer goes into case/thread history.
    """
    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}

    from app.services.tournament_ab import ABPreference
    pref = ABPreference(
        pair_id=req.pair_id,
        user_id=req.user_id,
        preference=req.preference,  # answer_1, answer_2, both_good, both_bad
        reason=f"tags:{','.join(req.reason_tags)}|text:{req.reason_text or ''}",
        time_to_decide_ms=req.time_to_decide_ms,
    )

    success = await tournament.record_preference(pref)

    return {
        "success": success,
        "preference": req.preference,
        "adopted": req.adopted_answer or req.preference,
        "note": "Only the adopted answer will appear in case history",
    }


@router.get("/stats")
async def tournament_stats(request: Request):
    """Get win rates and training readiness (includes temporal weighting)."""
    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}
    return await tournament.get_stats()


@router.get("/training")
async def export_training_data(request: Request):
    """
    Export preference data for curation — NOT auto-finetune.

    500+ preferences = ready for CURATION, not auto-training.
    Use this data for: eval sets, routing decisions, reranker training,
    confidence calibration. Only fine-tune after dedup, quality filter,
    safety review, and offline eval pass.

    Filters:
    - Only pairs with agreement_score >= 0.7 (inter-annotator gate)
    - Temporal decay weights applied (recent data weighted higher)
    """
    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}
    triples = await tournament.get_training_data()
    return {
        "triples_available": len(triples),
        "data": triples[:100],
        "filters_applied": {
            "agreement_threshold": ">= 0.7",
            "min_annotators": 3,
            "temporal_decay": "7d=1.0, 30d=0.8, 90d=0.5, older=0.2",
        },
        "recommended_uses": [
            "1. Create eval sets from high-agreement preferences",
            "2. Per-domain routing decisions (which lane wins for which query type)",
            "3. Reranker training (preference pairs as ranking signal)",
            "4. Confidence calibration (when does confidence predict user preference?)",
            "5. Answer-model fine-tuning (ONLY after dedup + safety review + offline eval)",
        ],
        "do_not": "Auto-finetune on raw preferences without curation",
    }


@router.get("/needs-annotation")
async def pairs_needing_annotation(request: Request):
    """
    Return pairs that need more ICRM annotators for inter-annotator agreement.
    These pairs have been rated by fewer than 3 users and should be shown
    to additional annotators before being treated as ground truth.
    """
    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}

    pairs = await tournament.get_pairs_needing_annotation(limit=50)
    return {
        "pairs_needing_annotation": len(pairs),
        "target_annotators_per_pair": 3,
        "pairs": pairs,
        "instructions": (
            "Show these pairs to additional ICRM users. "
            "Agreement of 3/3 = weight 1.0, 2/3 = 0.7, 1/3 = discarded."
        ),
    }


@router.get("/failures")
async def failure_report(request: Request):
    """
    Failure report: domains where users voted "both_bad" at high rates.

    Domains with >20% "both_bad" rate are flagged as needing more training data.
    Use this to prioritize corpus improvements and identify retrieval blind spots.
    """
    tournament = _get_tournament(request)
    if not tournament:
        return {"error": "Tournament A/B not initialized"}

    report = await tournament.get_failure_report()
    return report

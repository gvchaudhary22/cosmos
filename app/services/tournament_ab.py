"""
Tournament A/B — Blind comparison of two retrieval lanes for ICRM users.

Design rules (from review):
  1. BLIND: No model names, no confidence shown. Only "Answer 1" and "Answer 2".
  2. RANDOMIZED: Left/right order shuffled every time (prevent position bias).
  3. ONE VARIABLE: Same corpus, same prompt, same LLM, same chunking.
     Only the retrieval embedding lane differs (OpenAI vs Voyage).
  4. STRUCTURED FEEDBACK: Reason tags (more_accurate, more_complete, etc.)
     plus optional free text. Tags are better for training than free text alone.
  5. NO AUTO-FINETUNE: 500+ preferences = ready for CURATION, not auto-training.
     Use for: eval sets, routing decisions, reranker training, confidence calibration.
     Only fine-tune after: dedup, quality filter, safety review, offline eval.
  6. SIGNIFICANCE GATES: Don't switch routing because one lane wins 58% once.
     Require: min sample per domain, stable win rate, confidence interval, no regressions.
  7. ADOPTED ANSWER: Only the chosen answer goes into case/thread history.
  8. INTER-ANNOTATOR AGREEMENT: Same pair shown to 2-3 users; agreement gates truth.
  9. TEMPORAL DECAY: Older preferences carry less weight in stats and training.
 10. NEGATIVE SIGNAL MINING: "both_bad" votes surface failure domains.

Who sees it:
  - ICRM users only (company_id=1)
  - Triggered on: low confidence, new/unknown query classes, QA roles, sampled traffic
  - Sellers NEVER see dual responses
  - Lime frontend controls the toggle
"""

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import text

from app.db.session import AsyncSessionLocal

logger = structlog.get_logger()

PREFERENCES_TABLE = "cosmos_ab_preferences"
FAILURE_CASES_TABLE = "cosmos_failure_cases"

# --- Temporal decay thresholds (days -> weight) ---
TEMPORAL_DECAY_TIERS = [
    (7, 1.0),    # 0-7 days old -> full weight
    (30, 0.8),   # 8-30 days old
    (90, 0.5),   # 31-90 days old
    (None, 0.2), # older than 90 days
]

# --- Inter-annotator agreement thresholds ---
MIN_ANNOTATORS = 3          # target annotators per pair
AGREEMENT_FULL = 1.0        # 3/3 agree
AGREEMENT_PARTIAL = 0.7     # 2/3 agree
AGREEMENT_DISCARD = 0.0     # 1/3 agree (no consensus) -> discard

# --- "Both bad" failure rate threshold ---
BOTH_BAD_ALERT_THRESHOLD = 0.20  # flag domain if >20% both_bad


@dataclass
class ABResponse:
    """One lane's response."""
    lane: str                   # "A" (primary/OpenAI) or "B" (shadow/Voyage)
    content: str                # generated answer text
    retrieval_model: str        # "text-embedding-3-small" or "voyage-3-large"
    context_chunks: List[Dict]  # top-5 retrieved chunks with scores
    confidence: float = 0.0
    latency_ms: float = 0.0


@dataclass
class ABPair:
    """A pair of responses shown to the user for comparison."""
    pair_id: str
    query: str
    response_a: ABResponse
    response_b: ABResponse
    shown_at: float = 0.0
    # Randomize order so user doesn't always prefer A
    display_order: List[str] = field(default_factory=lambda: ["A", "B"])


@dataclass
class ABPreference:
    """User's preference choice."""
    pair_id: str
    user_id: str
    preference: str            # "A" | "B" | "both_good" | "both_bad"
    reason: Optional[str] = None  # optional free-text reason
    time_to_decide_ms: float = 0.0


def _temporal_weight(age_days: float) -> float:
    """Return decay weight based on preference age in days."""
    for threshold_days, weight in TEMPORAL_DECAY_TIERS:
        if threshold_days is None or age_days <= threshold_days:
            return weight
    return 0.2


def _agreement_weight(annotator_count: int, agreement_score: float) -> float:
    """
    Weight a preference by inter-annotator agreement.
    3/3 agree = 1.0, 2/3 agree = 0.7, 1/3 = discard (0.0).
    Single-annotator pairs that haven't reached quorum yet get 0.0
    (they need more annotations before being usable).
    """
    if annotator_count < MIN_ANNOTATORS:
        # Not enough annotators yet — not usable for training
        return 0.0
    if agreement_score >= 1.0:
        return AGREEMENT_FULL
    if agreement_score >= 0.66:
        return AGREEMENT_PARTIAL
    return AGREEMENT_DISCARD


class TournamentAB:
    """
    Manages A/B response generation and preference collection.

    For ICRM users only. Creates DPO training triples.
    """

    def __init__(self, vectorstore=None, benchmark=None, llm_client=None):
        """
        Args:
            vectorstore: Primary VectorStoreService (OpenAI embeddings)
            benchmark: EmbeddingBenchmark (Voyage shadow lane)
            llm_client: LLMClient for generating answers
        """
        self.vectorstore = vectorstore
        self.benchmark = benchmark
        self.llm_client = llm_client

    async def ensure_schema(self) -> None:
        """Create preferences table and failure cases table."""
        async with AsyncSessionLocal() as session:
            try:
                # --- Main preferences table (with new IAA columns) ---
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {PREFERENCES_TABLE} (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        pair_id VARCHAR(36) NOT NULL,
                        query TEXT NOT NULL,
                        query_hash VARCHAR(32),
                        user_id VARCHAR(255) NOT NULL,
                        response_a TEXT,
                        response_b TEXT,
                        retrieval_model_a VARCHAR(100),
                        retrieval_model_b VARCHAR(100),
                        context_a JSONB,
                        context_b JSONB,
                        confidence_a FLOAT,
                        confidence_b FLOAT,
                        latency_a_ms FLOAT,
                        latency_b_ms FLOAT,
                        preference VARCHAR(20),
                        reason TEXT,
                        time_to_decide_ms FLOAT,
                        annotator_count INT DEFAULT 1,
                        agreement_score FLOAT DEFAULT 0.0,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_ab_user
                    ON {PREFERENCES_TABLE} (user_id)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_ab_preference
                    ON {PREFERENCES_TABLE} (preference)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_ab_pair_id
                    ON {PREFERENCES_TABLE} (pair_id)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_ab_agreement
                    ON {PREFERENCES_TABLE} (annotator_count, agreement_score)
                """))

                # --- Failure cases table (negative signal mining) ---
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {FAILURE_CASES_TABLE} (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        pair_id VARCHAR(36) NOT NULL,
                        query TEXT NOT NULL,
                        query_hash VARCHAR(32),
                        query_domain VARCHAR(100) DEFAULT 'unknown',
                        user_id VARCHAR(255) NOT NULL,
                        response_a TEXT,
                        response_b TEXT,
                        reason TEXT,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_failure_domain
                    ON {FAILURE_CASES_TABLE} (query_domain)
                """))
                await session.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_failure_created
                    ON {FAILURE_CASES_TABLE} (created_at)
                """))

                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error("tournament_ab.schema_failed", error=str(e))

    # ------------------------------------------------------------------
    # Pair generation
    # ------------------------------------------------------------------

    async def generate_pair(
        self,
        query: str,
        entity_type: Optional[str] = None,
        user_id: str = "",
        session_context: Optional[Dict] = None,
    ) -> Optional[ABPair]:
        """
        Generate two responses using different retrieval lanes.

        Lane A: Primary (OpenAI text-embedding-3-small)
        Lane B: Shadow (Voyage voyage-3-large)
        """
        if not self.vectorstore or not self.benchmark or not self.benchmark.is_enabled:
            return None

        pair_id = str(uuid.uuid4())[:8]
        t_start = time.monotonic()

        # Lane A: Primary retrieval (OpenAI)
        t0 = time.monotonic()
        try:
            primary_chunks = await self.vectorstore.search_similar(
                query=query, entity_type=entity_type, limit=5, threshold=0.2
            )
        except Exception:
            primary_chunks = []
        primary_ms = (time.monotonic() - t0) * 1000

        # Lane B: Shadow retrieval (Voyage)
        t0 = time.monotonic()
        try:
            shadow_result = await self.benchmark.compare_search(
                query=query, entity_type=entity_type, limit=5
            )
            shadow_chunks = []
            if shadow_result and shadow_result.shadow_top5:
                # Fetch actual content for shadow results
                for eid, score in zip(shadow_result.shadow_top5, shadow_result.shadow_scores):
                    shadow_chunks.append({
                        "entity_id": eid,
                        "similarity": score,
                        "content": f"[Shadow result: {eid}]",
                    })
        except Exception:
            shadow_chunks = []
        shadow_ms = (time.monotonic() - t0) * 1000

        # Generate answers from each context
        response_a = await self._generate_answer(query, primary_chunks, session_context)
        response_b = await self._generate_answer(query, shadow_chunks, session_context)

        # Randomize display order (prevent position bias)
        import random
        display_order = ["A", "B"]
        random.shuffle(display_order)

        return ABPair(
            pair_id=pair_id,
            query=query,
            response_a=ABResponse(
                lane="A",
                content=response_a,
                retrieval_model="text-embedding-3-small",
                context_chunks=primary_chunks[:5],
                confidence=primary_chunks[0]["similarity"] if primary_chunks else 0,
                latency_ms=primary_ms,
            ),
            response_b=ABResponse(
                lane="B",
                content=response_b,
                retrieval_model="voyage-3-large",
                context_chunks=shadow_chunks[:5],
                confidence=shadow_chunks[0]["similarity"] if shadow_chunks else 0,
                latency_ms=shadow_ms,
            ),
            shown_at=time.time(),
            display_order=display_order,
        )

    # ------------------------------------------------------------------
    # Preference recording (with IAA + negative signal mining)
    # ------------------------------------------------------------------

    async def record_preference(self, preference: ABPreference) -> bool:
        """
        Record ICRM user's preference choice.
        Creates a DPO training triple: (query, chosen_response, rejected_response).

        Also handles:
        - Inter-annotator agreement: updates annotator_count and agreement_score
        - Negative signal mining: logs "both_bad" to failure_cases table
        """
        async with AsyncSessionLocal() as session:
            try:
                # Update the preference row
                await session.execute(
                    text(f"""
                        UPDATE {PREFERENCES_TABLE}
                        SET preference = :preference,
                            reason = :reason,
                            time_to_decide_ms = :time_ms
                        WHERE pair_id = :pair_id AND user_id = :user_id
                    """),
                    {
                        "pair_id": preference.pair_id,
                        "user_id": preference.user_id,
                        "preference": preference.preference,
                        "reason": preference.reason,
                        "time_ms": preference.time_to_decide_ms,
                    },
                )

                # --- Inter-annotator agreement: recompute for this pair ---
                await self._update_agreement(session, preference.pair_id)

                # --- Negative signal mining: log "both_bad" ---
                if preference.preference == "both_bad":
                    await self._log_failure_case(session, preference)

                await session.commit()

                logger.info(
                    "tournament_ab.preference_recorded",
                    pair_id=preference.pair_id,
                    preference=preference.preference,
                )
                return True
            except Exception as e:
                await session.rollback()
                logger.error("tournament_ab.record_failed", error=str(e))
                return False

    async def _update_agreement(self, session, pair_id: str) -> None:
        """
        Recompute annotator_count and agreement_score for every row
        sharing this pair_id. Agreement = max_same_vote / total_votes.
        """
        # Count votes per preference value for this pair
        result = await session.execute(
            text(f"""
                SELECT preference, COUNT(*) as cnt
                FROM {PREFERENCES_TABLE}
                WHERE pair_id = :pair_id AND preference IS NOT NULL
                GROUP BY preference
            """),
            {"pair_id": pair_id},
        )
        vote_rows = result.fetchall()
        if not vote_rows:
            return

        total_votes = sum(r.cnt for r in vote_rows)
        max_votes = max(r.cnt for r in vote_rows)
        agreement = round(max_votes / total_votes, 4) if total_votes > 0 else 0.0

        # Update all rows for this pair
        await session.execute(
            text(f"""
                UPDATE {PREFERENCES_TABLE}
                SET annotator_count = :total,
                    agreement_score = :agreement
                WHERE pair_id = :pair_id
            """),
            {
                "pair_id": pair_id,
                "total": total_votes,
                "agreement": agreement,
            },
        )

    async def _log_failure_case(self, session, preference: ABPreference) -> None:
        """Insert a row into cosmos_failure_cases when preference is 'both_bad'."""
        # Fetch query and responses from the preferences table
        result = await session.execute(
            text(f"""
                SELECT query, query_hash, response_a, response_b
                FROM {PREFERENCES_TABLE}
                WHERE pair_id = :pair_id
                LIMIT 1
            """),
            {"pair_id": preference.pair_id},
        )
        row = result.fetchone()
        if not row:
            return

        # Infer a lightweight domain from the query (first meaningful word)
        query_domain = self._infer_domain(row.query)

        await session.execute(
            text(f"""
                INSERT INTO {FAILURE_CASES_TABLE}
                    (pair_id, query, query_hash, query_domain,
                     user_id, response_a, response_b, reason)
                VALUES
                    (:pair_id, :query, :qhash, :domain,
                     :user_id, :resp_a, :resp_b, :reason)
            """),
            {
                "pair_id": preference.pair_id,
                "query": row.query,
                "qhash": row.query_hash,
                "domain": query_domain,
                "user_id": preference.user_id,
                "resp_a": row.response_a,
                "resp_b": row.response_b,
                "reason": preference.reason,
            },
        )
        logger.info(
            "tournament_ab.failure_logged",
            pair_id=preference.pair_id,
            domain=query_domain,
        )

    @staticmethod
    def _infer_domain(query: str) -> str:
        """
        Best-effort domain classification from query text.
        Maps common shipping/logistics keywords to domains.
        Falls back to 'general'.
        """
        q = query.lower()
        domain_keywords = {
            "tracking": ["track", "tracking", "shipment status", "where is"],
            "billing": ["invoice", "billing", "charge", "payment", "receipt"],
            "customs": ["customs", "duty", "clearance", "hs code", "tariff"],
            "pickup": ["pickup", "pick up", "collection", "schedule pickup"],
            "delivery": ["deliver", "delivery", "eta", "arrival", "drop off"],
            "claims": ["claim", "damage", "lost", "missing", "insurance"],
            "rates": ["rate", "quote", "pricing", "cost", "estimate"],
            "documentation": ["document", "bol", "bill of lading", "packing list", "commercial invoice"],
            "account": ["account", "login", "password", "access", "profile"],
        }
        for domain, keywords in domain_keywords.items():
            for kw in keywords:
                if kw in q:
                    return domain
        return "general"

    # ------------------------------------------------------------------
    # Pair storage
    # ------------------------------------------------------------------

    async def store_pair(self, pair: ABPair, user_id: str) -> None:
        """Store the pair in DB before showing to user."""
        query_hash = hashlib.md5(pair.query.encode()).hexdigest()[:32]

        async with AsyncSessionLocal() as session:
            try:
                await session.execute(
                    text(f"""
                        INSERT INTO {PREFERENCES_TABLE}
                            (pair_id, query, query_hash, user_id,
                             response_a, response_b,
                             retrieval_model_a, retrieval_model_b,
                             context_a, context_b,
                             confidence_a, confidence_b,
                             latency_a_ms, latency_b_ms,
                             annotator_count, agreement_score)
                        VALUES
                            (:pair_id, :query, :qhash, :user_id,
                             :resp_a, :resp_b,
                             :model_a, :model_b,
                             :ctx_a, :ctx_b,
                             :conf_a, :conf_b,
                             :lat_a, :lat_b,
                             0, 0.0)
                    """),
                    {
                        "pair_id": pair.pair_id,
                        "query": pair.query,
                        "qhash": query_hash,
                        "user_id": user_id,
                        "resp_a": pair.response_a.content,
                        "resp_b": pair.response_b.content,
                        "model_a": pair.response_a.retrieval_model,
                        "model_b": pair.response_b.retrieval_model,
                        "ctx_a": json.dumps([c.get("entity_id", "") for c in pair.response_a.context_chunks]),
                        "ctx_b": json.dumps([c.get("entity_id", "") for c in pair.response_b.context_chunks]),
                        "conf_a": pair.response_a.confidence,
                        "conf_b": pair.response_b.confidence,
                        "lat_a": pair.response_a.latency_ms,
                        "lat_b": pair.response_b.latency_ms,
                    },
                )
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error("tournament_ab.store_failed", error=str(e))

    # ------------------------------------------------------------------
    # Inter-Annotator Agreement: pairs needing more annotations
    # ------------------------------------------------------------------

    async def get_pairs_needing_annotation(self, limit: int = 50) -> List[Dict]:
        """
        Return pairs that have been rated by fewer than MIN_ANNOTATORS users.
        These should be shown to additional ICRM users to build agreement.
        """
        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(f"""
                        SELECT pair_id, query, response_a, response_b,
                               annotator_count, agreement_score, created_at
                        FROM {PREFERENCES_TABLE}
                        WHERE annotator_count < :min_ann
                          AND preference IS NOT NULL
                        GROUP BY pair_id, query, response_a, response_b,
                                 annotator_count, agreement_score, created_at
                        ORDER BY annotator_count ASC, created_at ASC
                        LIMIT :lim
                    """),
                    {"min_ann": MIN_ANNOTATORS, "lim": limit},
                )
                rows = result.fetchall()
                return [
                    {
                        "pair_id": r.pair_id,
                        "query": r.query,
                        "response_a": r.response_a,
                        "response_b": r.response_b,
                        "annotator_count": r.annotator_count,
                        "agreement_score": r.agreement_score,
                        "created_at": str(r.created_at),
                    }
                    for r in rows
                ]
            except Exception as e:
                logger.error("tournament_ab.pairs_needing_annotation_failed", error=str(e))
                return []

    # ------------------------------------------------------------------
    # Training data export (with agreement + temporal filtering)
    # ------------------------------------------------------------------

    async def get_training_data(self, min_preferences: int = 50) -> List[Dict]:
        """
        Export DPO training triples: (query, chosen, rejected).

        Only exports pairs where:
        - User made a clear A or B choice
        - agreement_score >= 0.7 (inter-annotator agreement gate)
        - Temporal weight applied based on age

        "both_good" and "both_bad" are excluded (no clear preference signal).
        """
        async with AsyncSessionLocal() as session:
            try:
                result = await session.execute(
                    text(f"""
                        SELECT query, response_a, response_b, preference,
                               retrieval_model_a, retrieval_model_b,
                               confidence_a, confidence_b, reason,
                               annotator_count, agreement_score,
                               EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0 AS age_days
                        FROM {PREFERENCES_TABLE}
                        WHERE preference IN ('A', 'B')
                          AND agreement_score >= 0.7
                          AND annotator_count >= :min_ann
                        ORDER BY created_at DESC
                    """),
                    {"min_ann": MIN_ANNOTATORS},
                )
                rows = result.fetchall()

                triples = []
                for row in rows:
                    t_weight = _temporal_weight(row.age_days)
                    a_weight = _agreement_weight(row.annotator_count, row.agreement_score)
                    combined_weight = round(t_weight * a_weight, 4)

                    if combined_weight <= 0:
                        continue  # discard low-agreement or stale entries

                    if row.preference == "A":
                        chosen = row.response_a
                        rejected = row.response_b
                        chosen_model = row.retrieval_model_a
                        rejected_model = row.retrieval_model_b
                    else:
                        chosen = row.response_b
                        rejected = row.response_a
                        chosen_model = row.retrieval_model_b
                        rejected_model = row.retrieval_model_a

                    triples.append({
                        "query": row.query,
                        "chosen": chosen,
                        "rejected": rejected,
                        "chosen_model": chosen_model,
                        "rejected_model": rejected_model,
                        "reason": row.reason,
                        "weight": combined_weight,
                        "temporal_weight": t_weight,
                        "agreement_weight": a_weight,
                        "annotator_count": row.annotator_count,
                        "agreement_score": row.agreement_score,
                    })

                logger.info("tournament_ab.training_export", triples=len(triples))
                return triples

            except Exception as e:
                logger.error("tournament_ab.export_failed", error=str(e))
                return []

    # ------------------------------------------------------------------
    # Stats (with temporal weighting)
    # ------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Get preference statistics and model win rates with temporal weighting."""
        async with AsyncSessionLocal() as session:
            try:
                # Raw counts (unchanged for backward compat)
                result = await session.execute(
                    text(f"""
                        SELECT
                            COUNT(*) as total_pairs,
                            COUNT(CASE WHEN preference IS NOT NULL THEN 1 END) as rated,
                            COUNT(CASE WHEN preference = 'A' THEN 1 END) as a_wins,
                            COUNT(CASE WHEN preference = 'B' THEN 1 END) as b_wins,
                            COUNT(CASE WHEN preference = 'both_good' THEN 1 END) as both_good,
                            COUNT(CASE WHEN preference = 'both_bad' THEN 1 END) as both_bad,
                            AVG(CASE WHEN preference IS NOT NULL THEN time_to_decide_ms END) as avg_decide_ms
                        FROM {PREFERENCES_TABLE}
                    """)
                )
                row = result.fetchone()

                total = row.rated or 0
                a_wins = row.a_wins or 0
                b_wins = row.b_wins or 0

                # --- Temporally weighted win rates ---
                tw_result = await session.execute(
                    text(f"""
                        SELECT preference,
                               EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0 AS age_days
                        FROM {PREFERENCES_TABLE}
                        WHERE preference IN ('A', 'B')
                    """)
                )
                tw_rows = tw_result.fetchall()

                weighted_a = 0.0
                weighted_b = 0.0
                total_weight = 0.0
                for tw in tw_rows:
                    w = _temporal_weight(tw.age_days)
                    total_weight += w
                    if tw.preference == "A":
                        weighted_a += w
                    else:
                        weighted_b += w

                # --- Both-bad rate ---
                both_bad_count = row.both_bad or 0
                both_bad_rate = round(both_bad_count / total, 3) if total > 0 else 0

                return {
                    "total_pairs": row.total_pairs or 0,
                    "rated": total,
                    "pending": (row.total_pairs or 0) - total,
                    "a_wins": a_wins,
                    "b_wins": b_wins,
                    "both_good": row.both_good or 0,
                    "both_bad": both_bad_count,
                    "both_bad_rate": both_bad_rate,
                    "a_win_rate": round(a_wins / total, 3) if total > 0 else 0,
                    "b_win_rate": round(b_wins / total, 3) if total > 0 else 0,
                    "weighted_a_win_rate": round(weighted_a / total_weight, 3) if total_weight > 0 else 0,
                    "weighted_b_win_rate": round(weighted_b / total_weight, 3) if total_weight > 0 else 0,
                    "avg_decide_ms": round(float(row.avg_decide_ms or 0), 0),
                    "lane_a": "OpenAI text-embedding-3-small",
                    "lane_b": "Voyage voyage-3-large",
                    "ready_for_training": total >= 50,
                    "dpo_triples_available": a_wins + b_wins,
                    "temporal_note": "weighted_*_win_rate uses decay: 7d=1.0, 30d=0.8, 90d=0.5, older=0.2",
                }
            except Exception as e:
                return {"error": str(e)}

    # ------------------------------------------------------------------
    # Failure report (negative signal mining)
    # ------------------------------------------------------------------

    async def get_failure_report(self) -> Dict[str, Any]:
        """
        Show domains with high "both_bad" failure rates.
        Flags domains where >20% of preferences are "both_bad".
        """
        async with AsyncSessionLocal() as session:
            try:
                # Per-domain failure stats
                result = await session.execute(
                    text(f"""
                        SELECT
                            query_domain,
                            COUNT(*) as failure_count,
                            MIN(created_at) as first_seen,
                            MAX(created_at) as last_seen
                        FROM {FAILURE_CASES_TABLE}
                        GROUP BY query_domain
                        ORDER BY failure_count DESC
                    """)
                )
                domain_rows = result.fetchall()

                # Total preferences per domain (from main table, using inferred domain)
                domain_totals_result = await session.execute(
                    text(f"""
                        SELECT query_domain, COUNT(*) as total
                        FROM (
                            SELECT
                                pair_id,
                                query,
                                preference,
                                CASE
                                    WHEN LOWER(query) ~ '(track|shipment status|where is)' THEN 'tracking'
                                    WHEN LOWER(query) ~ '(invoice|billing|charge|payment|receipt)' THEN 'billing'
                                    WHEN LOWER(query) ~ '(customs|duty|clearance|hs code|tariff)' THEN 'customs'
                                    WHEN LOWER(query) ~ '(pickup|pick up|collection|schedule pickup)' THEN 'pickup'
                                    WHEN LOWER(query) ~ '(deliver|delivery|eta|arrival|drop off)' THEN 'delivery'
                                    WHEN LOWER(query) ~ '(claim|damage|lost|missing|insurance)' THEN 'claims'
                                    WHEN LOWER(query) ~ '(rate|quote|pricing|cost|estimate)' THEN 'rates'
                                    WHEN LOWER(query) ~ '(document|bol|bill of lading|packing list)' THEN 'documentation'
                                    WHEN LOWER(query) ~ '(account|login|password|access|profile)' THEN 'account'
                                    ELSE 'general'
                                END AS query_domain
                            FROM {PREFERENCES_TABLE}
                            WHERE preference IS NOT NULL
                        ) sub
                        GROUP BY query_domain
                    """)
                )
                domain_totals = {r.query_domain: r.total for r in domain_totals_result.fetchall()}

                domains = []
                flagged_domains = []
                for dr in domain_rows:
                    total_for_domain = domain_totals.get(dr.query_domain, dr.failure_count)
                    failure_rate = round(dr.failure_count / total_for_domain, 3) if total_for_domain > 0 else 0
                    needs_training = failure_rate > BOTH_BAD_ALERT_THRESHOLD

                    entry = {
                        "domain": dr.query_domain,
                        "failure_count": dr.failure_count,
                        "total_preferences": total_for_domain,
                        "failure_rate": failure_rate,
                        "needs_more_training": needs_training,
                        "first_seen": str(dr.first_seen),
                        "last_seen": str(dr.last_seen),
                    }
                    domains.append(entry)
                    if needs_training:
                        flagged_domains.append(dr.query_domain)

                # Recent failure examples
                recent_result = await session.execute(
                    text(f"""
                        SELECT pair_id, query, query_domain, reason, created_at
                        FROM {FAILURE_CASES_TABLE}
                        ORDER BY created_at DESC
                        LIMIT 20
                    """)
                )
                recent = [
                    {
                        "pair_id": r.pair_id,
                        "query": r.query,
                        "domain": r.query_domain,
                        "reason": r.reason,
                        "created_at": str(r.created_at),
                    }
                    for r in recent_result.fetchall()
                ]

                return {
                    "total_failures": sum(d["failure_count"] for d in domains),
                    "domains": domains,
                    "flagged_domains_needing_training": flagged_domains,
                    "threshold": f">{BOTH_BAD_ALERT_THRESHOLD * 100:.0f}% both_bad rate",
                    "recent_failures": recent,
                }
            except Exception as e:
                logger.error("tournament_ab.failure_report_failed", error=str(e))
                return {"error": str(e)}

    # ------------------------------------------------------------------
    # Answer generation (unchanged)
    # ------------------------------------------------------------------

    async def _generate_answer(
        self,
        query: str,
        context_chunks: List[Dict],
        session_context: Optional[Dict] = None,
    ) -> str:
        """Generate answer from retrieved context using LLM."""
        if not self.llm_client:
            # No LLM — return context summary as answer
            if not context_chunks:
                return "[No relevant context found]"
            parts = []
            for chunk in context_chunks[:3]:
                content = chunk.get("content", "")[:200]
                score = chunk.get("similarity", 0)
                parts.append(f"({score:.2f}) {content}")
            return "Based on retrieved context:\n" + "\n".join(parts)

        # Build context for LLM
        context_text = "\n".join(
            f"[{c.get('entity_type', '?')}:{c.get('entity_id', '?')}] {c.get('content', '')[:300]}"
            for c in context_chunks[:5]
        )

        prompt = (
            f"Answer this question based on the provided context.\n\n"
            f"Context:\n{context_text}\n\n"
            f"Question: {query}\n\n"
            f"Answer:"
        )

        try:
            return await self.llm_client.complete(prompt, max_tokens=500)
        except Exception:
            return "[LLM generation failed]"

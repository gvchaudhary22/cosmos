"""
Knowledge manager for COSMOS.

Manages ICRM knowledge base for context enrichment.
Uses basic TF-IDF with math module for text similarity search (no sklearn dependency).
"""

import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from cosmos.app.db.models import KnowledgeEntry as KnowledgeEntryModel, KnowledgeCategory

logger = structlog.get_logger()

# Intent-to-category mapping for context retrieval
_INTENT_CATEGORY_MAP: Dict[str, List[str]] = {
    "lookup": ["faq", "process"],
    "explain": ["faq", "troubleshooting", "policy"],
    "act": ["process", "policy"],
    "report": ["faq", "process"],
    "navigate": ["faq"],
    "unknown": ["faq", "troubleshooting"],
}


class KnowledgeManager:
    """Manages ICRM knowledge base for context enrichment."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def add_knowledge(
        self,
        category: str,
        question: str,
        answer: str,
        source: str,
        confidence: float = 1.0,
    ) -> str:
        """Add a knowledge entry. Categories: faq/policy/process/troubleshooting."""
        try:
            cat_enum = KnowledgeCategory(category)
        except ValueError:
            raise ValueError(
                f"Invalid category: {category}. "
                f"Valid: {[c.value for c in KnowledgeCategory]}"
            )

        entry_id = str(uuid.uuid4())
        entry = KnowledgeEntryModel(
            id=uuid.UUID(entry_id),
            category=cat_enum,
            question=question,
            answer=answer,
            source=source,
            confidence=confidence,
        )

        try:
            self.db.add(entry)
            await self.db.commit()
            logger.info("knowledge.added", entry_id=entry_id, category=category)
        except Exception as exc:
            await self.db.rollback()
            logger.error("knowledge.add_failed", error=str(exc))
            raise

        return entry_id

    async def search_knowledge(
        self,
        query: str,
        category: str = None,
        limit: int = 5,
    ) -> List[dict]:
        """Search knowledge base by text similarity (TF-IDF based)."""
        filters = [KnowledgeEntryModel.enabled.is_(True)]
        if category:
            try:
                cat_enum = KnowledgeCategory(category)
                filters.append(KnowledgeEntryModel.category == cat_enum)
            except ValueError:
                pass

        stmt = select(KnowledgeEntryModel).where(and_(*filters))
        result = await self.db.execute(stmt)
        entries = result.scalars().all()

        if not entries:
            return []

        # Build corpus and score with TF-IDF
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored = []
        documents = []
        for entry in entries:
            doc_text = f"{entry.question} {entry.answer}"
            documents.append(_tokenize(doc_text))

        # IDF computation
        idf = _compute_idf(documents, query_tokens)

        for i, entry in enumerate(entries):
            doc_tokens = documents[i]
            similarity = _tfidf_similarity(query_tokens, doc_tokens, idf)
            if similarity > 0:
                scored.append((similarity, entry))

        # Sort by similarity descending
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, entry in scored[:limit]:
            # Increment usage count
            entry.usage_count = (entry.usage_count or 0) + 1
            results.append({
                "id": str(entry.id),
                "category": entry.category.value if entry.category else None,
                "question": entry.question,
                "answer": entry.answer,
                "source": entry.source,
                "confidence": entry.confidence,
                "similarity": round(sim, 4),
            })

        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()

        return results

    async def get_relevant_context(
        self,
        intent: str,
        entity: str,
        query: str,
    ) -> List[dict]:
        """Get context-relevant knowledge for ReAct engine enrichment."""
        # Determine relevant categories from intent
        categories = _INTENT_CATEGORY_MAP.get(intent, ["faq"])

        all_results = []
        for cat in categories:
            results = await self.search_knowledge(
                query=f"{entity} {query}",
                category=cat,
                limit=3,
            )
            all_results.extend(results)

        # Deduplicate by ID and sort by similarity
        seen = set()
        unique = []
        for r in all_results:
            if r["id"] not in seen:
                seen.add(r["id"])
                unique.append(r)

        unique.sort(key=lambda x: x["similarity"], reverse=True)
        return unique[:5]

    async def update_from_feedback(
        self,
        record_id: str,
        corrected_answer: str,
    ) -> None:
        """When an agent corrects an answer, create/update knowledge entry."""
        # Try to find existing entry by ID (as a knowledge entry)
        stmt = select(KnowledgeEntryModel).where(
            KnowledgeEntryModel.id == record_id
        )
        result = await self.db.execute(stmt)
        entry = result.scalar_one_or_none()

        if entry:
            entry.answer = corrected_answer
            entry.updated_at = datetime.now(timezone.utc)
            entry.source = f"corrected:{entry.source or 'agent'}"
        else:
            # Create a new troubleshooting entry from the correction
            entry = KnowledgeEntryModel(
                id=uuid.uuid4(),
                category=KnowledgeCategory.troubleshooting,
                question=f"Correction for record {record_id}",
                answer=corrected_answer,
                source="agent_correction",
                confidence=0.9,
            )
            self.db.add(entry)

        try:
            await self.db.commit()
            logger.info("knowledge.updated_from_feedback", record_id=record_id)
        except Exception as exc:
            await self.db.rollback()
            logger.error("knowledge.update_failed", error=str(exc))
            raise

    async def get_knowledge_stats(self) -> dict:
        """Stats: total entries per category, last updated, coverage gaps."""
        # Per-category count
        cat_stmt = (
            select(KnowledgeEntryModel.category, func.count())
            .where(KnowledgeEntryModel.enabled.is_(True))
            .group_by(KnowledgeEntryModel.category)
        )
        cat_result = await self.db.execute(cat_stmt)
        by_category = {
            row[0].value if row[0] else "unknown": row[1]
            for row in cat_result.all()
        }

        # Total
        total = sum(by_category.values())

        # Last updated
        last_stmt = select(func.max(KnowledgeEntryModel.updated_at))
        last_result = await self.db.execute(last_stmt)
        last_updated = last_result.scalar()

        # Top used
        top_stmt = (
            select(KnowledgeEntryModel)
            .where(KnowledgeEntryModel.enabled.is_(True))
            .order_by(KnowledgeEntryModel.usage_count.desc())
            .limit(5)
        )
        top_result = await self.db.execute(top_stmt)
        top_used = [
            {
                "id": str(e.id),
                "question": e.question[:100],
                "usage_count": e.usage_count or 0,
            }
            for e in top_result.scalars().all()
        ]

        # Coverage gaps — categories with zero entries
        all_categories = {c.value for c in KnowledgeCategory}
        covered = set(by_category.keys())
        gaps = list(all_categories - covered)

        return {
            "total_entries": total,
            "by_category": by_category,
            "last_updated": last_updated.isoformat() if last_updated else None,
            "top_used": top_used,
            "coverage_gaps": gaps,
        }


# ---------------------------------------------------------------------------
# TF-IDF helpers (pure Python, no sklearn)
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "and", "but", "or", "nor", "not",
    "so", "yet", "both", "either", "neither", "each", "every", "all",
    "any", "few", "more", "most", "other", "some", "such", "no", "only",
    "same", "than", "too", "very", "just", "because", "if", "when",
    "where", "how", "what", "which", "who", "whom", "this", "that",
    "these", "those", "i", "me", "my", "we", "our", "you", "your",
    "he", "him", "his", "she", "her", "it", "its", "they", "them", "their",
}


def _tokenize(text: str) -> List[str]:
    """Lowercase, remove punctuation, split, remove stop words."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = text.split()
    return [t for t in tokens if t and t not in _STOP_WORDS]


def _compute_idf(documents: List[List[str]], query_tokens: List[str]) -> Dict[str, float]:
    """Compute IDF for query tokens across the document corpus."""
    n = len(documents)
    if n == 0:
        return {}

    idf = {}
    for token in set(query_tokens):
        doc_count = sum(1 for doc in documents if token in doc)
        # Smoothed IDF
        idf[token] = math.log((n + 1) / (doc_count + 1)) + 1
    return idf


def _tfidf_similarity(
    query_tokens: List[str],
    doc_tokens: List[str],
    idf: Dict[str, float],
) -> float:
    """Compute cosine similarity between query and document using TF-IDF vectors."""
    if not query_tokens or not doc_tokens:
        return 0.0

    # TF for query
    query_tf = Counter(query_tokens)
    # TF for document
    doc_tf = Counter(doc_tokens)

    # All terms in both
    all_terms = set(query_tokens) | set(doc_tokens)

    # Build TF-IDF vectors
    q_vec = {}
    d_vec = {}
    for term in all_terms:
        term_idf = idf.get(term, 1.0)
        q_vec[term] = query_tf.get(term, 0) * term_idf
        d_vec[term] = doc_tf.get(term, 0) * term_idf

    # Cosine similarity
    dot = sum(q_vec[t] * d_vec[t] for t in all_terms)
    mag_q = math.sqrt(sum(v * v for v in q_vec.values()))
    mag_d = math.sqrt(sum(v * v for v in d_vec.values()))

    if mag_q == 0 or mag_d == 0:
        return 0.0

    return dot / (mag_q * mag_d)

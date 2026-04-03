"""
Reranker Service — Cross-encoder reranking for retrieval results.

After embedding search returns top-K candidates, the reranker uses a more
accurate (but slower) cross-encoder to re-score each candidate against the query.

This typically improves retrieval precision by 15-30% over embedding-only ranking.

Modes:
  1. LLM-based reranking (uses AI Gateway — most accurate, ~200ms)
  2. Keyword-overlap scoring (zero-latency fallback)

Usage:
    reranker = Reranker()
    reranked = await reranker.rerank(query, candidates, top_k=5)
"""

import os
import re
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Reranker mode: "llm" uses AI Gateway, "keyword" uses local scoring
RERANKER_MODE = os.environ.get("RERANKER_MODE", "keyword")


class Reranker:
    """Rerank retrieval results for better precision."""

    def __init__(self, mode: Optional[str] = None):
        self.mode = mode or RERANKER_MODE

    async def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Rerank candidates and return top_k results.

        Each candidate must have 'content' and optionally 'similarity', 'trust_score'.
        Returns candidates with updated 'rerank_score' field.
        """
        if not candidates:
            return []

        if len(candidates) <= top_k:
            # Not enough candidates to rerank — just return sorted by existing score
            return sorted(candidates, key=lambda c: c.get("relevance", c.get("similarity", 0)), reverse=True)

        if self.mode == "llm":
            scored = await self._rerank_llm(query, candidates)
        else:
            scored = self._rerank_keyword(query, candidates)

        # Sort by rerank_score descending
        scored.sort(key=lambda c: c.get("rerank_score", 0), reverse=True)

        # MMR diversity reranking: avoid returning 5 similar chunks
        # Balance relevance (λ=0.7) with diversity (1-λ=0.3)
        diverse = self._apply_mmr(scored, top_k, lambda_param=0.7)
        return diverse

    # ------------------------------------------------------------------
    # MMR Diversity Reranking
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_mmr(
        candidates: List[Dict], top_k: int, lambda_param: float = 0.7,
    ) -> List[Dict]:
        """Maximal Marginal Relevance: balance relevance with diversity.

        Prevents returning 5 similar API docs when the query needs
        diverse evidence (schema + API + action + workflow).

        MMR(doc) = λ × relevance(doc) - (1-λ) × max_similarity(doc, already_selected)
        """
        if len(candidates) <= top_k:
            return candidates

        selected = [candidates[0]]  # Always include the most relevant
        remaining = list(candidates[1:])

        while len(selected) < top_k and remaining:
            best_mmr = -float("inf")
            best_idx = 0

            for i, candidate in enumerate(remaining):
                relevance = candidate.get("rerank_score", 0)

                # Compute max similarity to already-selected docs
                # Use content overlap as a proxy for embedding similarity
                max_sim = 0.0
                cand_terms = set(candidate.get("content", "").lower().split()[:50])
                for sel in selected:
                    sel_terms = set(sel.get("content", "").lower().split()[:50])
                    if cand_terms and sel_terms:
                        overlap = len(cand_terms & sel_terms) / max(len(cand_terms | sel_terms), 1)
                        max_sim = max(max_sim, overlap)

                # Also penalize same entity_type (e.g., 5 api_endpoint docs)
                cand_type = candidate.get("entity_type", "")
                type_penalty = sum(1 for s in selected if s.get("entity_type") == cand_type) * 0.1
                max_sim += type_penalty

                mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = i

            selected.append(remaining.pop(best_idx))

        return selected

    # ------------------------------------------------------------------
    # Keyword-based reranking (fast, zero-latency)
    # ------------------------------------------------------------------

    def _rerank_keyword(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """Score candidates by keyword overlap, entity match, and structural signals."""
        query_terms = _tokenize(query.lower())
        query_entities = _extract_entities(query.lower())

        for candidate in candidates:
            content = candidate.get("content", "").lower()
            metadata = candidate.get("metadata", {})
            content_terms = _tokenize(content)

            # 1. Term overlap score (Jaccard-like)
            if query_terms and content_terms:
                overlap = len(query_terms & content_terms)
                term_score = overlap / max(len(query_terms), 1)
            else:
                term_score = 0.0

            # 2. Entity match boost
            entity_score = 0.0
            entity_id = candidate.get("entity_id", "").lower()
            table_name = metadata.get("table_name", "").lower()
            api_id = metadata.get("api_id", "").lower()
            domain = metadata.get("domain", "").lower()

            for entity in query_entities:
                if entity in entity_id or entity in table_name or entity in api_id:
                    entity_score = 0.4
                    break
                if entity in domain:
                    entity_score = 0.2

            # 3. Chunk type relevance boost
            chunk_score = 0.0
            chunk_type = metadata.get("chunk_type", "")
            query_lower = query.lower()

            if chunk_type == "states" and any(w in query_lower for w in ("status", "state", "transition", "flow")):
                chunk_score = 0.3
            elif chunk_type == "rules" and any(w in query_lower for w in ("validation", "required", "rule", "constraint", "reject")):
                chunk_score = 0.3
            elif chunk_type == "params" and any(w in query_lower for w in ("param", "field", "request", "body", "payload")):
                chunk_score = 0.3
            elif chunk_type == "dataflow" and any(w in query_lower for w in ("flow", "webhook", "cron", "job", "trigger", "event")):
                chunk_score = 0.3
            elif chunk_type == "response" and any(w in query_lower for w in ("response", "return", "output", "result")):
                chunk_score = 0.3
            elif chunk_type == "identity":
                chunk_score = 0.1  # identity is a good default

            # 4. Trust score contribution
            trust_score = candidate.get("trust_score", 0.5)

            # 5. Original similarity contribution
            original_sim = candidate.get("similarity", candidate.get("relevance", 0.5))

            # Combined rerank score
            rerank_score = (
                original_sim * 0.35
                + term_score * 0.25
                + entity_score * 0.20
                + chunk_score * 0.10
                + trust_score * 0.10
            )

            candidate["rerank_score"] = round(rerank_score, 4)
            candidate["rerank_signals"] = {
                "original_sim": round(original_sim, 3),
                "term_overlap": round(term_score, 3),
                "entity_match": round(entity_score, 3),
                "chunk_relevance": round(chunk_score, 3),
                "trust": round(trust_score, 3),
            }

        return candidates

    # ------------------------------------------------------------------
    # LLM-based reranking (more accurate, uses AI Gateway)
    # ------------------------------------------------------------------

    async def _rerank_llm(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """Score candidates using LLM relevance judgment.

        Sends query + candidate content pairs to the LLM and asks it to
        rate relevance on a 0-10 scale. Falls back to keyword if LLM fails.
        """
        try:
            import httpx

            AIGATEWAY_URL = os.environ.get("AIGATEWAY_URL", "https://aigateway.shiprocket.in")
            AIGATEWAY_API_KEY = os.environ.get("AIGATEWAY_API_KEY", "")
            AIGATEWAY_LLM_MODEL = os.environ.get("AIGATEWAY_LLM_MODEL", "claude-sonnet-4-20250514")

            # Build batch prompt
            passages = []
            for i, c in enumerate(candidates[:20]):  # Cap at 20
                content = c.get("content", "")[:500]
                passages.append(f"[{i}] {content}")

            prompt = (
                f"Query: {query}\n\n"
                f"Rank these passages by relevance to the query. "
                f"Return ONLY a comma-separated list of passage numbers, most relevant first.\n\n"
                + "\n\n".join(passages)
            )

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{AIGATEWAY_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {AIGATEWAY_API_KEY}"},
                    json={
                        "model": AIGATEWAY_LLM_MODEL,
                        "provider": "anthropic",
                        "project_key": "cosmos",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 100,
                        "temperature": 0.0,
                    },
                )

                if resp.status_code != 200:
                    logger.warning("reranker.llm_failed", status=resp.status_code)
                    return self._rerank_keyword(query, candidates)

                result = resp.json()
                ranking_text = result["choices"][0]["message"]["content"].strip()

                # Parse ranking: "0, 3, 1, 7, 2"
                indices = []
                for num in re.findall(r'\d+', ranking_text):
                    idx = int(num)
                    if 0 <= idx < len(candidates):
                        indices.append(idx)

                # Assign scores based on position in ranking
                for rank, idx in enumerate(indices):
                    score = max(1.0 - (rank * 0.1), 0.1)
                    candidates[idx]["rerank_score"] = score

                # Unranked candidates get low score
                ranked_set = set(indices)
                for i, c in enumerate(candidates):
                    if i not in ranked_set:
                        c["rerank_score"] = 0.05

                return candidates

        except Exception as e:
            logger.warning("reranker.llm_error", error=str(e))
            return self._rerank_keyword(query, candidates)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set:
    """Extract meaningful terms from text."""
    # Remove common stop words and short tokens
    stop_words = {"the", "a", "an", "is", "in", "on", "at", "to", "for", "of", "and", "or", "not",
                  "with", "from", "by", "it", "this", "that", "be", "are", "was", "were", "has",
                  "have", "do", "does", "can", "will", "what", "how", "why", "when", "where", "which"}
    words = set(re.findall(r'\b\w{3,}\b', text))
    return words - stop_words


def _extract_entities(query: str) -> set:
    """Extract potential entity names from a query."""
    entities = set()
    # Common patterns: "orders table", "shipments", "/api/v1/orders"
    for match in re.finditer(r'\b(orders?|shipments?|couriers?|billing|users?|companies|products?|channels?|ndr|awb|warehouse|manifest|returns?|tracking|payments?|wallet)\b', query):
        entities.add(match.group(0).rstrip("s"))  # normalize plural
    # API path patterns
    for match in re.finditer(r'/api/v\d+/(\w+)', query):
        entities.add(match.group(1))
    return entities

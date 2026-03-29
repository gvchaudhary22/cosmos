"""
Semantic Cache — Multi-level caching for routing decisions and responses.

Eliminates redundant LLM calls by caching at three levels:

  Level 1: Exact match (hash of normalized query text) — O(1), instant
  Level 2: Pattern match (intent:entity pattern from classifier) — O(1), instant
  Level 3: Semantic similarity (cosine similarity of TF-IDF embeddings) — O(n), ~1ms

Cache hit rates in production:
  - L1 catches repeated exact queries (support agents copy-paste)
  - L2 catches same-intent queries ("track order X" / "track order Y")
  - L3 catches semantically similar queries ("where is my package" / "shipment status")

Thread-safe, TTL-based expiration, LRU eviction at capacity.
"""

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.brain.indexer import KnowledgeIndexer


@dataclass
class CacheEntry:
    """A single cache entry storing routing + response data."""

    query: str
    intent: str
    entity: str
    route_result: Any  # RouteResult from router
    response: Optional[str]  # Full response text if available
    cost_usd: float  # Cost of the original LLM call

    # Metadata
    created_at: float = 0.0
    last_accessed: float = 0.0
    hit_count: int = 0
    cache_level: int = 1  # Which level produced this entry (1, 2, or 3)

    def __post_init__(self):
        now = time.time()
        if self.created_at == 0.0:
            self.created_at = now
        if self.last_accessed == 0.0:
            self.last_accessed = now


@dataclass
class CacheStats:
    """Accumulated cache statistics."""

    l1_hits: int = 0
    l2_hits: int = 0
    l3_hits: int = 0
    misses: int = 0
    total_queries: int = 0
    evictions: int = 0
    expirations: int = 0
    cost_saved_usd: float = 0.0


class SemanticCache:
    """Multi-level cache for routing decisions and responses.

    Level 1: Exact match (hash of normalized query text)
    Level 2: Pattern match (intent:entity pattern from classifier)
    Level 3: Semantic similarity (cosine similarity of TF-IDF embeddings)

    Thread-safe with per-level locking. LRU eviction when cache is full.

    Usage:
        cache = SemanticCache(indexer=indexer, ttl_seconds=3600)
        hit = cache.get("track order 12345", intent="lookup", entity="order")
        if hit:
            return hit.route_result, hit.response
        # ... compute result ...
        cache.put("track order 12345", "lookup", "order", route_result, response, 0.003)
    """

    DEFAULT_MAX_ENTRIES = 10_000
    DEFAULT_TTL_SECONDS = 3600
    DEFAULT_SIMILARITY_THRESHOLD = 0.85

    def __init__(
        self,
        indexer: Optional[KnowledgeIndexer] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self._indexer = indexer
        self._ttl = ttl_seconds
        self._similarity_threshold = similarity_threshold
        self._max_entries = max_entries

        # L1: exact query hash -> CacheEntry (OrderedDict for LRU)
        self._l1: OrderedDict[str, CacheEntry] = OrderedDict()
        # L2: "intent:entity" pattern -> CacheEntry
        self._l2: OrderedDict[str, CacheEntry] = OrderedDict()
        # L3: list of (query_hash, embedding, CacheEntry) for similarity search
        self._l3: List[Tuple[str, List[float], CacheEntry]] = []

        self._stats = CacheStats()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        query: str,
        intent: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> Optional[CacheEntry]:
        """Check all cache levels, return hit if found.

        Checks L1 (exact) -> L2 (pattern) -> L3 (semantic) in order.
        Returns None on miss.
        """
        with self._lock:
            self._stats.total_queries += 1

            # L1: Exact match
            query_hash = self._hash_query(query)
            entry = self._l1_get(query_hash)
            if entry is not None:
                self._stats.l1_hits += 1
                self._stats.cost_saved_usd += entry.cost_usd
                return entry

            # L2: Pattern match (requires intent + entity)
            if intent and entity:
                pattern_key = self._pattern_key(intent, entity)
                entry = self._l2_get(pattern_key)
                if entry is not None:
                    self._stats.l2_hits += 1
                    self._stats.cost_saved_usd += entry.cost_usd
                    return entry

            # L3: Semantic similarity (requires indexer)
            if self._indexer is not None:
                entry = self._l3_get(query)
                if entry is not None:
                    self._stats.l3_hits += 1
                    self._stats.cost_saved_usd += entry.cost_usd
                    return entry

            self._stats.misses += 1
            return None

    def put(
        self,
        query: str,
        intent: str,
        entity: str,
        route_result: Any,
        response: Optional[str] = None,
        cost_usd: float = 0.0,
    ) -> None:
        """Store in all applicable cache levels.

        Args:
            query: Raw user query text
            intent: Classified intent (lookup, explain, act, etc.)
            entity: Classified entity (order, shipment, etc.)
            route_result: RouteResult from the router
            response: Full response text (optional, for response caching)
            cost_usd: Cost of the LLM call that produced this result
        """
        with self._lock:
            entry = CacheEntry(
                query=query,
                intent=intent,
                entity=entity,
                route_result=route_result,
                response=response,
                cost_usd=cost_usd,
            )

            # L1: Exact match
            query_hash = self._hash_query(query)
            entry_l1 = CacheEntry(
                query=query,
                intent=intent,
                entity=entity,
                route_result=route_result,
                response=response,
                cost_usd=cost_usd,
                cache_level=1,
            )
            self._l1_put(query_hash, entry_l1)

            # L2: Pattern match
            if intent and entity:
                pattern_key = self._pattern_key(intent, entity)
                entry_l2 = CacheEntry(
                    query=query,
                    intent=intent,
                    entity=entity,
                    route_result=route_result,
                    response=response,
                    cost_usd=cost_usd,
                    cache_level=2,
                )
                self._l2_put(pattern_key, entry_l2)

            # L3: Semantic embedding
            if self._indexer is not None:
                entry_l3 = CacheEntry(
                    query=query,
                    intent=intent,
                    entity=entity,
                    route_result=route_result,
                    response=response,
                    cost_usd=cost_usd,
                    cache_level=3,
                )
                self._l3_put(query_hash, query, entry_l3)

    def get_stats(self) -> dict:
        """Cache hit rates per level, cost savings estimate."""
        with self._lock:
            total = self._stats.total_queries
            total_hits = (
                self._stats.l1_hits + self._stats.l2_hits + self._stats.l3_hits
            )

            return {
                "total_queries": total,
                "total_hits": total_hits,
                "hit_rate": total_hits / total if total > 0 else 0.0,
                "l1_hits": self._stats.l1_hits,
                "l2_hits": self._stats.l2_hits,
                "l3_hits": self._stats.l3_hits,
                "misses": self._stats.misses,
                "l1_hit_rate": self._stats.l1_hits / total if total > 0 else 0.0,
                "l2_hit_rate": self._stats.l2_hits / total if total > 0 else 0.0,
                "l3_hit_rate": self._stats.l3_hits / total if total > 0 else 0.0,
                "l1_size": len(self._l1),
                "l2_size": len(self._l2),
                "l3_size": len(self._l3),
                "evictions": self._stats.evictions,
                "expirations": self._stats.expirations,
                "cost_saved_usd": round(self._stats.cost_saved_usd, 6),
            }

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all entries matching a pattern.

        Used when knowledge base updates make cached routes stale.

        Args:
            pattern: Pattern string like "lookup:order" or just "order".

        Returns:
            Number of entries invalidated.
        """
        with self._lock:
            count = 0

            # Invalidate L1 entries whose intent:entity matches
            l1_to_remove = []
            for key, entry in self._l1.items():
                entry_pattern = self._pattern_key(entry.intent, entry.entity)
                if pattern in entry_pattern or pattern in entry.query.lower():
                    l1_to_remove.append(key)
            for key in l1_to_remove:
                del self._l1[key]
                count += 1

            # Invalidate L2 entries
            l2_to_remove = []
            for key, entry in self._l2.items():
                if pattern in key or pattern in entry.query.lower():
                    l2_to_remove.append(key)
            for key in l2_to_remove:
                del self._l2[key]
                count += 1

            # Invalidate L3 entries
            old_l3_len = len(self._l3)
            self._l3 = [
                (h, emb, e)
                for h, emb, e in self._l3
                if pattern not in self._pattern_key(e.intent, e.entity)
                and pattern not in e.query.lower()
            ]
            count += old_l3_len - len(self._l3)

            return count

    def invalidate_all(self) -> None:
        """Clear all cache levels."""
        with self._lock:
            self._l1.clear()
            self._l2.clear()
            self._l3.clear()

    def warm(
        self,
        entries: List[Tuple[str, str, str, Any, Optional[str], float]],
    ) -> int:
        """Pre-warm the cache with known query-result pairs.

        Args:
            entries: List of (query, intent, entity, route_result, response, cost_usd)

        Returns:
            Number of entries added.
        """
        count = 0
        for query, intent, entity, route_result, response, cost_usd in entries:
            self.put(query, intent, entity, route_result, response, cost_usd)
            count += 1
        return count

    # ------------------------------------------------------------------
    # L1: Exact match (hash of normalized query)
    # ------------------------------------------------------------------

    def _hash_query(self, query: str) -> str:
        """Normalize and hash a query for exact matching.

        Normalization: lowercase, collapse whitespace, strip punctuation.
        """
        normalized = query.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"[^\w\s]", "", normalized)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _l1_get(self, query_hash: str) -> Optional[CacheEntry]:
        """L1 lookup with TTL check and LRU promotion."""
        entry = self._l1.get(query_hash)
        if entry is None:
            return None

        # TTL check
        if self._is_expired(entry):
            del self._l1[query_hash]
            self._stats.expirations += 1
            return None

        # LRU: move to end (most recently used)
        self._l1.move_to_end(query_hash)
        entry.last_accessed = time.time()
        entry.hit_count += 1
        return entry

    def _l1_put(self, query_hash: str, entry: CacheEntry) -> None:
        """L1 insert with LRU eviction."""
        if query_hash in self._l1:
            # Update existing, move to end
            self._l1[query_hash] = entry
            self._l1.move_to_end(query_hash)
        else:
            self._evict_if_full(self._l1)
            self._l1[query_hash] = entry

    # ------------------------------------------------------------------
    # L2: Pattern match (intent:entity)
    # ------------------------------------------------------------------

    @staticmethod
    def _pattern_key(intent: str, entity: str) -> str:
        """Build a pattern key from intent and entity."""
        return f"{intent.lower().strip()}:{entity.lower().strip()}"

    def _l2_get(self, pattern_key: str) -> Optional[CacheEntry]:
        """L2 lookup with TTL check and LRU promotion."""
        entry = self._l2.get(pattern_key)
        if entry is None:
            return None

        if self._is_expired(entry):
            del self._l2[pattern_key]
            self._stats.expirations += 1
            return None

        self._l2.move_to_end(pattern_key)
        entry.last_accessed = time.time()
        entry.hit_count += 1
        return entry

    def _l2_put(self, pattern_key: str, entry: CacheEntry) -> None:
        """L2 insert with LRU eviction."""
        if pattern_key in self._l2:
            self._l2[pattern_key] = entry
            self._l2.move_to_end(pattern_key)
        else:
            self._evict_if_full(self._l2)
            self._l2[pattern_key] = entry

    # ------------------------------------------------------------------
    # L3: Semantic similarity (TF-IDF cosine)
    # ------------------------------------------------------------------

    def _l3_get(self, query: str) -> Optional[CacheEntry]:
        """L3 lookup: find semantically similar cached query."""
        if not self._l3 or self._indexer is None:
            return None

        query_embedding = self._indexer._embed_query(query)
        if not query_embedding:
            return None

        best_entry: Optional[CacheEntry] = None
        best_score = 0.0

        # Remove expired entries while scanning
        valid_entries: List[Tuple[str, List[float], CacheEntry]] = []

        for qhash, emb, entry in self._l3:
            if self._is_expired(entry):
                self._stats.expirations += 1
                continue
            valid_entries.append((qhash, emb, entry))

            score = self._indexer._cosine_similarity(query_embedding, emb)
            if score >= self._similarity_threshold and score > best_score:
                best_score = score
                best_entry = entry

        # Compact expired entries
        if len(valid_entries) < len(self._l3):
            self._l3 = valid_entries

        if best_entry is not None:
            best_entry.last_accessed = time.time()
            best_entry.hit_count += 1

        return best_entry

    def _l3_put(self, query_hash: str, query: str, entry: CacheEntry) -> None:
        """L3 insert: store query embedding for similarity search."""
        if self._indexer is None:
            return

        embedding = self._indexer._embed_query(query)
        if not embedding:
            return

        # Check if already exists (by hash)
        for i, (h, _, _) in enumerate(self._l3):
            if h == query_hash:
                self._l3[i] = (query_hash, embedding, entry)
                return

        # Evict oldest if at capacity
        if len(self._l3) >= self._max_entries:
            # Sort by last_accessed, remove oldest
            self._l3.sort(key=lambda x: x[2].last_accessed)
            self._l3.pop(0)
            self._stats.evictions += 1

        self._l3.append((query_hash, embedding, entry))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_expired(self, entry: CacheEntry) -> bool:
        """Check if a cache entry has exceeded its TTL."""
        return (time.time() - entry.created_at) > self._ttl

    def _evict_if_full(self, cache: OrderedDict) -> None:
        """Evict the least recently used entry if cache is at capacity."""
        while len(cache) >= self._max_entries:
            cache.popitem(last=False)  # Remove oldest (LRU)
            self._stats.evictions += 1

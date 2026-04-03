"""
Tests for the Semantic Cache module.

Covers:
  - L1: Exact match caching, TTL expiration, LRU eviction
  - L2: Pattern match caching (intent:entity), TTL, LRU
  - L3: Semantic similarity caching via TF-IDF cosine
  - Cross-level: get() checks all levels in order
  - Invalidation: pattern-based, full clear
  - Stats: hit rates, cost savings tracking
  - Thread safety: concurrent reads/writes
  - Edge cases: empty queries, missing indexer, capacity limits
"""

import math
import threading
import time
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from app.brain.cache import CacheEntry, SemanticCache
from app.brain.indexer import KnowledgeIndexer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache():
    """Basic cache with no indexer (L1 + L2 only)."""
    return SemanticCache(indexer=None, ttl_seconds=3600, max_entries=100)


@pytest.fixture
def mock_indexer():
    """Mock indexer that returns controlled embeddings."""
    indexer = MagicMock(spec=KnowledgeIndexer)
    # Default: return a simple embedding
    indexer._embed_query.return_value = [1.0, 0.0, 0.0]
    indexer._cosine_similarity.return_value = 0.95
    return indexer


@pytest.fixture
def cache_with_indexer(mock_indexer):
    """Cache with mock indexer (all 3 levels)."""
    return SemanticCache(
        indexer=mock_indexer,
        ttl_seconds=3600,
        similarity_threshold=0.85,
        max_entries=100,
    )


# =====================================================================
# CacheEntry
# =====================================================================


class TestCacheEntry:
    """Tests for the CacheEntry dataclass."""

    def test_entry_auto_timestamps(self):
        entry = CacheEntry(
            query="test",
            intent="lookup",
            entity="order",
            route_result={"tier": "tier1"},
            response="result",
            cost_usd=0.003,
        )
        assert entry.created_at > 0
        assert entry.last_accessed > 0
        assert entry.hit_count == 0
        assert entry.cache_level == 1

    def test_entry_preserves_explicit_timestamps(self):
        entry = CacheEntry(
            query="test",
            intent="lookup",
            entity="order",
            route_result=None,
            response=None,
            cost_usd=0.0,
            created_at=1000.0,
            last_accessed=1000.0,
        )
        assert entry.created_at == 1000.0
        assert entry.last_accessed == 1000.0

    def test_entry_stores_all_fields(self):
        result = {"tier": "tier1", "api": "orders.get"}
        entry = CacheEntry(
            query="track order 123",
            intent="lookup",
            entity="order",
            route_result=result,
            response="Order 123 is in transit",
            cost_usd=0.005,
            cache_level=2,
        )
        assert entry.query == "track order 123"
        assert entry.intent == "lookup"
        assert entry.entity == "order"
        assert entry.route_result == result
        assert entry.response == "Order 123 is in transit"
        assert entry.cost_usd == 0.005
        assert entry.cache_level == 2


# =====================================================================
# L1: Exact Match
# =====================================================================


class TestL1ExactMatch:
    """Level 1 cache: exact normalized query hash."""

    def test_exact_hit(self, cache):
        cache.put("track order 123", "lookup", "order", {"tier": "t1"}, "resp", 0.01)
        hit = cache.get("track order 123")
        assert hit is not None
        assert hit.response == "resp"
        assert hit.cache_level == 1

    def test_normalized_match(self, cache):
        """Queries that normalize to the same hash should hit."""
        cache.put("Track Order 123", "lookup", "order", {"tier": "t1"}, "resp", 0.01)
        # Lowercase + extra spaces should still match
        hit = cache.get("track  order  123")
        assert hit is not None

    def test_punctuation_normalization(self, cache):
        """Punctuation is stripped during normalization."""
        cache.put("what's my order?", "lookup", "order", {"tier": "t1"}, "resp", 0.01)
        hit = cache.get("whats my order")
        assert hit is not None

    def test_different_query_misses(self, cache):
        cache.put("track order 123", "lookup", "order", {"tier": "t1"}, "resp", 0.01)
        hit = cache.get("cancel order 456")
        assert hit is None

    def test_miss_on_empty_cache(self, cache):
        hit = cache.get("anything")
        assert hit is None

    def test_hit_increments_count(self, cache):
        cache.put("test query", "lookup", "order", {}, None, 0.01)
        cache.get("test query")
        cache.get("test query")
        hit = cache.get("test query")
        assert hit is not None
        assert hit.hit_count == 3

    def test_ttl_expiration(self):
        cache = SemanticCache(ttl_seconds=1, max_entries=100)
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        # Should hit immediately
        assert cache.get("test") is not None
        # Wait for expiration
        time.sleep(1.1)
        assert cache.get("test") is None

    def test_lru_eviction(self):
        cache = SemanticCache(max_entries=3)
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "lookup", "order", {}, "r2", 0.01)
        cache.put("q3", "lookup", "order", {}, "r3", 0.01)
        # Access q1 to make it recently used
        cache.get("q1")
        # Add q4 — should evict q2 (least recently used)
        cache.put("q4", "lookup", "order", {}, "r4", 0.01)
        assert cache.get("q1") is not None  # kept (recently accessed)
        assert cache.get("q2") is None  # evicted
        assert cache.get("q3") is not None  # kept
        assert cache.get("q4") is not None  # new entry

    def test_update_existing_entry(self, cache):
        cache.put("test", "lookup", "order", {"v": 1}, "old", 0.01)
        cache.put("test", "lookup", "order", {"v": 2}, "new", 0.02)
        hit = cache.get("test")
        assert hit is not None
        assert hit.response == "new"
        assert hit.cost_usd == 0.02


# =====================================================================
# L2: Pattern Match
# =====================================================================


class TestL2PatternMatch:
    """Level 2 cache: intent:entity pattern."""

    def test_pattern_hit(self, cache):
        cache.put("track order 123", "lookup", "order", {"tier": "t1"}, "resp", 0.01)
        # Different query but same intent:entity pattern
        hit = cache.get("where is order 456", intent="lookup", entity="order")
        assert hit is not None
        assert hit.cache_level == 2

    def test_pattern_case_insensitive(self, cache):
        cache.put("test", "LOOKUP", "ORDER", {}, "resp", 0.01)
        hit = cache.get("different query", intent="lookup", entity="order")
        assert hit is not None

    def test_different_pattern_misses(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        hit = cache.get("other", intent="act", entity="order")
        assert hit is None

    def test_l2_requires_intent_and_entity(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        # No intent/entity -> L2 is skipped, falls to miss
        hit = cache.get("completely different query")
        assert hit is None

    def test_l2_ttl_expiration(self):
        cache = SemanticCache(ttl_seconds=1, max_entries=100)
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        assert cache.get("other", intent="lookup", entity="order") is not None
        time.sleep(1.1)
        assert cache.get("other", intent="lookup", entity="order") is None

    def test_l2_updates_on_same_pattern(self, cache):
        cache.put("q1", "lookup", "order", {"v": 1}, "old", 0.01)
        cache.put("q2", "lookup", "order", {"v": 2}, "new", 0.02)
        hit = cache.get("q3", intent="lookup", entity="order")
        assert hit is not None
        assert hit.response == "new"


# =====================================================================
# L3: Semantic Similarity
# =====================================================================


class TestL3SemanticSimilarity:
    """Level 3 cache: TF-IDF cosine similarity."""

    def test_semantic_hit(self, cache_with_indexer, mock_indexer):
        mock_indexer._cosine_similarity.return_value = 0.95
        cache_with_indexer.put("track my shipment", "lookup", "shipment", {}, "resp", 0.05)
        # Different query but semantically similar
        hit = cache_with_indexer.get("where is my package")
        assert hit is not None
        assert hit.cache_level == 3

    def test_below_threshold_misses(self, cache_with_indexer, mock_indexer):
        mock_indexer._cosine_similarity.return_value = 0.5  # Below 0.85
        cache_with_indexer.put("track shipment", "lookup", "shipment", {}, "resp", 0.05)
        hit = cache_with_indexer.get("cancel my order")
        assert hit is None

    def test_l3_disabled_without_indexer(self, cache):
        """L3 is gracefully skipped when no indexer."""
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        hit = cache.get("semantically similar query")
        assert hit is None  # Only L1 exact, no L3

    def test_l3_empty_embedding_skipped(self, cache_with_indexer, mock_indexer):
        mock_indexer._embed_query.return_value = []
        cache_with_indexer.put("test", "lookup", "order", {}, "resp", 0.01)
        # Should not crash, just skip L3
        hit = cache_with_indexer.get("another query")
        assert hit is None

    def test_l3_ttl_expiration(self, mock_indexer):
        cache = SemanticCache(
            indexer=mock_indexer, ttl_seconds=1, similarity_threshold=0.85
        )
        mock_indexer._cosine_similarity.return_value = 0.95
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        assert cache.get("similar") is not None
        time.sleep(1.1)
        assert cache.get("similar") is None

    def test_l3_eviction_at_capacity(self, mock_indexer):
        cache = SemanticCache(
            indexer=mock_indexer,
            ttl_seconds=3600,
            similarity_threshold=0.85,
            max_entries=3,
        )
        mock_indexer._cosine_similarity.return_value = 0.95
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "lookup", "shipment", {}, "r2", 0.02)
        cache.put("q3", "act", "order", {}, "r3", 0.03)
        # This should trigger eviction of oldest L3 entry
        cache.put("q4", "lookup", "payment", {}, "r4", 0.04)
        assert len(cache._l3) <= 3

    def test_l3_update_existing_hash(self, cache_with_indexer, mock_indexer):
        """Putting same query twice should update, not duplicate."""
        cache_with_indexer.put("test", "lookup", "order", {}, "old", 0.01)
        cache_with_indexer.put("test", "lookup", "order", {}, "new", 0.02)
        # Should have only one L3 entry for this query
        hashes = [h for h, _, _ in cache_with_indexer._l3]
        assert len(set(hashes)) == len(hashes)  # No duplicate hashes


# =====================================================================
# Multi-Level Lookup Order
# =====================================================================


class TestMultiLevelLookup:
    """Verify get() checks L1 -> L2 -> L3 in order."""

    def test_l1_takes_priority(self, cache_with_indexer, mock_indexer):
        """L1 hit should return without checking L2/L3."""
        mock_indexer._cosine_similarity.return_value = 0.95
        cache_with_indexer.put("exact query", "lookup", "order", {}, "resp", 0.01)
        hit = cache_with_indexer.get("exact query", intent="lookup", entity="order")
        assert hit is not None
        assert hit.cache_level == 1

    def test_l2_when_l1_misses(self, cache):
        cache.put("track order 123", "lookup", "order", {}, "resp", 0.01)
        # Different exact query but same pattern
        hit = cache.get("find order 789", intent="lookup", entity="order")
        assert hit is not None
        assert hit.cache_level == 2

    def test_l3_when_l1_and_l2_miss(self, cache_with_indexer, mock_indexer):
        mock_indexer._cosine_similarity.return_value = 0.95
        cache_with_indexer.put("track my package", "lookup", "shipment", {}, "resp", 0.05)
        # Different query, different pattern, but semantically similar
        hit = cache_with_indexer.get("where is parcel", intent="act", entity="payment")
        assert hit is not None
        assert hit.cache_level == 3

    def test_complete_miss(self, cache):
        hit = cache.get("never seen this", intent="unknown", entity="unknown")
        assert hit is None


# =====================================================================
# Stats Tracking
# =====================================================================


class TestStats:
    """Cache statistics and cost savings."""

    def test_initial_stats_empty(self, cache):
        stats = cache.get_stats()
        assert stats["total_queries"] == 0
        assert stats["total_hits"] == 0
        assert stats["misses"] == 0
        assert stats["cost_saved_usd"] == 0.0

    def test_l1_hit_tracked(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        cache.get("test")
        stats = cache.get_stats()
        assert stats["l1_hits"] == 1
        assert stats["total_hits"] == 1
        assert stats["misses"] == 0

    def test_l2_hit_tracked(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        cache.get("different query", intent="lookup", entity="order")
        stats = cache.get_stats()
        assert stats["l2_hits"] == 1

    def test_l3_hit_tracked(self, cache_with_indexer, mock_indexer):
        mock_indexer._cosine_similarity.return_value = 0.95
        cache_with_indexer.put("test", "lookup", "order", {}, "resp", 0.05)
        cache_with_indexer.get("similar query")
        stats = cache_with_indexer.get_stats()
        assert stats["l3_hits"] == 1

    def test_miss_tracked(self, cache):
        cache.get("nonexistent")
        stats = cache.get_stats()
        assert stats["misses"] == 1
        assert stats["total_hits"] == 0

    def test_cost_savings_accumulate(self, cache):
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "shipment", {}, "r2", 0.05)
        cache.get("q1")  # saves 0.01
        cache.get("q1")  # saves 0.01
        cache.get("q2")  # saves 0.05
        stats = cache.get_stats()
        assert abs(stats["cost_saved_usd"] - 0.07) < 1e-6

    def test_hit_rate_calculation(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        cache.get("test")  # hit
        cache.get("miss1")  # miss
        cache.get("test")  # hit
        cache.get("miss2")  # miss
        stats = cache.get_stats()
        assert stats["total_queries"] == 4
        assert stats["total_hits"] == 2
        assert abs(stats["hit_rate"] - 0.5) < 1e-6

    def test_l1_hit_rate(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        cache.get("test")  # L1 hit
        cache.get("miss")  # miss
        stats = cache.get_stats()
        assert abs(stats["l1_hit_rate"] - 0.5) < 1e-6

    def test_sizes_reported(self, cache):
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "shipment", {}, "r2", 0.01)
        stats = cache.get_stats()
        assert stats["l1_size"] == 2
        assert stats["l2_size"] == 2
        assert stats["l3_size"] == 0  # No indexer

    def test_eviction_counted(self):
        cache = SemanticCache(max_entries=2)
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "order", {}, "r2", 0.01)
        cache.put("q3", "lookup", "shipment", {}, "r3", 0.01)
        stats = cache.get_stats()
        assert stats["evictions"] >= 1


# =====================================================================
# Invalidation
# =====================================================================


class TestInvalidation:
    """Pattern-based and full invalidation."""

    def test_invalidate_by_pattern(self, cache):
        cache.put("track order 123", "lookup", "order", {}, "r1", 0.01)
        cache.put("cancel order 456", "act", "order", {}, "r2", 0.01)
        cache.put("track shipment 789", "lookup", "shipment", {}, "r3", 0.01)
        count = cache.invalidate_pattern("order")
        assert count >= 2  # Both order entries invalidated
        assert cache.get("track order 123") is None
        assert cache.get("track shipment 789") is not None

    def test_invalidate_by_intent_entity(self, cache):
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "order", {}, "r2", 0.01)
        count = cache.invalidate_pattern("lookup:order")
        assert count >= 1

    def test_invalidate_all(self, cache):
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "shipment", {}, "r2", 0.01)
        cache.invalidate_all()
        assert cache.get("q1") is None
        assert cache.get("q2") is None
        stats = cache.get_stats()
        assert stats["l1_size"] == 0
        assert stats["l2_size"] == 0

    def test_invalidate_nonexistent_pattern(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        count = cache.invalidate_pattern("nonexistent_xyz")
        assert count == 0
        assert cache.get("test") is not None

    def test_invalidate_l3_entries(self, cache_with_indexer, mock_indexer):
        cache_with_indexer.put("track order", "lookup", "order", {}, "r1", 0.01)
        count = cache_with_indexer.invalidate_pattern("order")
        assert count >= 1
        assert len(cache_with_indexer._l3) == 0


# =====================================================================
# Cache Warm
# =====================================================================


class TestWarm:
    """Pre-warming the cache."""

    def test_warm_populates_cache(self, cache):
        entries = [
            ("track order 1", "lookup", "order", {"tier": "t1"}, "r1", 0.01),
            ("cancel order 2", "act", "order", {"tier": "t1"}, "r2", 0.02),
        ]
        count = cache.warm(entries)
        assert count == 2
        assert cache.get("track order 1") is not None
        assert cache.get("cancel order 2") is not None

    def test_warm_empty_list(self, cache):
        count = cache.warm([])
        assert count == 0


# =====================================================================
# Thread Safety
# =====================================================================


class TestThreadSafety:
    """Concurrent access tests."""

    def test_concurrent_reads_writes(self, cache):
        errors = []

        def writer(n):
            try:
                for i in range(50):
                    cache.put(f"query_{n}_{i}", "lookup", "order", {"n": n}, f"r{i}", 0.001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(50):
                    cache.get(f"query_0_{i}", intent="lookup", entity="order")
            except Exception as e:
                errors.append(e)

        threads = []
        for n in range(4):
            threads.append(threading.Thread(target=writer, args=(n,)))
        for _ in range(2):
            threads.append(threading.Thread(target=reader))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"

    def test_concurrent_invalidation(self, cache):
        errors = []

        def writer():
            try:
                for i in range(30):
                    cache.put(f"order_q_{i}", "lookup", "order", {}, f"r{i}", 0.01)
            except Exception as e:
                errors.append(e)

        def invalidator():
            try:
                for _ in range(10):
                    cache.invalidate_pattern("order")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=invalidator),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"

    def test_concurrent_stats(self, cache):
        errors = []

        def work():
            try:
                for i in range(20):
                    cache.put(f"q_{i}", "lookup", "order", {}, f"r{i}", 0.01)
                    cache.get(f"q_{i}")
                    cache.get_stats()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0


# =====================================================================
# Edge Cases
# =====================================================================


class TestEdgeCases:
    """Boundary conditions and edge cases."""

    def test_empty_query(self, cache):
        cache.put("", "lookup", "order", {}, "resp", 0.01)
        hit = cache.get("")
        assert hit is not None

    def test_very_long_query(self, cache):
        long_q = "track " * 10000
        cache.put(long_q, "lookup", "order", {}, "resp", 0.01)
        hit = cache.get(long_q)
        assert hit is not None

    def test_unicode_query(self, cache):
        cache.put("ऑर्डर ट्रैक करें 123", "lookup", "order", {}, "resp", 0.01)
        hit = cache.get("ऑर्डर ट्रैक करें 123")
        assert hit is not None

    def test_none_response(self, cache):
        cache.put("test", "lookup", "order", {}, None, 0.01)
        hit = cache.get("test")
        assert hit is not None
        assert hit.response is None

    def test_zero_cost(self, cache):
        cache.put("test", "lookup", "order", {}, "resp", 0.0)
        cache.get("test")
        stats = cache.get_stats()
        assert stats["cost_saved_usd"] == 0.0

    def test_max_entries_one(self):
        cache = SemanticCache(max_entries=1)
        cache.put("q1", "lookup", "order", {}, "r1", 0.01)
        cache.put("q2", "act", "shipment", {}, "r2", 0.02)
        # Only latest should survive in L1
        stats = cache.get_stats()
        assert stats["l1_size"] == 1

    def test_ttl_zero_always_expires(self):
        cache = SemanticCache(ttl_seconds=0)
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        # Immediately expired
        hit = cache.get("test")
        assert hit is None

    def test_put_without_intent_entity(self, cache):
        """Empty intent/entity should skip L2 but not crash."""
        cache.put("test", "", "", {}, "resp", 0.01)
        # L1 should still work
        hit = cache.get("test")
        assert hit is not None
        # L2 should not have been populated
        assert len(cache._l2) == 0

    def test_get_with_partial_intent(self, cache):
        """Get with only intent (no entity) should skip L2."""
        cache.put("test", "lookup", "order", {}, "resp", 0.01)
        hit = cache.get("different", intent="lookup", entity=None)
        assert hit is None  # L2 needs both intent and entity

    def test_hash_deterministic(self, cache):
        """Same normalized query always produces same hash."""
        h1 = cache._hash_query("Track Order 123!")
        h2 = cache._hash_query("track  order  123")
        assert h1 == h2

    def test_pattern_key_deterministic(self):
        k1 = SemanticCache._pattern_key("LOOKUP", "ORDER")
        k2 = SemanticCache._pattern_key("lookup", "order")
        assert k1 == k2


# =====================================================================
# Default Configuration
# =====================================================================


class TestDefaults:
    """Verify default configuration values."""

    def test_default_max_entries(self):
        cache = SemanticCache()
        assert cache._max_entries == 10_000

    def test_default_ttl(self):
        cache = SemanticCache()
        assert cache._ttl == 3600

    def test_default_similarity_threshold(self):
        cache = SemanticCache()
        assert cache._similarity_threshold == 0.85

    def test_default_no_indexer(self):
        cache = SemanticCache()
        assert cache._indexer is None

"""Tests for ELKClient."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from app.clients.elk import ELKClient, ELKError, LogSearchResult, EndpointUsageResult


def _make_elk_client():
    """Create an ELKClient with a mocked AsyncElasticsearch to avoid needing aiohttp."""
    with patch("app.clients.elk.AsyncElasticsearch"):
        client = ELKClient(hosts="http://localhost:9200")
    return client


# ------------------------------------------------------------------ #
# Time range parsing
# ------------------------------------------------------------------ #


class TestParseTimeRange:
    def setup_method(self):
        self.client = _make_elk_client()

    def test_parse_minutes(self):
        result = self.client._parse_time_range("30m")
        expected_approx = (datetime.utcnow() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
        assert result.startswith(expected_approx[:13])  # match at least hour

    def test_parse_hours(self):
        result = self.client._parse_time_range("24h")
        expected_approx = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H")
        assert result.startswith(expected_approx[:13])

    def test_parse_days(self):
        result = self.client._parse_time_range("7d")
        expected_approx = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        assert result.startswith(expected_approx[:10])

    def test_parse_invalid_raises(self):
        with pytest.raises(ELKError, match="Invalid time_range"):
            self.client._parse_time_range("bad")

    def test_parse_no_unit_raises(self):
        with pytest.raises(ELKError, match="Invalid time_range"):
            self.client._parse_time_range("100")


# ------------------------------------------------------------------ #
# Search query building
# ------------------------------------------------------------------ #


class TestSearchQueryBuilding:
    def test_search_mode_uses_multi_match(self):
        q = ELKClient._build_search_query(
            "connection refused",
            {"range": {"@timestamp": {"gte": "2025-01-01T00:00:00.000Z"}}},
            limit=50,
        )
        assert q["query"]["bool"]["must"][0]["multi_match"]["query"] == "connection refused"
        assert "message" in q["query"]["bool"]["must"][0]["multi_match"]["fields"]
        assert q["size"] == 50

    def test_trace_mode_uses_term(self):
        q = ELKClient._build_trace_query(
            "abc-123",
            {"range": {"@timestamp": {"gte": "2025-01-01T00:00:00.000Z"}}},
            limit=100,
        )
        assert q["query"]["bool"]["must"][0]["term"]["trace_id"] == "abc-123"
        # trace mode sorts ascending (chronological)
        assert q["sort"][0]["@timestamp"]["order"] == "asc"

    def test_diagnose_mode_filters_errors(self):
        q = ELKClient._build_diagnose_query(
            "timeout",
            {"range": {"@timestamp": {"gte": "2025-01-01T00:00:00.000Z"}}},
            limit=100,
        )
        # Should filter by error/fatal levels
        level_filter = q["query"]["bool"]["filter"][1]
        assert "error" in level_filter["terms"]["level"]
        # Should include error_types aggregation
        assert "error_types" in q["aggs"]


# ------------------------------------------------------------------ #
# Endpoint usage aggregation
# ------------------------------------------------------------------ #


class TestEndpointUsage:
    @pytest.mark.asyncio
    async def test_endpoint_usage_aggregation(self):
        """Verify aggregation query is built and results are parsed correctly."""
        mock_response = {
            "took": 15,
            "hits": {"total": {"value": 5000, "relation": "eq"}, "hits": []},
            "aggregations": {
                "by_path": {
                    "buckets": [
                        {
                            "key": "/api/v1/orders",
                            "doc_count": 3000,
                            "avg_latency": {"value": 120.5},
                            "error_count": {"doc_count": 150},
                        },
                        {
                            "key": "/api/v1/tracking",
                            "doc_count": 2000,
                            "avg_latency": {"value": 85.3},
                            "error_count": {"doc_count": 40},
                        },
                    ]
                }
            },
        }

        client = _make_elk_client()
        client.es = AsyncMock()
        client.es.search = AsyncMock(return_value=mock_response)

        result = await client.get_endpoint_usage(time_range="24h", top_n=10)

        assert isinstance(result, EndpointUsageResult)
        assert result.total_requests == 5000
        assert len(result.endpoints) == 2
        assert result.endpoints[0]["path"] == "/api/v1/orders"
        assert result.endpoints[0]["count"] == 3000
        assert result.endpoints[0]["avg_latency"] == 120.5
        assert result.endpoints[0]["error_rate"] == 5.0  # 150/3000 * 100
        assert result.endpoints[1]["error_rate"] == 2.0  # 40/2000 * 100

    @pytest.mark.asyncio
    async def test_endpoint_usage_with_filters(self):
        """Verify path_filter and status_filter are included in the query."""
        client = _make_elk_client()
        client.es = AsyncMock()
        client.es.search = AsyncMock(
            return_value={
                "took": 5,
                "hits": {"total": {"value": 100, "relation": "eq"}, "hits": []},
                "aggregations": {"by_path": {"buckets": []}},
            }
        )

        await client.get_endpoint_usage(
            time_range="1h", path_filter="/api/v1/*", status_filter=500
        )

        call_body = client.es.search.call_args[1]["body"]
        filters = call_body["query"]["bool"]["filter"]

        # Should have 3 filters: time range, wildcard, term
        assert len(filters) == 3
        assert any("wildcard" in f for f in filters)
        assert any("term" in f for f in filters)

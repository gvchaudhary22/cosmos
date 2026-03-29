"""Async Elasticsearch client for ICRM log search and analytics."""

import re
import structlog
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from elasticsearch import AsyncElasticsearch

logger = structlog.get_logger()


@dataclass
class LogSearchResult:
    total: int
    hits: List[Dict[str, Any]]
    took_ms: int


@dataclass
class EndpointUsageResult:
    total_requests: int
    endpoints: List[Dict[str, Any]] = field(default_factory=list)
    # Each endpoint dict: {path, count, avg_latency, error_rate}


class ELKError(Exception):
    pass


class ELKClient:
    """Async Elasticsearch client for ICRM log search and analytics."""

    _TIME_RE = re.compile(r"^(\d+)(m|h|d)$")

    def __init__(
        self,
        hosts: str,
        username: str = None,
        password: str = None,
    ):
        kwargs: Dict[str, Any] = {"hosts": [hosts]}
        if username and password:
            kwargs["basic_auth"] = (username, password)
        kwargs["verify_certs"] = False
        self.es = AsyncElasticsearch(**kwargs)

    async def close(self):
        await self.es.close()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _parse_time_range(self, time_range: str) -> str:
        """Convert '24h', '7d', '30m' etc. to an ISO-8601 timestamp string."""
        match = self._TIME_RE.match(time_range)
        if not match:
            raise ELKError(f"Invalid time_range format: {time_range}. Use e.g. 30m, 24h, 7d")

        value = int(match.group(1))
        unit = match.group(2)

        now = datetime.utcnow()
        if unit == "m":
            dt = now - timedelta(minutes=value)
        elif unit == "h":
            dt = now - timedelta(hours=value)
        elif unit == "d":
            dt = now - timedelta(days=value)
        else:
            raise ELKError(f"Unknown time unit: {unit}")

        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # ------------------------------------------------------------------ #
    # Log search
    # ------------------------------------------------------------------ #

    async def search_logs(
        self,
        query: str,
        index_pattern: str = "star-api-*",
        time_range: str = "24h",
        mode: str = "search",
        limit: int = 100,
    ) -> LogSearchResult:
        """
        Search modes:
        - search: full-text search across message/error fields
        - trace: find all logs for a trace_id
        - diagnose: find error patterns and group by type
        """
        log = logger.bind(mode=mode, query=query, index=index_pattern, time_range=time_range)
        log.info("elk_search_start")

        since = self._parse_time_range(time_range)
        time_filter = {"range": {"@timestamp": {"gte": since}}}

        if mode == "search":
            body = self._build_search_query(query, time_filter, limit)
        elif mode == "trace":
            body = self._build_trace_query(query, time_filter, limit)
        elif mode == "diagnose":
            body = self._build_diagnose_query(query, time_filter, limit)
        else:
            raise ELKError(f"Unknown search mode: {mode}")

        try:
            resp = await self.es.search(index=index_pattern, body=body)
        except Exception as exc:
            log.error("elk_search_error", error=str(exc))
            raise ELKError(f"Elasticsearch query failed: {exc}") from exc

        total = resp["hits"]["total"]
        total_value = total["value"] if isinstance(total, dict) else total
        hits = [hit["_source"] for hit in resp["hits"]["hits"]]
        took_ms = resp.get("took", 0)

        log.info("elk_search_complete", total=total_value, took_ms=took_ms)
        return LogSearchResult(total=total_value, hits=hits, took_ms=took_ms)

    # -- query builders ------------------------------------------------ #

    @staticmethod
    def _build_search_query(query: str, time_filter: Dict, limit: int) -> Dict:
        """Full-text search across message and error fields."""
        return {
            "size": limit,
            "sort": [{"@timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["message", "error", "log.message"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": [time_filter],
                }
            },
        }

    @staticmethod
    def _build_trace_query(trace_id: str, time_filter: Dict, limit: int) -> Dict:
        """Find all logs for a given trace_id."""
        return {
            "size": limit,
            "sort": [{"@timestamp": {"order": "asc"}}],
            "query": {
                "bool": {
                    "must": [{"term": {"trace_id": trace_id}}],
                    "filter": [time_filter],
                }
            },
        }

    @staticmethod
    def _build_diagnose_query(query: str, time_filter: Dict, limit: int) -> Dict:
        """Find error patterns and group by error type."""
        return {
            "size": limit,
            "sort": [{"@timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["message", "error", "log.message"],
                            }
                        }
                    ],
                    "filter": [
                        time_filter,
                        {
                            "terms": {
                                "level": ["error", "ERROR", "fatal", "FATAL"]
                            }
                        },
                    ],
                }
            },
            "aggs": {
                "error_types": {
                    "terms": {"field": "error.keyword", "size": 20}
                }
            },
        }

    # ------------------------------------------------------------------ #
    # Endpoint usage analytics
    # ------------------------------------------------------------------ #

    async def get_endpoint_usage(
        self,
        time_range: str = "24h",
        path_filter: str = None,
        status_filter: int = None,
        top_n: int = 20,
    ) -> EndpointUsageResult:
        """Aggregate nginx access logs to show endpoint usage stats."""
        log = logger.bind(time_range=time_range, path_filter=path_filter)
        log.info("elk_endpoint_usage_start")

        since = self._parse_time_range(time_range)
        filters: List[Dict] = [{"range": {"@timestamp": {"gte": since}}}]

        if path_filter:
            filters.append({"wildcard": {"path": path_filter}})
        if status_filter is not None:
            filters.append({"term": {"status": status_filter}})

        body = {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "by_path": {
                    "terms": {"field": "path.keyword", "size": top_n, "order": {"_count": "desc"}},
                    "aggs": {
                        "avg_latency": {"avg": {"field": "response_time"}},
                        "error_count": {
                            "filter": {"range": {"status": {"gte": 400}}}
                        },
                    },
                }
            },
        }

        try:
            resp = await self.es.search(index="nginx-access-*", body=body)
        except Exception as exc:
            log.error("elk_endpoint_usage_error", error=str(exc))
            raise ELKError(f"Elasticsearch aggregation failed: {exc}") from exc

        total_hits = resp["hits"]["total"]
        total_requests = (
            total_hits["value"] if isinstance(total_hits, dict) else total_hits
        )

        buckets = resp.get("aggregations", {}).get("by_path", {}).get("buckets", [])
        endpoints: List[Dict[str, Any]] = []
        for bucket in buckets:
            count = bucket["doc_count"]
            avg_lat = bucket.get("avg_latency", {}).get("value", 0) or 0
            err_count = bucket.get("error_count", {}).get("doc_count", 0)
            error_rate = round((err_count / count) * 100, 2) if count else 0
            endpoints.append(
                {
                    "path": bucket["key"],
                    "count": count,
                    "avg_latency": round(avg_lat, 2),
                    "error_rate": error_rate,
                }
            )

        log.info("elk_endpoint_usage_complete", total=total_requests, endpoints=len(endpoints))
        return EndpointUsageResult(total_requests=total_requests, endpoints=endpoints)

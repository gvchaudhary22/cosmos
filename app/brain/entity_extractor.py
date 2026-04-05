"""
EntityExtractor — extract structured entities from natural-language ICRM queries.

Handles:
  - Date range resolution: "yesterday", "this week", "last 7 days", etc. → ISO from/to dates
  - Company/seller ID extraction
  - AWB extraction
  - Order ID extraction

All date boundaries are computed in IST (Asia/Kolkata, UTC+5:30).
No LLM involved — pure deterministic regex + datetime arithmetic.

Usage:
    extractor = EntityExtractor()
    result = extractor.extract("how many NDRs for company 25149 this week?")
    # result.company_id == "25149"
    # result.from_date == "2026-03-30"
    # result.to_date  == "2026-04-05"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# IST offset (UTC+5:30)
_IST = timezone(timedelta(hours=5, minutes=30))

# Regex patterns
_COMPANY_ID_RE = re.compile(
    r'(?:company|seller|client|cid|client_id|company_id)'
    r'[\s_]*(?:id)?[\s:=#]*(\d{3,10})',
    re.I,
)
_AWB_RE = re.compile(
    r'\b(?:awb|tracking|track)[:\s#-]*([A-Z0-9]{8,20})\b',
    re.I,
)
_ORDER_ID_RE = re.compile(r'\b(\d{7,12})\b')

# Date phrase patterns — ordered longest-match first
_DATE_PATTERNS = [
    (re.compile(r'\blast\s+30\s+days?\b', re.I), "last_30_days"),
    (re.compile(r'\blast\s+7\s+days?\b', re.I), "last_7_days"),
    (re.compile(r'\blast\s+(\d+)\s+days?\b', re.I), "last_N_days"),
    (re.compile(r'\blast\s+week\b', re.I), "last_week"),
    (re.compile(r'\bthis\s+week\b', re.I), "this_week"),
    (re.compile(r'\blast\s+month\b', re.I), "last_month"),
    (re.compile(r'\bthis\s+month\b', re.I), "this_month"),
    (re.compile(r'\byesterday\b', re.I), "yesterday"),
    (re.compile(r'\btoday\b', re.I), "today"),
]


@dataclass
class ExtractedEntities:
    """Structured entities extracted from a query."""
    company_id: Optional[str] = None
    from_date: Optional[str] = None     # ISO "YYYY-MM-DD" in IST
    to_date: Optional[str] = None       # ISO "YYYY-MM-DD" in IST
    date_label: Optional[str] = None    # Human-readable: "this week", "last 7 days"
    awb: Optional[str] = None
    order_ids: list = field(default_factory=list)

    def has_date_range(self) -> bool:
        return self.from_date is not None and self.to_date is not None

    def has_company(self) -> bool:
        return self.company_id is not None


class EntityExtractor:
    """
    Extract structured entities from natural-language ICRM queries.

    All date math is done in IST timezone. No hardcoded dates — always
    computed relative to `datetime.now(_IST)`.
    """

    def extract(self, query: str, _now: Optional[datetime] = None) -> ExtractedEntities:
        """
        Extract all entities from query text.

        Args:
            query: Raw natural-language query string.
            _now: Override current time (for testing). Defaults to now in IST.

        Returns:
            ExtractedEntities with all found values.
        """
        now = _now or datetime.now(_IST)
        today = now.date()

        result = ExtractedEntities()

        # Company ID
        m = _COMPANY_ID_RE.search(query)
        if m:
            result.company_id = m.group(1)

        # AWB
        m = _AWB_RE.search(query)
        if m:
            result.awb = m.group(1).upper()

        # Order IDs (7-12 digit numbers not already captured as company_id)
        result.order_ids = [int(x) for x in _ORDER_ID_RE.findall(query)]

        # Date range
        from_date, to_date, label = self._resolve_date(query, today)
        result.from_date = from_date
        result.to_date = to_date
        result.date_label = label

        return result

    def _resolve_date(
        self, query: str, today: date
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Resolve a date expression in query to (from_date, to_date, label).
        Returns (None, None, None) if no date expression found.
        All dates as ISO strings "YYYY-MM-DD".
        """
        for pattern, key in _DATE_PATTERNS:
            m = pattern.search(query)
            if not m:
                continue

            if key == "today":
                return _iso(today), _iso(today), "today"

            if key == "yesterday":
                d = today - timedelta(days=1)
                return _iso(d), _iso(d), "yesterday"

            if key == "last_7_days":
                return _iso(today - timedelta(days=6)), _iso(today), "last 7 days"

            if key == "last_30_days":
                return _iso(today - timedelta(days=29)), _iso(today), "last 30 days"

            if key == "last_N_days":
                n = int(m.group(1))
                return _iso(today - timedelta(days=n - 1)), _iso(today), f"last {n} days"

            if key == "this_week":
                # Monday of current week → today
                monday = today - timedelta(days=today.weekday())
                return _iso(monday), _iso(today), "this week"

            if key == "last_week":
                # Monday–Sunday of previous week
                this_monday = today - timedelta(days=today.weekday())
                last_monday = this_monday - timedelta(days=7)
                last_sunday = this_monday - timedelta(days=1)
                return _iso(last_monday), _iso(last_sunday), "last week"

            if key == "this_month":
                first = today.replace(day=1)
                return _iso(first), _iso(today), "this month"

            if key == "last_month":
                first_this = today.replace(day=1)
                last_day_prev = first_this - timedelta(days=1)
                first_prev = last_day_prev.replace(day=1)
                return _iso(first_prev), _iso(last_day_prev), "last month"

        return None, None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(d: date) -> str:
    return d.isoformat()

"""
Tests for EntityExtractor — date entity resolution (M3-P2 W2-A).

All date tests use a fixed reference date to avoid flakiness.
Reference date: 2026-04-05 (Sunday) in IST.

IST week starts Monday. So:
  - this_week Mon = 2026-03-30
  - last_week Mon = 2026-03-23, Sun = 2026-03-29
  - last_month = 2026-03-01 → 2026-03-31
"""
from datetime import datetime, timezone, timedelta

import pytest

from app.brain.entity_extractor import EntityExtractor, ExtractedEntities

# Fixed reference: 2026-04-05 Sunday, 12:00 IST
_IST = timezone(timedelta(hours=5, minutes=30))
_REF = datetime(2026, 4, 5, 12, 0, 0, tzinfo=_IST)


def _extract(query: str) -> ExtractedEntities:
    return EntityExtractor().extract(query, _now=_REF)


# ---------------------------------------------------------------------------
# Date expression resolution
# ---------------------------------------------------------------------------

def test_today():
    r = _extract("how many orders today for company 25149?")
    assert r.from_date == "2026-04-05"
    assert r.to_date == "2026-04-05"
    assert r.date_label == "today"


def test_yesterday():
    r = _extract("NDR count yesterday for company 25149")
    assert r.from_date == "2026-04-04"
    assert r.to_date == "2026-04-04"
    assert r.date_label == "yesterday"


def test_this_week():
    r = _extract("how many NDRs this week for company 25149?")
    assert r.from_date == "2026-03-30"   # Monday
    assert r.to_date == "2026-04-05"     # today (Sunday)
    assert r.date_label == "this week"


def test_last_week():
    r = _extract("shipments last week for 25149")
    assert r.from_date == "2026-03-23"   # Monday
    assert r.to_date == "2026-03-29"     # Sunday
    assert r.date_label == "last week"


def test_last_7_days():
    r = _extract("orders last 7 days company 25149")
    assert r.from_date == "2026-03-30"   # 7 days inclusive: 30 Mar – 5 Apr
    assert r.to_date == "2026-04-05"
    assert r.date_label == "last 7 days"


def test_last_30_days():
    r = _extract("total shipments last 30 days for company 25149")
    assert r.from_date == "2026-03-07"   # 30 days inclusive
    assert r.to_date == "2026-04-05"
    assert r.date_label == "last 30 days"


def test_this_month():
    r = _extract("this month orders for company 25149")
    assert r.from_date == "2026-04-01"
    assert r.to_date == "2026-04-05"
    assert r.date_label == "this month"


def test_last_month():
    r = _extract("how many NDRs last month for company 25149?")
    assert r.from_date == "2026-03-01"
    assert r.to_date == "2026-03-31"
    assert r.date_label == "last month"


def test_last_N_days_custom():
    r = _extract("orders last 14 days company 25149")
    assert r.from_date == "2026-03-23"   # 14 days inclusive
    assert r.to_date == "2026-04-05"
    assert r.date_label == "last 14 days"


# ---------------------------------------------------------------------------
# No date expression — returns None
# ---------------------------------------------------------------------------

def test_no_date_returns_none():
    r = _extract("how many NDRs for company 25149?")
    assert r.from_date is None
    assert r.to_date is None
    assert r.date_label is None
    assert not r.has_date_range()


# ---------------------------------------------------------------------------
# Company ID extraction
# ---------------------------------------------------------------------------

def test_company_id_extracted():
    r = _extract("NDRs for company 25149 this week")
    assert r.company_id == "25149"


def test_seller_id_alias():
    r = _extract("seller 12345 shipments this week")
    assert r.company_id == "12345"


def test_client_id_alias():
    r = _extract("client_id 98765 orders today")
    assert r.company_id == "98765"


def test_no_company_id():
    r = _extract("how many NDRs this week?")
    assert r.company_id is None
    assert not r.has_company()


# ---------------------------------------------------------------------------
# AWB extraction
# ---------------------------------------------------------------------------

def test_awb_extracted():
    r = _extract("track AWB SH123456789")
    assert r.awb == "SH123456789"


def test_no_awb():
    r = _extract("how many NDRs for company 25149 today?")
    assert r.awb is None


# ---------------------------------------------------------------------------
# has_date_range / has_company helpers
# ---------------------------------------------------------------------------

def test_has_date_range_true():
    r = _extract("orders this week for company 25149")
    assert r.has_date_range() is True


def test_has_company_true():
    r = _extract("company 25149 NDRs")
    assert r.has_company() is True

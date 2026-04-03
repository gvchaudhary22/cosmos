"""
Tests for Hinglish (Hindi + English) classifier support.

Covers:
  - Hinglish lookup intents
  - Hinglish explain intents
  - Hinglish act intents
  - Hinglish report intents
  - Hinglish navigate intents
  - Hinglish entity detection
  - Mixed Hinglish + English queries
  - Entity ID extraction from Hinglish text
  - Regression: pure English still works
"""

import pytest

from app.engine.classifier import (
    ClassifyResult,
    Entity,
    Intent,
    IntentClassifier,
)


class TestHinglishClassifier:
    def setup_method(self):
        self.clf = IntentClassifier(hinglish_enabled=True)

    # ------------------------------------------------------------------
    # Hinglish lookup intents
    # ------------------------------------------------------------------

    def test_hinglish_lookup_status_batao(self):
        """'order 12345 ka status batao' -> LOOKUP, ORDER, id=12345"""
        result = self.clf.classify("order 12345 ka status batao")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.ORDER
        assert result.entity_id == "12345"
        assert result.needs_ai is False

    def test_hinglish_lookup_dikhao(self):
        """'order dikhao' -> LOOKUP, ORDER"""
        result = self.clf.classify("order dikhao")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.ORDER

    def test_hinglish_lookup_kahan_hai(self):
        """'shipment kahan hai' -> LOOKUP, SHIPMENT"""
        result = self.clf.classify("shipment kahan hai")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.SHIPMENT

    def test_hinglish_lookup_check_karo(self):
        """'payment check karo' -> LOOKUP, PAYMENT"""
        result = self.clf.classify("payment check karo")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.PAYMENT

    # ------------------------------------------------------------------
    # Hinglish explain intents
    # ------------------------------------------------------------------

    def test_hinglish_explain_kyun(self):
        """'delivery kyun late hai' -> EXPLAIN, SHIPMENT"""
        result = self.clf.classify("delivery kyun late hai")
        assert result.intent == Intent.EXPLAIN
        assert result.entity == Entity.SHIPMENT

    def test_hinglish_explain_kaise(self):
        """'return kaise kare' -> EXPLAIN, RETURN"""
        result = self.clf.classify("return kaise kare")
        assert result.intent == Intent.EXPLAIN
        assert result.entity == Entity.RETURN

    def test_hinglish_explain_samjhao(self):
        """'billing samjhao' -> EXPLAIN, BILLING"""
        result = self.clf.classify("billing samjhao")
        assert result.intent == Intent.EXPLAIN
        assert result.entity == Entity.BILLING

    # ------------------------------------------------------------------
    # Hinglish act intents
    # ------------------------------------------------------------------

    def test_hinglish_act_cancel_karo(self):
        """'order cancel karo' -> ACT, ORDER"""
        result = self.clf.classify("order cancel karo")
        assert result.intent == Intent.ACT
        assert result.entity == Entity.ORDER
        assert result.needs_ai is False

    def test_hinglish_act_refund_karo(self):
        """'refund karo for order 99999' -> ACT, PAYMENT/ORDER"""
        result = self.clf.classify("refund karo for order 99999")
        assert result.intent == Intent.ACT
        assert result.entity_id == "99999"

    def test_hinglish_act_wapas_karo(self):
        """'paisa wapas karo' -> ACT, RETURN (wapas=return in Hinglish entity)"""
        result = self.clf.classify("paisa wapas karo")
        assert result.intent == Intent.ACT
        # "wapas" matches RETURN entity in Hinglish patterns
        assert result.entity == Entity.RETURN

    # ------------------------------------------------------------------
    # Hinglish report intents
    # ------------------------------------------------------------------

    def test_hinglish_report_kitne(self):
        """'kitne orders pending hai' -> REPORT, ORDER"""
        result = self.clf.classify("kitne orders pending hai")
        assert result.intent == Intent.REPORT
        assert result.entity == Entity.ORDER

    def test_hinglish_report_total_batao(self):
        """'total shipping batao' -> REPORT, SHIPMENT"""
        result = self.clf.classify("total shipping batao")
        # "total" matches English REPORT
        assert result.intent == Intent.REPORT
        assert result.entity == Entity.SHIPMENT

    # ------------------------------------------------------------------
    # Hinglish navigate intents
    # ------------------------------------------------------------------

    def test_hinglish_navigate_dashboard_dikhao(self):
        """'dashboard dikhao' -> LOOKUP (dikhao is a lookup word in Hinglish)"""
        result = self.clf.classify("dashboard dikhao")
        # "dikhao" matches Hinglish LOOKUP (show me), which is correct
        assert result.intent == Intent.LOOKUP

    def test_hinglish_navigate_kholo(self):
        """'order page kholo' -> NAVIGATE, ORDER"""
        result = self.clf.classify("order page kholo")
        assert result.intent == Intent.NAVIGATE
        assert result.entity == Entity.ORDER

    # ------------------------------------------------------------------
    # Mixed Hinglish + English
    # ------------------------------------------------------------------

    def test_mixed_payment_refund(self):
        """'mera payment ka refund karo for order 55555' -> ACT, PAYMENT, id=55555"""
        result = self.clf.classify("mera payment ka refund karo for order 55555")
        assert result.intent == Intent.ACT
        assert result.entity == Entity.PAYMENT
        assert result.entity_id == "55555"

    def test_hinglish_entity_paisa(self):
        """'paisa' maps to PAYMENT entity."""
        result = self.clf.classify("paisa batao")
        assert result.entity == Entity.PAYMENT

    def test_hinglish_entity_wapsi(self):
        """'wapsi' maps to RETURN entity."""
        result = self.clf.classify("wapsi ka status batao")
        assert result.entity == Entity.RETURN

    def test_hinglish_entity_grahak(self):
        """'grahak' maps to CUSTOMER entity."""
        result = self.clf.classify("grahak ki details dikhao")
        assert result.entity == Entity.CUSTOMER

    # ------------------------------------------------------------------
    # Regression: pure English still works
    # ------------------------------------------------------------------

    def test_english_lookup_still_works(self):
        """English 'show order 12345' still works."""
        result = self.clf.classify("show order 12345")
        assert result.intent == Intent.LOOKUP
        assert result.entity == Entity.ORDER
        assert result.entity_id == "12345"
        assert result.confidence == 1.0

    def test_english_act_still_works(self):
        """English 'cancel order 12345' still works."""
        result = self.clf.classify("cancel order 12345")
        assert result.intent == Intent.ACT
        assert result.entity == Entity.ORDER
        assert result.entity_id == "12345"
        assert result.confidence == 1.0

    def test_english_explain_still_works(self):
        """English 'why is order delayed' still works."""
        result = self.clf.classify("why is order delayed")
        assert result.intent == Intent.EXPLAIN
        assert result.entity == Entity.ORDER

    def test_english_report_still_works(self):
        """English 'how many orders today' still works."""
        result = self.clf.classify("how many orders today")
        assert result.intent == Intent.REPORT
        assert result.entity == Entity.ORDER

    # ------------------------------------------------------------------
    # Hinglish disabled
    # ------------------------------------------------------------------

    def test_hinglish_disabled(self):
        """When hinglish_enabled=False, Hinglish patterns are skipped."""
        clf = IntentClassifier(hinglish_enabled=False)
        result = clf.classify("order dikhao")
        # "dikhao" is Hinglish-only, so intent should be UNKNOWN
        assert result.intent == Intent.UNKNOWN
        # But "order" is English entity, so entity still matches
        assert result.entity == Entity.ORDER

    # ------------------------------------------------------------------
    # Entity ID extraction from Hinglish text
    # ------------------------------------------------------------------

    def test_extract_id_from_hinglish(self):
        """ID extraction works with Hinglish patterns."""
        result = self.clf.classify("order 67890 ka status batao")
        assert result.entity_id == "67890"

    def test_extract_id_mixed(self):
        """ID extraction with mixed text: '55555 wala order dikhao'."""
        result = self.clf.classify("55555 wala order dikhao")
        assert result.entity_id == "55555"

    # ------------------------------------------------------------------
    # Confidence for Hinglish
    # ------------------------------------------------------------------

    def test_hinglish_confidence_slightly_lower(self):
        """Hinglish-only matches have slightly lower confidence (0.95 vs 1.0)."""
        result = self.clf.classify("order dikhao")
        assert result.confidence == 0.95

    def test_english_confidence_full(self):
        """English matches still get full 1.0 confidence."""
        result = self.clf.classify("show order 12345")
        assert result.confidence == 1.0

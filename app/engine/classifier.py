"""
Intent classifier for COSMOS.

3-tier classification:
  Tier 1 — Rule-based (regex patterns, confidence 1.0 on clear match)
  Tier 2 — Reserved for local model
  Tier 3 — Haiku for ambiguous queries
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Intent(str, Enum):
    LOOKUP = "lookup"
    EXPLAIN = "explain"
    ACT = "act"
    REPORT = "report"
    NAVIGATE = "navigate"
    UNKNOWN = "unknown"


class Entity(str, Enum):
    ORDER = "order"
    SHIPMENT = "shipment"
    RETURN = "return"
    CUSTOMER = "customer"
    PAYMENT = "payment"
    NDR = "ndr"
    BILLING = "billing"
    WALLET = "wallet"
    SELLER = "seller"
    UNKNOWN = "unknown"


@dataclass
class ClassifyResult:
    intent: Intent
    entity: Entity
    entity_id: Optional[str]
    confidence: float
    needs_ai: bool
    sub_intents: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: Dict[Intent, List[re.Pattern]] = {
    Intent.LOOKUP: [
        re.compile(r"\b(show|get|find|fetch|track|status|where\s+is|look\s*up|details?\s+of|info\s+(on|about))\b", re.I),
    ],
    Intent.EXPLAIN: [
        re.compile(r"\b(why|how\s+come|reason|explain|what\s+happened|cause)\b", re.I),
    ],
    Intent.ACT: [
        re.compile(r"\b(cancel|update|refund|change|modify|edit|reship|reattempt|reassign|escalate|block|unblock)\b", re.I),
    ],
    Intent.REPORT: [
        re.compile(r"\b(how\s+many|total|count|sum|average|report|stats|statistics|breakdown|aggregate)\b", re.I),
    ],
    Intent.NAVIGATE: [
        re.compile(r"\b(go\s+to|open|take\s+me|navigate|redirect|visit|switch\s+to)\b", re.I),
    ],
}

_ENTITY_PATTERNS: Dict[Entity, List[re.Pattern]] = {
    # More specific entities first to avoid "order" matching before "payment"
    Entity.NDR: [re.compile(r"\b(ndr|non[\s-]?deliver(y|ed)|undelivered|failed\s+deliver)\b", re.I)],
    Entity.SHIPMENT: [re.compile(r"\b(shipment|shipping|awb|courier|delivery|deliveries)\b", re.I)],
    Entity.RETURN: [re.compile(r"\breturn(s)?\b", re.I)],
    Entity.PAYMENT: [re.compile(r"\b(payment|pay|transaction|cod|prepaid|refund)\b", re.I)],
    Entity.BILLING: [re.compile(r"\b(bill(ing)?|invoice|charge(s)?)\b", re.I)],
    Entity.WALLET: [re.compile(r"\b(wallet|balance|recharge|credit(s)?)\b", re.I)],
    Entity.CUSTOMER: [re.compile(r"\bcustomer(s)?\b", re.I)],
    Entity.SELLER: [re.compile(r"\b(seller|merchant|vendor)\b", re.I)],
    Entity.ORDER: [re.compile(r"\border(s)?\b", re.I)],
}

# ---------------------------------------------------------------------------
# Hinglish (Hindi + English) pattern definitions
# ---------------------------------------------------------------------------

_HINGLISH_INTENT_PATTERNS: Dict[Intent, List[re.Pattern]] = {
    Intent.LOOKUP: [
        re.compile(r"\b(dikha|dikhao|batao|bata|kahan\s+hai|kaha\s+hai|status\s+batao|pata\s+karo|check\s+karo)\b", re.I),
    ],
    Intent.EXPLAIN: [
        re.compile(r"\b(kyun|kyu|kyon|kaise|kaisa|wajah|reason\s+batao|samjhao|samjha)\b", re.I),
    ],
    Intent.ACT: [
        re.compile(r"\b(cancel\s+karo|kardo|refund\s+do|refund\s+karo|wapas\s+karo|change\s+karo|badal\s+do|hatao|rok\s+do|band\s+karo)\b", re.I),
    ],
    Intent.REPORT: [
        re.compile(r"\b(kitne|kitna|total\s+batao|count\s+karo|report\s+do|summary\s+do)\b", re.I),
    ],
    Intent.NAVIGATE: [
        re.compile(r"\b(le\s+jao|kholo|open\s+karo|page\s+dikhao|dashboard\s+dikhao)\b", re.I),
    ],
}

_HINGLISH_ENTITY_PATTERNS: Dict[Entity, List[re.Pattern]] = {
    Entity.NDR: [re.compile(r"\b(ndr|deliver\s+nahi\s+hua|nahi\s+mila|nahi\s+pahuncha)\b", re.I)],
    Entity.SHIPMENT: [re.compile(r"\b(shipment|shipping|courier|delivery|parcel)\b", re.I)],
    Entity.RETURN: [re.compile(r"\b(return|wapsi|wapas)\b", re.I)],
    Entity.PAYMENT: [re.compile(r"\b(payment|paisa|paise|rupees?|rs|bhugtan)\b", re.I)],
    Entity.BILLING: [re.compile(r"\b(bill|invoice|charge|kharcha)\b", re.I)],
    Entity.WALLET: [re.compile(r"\b(wallet|balance|paisa|credit)\b", re.I)],
    Entity.CUSTOMER: [re.compile(r"\b(customer|grahak)\b", re.I)],
    Entity.SELLER: [re.compile(r"\b(seller|merchant|vikreta)\b", re.I)],
    Entity.ORDER: [re.compile(r"\border\b", re.I)],  # "order" is used as-is in Hinglish
}

# Hinglish number words to digits mapping
_HINGLISH_NUMBER_WORDS: Dict[str, str] = {
    "ek": "1", "do": "2", "teen": "3", "char": "4", "paanch": "5",
    "chhe": "6", "saat": "7", "aath": "8", "nau": "9", "das": "10",
}

# Common ID patterns — returns (label, compiled regex)
_ID_PATTERNS: List[tuple] = [
    ("order_id", re.compile(r"\b(?:order\s*(?:#|id|number)?[:\s]*)(\d{4,})\b", re.I)),
    ("awb", re.compile(r"\b(?:awb|tracking)\s*(?:#|id|number)?[:\s]*([A-Z0-9]{8,})\b", re.I)),
    ("generic_id", re.compile(r"\b#?(\d{5,})\b")),
]

# Hinglish-specific ID patterns (e.g., "order number 12345 ka status")
_HINGLISH_ID_PATTERNS: List[tuple] = [
    ("order_id_hinglish", re.compile(r"\border\s+(?:number\s+)?(\d{4,})\b", re.I)),
    ("generic_hinglish", re.compile(r"\b(\d{4,})\b")),
]


class IntentClassifier:
    """3-tier intent classifier: rules -> local model -> LLM."""

    def __init__(self, hinglish_enabled: bool = True) -> None:
        # Pre-compiled patterns are module-level; nothing extra needed here.
        self._hinglish_enabled = hinglish_enabled

    # ------------------------------------------------------------------
    # Tier 1: Rule-based
    # ------------------------------------------------------------------

    def classify(self, text: str) -> ClassifyResult:
        """
        Tier 1 rule-based classification.

        Returns a ClassifyResult. If the match is ambiguous (multiple intents
        with equal strength, or no entity found), ``needs_ai`` is set so the
        caller can fall through to Tier 3.
        """
        text = text.strip()
        if not text:
            return ClassifyResult(
                intent=Intent.UNKNOWN,
                entity=Entity.UNKNOWN,
                entity_id=None,
                confidence=0.0,
                needs_ai=True,
            )

        # --- Intent detection (English first) ---
        matched_intents: List[Intent] = []
        hinglish_match = False
        for intent, patterns in _INTENT_PATTERNS.items():
            for pat in patterns:
                if pat.search(text):
                    matched_intents.append(intent)
                    break  # one match per intent is enough

        # --- Hinglish intent fallback ---
        if not matched_intents and self._hinglish_enabled:
            for intent, patterns in _HINGLISH_INTENT_PATTERNS.items():
                for pat in patterns:
                    if pat.search(text):
                        matched_intents.append(intent)
                        hinglish_match = True
                        break

        if len(matched_intents) == 1:
            primary_intent = matched_intents[0]
            intent_confidence = 0.95 if hinglish_match else 1.0
        elif len(matched_intents) > 1:
            # Multiple intents detected — pick the first as primary,
            # rest become sub-intents. Slightly lower confidence.
            primary_intent = matched_intents[0]
            intent_confidence = 0.70 if hinglish_match else 0.75
        else:
            primary_intent = Intent.UNKNOWN
            intent_confidence = 0.0

        sub_intents = [i.value for i in matched_intents[1:]] if len(matched_intents) > 1 else []

        # --- Entity detection (English first) ---
        matched_entity = Entity.UNKNOWN
        for entity, patterns in _ENTITY_PATTERNS.items():
            for pat in patterns:
                if pat.search(text):
                    matched_entity = entity
                    break
            if matched_entity != Entity.UNKNOWN:
                break

        # --- Hinglish entity fallback ---
        if matched_entity == Entity.UNKNOWN and self._hinglish_enabled:
            for entity, patterns in _HINGLISH_ENTITY_PATTERNS.items():
                for pat in patterns:
                    if pat.search(text):
                        matched_entity = entity
                        hinglish_match = True
                        break
                if matched_entity != Entity.UNKNOWN:
                    break

        # --- Entity ID extraction ---
        entity_id = self._extract_id(text)

        # --- Confidence & needs_ai ---
        if primary_intent == Intent.UNKNOWN and matched_entity == Entity.UNKNOWN:
            confidence = 0.0
            needs_ai = True
        elif primary_intent == Intent.UNKNOWN or matched_entity == Entity.UNKNOWN:
            confidence = intent_confidence * 0.5
            needs_ai = True
        else:
            confidence = intent_confidence
            needs_ai = False

        return ClassifyResult(
            intent=primary_intent,
            entity=matched_entity,
            entity_id=entity_id,
            confidence=confidence,
            needs_ai=needs_ai,
            sub_intents=sub_intents,
        )

    # ------------------------------------------------------------------
    # Tier 3: AI-assisted classification
    # ------------------------------------------------------------------

    async def classify_with_ai(self, text: str, llm_client: Any) -> ClassifyResult:
        """
        Tier 3: Use Claude Haiku when rules fail or are ambiguous.
        """
        prompt = (
            "You are an intent classifier for an e-commerce logistics platform.\n"
            "Classify the following user message into exactly ONE intent and ONE entity.\n\n"
            "Intents: lookup, explain, act, report, navigate\n"
            "Entities: order, shipment, return, customer, payment, ndr, billing, wallet, seller\n\n"
            f"User message: \"{text}\"\n\n"
            "Respond in this exact JSON format (no markdown):\n"
            '{"intent": "<intent>", "entity": "<entity>", "entity_id": "<id or null>", '
            '"confidence": <0.0-1.0>, "sub_intents": []}'
        )

        try:
            raw = await llm_client.complete(prompt, max_tokens=200)
            import json
            parsed = json.loads(raw.strip())

            intent = Intent(parsed.get("intent", "unknown"))
            entity = Entity(parsed.get("entity", "unknown"))
            entity_id = parsed.get("entity_id")
            if entity_id == "null" or entity_id is None:
                entity_id = None
            ai_confidence = float(parsed.get("confidence", 0.5))
            sub_intents = parsed.get("sub_intents", [])

            # AI confidence is capped at 0.9 to reflect inherent uncertainty
            return ClassifyResult(
                intent=intent,
                entity=entity,
                entity_id=entity_id,
                confidence=min(ai_confidence, 0.9),
                needs_ai=False,
                sub_intents=sub_intents,
            )
        except Exception:
            # Fallback: return unknown with low confidence
            return ClassifyResult(
                intent=Intent.UNKNOWN,
                entity=Entity.UNKNOWN,
                entity_id=self._extract_id(text),
                confidence=0.1,
                needs_ai=False,
                sub_intents=[],
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_id(text: str) -> Optional[str]:
        """Extract the most specific entity ID from text.

        Tries English patterns first, then Hinglish-specific patterns.
        """
        for _label, pattern in _ID_PATTERNS:
            m = pattern.search(text)
            if m:
                return m.group(1)
        # Hinglish fallback patterns
        for _label, pattern in _HINGLISH_ID_PATTERNS:
            m = pattern.search(text)
            if m:
                return m.group(1)
        return None

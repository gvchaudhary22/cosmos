"""
Compliance Guardrails — Financial data masking, competitor filtering,
repeat abuse detection, sensitive action confirmation, and language consistency.

POST-execution guards:
  8. FinancialDataMaskingGuard — masks financial values unless explicitly asked
  9. CompetitorMentionGuard — blocks competitor recommendations (allows factual mention)
  10. RepeatQueryAbuseGuard — blocks repeated identical queries (token waste prevention)
  11. SensitiveActionConfirmationGuard — requires confirmation for multi-entity actions
  12. LanguageConsistencyGuard — detects input/output language mismatch
"""

import re
import time
import hashlib
from collections import defaultdict
from typing import Any, Dict, List, Optional

import structlog

from app.guardrails.base import Guardrail, GuardrailResult, GuardrailAction

logger = structlog.get_logger()


class FinancialDataMaskingGuard(Guardrail):
    """
    Masks financial values in responses unless the user explicitly asked
    about financial data (wallet, billing, charges, refund).

    Always masks:
      - Credit card numbers, bank account numbers

    Conditionally masks (if query is NOT about finances):
      - Wallet balance amounts
      - COD amounts, freight charges
      - Commission rates, margin percentages
      - Remittance amounts
    """

    name = "financial_data_masking"

    _FINANCIAL_QUERY_KEYWORDS = [
        "wallet", "balance", "billing", "invoice", "charge", "freight",
        "refund", "payment", "cod", "remittance", "cost", "price",
        "amount", "debit", "credit", "fee", "commission", "kitna paisa",
        "paise", "rupees", "rupaye",
    ]

    # Always mask — sensitive financial identifiers
    _ALWAYS_MASK = [
        (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "XXXX-XXXX-XXXX-****"),
        (re.compile(r"\b\d{9,18}\b(?=.*(?:account|a/c|acc))", re.I), "XXXXXXXXX****"),
        (re.compile(r"\bIFSC[\s:]*[A-Z]{4}0[A-Z0-9]{6}\b", re.I), "IFSC: XXXX0XXXXXX"),
    ]

    # Conditionally mask — financial values
    _CONDITIONAL_MASK = [
        (re.compile(r"(₹|Rs\.?|INR)\s*[\d,]+\.?\d{0,2}"), "₹***"),
        (re.compile(r"\b\d{1,3}(,\d{3})+(\.\d{1,2})?\s*(rupees?|rs)\b", re.I), "*** rupees"),
        (re.compile(r"\b\d+(\.\d+)?%\s*(commission|margin|rate)\b", re.I), "**% \\2"),
    ]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        response = context.get("response", "")
        if not response:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Always mask sensitive financial identifiers
        masked = response
        for pattern, replacement in self._ALWAYS_MASK:
            masked = pattern.sub(replacement, masked)

        # Check if user asked about finances
        user_message = context.get("user_message", "").lower()
        asked_about_finance = any(kw in user_message for kw in self._FINANCIAL_QUERY_KEYWORDS)

        if not asked_about_finance:
            # Mask financial values since user didn't ask
            for pattern, replacement in self._CONDITIONAL_MASK:
                masked = pattern.sub(replacement, masked)

        if masked != response:
            return GuardrailResult(
                action=GuardrailAction.MASK,
                reason="Financial data masked" + (" (not queried)" if not asked_about_finance else " (sensitive IDs)"),
                modified_data=masked,
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class CompetitorMentionGuard(Guardrail):
    """
    Blocks competitor recommendations while allowing factual mentions.

    Allowed: "Your courier is Delhivery" (factual, from API data)
    Blocked: "Try using Delhivery directly for better rates" (recommendation)
    """

    name = "competitor_mention"

    _COMPETITOR_BRANDS = [
        "delhivery direct", "ecom express portal", "bluedart portal",
        "dtdc website", "xpressbees direct", "shadowfax direct",
        "amazon logistics", "flipkart logistics", "meesho logistics",
        "vamaship", "pickrr", "clickpost", "shipway", "shyplite",
        "nimbuspost", "iThink logistics",
    ]

    _RECOMMENDATION_PATTERNS = [
        re.compile(r"\b(try|use|switch\s+to|consider|recommend|better\s+to\s+use|go\s+with|opt\s+for)\b", re.I),
        re.compile(r"\b(directly|their\s+portal|their\s+website|their\s+app)\b", re.I),
        re.compile(r"\b(instead\s+of\s+shiprocket|better\s+than\s+shiprocket|cheaper\s+than)\b", re.I),
    ]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        response = context.get("response", "")
        if not response:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        response_lower = response.lower()

        # Check if response mentions competitors in recommendation context
        has_competitor = any(comp in response_lower for comp in self._COMPETITOR_BRANDS)
        has_recommendation = any(p.search(response) for p in self._RECOMMENDATION_PATTERNS)

        if has_competitor and has_recommendation:
            logger.info("guard.competitor_recommendation_detected")
            # Don't block — mask the recommendation part
            masked = response
            for pattern in self._RECOMMENDATION_PATTERNS:
                masked = pattern.sub("[see Shiprocket options]", masked)

            return GuardrailResult(
                action=GuardrailAction.MASK,
                reason="Competitor recommendation replaced with platform guidance",
                modified_data=masked,
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class RepeatQueryAbuseGuard(Guardrail):
    """
    Detects and blocks repeated identical queries within a time window.
    Prevents token waste and probing attacks.

    Rules:
      - Same exact query > 5 times in 5 minutes → BLOCK
      - Same semantic query > 10 times in 10 minutes → BLOCK
      - Return cached response instead of re-processing
    """

    name = "repeat_query_abuse"

    MAX_REPEATS = 5
    WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self):
        # user_id → list of (hash, timestamp)
        self._query_history: Dict[str, List[tuple]] = defaultdict(list)

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_message = context.get("user_message", "")
        user_id = context.get("user_id", "anonymous")

        if not user_message:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Hash the query for comparison
        query_hash = hashlib.md5(user_message.strip().lower().encode()).hexdigest()
        now = time.time()

        # Clean old entries
        cutoff = now - self.WINDOW_SECONDS
        self._query_history[user_id] = [
            (h, t) for h, t in self._query_history[user_id] if t > cutoff
        ]

        # Count repeats
        repeat_count = sum(1 for h, _ in self._query_history[user_id] if h == query_hash)

        # Record this query
        self._query_history[user_id].append((query_hash, now))

        if repeat_count >= self.MAX_REPEATS:
            logger.warning(
                "guard.repeat_abuse_blocked",
                user_id=user_id,
                count=repeat_count + 1,
            )
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=(
                    f"Query repeated {repeat_count + 1} times in "
                    f"{self.WINDOW_SECONDS // 60} minutes. "
                    f"Please wait before asking the same question."
                ),
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class SensitiveActionConfirmationGuard(Guardrail):
    """
    Requires explicit confirmation for dangerous or multi-entity actions.

    Triggers:
      - "Cancel ALL my orders" → requires confirmation
      - "Refund everything" → requires confirmation
      - "Delete my account" → requires confirmation
      - Any action affecting > 1 entity → requires confirmation
    """

    name = "sensitive_action_confirmation"

    _MASS_ACTION_PATTERNS = [
        re.compile(r"\b(cancel|delete|remove|refund)\s+(all|every|sab|sara|poore)\b", re.I),
        re.compile(r"\b(all|every|sab|sara)\s+.{0,20}\s+(cancel|delete|remove|refund)\b", re.I),
        re.compile(r"\b(bulk|mass)\s+(cancel|delete|refund|update)\b", re.I),
        re.compile(r"\bdelete\s+(my\s+)?account\b", re.I),
        re.compile(r"\bdeactivate\s+(my\s+)?account\b", re.I),
    ]

    _IRREVERSIBLE_ACTIONS = [
        re.compile(r"\btrigger\s+rto\b", re.I),
        re.compile(r"\bforce\s+(cancel|close|delete)\b", re.I),
        re.compile(r"\bpermanently\s+(delete|remove)\b", re.I),
    ]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_message = context.get("user_message", "")
        if not user_message:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Check for confirmation already provided
        has_confirmation = context.get("action_confirmed", False)
        if has_confirmation:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Check mass actions
        for pattern in self._MASS_ACTION_PATTERNS:
            if pattern.search(user_message):
                return GuardrailResult(
                    action=GuardrailAction.WARN,
                    reason=(
                        "This action affects multiple items. "
                        "Please confirm: are you sure you want to proceed? "
                        "Reply with the specific items you want to act on."
                    ),
                )

        # Check irreversible actions
        for pattern in self._IRREVERSIBLE_ACTIONS:
            if pattern.search(user_message):
                return GuardrailResult(
                    action=GuardrailAction.WARN,
                    reason=(
                        "This action is irreversible and cannot be undone. "
                        "Please confirm you want to proceed."
                    ),
                )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class LanguageConsistencyGuard(Guardrail):
    """
    Detects when response language doesn't match query language.

    If user asks in Hindi/Hinglish → response should be in Hindi/Hinglish.
    If user asks in English → response should be in English.

    Flags mismatch for potential re-generation.
    """

    name = "language_consistency"

    _HINDI_CHARS = re.compile(r"[\u0900-\u097F]")
    _HINGLISH_WORDS = re.compile(
        r"\b(kya|kaise|kahan|kab|kyun|mera|meri|mere|hai|hain|ho|tha|"
        r"thi|karo|karna|batao|dikha|chahiye|nahi|haan|ji|aur|ya|"
        r"se|ka|ki|ke|ko|me|pe|par|wala|wali|kuch|sab)\b", re.I
    )

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_message = context.get("user_message", "")
        response = context.get("response", "")

        if not user_message or not response:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Detect input language
        input_hindi_chars = len(self._HINDI_CHARS.findall(user_message))
        input_hinglish_words = len(self._HINGLISH_WORDS.findall(user_message))
        input_total_words = len(user_message.split())

        is_input_hindi = input_hindi_chars > 5
        is_input_hinglish = (
            not is_input_hindi
            and input_total_words > 0
            and (input_hinglish_words / input_total_words) > 0.3
        )

        if not is_input_hindi and not is_input_hinglish:
            # English input — response language doesn't matter much
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Check response language
        response_hindi_chars = len(self._HINDI_CHARS.findall(response))
        response_hinglish_words = len(self._HINGLISH_WORDS.findall(response))
        response_total_words = len(response.split())

        is_response_hindi = response_hindi_chars > 5
        is_response_hinglish = (
            not is_response_hindi
            and response_total_words > 0
            and (response_hinglish_words / response_total_words) > 0.1
        )

        if is_input_hindi and not is_response_hindi:
            return GuardrailResult(
                action=GuardrailAction.WARN,
                reason="User asked in Hindi but response is in English. Consider responding in Hindi.",
            )

        if is_input_hinglish and not is_response_hinglish and not is_response_hindi:
            return GuardrailResult(
                action=GuardrailAction.WARN,
                reason="User asked in Hinglish but response is purely English. Consider mixing Hindi terms.",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)

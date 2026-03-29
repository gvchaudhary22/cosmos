"""
MARS Safety Guardrail — Enhanced injection detection implemented in MARS.

Ports patterns from:
  - mars/docs/prompt-safety.md (direct/indirect injection, data exfiltration)
  - mars/safety/evaluator (command injection, shell bypass patterns, risk scoring)
  - mars/hooks/pre-tool-use (obfuscation detection)

Risk scoring: 0-10 scale. Block at >= 7. Warn at >= 4.
"""

import re
from typing import Any, Dict, List, Tuple

import structlog

from app.guardrails.base import Guardrail, GuardrailResult, GuardrailAction

logger = structlog.get_logger()

# Risk score threshold — mars safety threshold
BLOCK_THRESHOLD = 7
WARN_THRESHOLD = 4


class MarsSafetyGuardrail(Guardrail):
    """Enhanced injection detection implemented in MARS.

    Six categories of threat patterns with risk scoring:
      1. Direct injection — override/replace system instructions (risk: 9)
      2. Indirect injection — fake structural markers (risk: 8)
      3. Command injection — shell bypass patterns (risk: 9)
      4. Data exfiltration — probing for system prompt (risk: 8)
      5. Tool hijacking — forcing unauthorized tool calls (risk: 9)
      6. Obfuscation — base64, encoded payloads, unicode tricks (risk: 9)

    Each pattern has an associated risk score (0-10). The guardrail blocks
    at score >= 7 and warns at score >= 4, mars safety threshold.
    """

    name = "mars_safety"

    # ------------------------------------------------------------------
    # Pattern tuples: (compiled_regex, risk_score)
    # ------------------------------------------------------------------

    # 1. Direct injection (mars/docs/prompt-safety.md)
    DIRECT_INJECTION_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I), 9),
        (re.compile(r"disregard\s+(your\s+)?instructions", re.I), 9),
        (re.compile(r"you\s+are\s+now\s+a", re.I), 9),
        (re.compile(r"pretend\s+you\s+are", re.I), 8),
        (re.compile(r"act\s+as\s+if", re.I), 7),
        (re.compile(r"from\s+now\s+on\s+you\s+will", re.I), 9),
        (re.compile(r"new\s+instructions?:?\s", re.I), 8),
        (re.compile(r"override\s+(safety|security|rules|guardrail)", re.I), 9),
        (re.compile(r"forget\s+(everything|all|your)\s+(you|about|previous)", re.I), 9),
        (re.compile(r"do\s+not\s+follow\s+(your|the|any)\s+(rules|instructions|guidelines)", re.I), 9),
        (re.compile(r"jailbreak", re.I), 9),
        (re.compile(r"DAN\s+mode", re.I), 9),
    ]

    # 2. Indirect injection (fake structural markers)
    INDIRECT_INJECTION_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"<\s*system\s*>", re.I), 8),
        (re.compile(r"<\s*/?\s*system[-_]?prompt\s*>", re.I), 9),
        (re.compile(r"\[INST\]", re.I), 8),
        (re.compile(r"\[/INST\]", re.I), 8),
        (re.compile(r"###\s*system", re.I), 7),
        (re.compile(r"^Human:\s*", re.I | re.M), 7),
        (re.compile(r"^Assistant:\s*", re.I | re.M), 7),
        (re.compile(r"<\|im_start\|>", re.I), 9),
        (re.compile(r"<\|im_end\|>", re.I), 9),
        (re.compile(r"<\|endoftext\|>", re.I), 9),
        (re.compile(r"<<SYS>>", re.I), 9),
        (re.compile(r"<</SYS>>", re.I), 9),
    ]

    # 3. Command injection (mars/safety/evaluator)
    COMMAND_INJECTION_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"\$\(.*\)"), 9),
        (re.compile(r"`[^`]+`"), 9),
        (re.compile(r"base64\s+--decode", re.I), 9),
        (re.compile(r"curl.*\|.*sh", re.I), 9),
        (re.compile(r"wget.*\|.*sh", re.I), 9),
        (re.compile(r">\s*/etc/"), 9),
        (re.compile(r"chmod\s+\+x", re.I), 9),
        (re.compile(r"sh\s+-c", re.I), 9),
        (re.compile(r"eval\s*\(", re.I), 8),
        (re.compile(r"exec\s*\(", re.I), 8),
        (re.compile(r"\brm\s+-rf\b", re.I), 9),
        (re.compile(r"\bmkfs\b", re.I), 9),
        (re.compile(r"\bdd\s+if=", re.I), 9),
        (re.compile(r"\bshutdown\b", re.I), 8),
        (re.compile(r"\breboot\b", re.I), 8),
    ]

    # 4. Data exfiltration (probing for system prompt or secrets)
    DATA_EXFILTRATION_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"(show|print|reveal|display|output)\s+(\w+\s+)*(system\s+)?prompt", re.I), 8),
        (re.compile(r"what\s+are\s+your\s+(instructions|rules|constraints|guidelines)", re.I), 7),
        (re.compile(r"repeat\s+(everything|all)\s+(above|before|from\s+the\s+start)", re.I), 8),
        (re.compile(r"(dump|leak|expose|exfiltrate)\s+.*\b(context|prompt|memory|config)\b", re.I), 9),
        (re.compile(r"/etc/(passwd|shadow|sudoers)", re.I), 8),
        (re.compile(r"\.(ssh|aws|env|bash_history)", re.I), 8),
        (re.compile(r"(api[_-]?key|secret[_-]?key|password|token)\s*[:=]", re.I), 7),
    ]

    # 5. Tool hijacking — forcing agent to call unauthorized tools
    TOOL_HIJACKING_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"(call|execute|run|invoke)\s+(the\s+)?(delete|drop|truncate|destroy)", re.I), 9),
        (re.compile(r"use\s+tool\s*:?\s*(rm|delete|drop|destroy)", re.I), 9),
        (re.compile(r"force\s+(call|execution|tool)", re.I), 8),
        (re.compile(r"bypass\s+(auth|permission|guard|check|validation)", re.I), 9),
        (re.compile(r"skip\s+(auth|verification|validation|approval)", re.I), 8),
        (re.compile(r"sudo\b", re.I), 7),
        (re.compile(r"admin\s+override", re.I), 8),
    ]

    # 6. Obfuscation — encoded payloads, unicode tricks
    OBFUSCATION_PATTERNS: List[Tuple[re.Pattern, int]] = [
        (re.compile(r"[a-zA-Z0-9+/=]{40,}"), 7),       # Long base64-like strings
        (re.compile(r"\\x[0-9a-fA-F]{2}(\\x[0-9a-fA-F]{2}){3,}"), 9),  # Hex escape sequences
        (re.compile(r"\\u[0-9a-fA-F]{4}(\\u[0-9a-fA-F]{4}){3,}"), 9),  # Unicode escapes
        (re.compile(r"&#x?[0-9a-fA-F]+;(&#x?[0-9a-fA-F]+;){3,}"), 8),  # HTML entities
        (re.compile(r"atob\s*\(", re.I), 9),              # JS base64 decode
        (re.compile(r"String\.fromCharCode", re.I), 9),   # JS char code
    ]

    _CATEGORIES: Dict[str, List[Tuple[re.Pattern, int]]]

    def __init__(self):
        self._CATEGORIES = {
            "direct_injection": self.DIRECT_INJECTION_PATTERNS,
            "indirect_injection": self.INDIRECT_INJECTION_PATTERNS,
            "command_injection": self.COMMAND_INJECTION_PATTERNS,
            "data_exfiltration": self.DATA_EXFILTRATION_PATTERNS,
            "tool_hijacking": self.TOOL_HIJACKING_PATTERNS,
            "obfuscation": self.OBFUSCATION_PATTERNS,
        }

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        """Check user message against all MARS safety patterns with risk scoring.

        Block at risk >= 7, warn at risk >= 4, allow otherwise.
        """
        user_message = context.get("user_message", "")
        if not user_message:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Also scan tool results for indirect injection via retrieved data
        tool_results = context.get("tool_results", "")
        texts_to_scan = [user_message]
        if tool_results:
            texts_to_scan.append(str(tool_results))

        max_risk = 0
        worst_match = None

        for text in texts_to_scan:
            for category, patterns in self._CATEGORIES.items():
                for pattern, risk in patterns:
                    if pattern.search(text):
                        source = "user_message" if text == user_message else "tool_result"
                        if risk > max_risk:
                            max_risk = risk
                            worst_match = {
                                "category": category,
                                "pattern": pattern.pattern,
                                "risk": risk,
                                "source": source,
                            }

        if max_risk >= BLOCK_THRESHOLD:
            logger.warning(
                "mars_safety.blocked",
                category=worst_match["category"],
                risk=max_risk,
                source=worst_match["source"],
            )
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=(
                    f"MARS safety violation [{worst_match['category']}] "
                    f"risk={max_risk}/10: matched '{worst_match['pattern']}' "
                    f"in {worst_match['source']}"
                ),
            )

        if max_risk >= WARN_THRESHOLD:
            logger.info(
                "mars_safety.warn",
                category=worst_match["category"],
                risk=max_risk,
            )
            return GuardrailResult(
                action=GuardrailAction.WARN,
                reason=(
                    f"MARS safety warning [{worst_match['category']}] "
                    f"risk={max_risk}/10: matched '{worst_match['pattern']}'"
                ),
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)

    def scan_all(self, text: str) -> List[Dict[str, Any]]:
        """Scan text and return all matches with risk scores (for logging/analytics)."""
        matches: List[Dict[str, Any]] = []
        for category, patterns in self._CATEGORIES.items():
            for pattern, risk in patterns:
                if pattern.search(text):
                    matches.append({
                        "category": category,
                        "pattern": pattern.pattern,
                        "risk": risk,
                    })
        return matches

    def risk_score(self, text: str) -> int:
        """Return the highest risk score for a given text. 0 = safe."""
        max_risk = 0
        for _, patterns in self._CATEGORIES.items():
            for pattern, risk in patterns:
                if pattern.search(text) and risk > max_risk:
                    max_risk = risk
        return max_risk

"""
Request Classifier — MARS three-dimension classification.

Classifies every incoming query along three axes:
1. Domain: ORDERS, SHIPMENTS, NDR, BILLING, SELLER, SYSTEM, GENERAL
2. Complexity: QUICK (skip deep), STANDARD (normal flow), COMPLEX (all deeps)
3. Mode: LOOKUP (read-only), ACTION (state-changing), DIAGNOSTIC (trace/debug)

The orchestrator uses complexity to decide whether to skip Stage 2 entirely
(QUICK queries) or fire all deep pipelines (COMPLEX queries).
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import structlog

logger = structlog.get_logger()


class QueryDomain(str, Enum):
    ORDERS = "orders"
    SHIPMENTS = "shipments"
    NDR = "ndr"
    BILLING = "billing"
    SELLER = "seller"
    SYSTEM = "system"       # sync, internal, ICRM, admin
    RETURNS = "returns"
    GENERAL = "general"


class QueryComplexity(str, Enum):
    QUICK = "quick"         # Simple lookup, skip Stage 2 entirely
    STANDARD = "standard"   # Normal flow, conditional deepening
    COMPLEX = "complex"     # Fire all deep pipelines


class QueryMode(str, Enum):
    LOOKUP = "lookup"       # Read-only information retrieval
    ACTION = "action"       # State-changing (refund, reassign, escalate)
    DIAGNOSTIC = "diagnostic"  # Trace, debug, "why" queries


@dataclass
class RequestClassification:
    domain: QueryDomain
    complexity: QueryComplexity
    mode: QueryMode
    confidence: float = 0.0
    sub_domains: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)  # what triggered this classification


# Domain detection patterns
_DOMAIN_PATTERNS = {
    QueryDomain.ORDERS: [
        r'\border(s)?\b', r'\bawb\b', r'\bmanifest\b', r'\bpickup\b',
        r'\bdelivery\b', r'\bdelivered\b', r'\bshipment\s*order\b',
        # Hinglish
        r'\border\s*kaha\b', r'\border\s*ka\b', r'\bmera\s*order\b',
    ],
    QueryDomain.SHIPMENTS: [
        r'\bshipment(s)?\b', r'\btracking\b', r'\bcourier\b', r'\btransit\b',
        r'\bin[\s-]*transit\b', r'\bout[\s-]*for[\s-]*delivery\b',
        r'\bship\b', r'\bdispatch(ed)?\b',
    ],
    QueryDomain.NDR: [
        r'\bndr\b', r'\bnon[\s-]*delivery\b', r'\brto\b', r'\breturn[\s-]*to[\s-]*origin\b',
        r'\bundelivered\b', r'\bfailed\s*delivery\b', r'\breattempt\b',
    ],
    QueryDomain.BILLING: [
        r'\bbill(ing)?\b', r'\binvoice\b', r'\bwallet\b', r'\bpayment\b',
        r'\bcharge(s)?\b', r'\brefund\b', r'\bcod\b', r'\bremittance\b',
        r'\bweight\s*discrepancy\b', r'\bfreight\b',
    ],
    QueryDomain.SELLER: [
        r'\bseller\b', r'\baccount\b', r'\bprofile\b', r'\bkyc\b',
        r'\bonboard(ing)?\b', r'\bregistration\b',
    ],
    QueryDomain.SYSTEM: [
        r'\bsync\b', r'\bsystem\b', r'\bicrm\b', r'\badmin\b', r'\binternal\b',
        r'\bpanel\b', r'\bbackend\b', r'\bapi\b', r'\bwebhook\b',
        r'\bstatus\s*(not\s*)?updat(ed|ing)\b',
    ],
    QueryDomain.RETURNS: [
        r'\breturn(s)?\b', r'\bexchange\b', r'\breverse\b',
        r'\bpickup\s*return\b',
    ],
}

# Complexity signals
_COMPLEX_SIGNALS = [
    r'\bwhy\b',
    r'\btrace\b',
    r'\bhow\s*(does|do|is|are)\b',
    r'\bsync\b.*\b(not|isn\'t|hasn\'t)\b',
    r'\bdelayed\b.*\bwhy\b',
    r'\bstuck\b',
    r'\bbroken\b',
    r'\bcompare\b',
    r'\bdifference\s*between\b',
    r'\bpath\b.*\bdata\b',
    r'\bfield\b.*\btrace\b',
    r'\bdb\b.*\bcolumn\b',
    r'\bapi\b.*\bmapping\b',
]

_QUICK_SIGNALS = [
    r'^(what|where)\s+is\s+(my|the)\s+\w+\s*\??$',  # "where is my order?"
    r'^(show|get|find|check)\s+',  # "show me X", "get order Y"
    r'^(status|track)\s+',  # "status of", "track order"
    r'^\w+\s*(id|number|#)\s*[:=]?\s*\w+',  # "order id: 123"
]

# Mode signals
_ACTION_SIGNALS = [
    r'\bcancel\b', r'\brefund\b', r'\bescalat(e|ion)\b', r'\breassign\b',
    r'\breschedul(e|ing)\b', r'\breattempt\b', r'\bupdate\b', r'\bchange\b',
    r'\bremove\b', r'\bdelete\b', r'\bcreate\b', r'\braise\b',
    # Hinglish
    r'\bkaro\b', r'\bkar\s*do\b', r'\bbadal\s*do\b',
]

_DIAGNOSTIC_SIGNALS = [
    r'\bwhy\b', r'\bhow\b', r'\btrace\b', r'\bdebug\b',
    r'\broot\s*cause\b', r'\binvestigat(e|ion)\b', r'\bdiagnos(e|tic)\b',
    r'\bexplain\b', r'\breason\b',
    # Hinglish
    r'\bkyu(n)?\b', r'\bkaise\b',
]


class RequestClassifier:
    """
    Three-dimension classifier for incoming queries.

    Used by the orchestrator to decide:
    - QUICK → skip Stage 2 entirely (probe-only, ~50ms total)
    - STANDARD → normal conditional deepening
    - COMPLEX → fire all deep pipelines (GraphRAG + cross-repo + session)
    """

    def classify(self, query: str) -> RequestClassification:
        """Classify a query along domain, complexity, and mode axes."""
        query_lower = query.lower().strip()
        signals = []

        # --- Domain ---
        domain_scores = {}
        for domain, patterns in _DOMAIN_PATTERNS.items():
            score = sum(1 for p in patterns if re.search(p, query_lower, re.IGNORECASE))
            if score > 0:
                domain_scores[domain] = score

        if domain_scores:
            domain = max(domain_scores, key=domain_scores.get)
            sub_domains = [d.value for d, s in domain_scores.items() if s > 0 and d != domain]
            signals.append(f"domain:{domain.value}(score={domain_scores[domain]})")
        else:
            domain = QueryDomain.GENERAL
            sub_domains = []
            signals.append("domain:general(no pattern match)")

        # --- Mode ---
        action_hits = sum(1 for p in _ACTION_SIGNALS if re.search(p, query_lower))
        diagnostic_hits = sum(1 for p in _DIAGNOSTIC_SIGNALS if re.search(p, query_lower))

        if action_hits > diagnostic_hits and action_hits > 0:
            mode = QueryMode.ACTION
            signals.append(f"mode:action(hits={action_hits})")
        elif diagnostic_hits > 0:
            mode = QueryMode.DIAGNOSTIC
            signals.append(f"mode:diagnostic(hits={diagnostic_hits})")
        else:
            mode = QueryMode.LOOKUP
            signals.append("mode:lookup(default)")

        # --- Complexity ---
        complex_hits = sum(1 for p in _COMPLEX_SIGNALS if re.search(p, query_lower))
        quick_hits = sum(1 for p in _QUICK_SIGNALS if re.search(p, query_lower))

        # Multi-domain queries are always complex
        multi_domain = len(domain_scores) >= 2
        # Diagnostic mode implies at least standard
        is_diagnostic = mode == QueryMode.DIAGNOSTIC
        # Multi-sentence queries (contains ? or . multiple times) suggest complexity
        multi_part = query_lower.count('?') >= 2 or len(query_lower.split('.')) >= 3

        if complex_hits >= 2 or multi_domain or multi_part:
            complexity = QueryComplexity.COMPLEX
            signals.append(f"complexity:complex(complex_hits={complex_hits}, multi_domain={multi_domain}, multi_part={multi_part})")
        elif quick_hits > 0 and complex_hits == 0 and not is_diagnostic:
            complexity = QueryComplexity.QUICK
            signals.append(f"complexity:quick(quick_hits={quick_hits})")
        else:
            complexity = QueryComplexity.STANDARD
            signals.append(f"complexity:standard(default)")

        # Confidence based on signal strength
        total_hits = sum(domain_scores.values()) + action_hits + diagnostic_hits + complex_hits + quick_hits
        confidence = min(0.95, 0.5 + (total_hits * 0.08))

        result = RequestClassification(
            domain=domain,
            complexity=complexity,
            mode=mode,
            confidence=confidence,
            sub_domains=sub_domains,
            signals=signals,
        )

        logger.debug(
            "request.classified",
            query=query[:60],
            domain=domain.value,
            complexity=complexity.value,
            mode=mode.value,
            confidence=round(confidence, 2),
        )

        return result

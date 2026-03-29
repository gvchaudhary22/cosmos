import re
import time
from typing import Any, Dict

from app.guardrails.base import Guardrail, GuardrailResult, GuardrailAction


class RoleAccessGuardrail(Guardrail):
    """Check if user's role can access the requested tool."""
    name = "role_access"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_role = context.get("user_role", "")
        tool_allowed_roles = context.get("tool_allowed_roles", [])

        if not tool_allowed_roles:  # empty = all roles allowed
            return GuardrailResult(action=GuardrailAction.ALLOW)

        if user_role in tool_allowed_roles:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        return GuardrailResult(
            action=GuardrailAction.BLOCK,
            reason=f"Role '{user_role}' is not authorized for this tool. Required: {tool_allowed_roles}",
        )


class TenantIsolationGuardrail(Guardrail):
    """Ensure seller can only access their own company data."""
    name = "tenant_isolation"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_role = context.get("user_role", "")

        # Non-seller roles (admin, support) can access any company
        if user_role in ("admin", "support_admin", "support_agent"):
            return GuardrailResult(action=GuardrailAction.ALLOW)

        user_company_id = context.get("user_company_id")
        target_company_id = context.get("target_company_id")

        # If no target company specified, allow (query will be scoped later)
        if target_company_id is None:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Seller must only access their own company
        if user_company_id is not None and str(target_company_id) != str(user_company_id):
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Tenant isolation violation: user belongs to company {user_company_id} but attempted to access company {target_company_id}",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class PIIProtectionGuardrail(Guardrail):
    """Mask PII in responses based on user role."""
    name = "pii_protection"

    PII_PATTERNS = {
        "phone": re.compile(r'\b[6-9]\d{9}\b'),
        "email": re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
        "aadhaar": re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),
        "pan": re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b'),
        "credit_card": re.compile(r'\b(?:\d{4}[\s-]?){3}\d{4}\b'),
    }

    AUTHORIZED_PII_ROLES = ["admin", "support_admin"]

    def mask_pii(self, text: str) -> str:
        """Apply masking to all PII patterns found in text."""
        # Phone: 9876543210 -> 98xxxxx210
        def mask_phone(m: re.Match) -> str:
            v = m.group()
            return v[:2] + "xxxxx" + v[-3:]

        # Email: user@domain.com -> us***@domain.com
        def mask_email(m: re.Match) -> str:
            v = m.group()
            local, domain = v.split("@", 1)
            masked_local = local[:2] + "***" if len(local) > 2 else "***"
            return f"{masked_local}@{domain}"

        # Aadhaar: 1234 5678 9012 -> xxxx xxxx 9012
        def mask_aadhaar(m: re.Match) -> str:
            v = m.group()
            # Keep last 4 digits, mask the rest
            digits = re.sub(r'[\s-]', '', v)
            last4 = digits[-4:]
            return f"xxxx xxxx {last4}"

        # PAN: ABCDE1234F -> ABCXXxxxxF
        def mask_pan(m: re.Match) -> str:
            v = m.group()
            return v[:3] + "XXxxxx" + v[-1]

        # Credit card: always fully masked
        def mask_cc(m: re.Match) -> str:
            return "xxxx xxxx xxxx xxxx"

        text = self.PII_PATTERNS["credit_card"].sub(mask_cc, text)
        text = self.PII_PATTERNS["aadhaar"].sub(mask_aadhaar, text)
        text = self.PII_PATTERNS["phone"].sub(mask_phone, text)
        text = self.PII_PATTERNS["email"].sub(mask_email, text)
        text = self.PII_PATTERNS["pan"].sub(mask_pan, text)
        return text

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_role = context.get("user_role", "")

        # Authorized roles see full data
        if user_role in self.AUTHORIZED_PII_ROLES:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        response = context.get("response", "")
        if not isinstance(response, str) or not response:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        masked = self.mask_pii(response)
        if masked != response:
            return GuardrailResult(
                action=GuardrailAction.MASK,
                reason="PII detected and masked",
                modified_data=masked,
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class RateLimitGuardrail(Guardrail):
    """Per-user rate limiting."""
    name = "rate_limit"

    def __init__(self, max_per_minute: int = 60):
        self.max_per_minute = max_per_minute
        self._requests: Dict[str, list] = {}  # user_id -> [timestamps]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_id = context.get("user_id", "anonymous")
        now = time.time()
        cutoff = now - 60.0

        # Get or create request list for this user
        if user_id not in self._requests:
            self._requests[user_id] = []

        # Clean old entries (>60s)
        self._requests[user_id] = [
            ts for ts in self._requests[user_id] if ts > cutoff
        ]

        # Check count
        if len(self._requests[user_id]) >= self.max_per_minute:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Rate limit exceeded: {self.max_per_minute} requests per minute",
            )

        # Record this request
        self._requests[user_id].append(now)
        return GuardrailResult(action=GuardrailAction.ALLOW)


class QuerySizeGuardrail(Guardrail):
    """Prevent unbounded queries."""
    name = "query_size"
    MAX_LIMIT = 500

    LARGE_TABLES = [
        "orders", "shipments", "tracking", "audit_log",
        "messages", "analytics", "logs",
    ]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        params = context.get("params", {})
        if not isinstance(params, dict):
            return GuardrailResult(action=GuardrailAction.ALLOW)

        # Check if limit exceeds MAX_LIMIT
        limit = params.get("limit")
        if limit is not None and int(limit) > self.MAX_LIMIT:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Query limit {limit} exceeds maximum allowed ({self.MAX_LIMIT})",
            )

        # Check if query targets a large table with no limit
        table = params.get("table", "")
        if table in self.LARGE_TABLES and limit is None:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Query on large table '{table}' must specify a limit (max {self.MAX_LIMIT})",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class PromptInjectionGuardrail(Guardrail):
    """Detect prompt injection attempts in user input."""
    name = "prompt_injection"

    INJECTION_PATTERNS = [
        re.compile(r'ignore\s+(all\s+)?(previous|above|prior)\s+instructions', re.I),
        re.compile(r'(you\s+are\s+now|act\s+as|pretend\s+to\s+be)', re.I),
        re.compile(r'system\s*:', re.I),
        re.compile(r'(show|output|reveal)\s+(your\s+)?(prompt|instructions|system)', re.I),
        re.compile(r'(sudo|admin\s+mode|unrestricted|override)', re.I),
    ]

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        user_message = context.get("user_message", "")
        if not user_message:
            return GuardrailResult(action=GuardrailAction.ALLOW)

        for pattern in self.INJECTION_PATTERNS:
            if pattern.search(user_message):
                return GuardrailResult(
                    action=GuardrailAction.BLOCK,
                    reason=f"Potential prompt injection detected: matched pattern '{pattern.pattern}'",
                )

        return GuardrailResult(action=GuardrailAction.ALLOW)


class ConfidenceEscalationGuardrail(Guardrail):
    """Escalate to human when confidence is too low."""
    name = "confidence_escalation"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        confidence = context.get("confidence", 1.0)
        if confidence < 0.3:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason="Low confidence. Escalating to human support.",
            )
        return GuardrailResult(action=GuardrailAction.ALLOW)


class CostBudgetGuardrail(Guardrail):
    """Check if query would exceed cost budget."""
    name = "cost_budget"

    async def check(self, context: Dict[str, Any]) -> GuardrailResult:
        estimated_cost = context.get("estimated_cost", 0.0)
        daily_budget = context.get("daily_budget", 100.0)
        budget_used = context.get("budget_used", 0.0)

        remaining = daily_budget - budget_used

        if remaining <= 0 or estimated_cost > remaining:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                reason=f"Cost budget exceeded: ${budget_used:.2f} used of ${daily_budget:.2f} daily budget, estimated cost ${estimated_cost:.2f}",
            )

        usage_pct = (budget_used / daily_budget) * 100 if daily_budget > 0 else 0
        if usage_pct > 80:
            return GuardrailResult(
                action=GuardrailAction.WARN,
                reason=f"Budget warning: {usage_pct:.0f}% of daily budget used (${budget_used:.2f}/${daily_budget:.2f})",
            )

        return GuardrailResult(action=GuardrailAction.ALLOW)

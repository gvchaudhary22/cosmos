from app.guardrails.base import GuardrailPipeline
from app.guardrails.rules import (
    CostBudgetGuardrail,
    ConfidenceEscalationGuardrail,
    PIIProtectionGuardrail,
    PromptInjectionGuardrail,
    QuerySizeGuardrail,
    RateLimitGuardrail,
    RoleAccessGuardrail,
    TenantIsolationGuardrail,
)
from app.guardrails.mars_safety import MarsSafetyGuardrail
from app.guardrails.kb_guardrails import (
    ApprovalModeGuardrail,
    BlastRadiusGuardrail,
    KBPIIFieldGuardrail,
    RoutingGuardrail,
)
from app.guardrails.advanced_guards import (
    CrossTenantLeakGuard,
    SessionPoisoningGuard,
    HinglishInjectionGuard,
    ToolScopeLimiterGuard,
    InternalLeakageGuard,
    HallucinationGuard,
    LegalCommitmentGuard,
)
from app.guardrails.compliance_guards import (
    FinancialDataMaskingGuard,
    CompetitorMentionGuard,
    RepeatQueryAbuseGuard,
    SensitiveActionConfirmationGuard,
    LanguageConsistencyGuard,
)


def create_guardrail_pipeline() -> GuardrailPipeline:
    pipeline = GuardrailPipeline()

    # ===================================================================
    # PRE-EXECUTION (before tools run) — 13 guards
    # ===================================================================

    # Layer 1: Injection detection (block malicious input)
    pipeline.add_pre(MarsSafetyGuardrail())        # MARS risk-scored injection (6 categories, block >= 7)
    pipeline.add_pre(PromptInjectionGuardrail())     # Basic injection patterns
    pipeline.add_pre(HinglishInjectionGuard())       # Hindi/Hinglish injection bypass
    pipeline.add_pre(SessionPoisoningGuard())        # Multi-turn context poisoning

    # Layer 2: Access control (block unauthorized access)
    pipeline.add_pre(RoleAccessGuardrail())          # Role-based access check
    pipeline.add_pre(TenantIsolationGuardrail())     # Company_id header isolation
    pipeline.add_pre(CrossTenantLeakGuard())         # Cross-company query detection in message text

    # Layer 3: Rate limiting & abuse prevention
    pipeline.add_pre(RateLimitGuardrail(max_per_minute=60))
    pipeline.add_pre(RepeatQueryAbuseGuard())        # Same query > 5x in 5 min
    pipeline.add_pre(QuerySizeGuardrail())           # Max message length

    # Layer 4: Cost & safety boundaries
    pipeline.add_pre(CostBudgetGuardrail())          # LLM token cost cap
    pipeline.add_pre(BlastRadiusGuardrail())         # KB-defined tool risk levels
    pipeline.add_pre(ApprovalModeGuardrail())        # KB-defined approval requirements

    # Layer 5: Action safety
    pipeline.add_pre(ToolScopeLimiterGuard())        # Block write tools on read queries
    pipeline.add_pre(SensitiveActionConfirmationGuard())  # Confirm mass/irreversible actions

    # ===================================================================
    # POST-EXECUTION (after tools run, before response) — 10 guards
    # ===================================================================

    # Layer 1: PII & data protection
    pipeline.add_post(PIIProtectionGuardrail())      # Mask phone/email/aadhaar
    pipeline.add_post(KBPIIFieldGuardrail())         # API-specific PII field masking
    pipeline.add_post(FinancialDataMaskingGuard())   # Mask financial values unless asked

    # Layer 2: Information leakage prevention
    pipeline.add_post(InternalLeakageGuard())        # Mask DB tables, internal APIs, hostnames
    pipeline.add_post(CompetitorMentionGuard())      # Block competitor recommendations

    # Layer 3: Response quality & safety
    pipeline.add_post(HallucinationGuard())          # Detect fabricated IDs
    pipeline.add_post(LegalCommitmentGuard())        # Replace unauthorized promises

    # Layer 4: Routing & escalation
    pipeline.add_post(RoutingGuardrail())            # Domain mismatch warnings
    pipeline.add_post(ConfidenceEscalationGuard())   # Escalate low confidence
    pipeline.add_post(LanguageConsistencyGuard())    # Input/output language match

    return pipeline


# Alias for backward compatibility
ConfidenceEscalationGuard = ConfidenceEscalationGuardrail

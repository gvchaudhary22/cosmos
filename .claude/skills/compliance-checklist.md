# Skill: Compliance Checklist
> Human-review checklists for common compliance readiness conversations

## IMPORTANT DISCLAIMER
This checklist is a starting point for human legal review. It is not a legal opinion and does not constitute compliance certification.

## ACTIVATION
- When a task asks for compliance readiness checklists, audit prep, or review framing.
- When launch or security work needs GDPR, SOC2, HIPAA-lite, or PCI-lite preparation notes for human review.
- When a team needs a gap list before engaging legal, compliance, or security reviewers.

## CORE PRINCIPLES
1. **Human Review Required**: Output must always position itself as preparatory material for qualified human review.
2. **Checklist, Not Certification**: Provide scoped questions, artifacts, and controls to verify, never approval claims.
3. **Evidence-Oriented**: Every checklist item should point to an observable control, document, or operational practice.
4. **Scope Awareness**: Call out where applicability depends on product shape, data handling, region, or deployment model.
5. **Risk Visibility**: Surface missing controls, unknowns, and escalation points instead of smoothing them over.

## PATTERNS

### GDPR Readiness Checklist
- Identify personal data categories collected, processed, and retained
- Confirm lawful basis, consent flow, or contractual necessity assumptions
- Check data subject request handling: access, deletion, correction, export
- Review processor/subprocessor inventory and cross-border transfer posture
- Verify retention policy and breach notification process

### SOC2 Readiness Checklist
- Confirm access control, least privilege, and joiner/mover/leaver processes
- Review logging, monitoring, and incident response evidence
- Check change management, deployment approvals, and rollback readiness
- Verify backup, recovery, and availability control documentation
- Ensure policies exist for security, vendor management, and risk review

### HIPAA-Lite Checklist
- Identify whether PHI may be stored, transmitted, or displayed
- Review access restrictions, audit logging, and encryption posture
- Confirm Business Associate Agreement needs are escalated for human review
- Check incident handling and minimum-necessary data access assumptions
- Verify retention and deletion expectations for sensitive records

### PCI-Lite Checklist
- Confirm whether cardholder data is handled directly or via a processor
- Review tokenization, hosted payment flow, and data storage boundaries
- Check access controls, logging, and network segmentation assumptions
- Verify vulnerability scanning and dependency patching expectations
- Escalate any direct payment-data handling to human security/compliance review

## CHECKLISTS

### Checklist Readiness
- [ ] Applicable framework identified
- [ ] In-scope systems and data flows listed
- [ ] Required evidence artifacts called out
- [ ] Unknowns and escalation points documented
- [ ] Human review disclaimer preserved

## ANTI-PATTERNS
- **Legal Theater**: Presenting the checklist as a certification or legal sign-off.
- **Scope Collapse**: Applying one framework blindly without checking whether it actually fits.
- **Missing Unknowns**: Hiding unanswered questions that need human review.
- **Control Fiction**: Claiming controls exist without evidence or implementation details.
- **Compliance by Template**: Reusing checklist answers without validating the real system.

## VERIFICATION WORKFLOW
1. Confirm the checklist stays in preparatory scope and does not assert legal or compliance certification.
2. Verify the output includes the human-review disclaimer and maps items to real controls or artifacts.
3. Ensure unknowns, gaps, and required human follow-ups are explicit in the checklist.

# Skill: Nexus Orchestration
> Coordination and intelligence across multiple repositories in the MARS platform workspace

## ACTIVATION
Activated when the task involves multi-repo logic across LIME (React), MARS (Go), and COSMOS (Python), or when `cosmos.integration.json` is referenced.

## CORE PRINCIPLES
1. **Workspace Sovereignty**: The Nexus root is the single source of truth for repository relationships.
2. **Context Federation**: Summaries from sub-repos must be aggregated, not the full source code (to avoid token bloat).
3. **Cross-Repo Traceability**: Every decision involving one repo that affects another must be documented in `NEXUS-STATE.md`.
4. **Least Privilege**: Subagents assigned to COSMOS should not have write access to MARS or LIME unless explicitly escalated.

## WORKFLOWS

### Cross-Repo Compatibility Check
1. **Identify**: Map the source of truth (e.g., MARS proto definitions) and the consumer (e.g., COSMOS gRPC servicers).
2. **Phase 1 (Extract)**:
   - Spawn Researcher in Repo A to extract capabilities (APIs, schemas, proto contracts).
   - Spawn Researcher in Repo B to extract requirements (dependencies, expected interfaces).
3. **Phase 2 (Analyze)**: The Architect Meta-Agent compares extracted data for protocol or version mismatches.
4. **Phase 3 (Report)**: Update `NEXUS-STATE.md` with the "Compatibility Score" and remediations.

## MARS Platform Repos
| Repo | Role | Port |
|------|------|------|
| LIME | React frontend | 3003 |
| MARS | Go backend (auth, routing) | 8080 |
| COSMOS | Python AI brain | 10001 |

## CHECKLISTS
- [ ] Does `cosmos.integration.json` correctly describe this repo's role in the platform?
- [ ] Are cross-repo API contracts (gRPC, REST) verified before deployment?
- [ ] Is the "Architect" agent loaded for cross-repo reasoning?
- [ ] Are findings documented in `NEXUS-STATE.md`?

## ANTI-PATTERNS
- **Context Bleeding**: Loading the full source of MARS into the context of COSMOS.
- **Ambiguous Dependencies**: Assuming COSMOS uses the latest MARS API without checking the proto definitions.
- **Silent Failures**: Changing a shared interface without running a cross-repo compatibility audit.

## VERIFICATION WORKFLOW
1. Verify `cosmos.integration.json` describes the correct service relationships.
2. Confirm `NEXUS-STATE.md` is updated after any cross-repo decision.
3. Ensure no sub-repo context is bleeding beyond the summary level.

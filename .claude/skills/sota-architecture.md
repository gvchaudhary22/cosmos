# Skill: SOTA Architecture (Kernel vs. Userland)
> Principles for keeping the COSMOS core lean while enabling infinite downstream extension

## ACTIVATION
Activated when the Architect or Forge detects a need for a new agent/skill, or when evaluating whether something belongs in core vs. project-local extensions.

## CORE PRINCIPLES
1. **The Kernel Immutable**: Core agents (Architect, Engineer, Forge, Strategist, etc.) are stable Pillars. Modifications to these are Major Releases.
2. **Userland Freedom**: Project-specific specialists live in `.claude/` extensions. They are the "Antibodies" for repo-specific problems.
3. **Abstraction-as-Propagation**: A new capability discovered in a project should be abstracted into a **Skill** first, not a new **Agent**.
4. **Nexus Awareness**: Before building or promoting, check `rocketmind.registry.json` to see if a similar skill already exists.

## WORKFLOWS

### The "SOTA Promotion" Filter
When considering moving a project-local agent/skill to the global core:
1. **Utility Test**: Is this useful to at least 3 unrelated contexts?
2. **Cohesion Test**: Does it overlap with existing core skills? If yes, **ENHANCE** the existing skill instead of adding a new one.
3. **Complexity Test**: Can this be solved with a simple rule update in `CLAUDE.md`?

## CHECKLISTS
- [ ] Is the agent tagged with the correct domain scope?
- [ ] Has the logic been abstracted into a Skill where possible?
- [ ] Does the `CHANGELOG.md` reflect why the Core was touched?
- [ ] If local, is it registered in `rocketmind.registry.json`?

## ANTI-PATTERNS
- **Agent Explosion**: Having 15 specialized agents when one skill would suffice.
- **Hardcoded Context**: Writing project-specific entity IDs or Shiprocket-specific details into a global agent/skill.
- **Framework Bloat**: Adding a core agent for every new Python library discovered.

## PATTERNS
1. **Core Pillar**: Every major domain must have one core agent.
2. **Pattern Matching**: Always look for a skills-based solution before an agent-based one.
3. **Registry First**: Every new asset must be registered in `rocketmind.registry.json`.

## VERIFICATION WORKFLOW
1. Run registry validation to ensure integrity.
2. Verify the "Promotion Filter" was followed.
3. Ensure the new asset is documented in `README.md`.

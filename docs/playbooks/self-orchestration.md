# Building COSMOS with COSMOS

> How COSMOS uses its own workflows, agents, and state surfaces to evolve the framework safely.

COSMOS is not only an AI brain for Shiprocket ICRM. The same orchestration model used to answer operator questions also governs how COSMOS evolves itself. That means new contributors should expect framework work to begin at a `/cosmos:*` command boundary, move through issue-backed branches, and leave behind durable state, review evidence, and updated docs rather than ad-hoc task transcripts.

## 1. The Self-Improvement Cascade

COSMOS improves itself in waves. Each wave strengthens the next one.

- Wave `N` adds or sharpens agents, skills, workflows, and enforcement primitives.
- Wave `N+1` then uses those improvements to execute the next slice of work with better routing, clearer review, and less manual cleanup.
- This creates a compounding loop: safer workflows produce better framework changes, which then make future framework work safer again.

In practice, the cascade looks like this:

1. Add or harden a control-plane primitive.
2. Update the registry, runtime docs, and verification surfaces.
3. Use the new primitive on the next issue instead of treating it as hypothetical.
4. Keep only the pieces that survive real use in release work.

## 2. Session Protocol

Use the same startup protocol every time so state stays trustworthy across threads, agents, and time gaps.

1. Start from `/cosmos:resume`.
2. Reload the current working context from `STATE.md`, checkpoint snapshots, and the active branch.
3. Confirm the active issue, branch, and PR before doing new work.
4. Continue through the appropriate workflow boundary:
   - `/cosmos:quick` for a focused issue slice
   - `/cosmos:plan` for multi-step design or architecture work
   - `/cosmos:build [N]` for wave execution
5. End by updating state surfaces, review evidence, and PR metadata in the same pass.

When the session was compacted or resumed in a fresh runtime, the goal is not to reconstruct context from memory. The goal is to recover it from durable evidence in `STATE.md` and `pre-compact-snapshot.md`.

## 3. TDD for Agent Files

COSMOS applies TDD to agent and skill definitions, not only to code.

The recommended pattern is:

1. Add or tighten eval assertions first.
2. Add dataset prompts that prove the routing or contract change matters.
3. Run the failing eval or contract test (`/cosmos:eval`).
4. Create or update `.claude/agents/*.md`, `.claude/skills/*.md`, and `rocketmind.registry.json`.
5. Rerun validation.

This keeps agent work grounded in executable expectations instead of prose-only intent. A new agent is not considered real just because an `.md` file exists. It becomes real when eval, registry, docs, and routing all agree.

## 4. The Wave 1.5 Gate

Newly created agents do not immediately become trusted framework primitives. Wave 1.5 is the productization gate.

At this gate, each new agent must:

- Clarify what it owns and what it does not
- Update downstream workflows that should use it
- Add durable docs or issue support artifacts
- Prove the contract in eval or runtime enforcement

This is where raw agent creation turns into release-grade behavior. Wave 1 can add the first contract. Wave 1.5 proves the contract belongs in the framework.

## 5. The Graduation Test

An agent is production-ready when it has been used at least once in the same release that created it.

That graduation test matters because:

- it exposes routing overlap that static docs miss
- it forces generated docs and workflow contracts to stay truthful
- it proves the agent can survive the real issue → branch → review → PR loop

If an agent exists in the registry but never gets used in the release that introduced it, treat it as provisional. Graduation requires real work, not just a definition file.

## 6. Self-Hosting Principle

When working inside the COSMOS repository itself, COSMOS must evolve itself through COSMOS workflows first. Default to `/cosmos:quick`, `/cosmos:plan`, `/cosmos:build`, `/cosmos:review`, and `/cosmos:ship` instead of ad-hoc execution whenever the task changes framework behavior, docs, hooks, agents, skills, workflows, or runtime contracts.

For every change to COSMOS internals:
- KB changes → `/cosmos:train` to verify embedding quality
- Agent/skill changes → `/cosmos:eval` to verify recall impact
- API changes → `pytest tests/` must pass before ship
- Hook changes → manually verify hooks fire correctly in a test commit

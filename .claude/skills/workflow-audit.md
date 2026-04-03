# Skill: Workflow Audit
> Principles and checklists for reviewing CI/CD pipeline configuration and release workflows

## ACTIVATION
Load this skill whenever reviewing or authoring `.github/workflows/` files, release pipelines, or CI/CD configuration. Auto-loaded by `devops` agent on any PR touching workflow files.

## CORE PRINCIPLES
1. **Workflows are production infrastructure** — treat them with the same rigour as application code. Step ordering bugs cause real incidents.
2. **Releases are irreversible** — once a package is published or a tag is pushed, they cannot be fully undone. Order operations defensively.
3. **Every step must be idempotent or skippable** — pipelines fail and re-run. Design for it.
4. **Triggers must be minimal and non-overlapping** — duplicate triggers cause duplicate CI runs and race conditions.

## PATTERNS

### Release Step Ordering Contract
The correct order for any release job is:
```
1. Validate (gates)        — fail fast before any side effects
2. Push tag                — point of no return; if this fails nothing is published
3. Check if already published — idempotency guard before any publish
4. Publish package         — only after tag is confirmed
5. Create GitHub Release   — only after package is confirmed
```

### Trigger Hygiene
```yaml
# Wrong — fires twice on PRs (push + pull_request)
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

# Correct — pull_request covers all PR activity
on:
  pull_request:
    branches: [main]
```

### Idempotency Guards
Every destructive or external operation needs a check-before-act:
```yaml
- name: Check if tag exists
  id: tag_check
  run: |
    if git rev-parse "refs/tags/$TAG" --verify > /dev/null 2>&1; then
      echo "exists=true" >> "$GITHUB_OUTPUT"
    else
      echo "exists=false" >> "$GITHUB_OUTPUT"
    fi
```

## CHECKLISTS

### Before merging any release workflow change
- [ ] Release steps are in order: validate → tag → publish check → publish → GitHub Release
- [ ] Tag push uses correct token if branch protection is active
- [ ] Publish step has idempotency guard (skip if version already exists)
- [ ] Tag step has idempotency guard (skip if tag already exists)
- [ ] No duplicate triggers (push + pull_request on same branch)

### Before merging any CI workflow change
- [ ] No redundant triggers
- [ ] New jobs have `needs:` declared if they depend on prior gates
- [ ] No new jobs duplicate work already done by existing jobs
- [ ] Artifact uploads use `if: always()` so failures still produce reports

## ANTI-PATTERNS
| Anti-pattern | Risk | Fix |
|---|---|---|
| Publish before tag push | Package live before release announced | Push tag first |
| `push` + `pull_request` triggers on same branch | Every PR runs CI twice | Use `pull_request` only |
| No idempotency guard on publish | Re-run crashes with 409 Conflict | Check version exists before publishing |

## VERIFICATION WORKFLOW
1. Trace the release job manually — walk through every step and ask: "if this step fails, what is the state of the world? Is it safe to re-run?"
2. Check triggers — for each workflow, verify no two `on:` events can fire simultaneously on the same commit.
3. Verify bypass actors — for any tag/branch ruleset, confirm the CI identity can push before assuming it will succeed.

## Summary
- <!-- concise summary bullet -->

## Issues
- Closes #<!-- issue -->
- Relates to #<!-- optional -->

## Ship Decision
- Review: `/cosmos:review`
- Head SHA: `<!-- replace with current branch head sha; update on every follow-up commit -->`
- Merge when checks are green

## Test plan
- `<!-- command -->`

## Merge notes
- <!-- optional notes / relevant exceptions -->

## Docs update
- Status: `<!-- UPDATED or EXEMPT -->`
- Notes: <!-- list updated docs or explain the exemption -->

> Keep the exact `- Status:` / `- Notes:` bullet format. CI parses these lines literally.

---

## COSMOS Self-Review

> COSMOS reviews Shiprocket's code. It must review its own.
> Run the relevant agent before raising this PR. Record the verdict below.
> If you push follow-up commits after opening the PR, refresh this body before requesting review again.
> CI requires this section and the `## Test plan` to contain real evidence, not placeholders.

### Agent Review Verdict

**Command run**: <!-- e.g. /cosmos:review, /cosmos:audit, /cosmos:plan -->

**Agent(s) dispatched**: <!-- e.g. reviewer, architect, security-engineer -->

**Ship decision**: <!-- APPROVED / APPROVED WITH CONDITIONS / BLOCKED -->

**Findings addressed** (paste critical/high findings and how you resolved them, or "none"):
```
(findings here)
```

**Residual risks** (use one label per item: `Tracked by #...`, `Waived: ...`, or `Operational: ...`, or `none`):
```
(residual risks here)
```

> Use plain triple backticks exactly as shown above. Do not use fenced info strings.

---

## Checklist

### Branch
- [ ] This PR is from a feature branch, NOT a direct push to `develop` or `main`
- [ ] Branch name follows convention: `<type>/<slug>` e.g. `feat/42-wave-executor` or `fix/56-timeout`
- [ ] If this PR changed after opening, the `Summary`, `Issues`, `Ship Decision`, `Test plan`, and `Merge notes` sections were refreshed before re-review
- [ ] If review left residual risks, their disposition is recorded: linked issue, new hardening issue, or explicit waiver

### Code
- [ ] Tests added or updated for changed behaviour
- [ ] `python -m pytest tests/ -x -q` passes
- [ ] `ruff check app/` passes
- [ ] `mypy app/ --ignore-missing-imports` passes
- [ ] No secrets in staged files
- [ ] No hardcoded model IDs — only semantic aliases from `cosmos.config.json → models.routing`
- [ ] No hardcoded local paths (`/Users/...`)

### Architecture
- [ ] Does not duplicate content already in `rocketmind.registry.json` (registry is SSOT)
- [ ] New agents registered in both `rocketmind.registry.json` AND updated in `CLAUDE.md`
- [ ] New skills added to the skills table in `CLAUDE.md`
- [ ] Kernel/userland boundary respected — no vertical domain content in global skills/agents

### Docs
- [ ] `CHANGELOG.md` updated
- [ ] `README.md` updated if behaviour or interface changed
- [ ] `## Docs update` reflects either the updated contract docs or the explicit exemption

### KB / AI (for changes touching app/ or knowledge_base/)
- [ ] `/cosmos:eval` run — recall@5 score maintained ≥ 0.85
- [ ] No hallucination introduced (HallucinationGuard checks pass)
- [ ] Confidence gate thresholds unchanged unless explicitly justified

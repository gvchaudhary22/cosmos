# SKILL: Git Worktree — Parallel Development (COSMOS)
> Run multiple COSMOS tasks simultaneously without branch conflicts or context collisions.

## ACTIVATION
- When two parallel development streams touch the same COSMOS repo.
- Running the KB pipeline (re-embedding 44k files) while developing retrieval improvements.
- Reviewing a PR while continuing wave execution development.
- Parallel wave: Engineer A improving graph retrieval while Engineer B adds P6 helpdesk contracts.

## CORE PRINCIPLES
1. **Task Isolation**: Each parallel task gets its own directory and branch.
2. **Zero-Stash Workflow**: Never stash. Switch directories to change context.
3. **Shared Git Object Store**: All worktrees share `.git` history — no duplication.
4. **Cleanliness**: Remove worktrees immediately after merge.

## PATTERNS

### Worktree-Per-Task
```bash
# Setup: create isolated worktree for a parallel task
git worktree add .worktrees/helpdesk-p6-contracts -b feat/42-helpdesk-p6-contracts
git worktree add .worktrees/wave-tuning -b feat/45-wave-rrf-weight-tuning

# Work in isolation
cd .worktrees/helpdesk-p6-contracts
# ... add KB files, run tests ...
cd .worktrees/wave-tuning
# ... tune RRF weights, run eval seeds ...

# Each has independent:
# - working tree (no checkout conflicts)
# - branch (independent git history)
# - local changes (no stash collisions)
```

### COSMOS-Specific: KB Pipeline + Dev in Parallel
```bash
# Worktree 1: long-running KB re-embedding (don't block dev work)
git worktree add .worktrees/kb-reindex -b chore/kb-reindex-helpdesk
cd .worktrees/kb-reindex
python -m app.services.kb_ingestor --repo helpdesk --force-reembed
# This runs for hours — doesn't block main worktree

# Main worktree: continue feature development
cd /path/to/cosmos
# ... develop retrieval improvements normally ...
```

### Cleanup After Merge
```bash
# After PR is merged
git worktree remove .worktrees/helpdesk-p6-contracts
git branch -d feat/42-helpdesk-p6-contracts

# Prune stale worktree metadata
git worktree prune

# List active worktrees
git worktree list
```

### .gitignore Entry (required)
```
# Worktrees — never commit
.worktrees/
```

## CHECKLISTS

### Worktree Health
- [ ] `.worktrees/` is in `.gitignore`
- [ ] No more than 4 active worktrees (performance)
- [ ] Each worktree has a unique branch
- [ ] `git worktree prune` run after every merge wave
- [ ] Worktrees used for long-running KB pipelines (never block main branch)

## ANTI-PATTERNS
- **Same Branch Two Worktrees**: Checking out `develop` in two worktrees — Git blocks this.
- **Stale Worktrees**: Leaving `.worktrees/kb-reindex` after the task is done — index bloat.
- **Nesting Worktrees**: Creating a worktree inside another worktree directory.
- **Committing .worktrees/**: Accidentally staging the worktree directory (prevented by .gitignore).

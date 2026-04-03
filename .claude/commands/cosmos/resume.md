---
description: "COSMOS /cosmos:resume — reload STATE.md and continue from where we left off after compaction or new session"
allowed-tools: all
---
Read CLAUDE.md to load COSMOS context.
Read .claude/commands/cosmos.md for this command's exact process specification.
If STATE.md exists at .claude/session-state/STATE.md, read it. Also read .claude/session-state/pre-compact-snapshot.md if it exists.
Run: git log --oneline -5 to confirm what was last committed.
Execute: /cosmos:resume $ARGUMENTS — follow the exact process defined, no shortcuts.

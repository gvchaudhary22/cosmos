# Runtime Adapters

COSMOS's control plane (registry, config, agents, skills, hooks) is runtime-agnostic. The same repository works across Claude Code (native), Codex (stable), and other compatible runtimes.

---

## Claude Code (Native)

**Status:** Native support — no adapter required.

Claude Code reads `CLAUDE.md` directly as the session orchestrator. All `/cosmos:*` slash commands are available natively.

### Setup
```bash
# Claude Code automatically picks up CLAUDE.md from the repo root
# Open a Claude Code session in the cosmos/ directory
# Type /cosmos:help to verify
```

### What Claude Code uses
- `CLAUDE.md` — session orchestrator (loaded automatically)
- `.claude/agents/` — agent definitions (loaded on demand)
- `.claude/skills/` — skill files (lazy-loaded)
- `.claude/hooks/` — lifecycle hooks (auto-executed)
- `.claude/commands/cosmos.md` — `/cosmos:*` slash commands
- `rocketmind.registry.json` — agent routing table
- `cosmos.config.json` — model routing + configuration

### Slash commands
All `/cosmos:*` commands defined in `.claude/commands/cosmos.md` are available in Claude Code sessions.

```
/cosmos:new     /cosmos:plan    /cosmos:build   /cosmos:verify
/cosmos:ship    /cosmos:next    /cosmos:quick   /cosmos:riper
/cosmos:forge   /cosmos:review  /cosmos:audit   /cosmos:debug
/cosmos:resume  /cosmos:progress /cosmos:train  /cosmos:eval
/cosmos:help
```

---

## Codex (Stable)

**Status:** Supported via `INSTRUCTIONS.md` adapter.

Codex does not read `CLAUDE.md` natively. The `templates/rocketmind.base.md` template is compiled into `INSTRUCTIONS.md` for Codex compatibility.

### Generate INSTRUCTIONS.md
```bash
node bin/cosmos.js generate --target codex
# or
npm run generate -- --target codex
```

### What Codex uses
- `INSTRUCTIONS.md` — compiled session orchestrator
- `.claude/agents/` — same agent files (compatible markdown)
- `rocketmind.registry.json` — same registry
- No hooks (Codex does not support lifecycle hooks)
- No slash commands (use explicit workflow prompts instead)

### Workflow prompts for Codex
Since Codex lacks `/cosmos:*` commands, use explicit prompts:
```
# Instead of /cosmos:plan
"Read STATE.md and ROADMAP.md. Activate the strategist agent and produce PHASE-N-PLAN.md."

# Instead of /cosmos:build
"Read PHASE-N-PLAN.md and ARCH.md. Activate the engineer agent and implement in autonomous mode."
```

### INSTRUCTIONS.md update cadence
Re-generate `INSTRUCTIONS.md` whenever `CLAUDE.md` or `templates/rocketmind.base.md` changes:
```bash
npm run generate
git add INSTRUCTIONS.md && git commit -m "chore: sync INSTRUCTIONS.md from base template"
```

---

## Other Runtimes (Experimental)

Any runtime that can:
1. Read markdown files from the repository
2. Execute bash scripts in response to lifecycle events
3. Follow structured role/trigger instructions

...can use COSMOS's control plane with minimal adaptation.

### Minimum viable adapter
1. Load `CLAUDE.md` (or `INSTRUCTIONS.md`) as system context
2. Load relevant agent file when routing to a specialist
3. Load skill files as needed
4. Respect hook outputs (blocking vs non-blocking)

---

## Templates

The `templates/` directory contains source-of-truth templates for generated files:

| Template | Generates | Command |
|----------|-----------|---------|
| `templates/rocketmind.base.md` | `CLAUDE.md`, `INSTRUCTIONS.md` | `npm run generate` |

`CLAUDE.md` is the canonical version. `INSTRUCTIONS.md` is always derived from the template — never edit it manually.

---

## Multi-Runtime Behavior Matrix

| Feature | Claude Code | Codex | Other |
|---------|-------------|-------|-------|
| Slash commands (`/cosmos:*`) | ✓ Native | ✗ | ✗ |
| Agent routing | ✓ Automatic | Manual prompt | Manual |
| Lazy skill loading | ✓ | ✓ | Varies |
| Lifecycle hooks | ✓ | ✗ | Varies |
| STATE.md persistence | ✓ | ✓ | ✓ |
| Wave execution | ✓ Python engine | ✓ Python engine | ✓ Python engine |
| Model routing | ✓ Via config | ✓ Via config | ✓ Via config |

Note: Wave execution, retrieval, and model routing happen in the Python FastAPI engine (`app/`), not in the AI runtime. These work identically regardless of which AI assistant is used.

#!/usr/bin/env bash
#
# Cosmos commit-msg hook — BLOCKING
# Strips any Co-Authored-By: Claude / noreply@anthropic lines from commit messages.
# Prevents AI tools from appearing as GitHub contributors.
#
# Install: ln -sf "$(pwd)/.claude/hooks/commit-msg.sh" "$(pwd)/.git/hooks/commit-msg"
# Or run:  bash scripts/setup.sh  (installs automatically)
#

set -euo pipefail

COMMIT_MSG_FILE="${1}"

if [[ -z "$COMMIT_MSG_FILE" ]] || [[ ! -f "$COMMIT_MSG_FILE" ]]; then
    echo "[commit-msg] No commit message file provided — skipping." >&2
    exit 0
fi

# Patterns to strip (case-insensitive):
#   Co-Authored-By: Claude ...
#   Co-Authored-By: ... <noreply@anthropic.com>
#   🤖 Generated with [Claude Code](...)
STRIPPED=$(grep -viE \
    'co-authored-by:.*claude|co-authored-by:.*noreply@anthropic|generated with \[?claude' \
    "$COMMIT_MSG_FILE" || true)

# Write cleaned message back (preserve trailing newline)
printf '%s\n' "$STRIPPED" > "$COMMIT_MSG_FILE"

# Verify no attribution slipped through
if grep -qiE 'co-authored-by:.*claude|noreply@anthropic' "$COMMIT_MSG_FILE" 2>/dev/null; then
    echo ""
    echo "[BLOCKED] Commit message contains Claude/Anthropic attribution." >&2
    echo "          Remove Co-Authored-By: Claude lines and retry." >&2
    echo ""
    exit 1
fi

exit 0

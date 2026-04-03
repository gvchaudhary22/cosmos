#!/usr/bin/env bash
#
# Cosmos Stop Hook — Session Logger
# Non-blocking. Logs session end and warns about uncommitted changes.
# Always exits 0.
#

if [[ "${COSMOS_HOOKS_DISABLED:-0}" == "1" ]]; then
    exit 0
fi

COSMOS_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STATE_DIR="$COSMOS_ROOT/.claude/session-state"
SESSION_LOG="$STATE_DIR/sessions.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$STATE_DIR"

echo "[$TIMESTAMP] session-end" >> "$SESSION_LOG" 2>/dev/null || true

# Keep log to 200 lines
if [[ -f "$SESSION_LOG" ]]; then
    tail -200 "$SESSION_LOG" > "$SESSION_LOG.tmp" 2>/dev/null && mv "$SESSION_LOG.tmp" "$SESSION_LOG" 2>/dev/null || true
fi

# Warn about uncommitted Python changes
CHANGED=$(git -C "$COSMOS_ROOT" diff --name-only 2>/dev/null | grep -cE '\.py$' || echo "0")
STAGED=$(git -C "$COSMOS_ROOT" diff --cached --name-only 2>/dev/null | grep -cE '\.py$' || echo "0")

if [[ "$CHANGED" -gt 0 ]] || [[ "$STAGED" -gt 0 ]]; then
    echo "[Cosmos] $((CHANGED + STAGED)) Python file(s) changed. Run pytest before marking done."
fi

exit 0

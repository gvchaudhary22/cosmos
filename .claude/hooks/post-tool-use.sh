#!/usr/bin/env bash
#
# Cosmos Post-Tool-Use Hook — Tool Logger
# Non-blocking. Logs tool calls to rolling session log.
# Always exits 0.
#

if [[ "${COSMOS_HOOKS_DISABLED:-0}" == "1" ]]; then
    exit 0
fi

COSMOS_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STATE_DIR="$COSMOS_ROOT/.claude/session-state"
TOOL_LOG="$STATE_DIR/tool-usage.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TOOL="${TOOL_NAME:-unknown}"
EXIT_CODE="${TOOL_EXIT_CODE:-0}"

mkdir -p "$STATE_DIR"

echo "[$TIMESTAMP] tool=$TOOL exit=$EXIT_CODE" >> "$TOOL_LOG" 2>/dev/null || true

# Rolling 500 entries
if [[ -f "$TOOL_LOG" ]]; then
    tail -500 "$TOOL_LOG" > "$TOOL_LOG.tmp" 2>/dev/null && mv "$TOOL_LOG.tmp" "$TOOL_LOG" 2>/dev/null || true
fi

exit 0

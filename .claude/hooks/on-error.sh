#!/usr/bin/env bash
# COSMOS On-Error Hook — recovery bridge for RIPER execute phase failures

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT_DIR/session-state"

TASK_NAME="${1:-unknown}"
PHASE="${2:-execute}"
ERROR_MSG="${3:-no details}"
SUMMARY_FILE="${4:-}"

# Write last error to state
mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/last_error.json" <<EOF
{
  "task": "$TASK_NAME",
  "phase": "$PHASE",
  "error": "$ERROR_MSG",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "retry_count": 0
}
EOF

echo "⚠️  COSMOS on-error: task='$TASK_NAME' phase='$PHASE'" >&2
echo "   Error: $ERROR_MSG" >&2
echo "   State written to: $STATE_DIR/last_error.json" >&2

# Append to SUMMARY.md if provided
if [[ -n "$SUMMARY_FILE" && -f "$SUMMARY_FILE" ]]; then
  cat >> "$SUMMARY_FILE" <<EOF

## Recovery Trace
- Task: $TASK_NAME
- Phase: $PHASE
- Error: $ERROR_MSG
- Decision: retry (check last_error.json for retry_count; halt at 3)
EOF
fi

exit 0

#!/usr/bin/env bash
# COSMOS Context Sync — refreshes STATE.md snapshot
# Non-blocking: commit/push/session hooks should warn, not fail, if sync is unavailable.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_FILE="$ROOT_DIR/session-state/STATE.md"
SNAPSHOT_FILE="$ROOT_DIR/session-state/pre-compact-snapshot.md"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "⚠️  COSMOS context sync skipped: session-state/STATE.md not found" >&2
  exit 0
fi

# Copy current STATE.md to snapshot
cp "$STATE_FILE" "$SNAPSHOT_FILE" 2>/dev/null && \
  echo "✅ COSMOS context synced from STATE.md → pre-compact-snapshot.md" || \
  echo "⚠️  COSMOS context sync failed" >&2

exit 0

#!/usr/bin/env bash
# COSMOS Post-Commit Hook — refreshes STATE.md snapshot after every successful commit

set -uo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT_DIR" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

"$ROOT_DIR/.claude/hooks/sync-context.sh" || true

exit 0

#!/usr/bin/env bash
#
# Cosmos Pre-Commit Hook — BLOCKING
# Gates: ruff lint + pytest + secret scan
# Set COSMOS_HOOKS_DISABLED=1 to skip.
#

set -euo pipefail

GREEN='\033[1;32m'
RED='\033[1;31m'
YELLOW='\033[1;33m'
RESET='\033[0m'
FAIL_COUNT=0
WARN_COUNT=0

fail() {
    echo -e "${RED}[FAIL]${RESET} $1"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

info() {
    echo -e "${GREEN}[OK]${RESET} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${RESET} $1"
    WARN_COUNT=$((WARN_COUNT + 1))
}

if [[ "${COSMOS_HOOKS_DISABLED:-0}" == "1" ]]; then
    echo "Cosmos hooks disabled (COSMOS_HOOKS_DISABLED=1), skipping."
    exit 0
fi

COSMOS_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

echo "=== Cosmos Pre-Commit Validation ==="
echo ""

# ---------------------------------------------------------------------------
# 1. ruff lint — BLOCKING (fast, catches critical errors)
# ---------------------------------------------------------------------------
echo "--- Lint (ruff) ---"
if command -v ruff &>/dev/null; then
    if (cd "$COSMOS_ROOT" && ruff check app/ --select=E,W,F --ignore=E501,E402 2>&1); then
        info "ruff lint passed"
    else
        fail "ruff lint FAILED — fix lint errors before committing"
    fi
else
    warn "ruff not installed (pip install ruff) — skipping lint"
fi

echo ""

# ---------------------------------------------------------------------------
# 2. pytest — BLOCKING
# ---------------------------------------------------------------------------
echo "--- Test Suite ---"
if command -v pytest &>/dev/null || python -m pytest --version &>/dev/null 2>&1; then
    if [[ -d "$COSMOS_ROOT/tests" ]]; then
        if (cd "$COSMOS_ROOT" && python -m pytest tests/ -x -q --tb=short --ignore=tests/eval 2>&1); then
            info "pytest passed"
        else
            fail "pytest FAILED — fix test failures before committing"
        fi
    else
        warn "tests/ directory not found — skipping pytest"
    fi
else
    warn "pytest not installed (pip install pytest) — skipping tests"
fi

echo ""

# ---------------------------------------------------------------------------
# 3. Secret scan — BLOCKING
# ---------------------------------------------------------------------------
echo "--- Secret Scan ---"
STAGED_FILES=$(git -C "$COSMOS_ROOT" diff --cached --name-only 2>/dev/null || true)
SECRET_PATTERNS='(AKIA[0-9A-Z]{16}|sk-[a-zA-Z0-9]{48}|ghp_[a-zA-Z0-9]{36}|xoxb-[0-9]+-[a-zA-Z0-9]+|[Pp]assword\s*=\s*["\x27][^"'\'']{8,}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----)'
SECRETS_FOUND=0

if [[ -n "$STAGED_FILES" ]]; then
    while IFS= read -r file; do
        if [[ "$file" =~ \.(md|example|txt|rst)$ ]] || [[ "$file" =~ (test_|_test\.py|\.env\.example) ]]; then
            continue
        fi
        FULL_PATH="$COSMOS_ROOT/$file"
        if [[ -f "$FULL_PATH" ]]; then
            MATCHES=$(grep -inE "$SECRET_PATTERNS" "$FULL_PATH" 2>/dev/null || true)
            if [[ -n "$MATCHES" ]]; then
                fail "Possible secret in $file"
                echo "$MATCHES" | head -3 | while IFS= read -r m; do echo "    $m"; done
                SECRETS_FOUND=1
            fi
        fi
    done <<< "$STAGED_FILES"
    if [[ "$SECRETS_FOUND" -eq 0 ]]; then
        info "No secrets detected in staged files"
    fi
else
    info "No staged files to scan"
fi

echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo -e "${RED}=== BLOCKED: $FAIL_COUNT gate(s) failed. Fix errors above. ===${RESET}"
    exit 1
elif [[ "$WARN_COUNT" -gt 0 ]]; then
    echo -e "${YELLOW}=== $WARN_COUNT advisory warning(s) — gates passed. ===${RESET}"
else
    echo -e "${GREEN}=== All checks passed ===${RESET}"
fi

exit 0

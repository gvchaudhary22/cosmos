#!/usr/bin/env bash
#
# Cosmos Pre-Tool-Use Hook — Safety Guard (COSMOS-enhanced)
# Blocks: destructive bash commands, .env writes, --no-verify bypasses,
#         prompt injection patterns, base64 payloads, eval/exec abuse.
# Exits 1 to block, 0 to allow.
#

if [[ "${COSMOS_HOOKS_DISABLED:-0}" == "1" ]]; then
    exit 0
fi

TOOL="${TOOL_NAME:-}"
INPUT="${TOOL_INPUT:-}"

# ---------------------------------------------------------------------------
# BASH TOOL GUARDS
# ---------------------------------------------------------------------------
if [[ "$TOOL" == "Bash" ]]; then

    # Block --no-verify (hook bypass)
    if echo "$INPUT" | grep -qE '\-\-no-verify'; then
        echo "[BLOCKED] --no-verify is not allowed. Fix the underlying hook failure." >&2
        exit 1
    fi

    # Block destructive rm -rf on root paths
    if echo "$INPUT" | grep -qE 'rm\s+-rf\s+(/|\./)\s*$'; then
        echo "[BLOCKED] Destructive rm -rf on root detected." >&2
        exit 1
    fi

    # Block force push to main/master
    if echo "$INPUT" | grep -qE 'git push.*(--force|-f)\s+(origin\s+)?(main|master)'; then
        echo "[BLOCKED] Force push to main/master is not allowed." >&2
        exit 1
    fi

    # Block base64 decode pipelines (common payload delivery vector)
    if echo "$INPUT" | grep -qE '(base64\s+-d|base64\s+--decode|base64\s+-D)'; then
        echo "[BLOCKED] base64 decode in shell command detected — potential payload injection." >&2
        exit 1
    fi

    # Block eval with variable expansion (code injection risk)
    if echo "$INPUT" | grep -qE '\beval\s+["'"'"']?\$'; then
        echo "[BLOCKED] eval with variable expansion detected — potential code injection." >&2
        exit 1
    fi

    # Block curl/wget piped directly to bash/sh (remote code execution pattern)
    if echo "$INPUT" | grep -qE '(curl|wget).*(bash|sh|python|node)\s*$'; then
        echo "[BLOCKED] Remote code execution pattern detected (curl/wget | bash)." >&2
        exit 1
    fi

    # Block deletion of .claude/ hooks or rules (integrity protection)
    if echo "$INPUT" | grep -qE 'rm.*\.claude/(hooks|rules|agents|skills)'; then
        echo "[BLOCKED] Deletion of .claude/ governance files is not allowed." >&2
        exit 1
    fi

fi

# ---------------------------------------------------------------------------
# WRITE / EDIT TOOL GUARDS
# ---------------------------------------------------------------------------
if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
    FILE_PATH="${TOOL_FILE_PATH:-}"

    # Block writing real secrets to .env
    if echo "$FILE_PATH" | grep -qE '\.env$'; then
        if echo "$INPUT" | grep -qiE '(SECRET|PASSWORD|API_KEY|TOKEN|PRIVATE_KEY)\s*=\s*[A-Za-z0-9+/]{8,}'; then
            echo "[BLOCKED] Writing secrets to .env file is not allowed. Use .env.example for templates." >&2
            exit 1
        fi
    fi

    # Block overwriting pre-commit or pre-tool-use hooks with empty/disabled content
    if echo "$FILE_PATH" | grep -qE '\.claude/hooks/(pre-commit|pre-tool-use)\.sh$'; then
        if echo "$INPUT" | grep -qE '^\s*exit 0\s*$'; then
            echo "[BLOCKED] Cannot replace safety hooks with no-op (exit 0 only)." >&2
            exit 1
        fi
    fi

    # Warn on writing to production .env (not block — might be legitimate)
    if echo "$FILE_PATH" | grep -qiE '\.env\.(production|prod)$'; then
        echo "[WARN] Writing to production environment file. Verify no real secrets are included." >&2
    fi

fi

exit 0

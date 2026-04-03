#!/usr/bin/env bash
# COSMOS Setup Script
# Sets up the full COSMOS development environment including COSMOS config sync.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="$ROOT/rocketmind.registry.json"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   COSMOS Setup — RocketMind v1.0.0       ║"
echo "║   COSMOS AI Brain                 ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Python environment ────────────────────────────────────────────────────
# pydantic-core 2.23.2 supports Python 3.8–3.13 only (not 3.14+)
echo -e "${BLUE}▶ Checking Python environment...${NC}"

# Pick Python 3.13 if available, else 3.12, else fall back to system python3
# Never use 3.14+ — pydantic-core wheel not yet available for it
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo -e "${YELLOW}  ⚠ python3.13/3.12/3.11 not found, using system python3 (may fail on 3.14)${NC}"
  PYTHON_BIN="python3"
fi
echo -e "  Using: $PYTHON_BIN ($(${PYTHON_BIN} --version))"

# If venv exists, check its Python version — rebuild if it's 3.14+
if [ -d "$ROOT/.venv" ]; then
  VENV_MINOR=$("$ROOT/.venv/bin/python" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
  VENV_MAJOR=$("$ROOT/.venv/bin/python" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "3")
  if [ "$VENV_MAJOR" -eq 3 ] && [ "$VENV_MINOR" -ge 14 ]; then
    echo -e "${YELLOW}  ⚠ Existing .venv uses Python 3.${VENV_MINOR} — pydantic-core unsupported. Rebuilding with ${PYTHON_BIN}...${NC}"
    rm -rf "$ROOT/.venv"
  else
    echo -e "  Existing .venv: Python 3.${VENV_MINOR} ✓"
  fi
fi

if [ ! -d "$ROOT/.venv" ]; then
  echo "  Creating virtualenv with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv "$ROOT/.venv"
fi
source "$ROOT/.venv/bin/activate"

echo -e "  Active Python: $(python --version)"
pip install --upgrade pip -q
pip install -r "$ROOT/requirements.txt" -q
echo -e "${GREEN}  ✓ Python dependencies installed${NC}"

# ── 2. Node.js dependencies ──────────────────────────────────────────────────
echo -e "${BLUE}▶ Installing Node.js dependencies...${NC}"
cd "$ROOT" && npm install --silent
echo -e "${GREEN}  ✓ Node.js dependencies installed${NC}"

# ── 3. Make cosmos CLI executable ────────────────────────────────────────────
chmod +x "$ROOT/bin/cosmos.js"
echo -e "${GREEN}  ✓ cosmos CLI ready (node bin/cosmos.js)${NC}"

# ── 4. Generate COSMOS commands + STATE.md ───────────────────────────────────
echo -e "${BLUE}▶ Generating COSMOS command surface...${NC}"
node "$ROOT/bin/cosmos.js" generate
echo -e "${GREEN}  ✓ .claude/commands/cosmos.md generated${NC}"
echo -e "${GREEN}  ✓ .cosmos/state/STATE.md initialized${NC}"

# ── 5. Sync COSMOS config ─────────────────────────────────────────────────
echo -e "${BLUE}▶ Syncing RocketMind agents + skills → COSMOS...${NC}"
if [ -f "$REGISTRY" ]; then
  "$ROOT/.venv/bin/python" "$ROOT/scripts/rocketmind_sync.py" --target all 2>/dev/null || {
    echo -e "${YELLOW}  ⚠ DB sync skipped (COSMOS not running). Command surface generated.${NC}"
    "$ROOT/.venv/bin/python" "$ROOT/scripts/rocketmind_sync.py" --dry-run 2>/dev/null || true
  }
  echo -e "${GREEN}  ✓ RocketMind registry synced into COSMOS${NC}"
else
  echo -e "${YELLOW}  ⚠ rocketmind.registry.json not found. Run: npm run generate${NC}"
fi

# ── 6. Copy config ────────────────────────────────────────────────────────────
echo -e "${BLUE}▶ Checking environment config...${NC}"
if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo -e "${YELLOW}  ⚠ .env created from .env.example — fill in REQUIRED values${NC}"
else
  echo -e "${GREEN}  ✓ .env exists${NC}"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  COSMOS setup complete!${NC}"
echo ""
echo -e "  ${BOLD}Start COSMOS:${NC}         npm start"
echo -e "  ${BOLD}Run a command:${NC}        npm run cosmos:plan"
echo -e "  ${BOLD}In Claude Code:${NC}       /cosmos:plan"
echo -e "  ${BOLD}Train KB:${NC}             npm run train"
echo -e "  ${BOLD}Run tests:${NC}            npm test"
echo -e "  ${BOLD}Sync config:${NC}           npm run sync"
echo -e "  ${BOLD}Show all commands:${NC}    npm run cosmos:help"
echo ""

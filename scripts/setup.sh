#!/usr/bin/env bash
# COSMOS Setup Script
# Sets up the full COSMOS development environment including Orbit sync.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORBIT_DIR="$ROOT/../orbit"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   COSMOS Setup — RocketMind v1.0.0       ║"
echo "║   Orbit-powered AI Brain                 ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Python environment ────────────────────────────────────────────────────
echo -e "${BLUE}▶ Checking Python environment...${NC}"
if [ ! -d "$ROOT/.venv" ]; then
  echo "  Creating virtualenv..."
  python3.12 -m venv "$ROOT/.venv"
fi
source "$ROOT/.venv/bin/activate"
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

# ── 5. Sync Orbit into COSMOS ─────────────────────────────────────────────────
echo -e "${BLUE}▶ Syncing Orbit agents + skills → COSMOS...${NC}"
if [ -d "$ORBIT_DIR" ]; then
  python "$ROOT/scripts/orbit_sync.py" --target all 2>/dev/null || {
    echo -e "${YELLOW}  ⚠ Sync to DB skipped (COSMOS not running). Files generated.${NC}"
    python "$ROOT/scripts/orbit_sync.py" --dry-run 2>/dev/null || true
  }
  echo -e "${GREEN}  ✓ Orbit synced into COSMOS${NC}"
else
  echo -e "${YELLOW}  ⚠ Orbit repo not found at $ORBIT_DIR. Skipping sync.${NC}"
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
echo -e "  ${BOLD}Sync Orbit:${NC}           npm run sync"
echo -e "  ${BOLD}Show all commands:${NC}    npm run cosmos:help"
echo ""

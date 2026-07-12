#!/usr/bin/env bash
# Optedge — one-command setup script
# Usage: bash install.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}╭──────────────────────────────────────╮${NC}"
echo -e "${BOLD}│  Optedge — easy setup                │${NC}"
echo -e "${BOLD}╰──────────────────────────────────────╯${NC}"

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}✗ Python 3 not found.${NC}"
    echo "  Install Python 3.12 from https://www.python.org/downloads/"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if ! python3 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)"; then
    echo -e "${RED}✗ Optedge requires Python 3.11 through 3.13 (found $PY_VERSION).${NC}"
    exit 1
fi
echo -e "${GREEN}✓${NC} Python $PY_VERSION"

# 2. Choose install method
echo ""
echo "Choose install method:"
echo "  1) Virtual environment (recommended — isolates Optedge from system Python)"
echo "  2) System pip (faster but may conflict with other packages)"
echo ""
read -p "Choice [1]: " choice
choice=${choice:-1}

if [ "$choice" = "1" ]; then
    if [ ! -d "venv" ]; then
        echo -e "${YELLOW}Creating virtual environment...${NC}"
        python3 -m venv venv
    fi
    source venv/bin/activate
    PIP="pip"
    PY="python"
else
    PIP="pip3"
    PY="python3"
fi

# 3. Install deps
echo -e "${YELLOW}Installing dependencies (this may take 30-60s)...${NC}"
$PIP install --quiet --upgrade pip
$PIP install --quiet -r requirements.txt
echo -e "${GREEN}✓${NC} Dependencies installed"

# 4. Run setup check
echo ""
echo -e "${BOLD}Running setup check (verifies data sources)...${NC}"
echo ""
$PY setup_check.py

# 5. Final guidance
echo ""
echo -e "${BOLD}╭──────────────────────────────────────╮${NC}"
echo -e "${BOLD}│  Setup complete                      │${NC}"
echo -e "${BOLD}╰──────────────────────────────────────╯${NC}"
echo ""
if [ "$choice" = "1" ]; then
    echo -e "  Activate the venv next time with:  ${BOLD}source venv/bin/activate${NC}"
fi
echo -e "  Run the pipeline:                    ${BOLD}$PY run.py${NC}"
echo -e "  Quick demo (no network):             ${BOLD}$PY run.py --demo${NC}"
echo -e "  Open the Trade Desk:                 ${BOLD}$PY scripts/local_cockpit.py${NC}"
echo ""
echo "  → Outputs land in data/"
echo "  → Open data/dashboard_*.html in your browser"
echo "  → Import data/tradingview_watchlist_*.txt in TradingView"
echo ""

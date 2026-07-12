#!/usr/bin/env bash
# Installs Optedge and its dependencies into a local virtual environment on Linux and macOS.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

printf '%b\n' "${BOLD}+--------------------------------------+${NC}"
printf '%b\n' "${BOLD}|  Optedge - reproducible setup       |${NC}"
printf '%b\n' "${BOLD}+--------------------------------------+${NC}"

PYTHON_BIN=""
for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    printf '%b\n' "${RED}ERROR: Python 3.11 through 3.13 is required.${NC}"
    printf '%s\n' "Install Python 3.12 from https://www.python.org/downloads/ and retry."
    exit 1
fi

PY_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
printf '%b\n' "${GREEN}[OK]${NC} Python $PY_VERSION via $PYTHON_BIN"

if [[ ! -d venv ]]; then
    printf '%b\n' "${YELLOW}Creating virtual environment...${NC}"
    "$PYTHON_BIN" -m venv venv
fi

PY="venv/bin/python"
if ! "$PY" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' >/dev/null 2>&1; then
    printf '%b\n' "${RED}ERROR: The existing venv is broken or uses an unsupported Python.${NC}"
    printf '%s\n' "Remove the venv directory and run this installer again."
    exit 1
fi
printf '%b\n' "${YELLOW}Installing Optedge and dependencies...${NC}"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
"$PY" -m pip check
printf '%b\n' "${GREEN}[OK]${NC} Dependencies installed and consistent"

printf '%b\n\n' "${BOLD}Running setup health check...${NC}"
"$PY" setup_check.py

printf '\n%b\n' "${BOLD}+--------------------------------------+${NC}"
printf '%b\n' "${BOLD}|  Setup complete                      |${NC}"
printf '%b\n' "${BOLD}+--------------------------------------+${NC}"
printf '%s\n' "Activate later: source venv/bin/activate"
printf '%s\n' "Run pipeline:   python run.py"
printf '%s\n' "Run demo:       python run.py --demo"
printf '%s\n' "Open Trade Desk: python scripts/local_cockpit.py"
printf '%s\n' "Outputs:        data/"

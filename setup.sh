#!/usr/bin/env bash
# Create .venv (if missing), install deps, and download Playwright's Chromium.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
    echo "Creating virtualenv at .venv ..."
    "$PYTHON" -m venv .venv
fi

source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

echo "Done. Activate with: source .venv/bin/activate"

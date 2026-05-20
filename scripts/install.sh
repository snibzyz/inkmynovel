#!/usr/bin/env bash
# First-time install for InkMyNovel (macOS / Linux): create .venv + deps.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================================"
echo "  InkMyNovel - install dependencies (macOS / Linux)"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  echo "  macOS : install from https://www.python.org/downloads/macos/  (or: brew install python)"
  echo "  Linux : sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "[2/3] Upgrading pip..."
.venv/bin/python -m pip install --upgrade pip

echo "[3/3] Installing requirements..."
.venv/bin/python -m pip install -r requirements.txt

echo
echo "Done. Launch the app with:  ./scripts/run.sh"

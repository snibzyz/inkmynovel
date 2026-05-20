#!/usr/bin/env bash
# Launch InkMyNovel from source (macOS / Linux).
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  echo "Virtual environment not found. Run ./scripts/install.sh first."
  exit 1
fi

exec .venv/bin/python inkxmynovel_pyqt.py

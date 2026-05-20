#!/usr/bin/env bash
# Build InkMyNovel into a single-file app with PyInstaller.
#   macOS -> dist/InkMyNovel.app
#   Linux -> dist/InkMyNovel
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================================"
echo " Building InkMyNovel"
echo "============================================================"

# Prefer the project's .venv; fall back to system python3.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "ERROR: no Python found. Run ./scripts/install.sh first."
  exit 1
fi

echo "[1/3] Installing build + runtime dependencies..."
"$PY" -m pip install --upgrade pip pyinstaller
"$PY" -m pip install -r requirements.txt

echo "[2/3] Cleaning previous build..."
rm -rf build dist

echo "[3/3] Running PyInstaller..."
"$PY" -m PyInstaller --noconfirm --clean InkMyNovel.spec

echo
echo "============================================================"
echo " Build OK"
if [ -d "dist/InkMyNovel.app" ]; then
  echo " Output : $ROOT/dist/InkMyNovel.app"
  echo " Tip    : drag InkMyNovel.app into /Applications."
  echo "          First launch: right-click the app -> Open -> Open."
else
  echo " Output : $ROOT/dist/InkMyNovel"
fi
echo "============================================================"

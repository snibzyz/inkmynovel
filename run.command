#!/usr/bin/env bash
# Double-clickable launcher (macOS) — delegates to scripts/run.sh
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
exec bash "$DIR/scripts/run.sh"

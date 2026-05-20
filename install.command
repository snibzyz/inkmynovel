#!/usr/bin/env bash
# Double-clickable installer (macOS) - delegates to scripts/install.sh
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
exec bash "$DIR/scripts/install.sh"

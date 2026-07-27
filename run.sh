#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  "$SCRIPT_DIR/setup.sh"
fi

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/obrik_flash.py" "$@"


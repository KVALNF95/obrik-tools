#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

"$SCRIPT_DIR/run.sh" "$@"
status=$?
echo
read -r -p "Finished with code $status. Press Enter to close..."
exit "$status"

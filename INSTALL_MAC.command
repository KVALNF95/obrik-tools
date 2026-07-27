#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Opening https://brew.sh ..."
  open "https://brew.sh"
  echo "Install Homebrew, then run INSTALL_MAC.command again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if "$SCRIPT_DIR/setup.sh"; then
  install_status=0
  echo
  echo "INSTALLATION COMPLETE. Start RUN_MAC.command to flash a drone."
else
  install_status=$?
  echo
  echo "INSTALLATION FAILED."
fi
read -r -p "Press Enter to close..."
exit "$install_status"

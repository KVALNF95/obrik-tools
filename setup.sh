#!/usr/bin/env bash
# Установка зависимостей в локальное окружение obrik-tools (macOS/Linux).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
  Darwin)
    command -v brew >/dev/null || { echo "Нужен Homebrew: https://brew.sh"; exit 1; }
    brew list dfu-util >/dev/null 2>&1 || brew install dfu-util
    ;;
  Linux)
    sudo apt-get update -qq
    sudo apt-get install -y -qq dfu-util python3-venv
    if [ ! -f /etc/udev/rules.d/99-px4.rules ]; then
      echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="1013", MODE="0666", GROUP="dialout"' | sudo tee /etc/udev/rules.d/99-px4.rules >/dev/null
      sudo udevadm control --reload-rules
      sudo udevadm trigger
    fi
    sudo usermod -a -G dialout "$USER" 2>/dev/null || true
    ;;
  *) echo "Неподдерживаемая ОС"; exit 1 ;;
esac

python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SCRIPT_DIR/.venv/bin/python" -m pip install pymavlink pyserial

echo "Готово. Проверка:"
echo "  $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/obrik_flash.py --dry-run"
echo "Прошивка:"
echo "  $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/obrik_flash.py --steps all"

#!/bin/bash
# obrik-tools/setup.sh — развернуть всё на новом ноутбуке
# Запуск: bash setup.sh

set -e
echo "=== obrik-tools: установка ==="

# 1. системные зависимости
echo "[1/4] системные пакеты..."
sudo apt-get update -qq
sudo apt-get install -y -qq dfu-util python3-pip python3-serial 2>/dev/null

# 2. python-зависимости
echo "[2/4] python-зависимости..."
pip install --user --quiet pymavlink 2>/dev/null || \
    pip install --user --break-system-packages --quiet pymavlink

# 3. udev-правило (доступ к /dev/ttyACM* без sudo)
echo "[3/4] udev-правило..."
if [ ! -f /etc/udev/rules.d/99-px4.rules ]; then
    echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="1013", MODE="0666", GROUP="dialout"' | \
        sudo tee /etc/udev/rules.d/99-px4.rules > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi
sudo usermod -a -G dialout $USER 2>/dev/null || true

# 4. проверка
echo "[4/4] проверка..."
python3 -c "from pymavlink import mavutil; print('pymavlink OK')"
dfu-util --version 2>&1 | head -1
echo ""
echo "=== ГОТОВО ==="
echo "Запуск утилиты:"
echo "  python3 ~/obrik-tools/obrik_flash.py -c ~/obrik-tools/obrik_flash.cfg --steps all"
echo ""
echo "ПЕРЕЗАГРУЗИТЕ СИСТЕМУ (или выйдите и зайдите), чтобы права dialout применились."

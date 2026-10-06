#!/bin/bash
# Создаёт ярлык «Sverk Tools» в меню приложений и на рабочем столе,
# чтобы запускать UI иконкой, а не командой в терминале.
# Запуск: bash install_launcher.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ICON="$DIR/sverk_icon.png"
PY="$(command -v python3)"

desktop_entry() {
cat <<EOF
[Desktop Entry]
Type=Application
Name=Sverk Tools
Comment=Прошивка и настройка дронов
Exec=$PY "$DIR/obrik_ui.py"
Path=$DIR
Icon=$ICON
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
}

# 1) в меню приложений
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
desktop_entry > "$APPS/sverk-tools.desktop"
chmod +x "$APPS/sverk-tools.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

# 2) на рабочий стол (чтобы была иконка как у приложения)
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
    desktop_entry > "$DESK/sverk-tools.desktop"
    chmod +x "$DESK/sverk-tools.desktop"
    # GNOME требует пометить ярлык «доверенным»
    gio set "$DESK/sverk-tools.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "Готово. Ярлык «Sverk Tools» в меню приложений и на рабочем столе."

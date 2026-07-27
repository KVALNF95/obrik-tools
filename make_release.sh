#!/usr/bin/env bash
# make_release.sh — собрать автономную зипку obrik-flash для раздачи.
#
# Кладёт в dist/ архив, который человек распаковывает и сразу запускает:
#   python3 obrik_flash.py -c obrik_flash.cfg --steps all
# Клонировать PX4 при этом НЕ нужно — всё вложено (прошивка, загрузчик,
# px_uploader/mavlink_shell, параметры).
#
# Использование:
#   ./make_release.sh                 # зипка из того, что лежит в репе
#   ./make_release.sh --fw path.px4   # перед сборкой обновить прошивку из свежего билда форка
#   ./make_release.sh --bl path.bin   # ... и/или загрузчик
set -euo pipefail
cd "$(dirname "$0")"

FW=""
BL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fw) FW="$2"; shift 2 ;;
    --bl) BL="$2"; shift 2 ;;
    *) echo "неизвестный аргумент: $1"; exit 1 ;;
  esac
done

# при желании подложить свежий билд из форка PX4
[ -n "$FW" ] && { cp "$FW" firmware/matek_h743-slim_default.px4;      echo "обновил прошивку из $FW"; }
[ -n "$BL" ] && { cp "$BL" firmware/matek_h743-slim_bootloader.bin;   echo "обновил загрузчик из $BL"; }

# проверка, что все обязательные ресурсы на месте
missing=0
for f in obrik_flash.py obrik_flash.cfg README.md setup.sh \
         tools/px_uploader.py tools/mavlink_shell.py \
         firmware/matek_h743-slim_default.px4 \
         firmware/matek_h743-slim_bootloader.bin; do
  [ -f "$f" ] || { echo "  ✗ отсутствует: $f"; missing=1; }
done
[ "$missing" -eq 0 ] || { echo "[ОШИБКА] не хватает файлов для релиза"; exit 1; }

# версия по git-описанию, иначе по дате
VER="$(git describe --tags --always --dirty 2>/dev/null || date +%Y%m%d)"
OUT="dist/obrik-flash-${VER}.zip"
mkdir -p dist
rm -f "$OUT"

# упаковать всё, кроме служебного
zip -r "$OUT" \
    obrik_flash.py obrik_flash.cfg README.md setup.sh \
    tools firmware params \
    -x '*/__pycache__/*' -x '*.pyc' >/dev/null

echo
echo "  ✓ готово: $OUT ($(du -h "$OUT" | cut -f1))"
echo "  проверить содержимое:  unzip -l $OUT"

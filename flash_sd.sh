#!/bin/bash
# Параллельно пишет один образ .img на несколько SD-карт через dd.
#
#   ./flash_sd.sh образ.img 4                  ждать, пока вставлено ровно 4 карты, и писать разом
#   ./flash_sd.sh образ.img                    писать все найденные карты
#   ./flash_sd.sh образ.img /dev/sdb /dev/sdc  писать явно указанные устройства
#   -c, --verify   после записи читает карту обратно и сравнивает с образом
#   -y, --yes      не спрашивать подтверждение
#   --ui           машинный вывод прогресса для GUI (UIPROG|dev|bytes|total|state)
#
# Карты ищутся автоматически: съёмные/USB/mmc-диски, кроме системного и кроме тех,
# что больше MAX_GB (по умолчанию 256). Нужен root — скрипт перезапустится через sudo.
set -euo pipefail

MAX_GB=${MAX_GB:-256}
die() { echo "Ошибка: $*" >&2; exit 1; }
usage() { sed -n '2,12p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

VERIFY=0; YES=0; UI=0; ARGS=()
for a in "$@"; do
  case "$a" in
    -c|--verify) VERIFY=1 ;;
    -y|--yes)    YES=1 ;;
    --ui)        UI=1; YES=1 ;;
    -h|--help)   usage ;;
    -*)          die "неизвестный ключ $a (см. --help)" ;;
    *)           ARGS+=("$a") ;;
  esac
done
[ ${#ARGS[@]} -ge 1 ] || usage 1
IMG=${ARGS[0]}
[ -f "$IMG" ] || die "нет файла образа: $IMG"
case "$IMG" in *.xz|*.gz|*.zip|*.zst) die "образ сжат — сначала распакуйте в .img" ;; esac
[ "$EUID" -eq 0 ] || exec sudo -- "$0" "$@"

SIZE=$(stat -Lc %s "$IMG")
[ "$SIZE" -gt 0 ] || die "образ пустой"
hsize() { numfmt --to=iec --format=%.1f "$1"; }

is_system_disk() { lsblk -nro MOUNTPOINTS "$1" | grep -qE '^(/|/boot.*|/home|/usr|/var|\[SWAP\])$'; }

detect() {
  FOUND=(); SKIPPED=()
  local name size type rm hp tran
  while read -r name size type rm hp tran; do
    [ "$type" = disk ] || continue
    [[ $name =~ (boot[0-9]+|rpmb)$ ]] && continue
    [[ $rm = 1 || $hp = 1 || $tran = usb || $name == /dev/mmcblk* ]] || continue
    [ "$size" -gt 0 ] || continue
    is_system_disk "$name" && continue
    if [ "$size" -gt $((MAX_GB * 1000000000)) ]; then
      SKIPPED+=("$name ($(hsize "$size")) — больше MAX_GB=$MAX_GB, пропущен"); continue
    fi
    if [ "$size" -lt "$SIZE" ]; then
      SKIPPED+=("$name ($(hsize "$size")) — меньше образа, пропущен"); continue
    fi
    FOUND+=("$name")
  done < <(lsblk -dbpnro NAME,SIZE,TYPE,RM,HOTPLUG,TRAN)
}

DEVS=()
if [ ${#ARGS[@]} -ge 2 ] && [[ ${ARGS[1]} == /dev/* ]]; then
  for d in "${ARGS[@]:1}"; do
    d=$(readlink -f "$d")
    [ -b "$d" ] || die "$d — не блочное устройство"
    [ "$(lsblk -dnro TYPE "$d")" = disk ] || die "$d — раздел, а нужен диск целиком (например /dev/sdb)"
    is_system_disk "$d" && die "$d — системный диск, отказ"
    [ "$(lsblk -dbnro SIZE "$d")" -ge "$SIZE" ] || die "$d меньше образа ($(hsize "$SIZE"))"
    [[ " ${DEVS[*]} " == *" $d "* ]] || DEVS+=("$d")
  done
else
  N=${ARGS[1]:-}
  [ ${#ARGS[@]} -le 2 ] || die "лишние аргументы: ${ARGS[*]:2}"
  [[ -z $N || $N =~ ^[1-9][0-9]*$ ]] || die "количество карт должно быть числом, а не '$N'"
  last=-1
  while :; do
    detect
    [ -n "$N" ] || break
    [ ${#FOUND[@]} -eq "$N" ] && break
    if [ ${#FOUND[@]} -gt "$N" ]; then
      printf '  %s\n' "${FOUND[@]}"
      die "найдено карт: ${#FOUND[@]}, а задано $N — выньте лишние или укажите устройства явно"
    fi
    if [ ${#FOUND[@]} -ne "$last" ]; then
      echo "Вставлено карт: ${#FOUND[@]} из $N${FOUND:+ (${FOUND[*]})} — жду остальные, Ctrl+C для отмены"
      last=${#FOUND[@]}
    fi
    sleep 1
  done
  [ ${#SKIPPED[@]} -eq 0 ] || printf 'Пропуск: %s\n' "${SKIPPED[@]}"
  [ ${#FOUND[@]} -gt 0 ] || die "SD-карты не найдены"
  DEVS=("${FOUND[@]}")
fi

echo "Образ: $IMG ($(hsize "$SIZE"))"
echo "Будут ПОЛНОСТЬЮ ПЕРЕЗАПИСАНЫ (${#DEVS[@]} шт.):"
for d in "${DEVS[@]}"; do
  printf '  %-14s %8s  %s\n' "$d" "$(lsblk -dnro SIZE "$d")" "$(lsblk -dno VENDOR,MODEL "$d" | xargs)"
done
if [ "$YES" -ne 1 ]; then
  read -rp "Продолжить? Введите yes: " ans || true
  [ "$ans" = yes ] || die "отменено"
fi

unmount_all() {
  local p
  while read -r p; do
    [ -n "$(lsblk -nro MOUNTPOINTS "$p")" ] || continue
    umount "$p" 2>/dev/null || umount -l "$p" || return 1
  done < <(lsblk -pnro NAME "$1")
}
for d in "${DEVS[@]}"; do unmount_all "$d" || die "не удалось отмонтировать $d"; done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
RDIRECT=; [ $((SIZE % 512)) -ne 0 ] || RDIRECT=direct,

worker() {
  local dev=$1 n; n=$(basename "$1")
  exec 9<"$dev"; flock -x 9
  echo write >"$TMP/$n.state"
  if ! LC_ALL=C dd if="$IMG" of="$dev" bs=4M oflag=direct conv=fsync status=progress 2>"$TMP/$n.log"; then
    echo fail-write >"$TMP/$n.state"; return 1
  fi
  if [ "$VERIFY" -eq 1 ]; then
    echo verify >"$TMP/$n.state"
    blockdev --flushbufs "$dev" || true
    if ! LC_ALL=C dd if="$dev" bs=4M iflag=${RDIRECT}count_bytes count="$SIZE" status=progress 2>"$TMP/$n.vlog" \
         | cmp -s - "$IMG"; then
      echo fail-verify >"$TMP/$n.state"; return 1
    fi
  fi
  echo ok >"$TMP/$n.state"
}

BARW=30
[ -t 1 ] && CLR=$'\033[K' || CLR=
# считать «записано байт» из dd-лога устройства
read_bytes() {
  tr '\r' '\n' <"$1" 2>/dev/null | awk '/bytes/ {b=$1} END {print b+0}'
}

render_ui() {   # машинный вывод для GUI
  local d n st log b
  for d in "${DEVS[@]}"; do
    n=$(basename "$d"); st=$(cat "$TMP/$n.state" 2>/dev/null || echo start)
    log="$TMP/$n.log"; [[ $st == *verify ]] && log="$TMP/$n.vlog"
    b=$(read_bytes "$log"); [ "$st" = ok ] && b=$SIZE
    echo "UIPROG|$d|${b:-0}|$SIZE|$st"
  done
  echo "UIEND"
}

render() {
  local d n st log b spd pct fill bar
  for d in "${DEVS[@]}"; do
    n=$(basename "$d"); st=$(cat "$TMP/$n.state" 2>/dev/null || echo start)
    log="$TMP/$n.log"; [[ $st == *verify ]] && log="$TMP/$n.vlog"
    b=$(read_bytes "$log")
    spd=$(tr '\r' '\n' <"$log" 2>/dev/null | awk '/bytes/ {s=$(NF-1)" "$NF} END {print s}')
    case "$st" in
      start|write) st="запись" ;;
      verify)      st="проверка" ;;
      ok)          st="ГОТОВО"; b=$SIZE ;;
      fail-write)  st="ОШИБКА ЗАПИСИ" ;;
      fail-verify) st="НЕ СОШЛОСЬ С ОБРАЗОМ" ;;
    esac
    b=${b:-0}; pct=$((b * 100 / SIZE)); fill=$((b * BARW / SIZE))
    bar=$(printf '%*s' "$fill" '' | tr ' ' '#')$(printf '%*s' $((BARW - fill)) '' | tr ' ' '.')
    printf '%s  %-12s [%s] %3d%%  %6s / %s  %-9s %s\n' "$CLR" "$d" "$bar" "$pct" \
      "$(hsize "$b")" "$(hsize "$SIZE")" "$spd" "$st"
  done
}

PIDS=()
stop() {
  echo; echo "Прервано — карты записаны не полностью, их нужно переписать."
  for p in "${PIDS[@]}"; do pkill -TERM -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done
  exit 130
}
trap stop INT TERM

START=$SECONDS
for d in "${DEVS[@]}"; do worker "$d" & PIDS+=($!); done

running() { local p; for p in "${PIDS[@]}"; do kill -0 "$p" 2>/dev/null && return 0; done; return 1; }
if [ "$UI" -eq 1 ]; then
  while running; do render_ui; sleep 1; done
elif [ -t 1 ]; then
  render
  while running; do sleep 1; printf '\033[%dA' ${#DEVS[@]}; render; done
  printf '\033[%dA' ${#DEVS[@]}
else
  while running; do render; echo; sleep 30; done
fi
FAILED=0
for p in "${PIDS[@]}"; do wait "$p" || FAILED=$((FAILED + 1)); done
[ "$UI" -eq 1 ] && render_ui || render

for d in "${DEVS[@]}"; do
  n=$(basename "$d")
  if [[ $(cat "$TMP/$n.state") == fail-* ]]; then
    err=$(cat "$TMP/$n.log" "$TMP/$n.vlog" 2>/dev/null | tr '\r' '\n' | grep -v 'bytes\|records' | tail -3) || true
    [ -z "$err" ] || echo "--- $d: $err"
  fi
done

sync
for d in "${DEVS[@]}"; do blockdev --rereadpt "$d" 2>/dev/null || true; done
udevadm settle 2>/dev/null || true; sleep 3
for d in "${DEVS[@]}"; do unmount_all "$d" || echo "Внимание: $d смонтирован — отмонтируйте перед извлечением"; done
sync

T=$((SECONDS - START))
echo "Время: $((T / 60)) мин $((T % 60)) с. Успешно: $((${#DEVS[@]} - FAILED)) из ${#DEVS[@]}."
[ "$FAILED" -eq 0 ] && echo "Карты можно вынимать." || exit 1

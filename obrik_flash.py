#!/usr/bin/env python3
"""
obrik_flash.py — утилита одной командой для прошивки и настройки дрона «Обрик».

Что делает:
  0. Mass-erase (DFU) — полное стирание flash (для проблемных плат)
  1. Прошивает загрузчик (DFU) — требуется нажать кнопку BOOT
  2. Прошивает основную прошивку PX4 (DFU)
  3. Загружает параметры в полётник (через MAVLink param_set)
  4. Записывает Beacon Delay = Infinite во все ESC (требуется АКБ)

Шаги 1–2 прошивают ВСЕГДА через DFU (кнопка BOOT) — единый процесс и для
новых плат (с заводским ArduPilot/Betaflight), и для уже прошитых. Если
плата запущена, утилита попросит переподключить её с зажатым BOOT; между
шагами 1 и 2 плата остаётся в DFU, кнопка нажимается один раз.

Шаги 3 и 4 работают через одно общее MAVLink-соединение; beacon пишется
через nsh поверх SERIAL_CONTROL этого же канала (отдельный mavlink_shell
не запускается).

Использование:
  python3 obrik_flash.py                     # с конфигом по умолчанию
  python3 obrik_flash.py --config my.cfg     # с указанным конфигом
  python3 obrik_flash.py --steps 1,2         # только прошивка
  python3 obrik_flash.py --steps beacon      # только отключение писка
  python3 obrik_flash.py --steps params      # только загрузка параметров
  python3 obrik_flash.py --steps erase       # только mass-erase

Конфиг-файл (obrik_flash.cfg) — формат key=value, см. пример внизу.
"""

import os, sys, time, re, glob, subprocess, argparse, json

# каталог скрипта — вложенные ресурсы (firmware/, params/, tools/) ищем
# рядом с ним, чтобы утилита работала из распакованной зипки без PX4-checkout
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── конфиг по умолчанию (пути — к вложенным ресурсам рядом со скриптом) ─
DEFAULT_CONFIG = {
    "bootloader":   os.path.join(SCRIPT_DIR, "firmware", "matek_h743-slim_bootloader.bin"),
    "firmware":     os.path.join(SCRIPT_DIR, "firmware", "matek_h743-slim_default.px4"),
    "firmware_bin": os.path.join(SCRIPT_DIR, "firmware", "matek_h743-slim_default.bin"),
    "params_file":  os.path.join(SCRIPT_DIR, "params", "obrik_last.params"),
    "px4_tools":    os.path.join(SCRIPT_DIR, "tools"),
    "dfu_address":  "0x08000000",
    "app_address":  "0x08020000",
    "baud":         "57600",
    "beacon_value": "5",
    "num_motors":   "4",
    # шаг 1: делать mass-erase перед записью загрузчика в одной DFU-команде
    # (1=да, чистый старт; 0=только записать загрузчик, не стирая чип)
    "bl_mass_erase": "1",
}

# ── определение состояния платы ──────────────────────────────────────

def detect_board_state():
    """
    Вернуть состояние платы:
      'dfu'        — плата в режиме DFU (готова к прошивке загрузчика)
      'running'    — плата запущена с прошивкой (/dev/ttyACM* есть)
      'none'       — плата не подключена
    """
    # DFU? — ищем реальное устройство: строка "Found DFU: [vid:pid]" или
    # "Dfuse" и USB-идентификатор 0483:df11 (STM32 DFU)
    result = subprocess.run("dfu-util -l 2>&1", shell=True, capture_output=True, text=True)
    out = result.stdout + result.stderr
    if re.search(r'Found DFU:\s*\[0483:', out) or re.search(r'0483.*df11', out, re.I):
        return "dfu"

    # ttyACM или serial/by-id?
    ports = glob.glob("/dev/ttyACM*") + glob.glob("/dev/serial/by-id/usb-Matek*")
    if ports:
        return "running"

    return "none"


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if not path:
        # auto-detect: сначала рядом со скриптом, потом в текущей папке
        script_dir = os.path.dirname(os.path.abspath(__file__))
        for d in [script_dir, os.getcwd()]:
            candidate = os.path.join(d, "obrik_flash.cfg")
            if os.path.exists(candidate):
                path = candidate
                break
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = os.path.expanduser(v.strip())
    # относительные пути резолвим от папки скрипта (работа из зипки)
    for k in ("bootloader", "firmware", "firmware_bin", "params_file", "px4_tools"):
        v = cfg.get(k, "")
        if v and not os.path.isabs(v):
            cfg[k] = os.path.normpath(os.path.join(SCRIPT_DIR, v))
    return cfg


def find_tty():
    """Вернуть первый /dev/ttyACM* или None."""
    ports = sorted(glob.glob("/dev/ttyACM*"))
    if ports:
        return ports[0]
    ids = sorted(glob.glob("/dev/serial/by-id/usb-Matek*"))
    if ids:
        return ids[0]
    return None


def wait_port(timeout_s=30):
    """Ждать появления /dev/ttyACM*."""
    print(f"ожидание порта (до {timeout_s} сек)...")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        p = find_tty()
        if p:
            print(f"  порт: {p}")
            return p
        time.sleep(0.5)
    print("  таймаут — порт не появился")
    return None


def mavlink_connect(port, baud=57600):
    """Подключиться к полётнику через MAVLink, дождаться heartbeat."""
    from pymavlink import mavutil
    print(f"  порт: {port}")
    m = mavutil.mavlink_connection(port, baud=baud)
    print("  жду heartbeat...")
    m.wait_heartbeat(timeout=15)
    print("  ✓ связь установлена")
    return m


def battery_voltage(m):
    """Считать напряжение батареи через MAVLink. Вернуть float или None."""
    # сначала запросим BATTERY_STATUS, если не шлётся само
    m.mav.command_long_send(
        1, 1,  # target_system, target_component
        147,   # MAV_CMD_REQUEST_MESSAGE
        0,     # confirmation
        147,   # MAVLINK_MSG_ID_BATTERY_STATUS (147)
        0, 0, 0, 0, 0, 0, 0
    )
    t0 = time.time()
    while time.time() - t0 < 3:
        msg = m.recv_match(type='BATTERY_STATUS', blocking=True, timeout=1)
        if msg is not None:
            v = msg.voltages[0] / 1000.0 if msg.voltages[0] < 65535 else 0.0
            return v
        msg = m.recv_match(type='SYS_STATUS', blocking=True, timeout=0.1)
        if msg is not None:
            v = msg.voltage_battery / 1000.0 if msg.voltage_battery < 65535 else 0.0
            return v
    return None


# SERIAL_CONTROL: устройство nsh-шелла и флаги — как в Tools/mavlink_shell.py.
# Раньше здесь слали в device 0 (это TELEM1, не шелл!) с флагом 1 — nsh молчал,
# из-за чего SERIAL_CONTROL считался «глючным».
SERIAL_CONTROL_DEV_SHELL = 10
SERIAL_CONTROL_FLAGS = 6  # EXCLUSIVE | RESPOND


def nsh_write(m, s):
    """Отправить строку в nsh-шелл (SERIAL_CONTROL, чанки по 70 байт)."""
    data = s.encode()
    while data:
        chunk, data = data[:70], data[70:]
        m.mav.serial_control_send(SERIAL_CONTROL_DEV_SHELL, SERIAL_CONTROL_FLAGS,
                                  0, 0, len(chunk), chunk.ljust(70, b"\x00"))


def nsh_read(m, dur, stop=None):
    """Собирать вывод nsh в течение dur секунд, шля heartbeat раз в секунду
    (как mavlink_shell.py). stop — подстроки для раннего выхода."""
    t0, out, next_hb = time.time(), "", 0.0
    while time.time() - t0 < dur:
        if time.time() >= next_hb:
            m.mav.heartbeat_send(6, 8, 0, 0, 0)  # MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID
            next_hb = time.time() + 1
        msg = m.recv_match(type='SERIAL_CONTROL', blocking=True, timeout=0.5)
        if msg is not None and msg.count:
            out += bytes(msg.data[:msg.count]).decode("ascii", "replace")
            if stop and any(s in out for s in stop):
                break
    return out


def nsh_send(m, cmd, timeout_s=6, stop=None):
    """Выполнить команду в nsh и вернуть её вывод."""
    nsh_write(m, "\n")   # разбудить шелл
    nsh_read(m, 0.3)     # съесть эхо/приглашение
    nsh_write(m, cmd + "\n")
    return nsh_read(m, timeout_s, stop=stop)


# ── общее MAVLink-соединение (шаги 3 и 4 работают через один канал) ───

_MAV_SESSION = {"m": None, "port": None}


def get_mavlink(cfg, wait_s=30):
    """Вернуть общее MAVLink-соединение, создав его при необходимости.
    Если порт исчез (плата перезагружалась/переподключалась) — переподключиться.
    Вернёт None, если порт так и не появился."""
    m, port = _MAV_SESSION["m"], _MAV_SESSION["port"]
    if m is not None and port and os.path.exists(port):
        return m
    close_mavlink()
    port = wait_port(wait_s)
    if not port:
        return None
    m = mavlink_connect(port, int(cfg.get("baud", "57600")))
    _MAV_SESSION.update(m=m, port=port)
    return m


def close_mavlink():
    """Закрыть общее соединение (освободить USB-порт)."""
    if _MAV_SESSION["m"] is not None:
        try:
            _MAV_SESSION["m"].close()
        except Exception:
            pass
    _MAV_SESSION.update(m=None, port=None)


# ── верификация ──────────────────────────────────────────────────────

def _dfu_upload(addr, nbytes):
    """Прочитать nbytes из flash по адресу через dfu-util. Вернуть bytes|None.
    Важно: dfu-util -U отказывается перезаписывать существующий файл, поэтому
    пишем в заведомо несуществующий путь внутри временной директории."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="obrik_dfu_")
    out = os.path.join(d, "readback.bin")
    try:
        cmd = f'dfu-util -a 0 --dfuse-address {addr}:{nbytes} -U "{out}"'
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def verify_dfu_write(addr, path):
    """Прочитать flash обратно и сравнить с файлом.
    True — совпало, False — НЕ совпало, None — readback невозможен."""
    want = open(path, "rb").read()
    got = _dfu_upload(addr, len(want))
    if got is None or len(got) < len(want):
        return None
    return got[:len(want)] == want


def verify_dfu_erased(addr, nbytes=4096):
    """Проверить, что регион flash стёрт (все байты 0xFF).
    True — стёрт, False — НЕ стёрт, None — readback невозможен."""
    got = _dfu_upload(addr, nbytes)
    if got is None or not got:
        return None
    return all(b == 0xFF for b in got)


def dfu_download(addr, path, label="образ", retries=3, extra="",
                 stall=25, hard_timeout=600, logdir="/tmp"):
    """Записать файл во flash через dfu-util с ЖИВЫМ ЛОГОМ и авто-ретраем.

    Показывает прогресс dfu-util построчно с таймштампами, поэтому сразу видно,
    идёт заливка или встало. Если новой активности нет дольше `stall` секунд —
    считает попытку ЗАВИСШЕЙ, убивает процесс (вместе с группой) и повторяет.
    Первая DFU-загрузка на STM32 часто виснет — вторая обычно проходит.
    extra — доп. опции DfuSe в адресе (напр. 'mass-erase:force').
    Полный сырой лог каждой попытки пишется в logdir/obrik_dfu_<label>_N.log."""
    import select, signal
    ok_re = re.compile(r'(download.*done|downloaded successfully|file downloaded)', re.I)
    hot_re = re.compile(r'(done|error|cannot|fail|lost device|not found|no error|'
                        r'setting alternate|downloading element|erase|download)', re.I)
    spec = f'{addr}:{extra}' if extra else addr
    size = os.path.getsize(path) if os.path.exists(path) else 0
    safe = re.sub(r'\W+', '_', label)

    for attempt in range(1, retries + 1):
        print(f"\n  [{label}] попытка {attempt}/{retries}: dfu-util → {spec} "
              f"({size:,} B)")
        logpath = os.path.join(logdir, f"obrik_dfu_{safe}_{attempt}.log")
        cmd = f'dfu-util -a 0 --dfuse-address {spec} -D "{path}"'
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0,
                                preexec_fn=os.setsid)
        chunks, t0, last_out, last_print = [], time.time(), time.time(), 0.0
        stalled = False
        while proc.poll() is None:
            r, _, _ = select.select([proc.stdout], [], [], 1.0)
            now = time.time()
            if r:
                try:
                    data = os.read(proc.stdout.fileno(), 4096)
                except OSError:
                    data = b""
                if data:
                    text = data.decode("ascii", "replace")
                    chunks.append(text)
                    last_out = now
                    for piece in re.split(r'[\r\n]', text):
                        p = piece.strip()
                        if not p:
                            continue
                        milestone = ('done' in p.lower() or 'element' in p.lower()
                                     or hot_re.search(p) and '%' not in p)
                        prog = '%' in p
                        if milestone or (prog and now - last_print >= 2.0):
                            print(f"    [{now - t0:5.1f}s] {p[:96]}")
                            last_print = now
            # детект зависания / жёсткий предел
            if now - last_out > stall:
                print(f"    ⚠ нет активности dfu-util {stall}с → попытка ЗАВИСЛА, убиваю")
                stalled = True
                break
            if now - t0 > hard_timeout:
                print(f"    ⚠ жёсткий таймаут {hard_timeout}с → убиваю")
                stalled = True
                break

        if stalled:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        try:
            tail = proc.stdout.read()
            if tail:
                chunks.append(tail.decode("ascii", "replace"))
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass

        combined = "".join(chunks)
        try:
            with open(logpath, "w") as f:
                f.write(combined)
        except Exception:
            logpath = "(лог не записан)"
        rc = proc.returncode

        if not stalled and rc == 0 and ok_re.search(combined):
            print(f"    ✓ {label} записан за {time.time() - t0:.0f}с "
                  f"(попытка {attempt}); лог: {logpath}")
            return True

        why = "зависла" if stalled else f"rc={rc}, нет строки успеха"
        print(f"    ✗ попытка {attempt} не удалась ({why}); полный лог: {logpath}")
        for l in [x for x in re.split(r'[\r\n]', combined) if x.strip()][-4:]:
            print(f"      | {l[:110]}")

        if attempt < retries:
            time.sleep(2)
            if detect_board_state() != "dfu":
                print("    >>> Плата вышла из DFU. Передёрните USB с зажатым BOOT. <<<")
                try:
                    input("    Нажмите Enter, когда плата снова в DFU...")
                except EOFError:
                    pass

    print(f"  [ОШИБКА] {label}: не удалось записать за {retries} попыт.")
    return False


def parse_px4_git(fw_path):
    """Достать git_identity/version из .px4 (JSON), либо None."""
    try:
        with open(fw_path) as f:
            data = json.load(f)
        return data.get("git_identity") or data.get("version")
    except Exception:
        return None


def _extract_bin_from_px4(px4_path, out_path):
    """.px4 — это JSON с base64+zlib-образом; достать из него .bin для DFU."""
    try:
        import base64, zlib
        with open(px4_path) as f:
            img = json.load(f)["image"]
        data = zlib.decompress(base64.b64decode(img))
        with open(out_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  ⚠ не удалось извлечь .bin из .px4: {e}")
        return False


def ensure_firmware_fresh(cfg):
    """Перед прошивкой: если в конфиге задан px4_src (репозиторий исходников
    PX4), проверить обновления ветки на git, при необходимости пересобрать
    прошивку и подставить свежие артефакты в cfg.
    True — можно шить (пусть даже прошивкой из комплекта), False — сборка
    требовалась, но не удалась."""
    src = cfg.get("px4_src", "")
    if not src:
        return True
    src = os.path.expanduser(src)
    branch = cfg.get("px4_branch", "")
    target = cfg.get("px4_target", "matek_h743-slim_default")
    repo_url = cfg.get("px4_repo", "")
    fw = os.path.join(src, "build", target, f"{target}.px4")

    print("\n" + "=" * 60)
    print("ПОДГОТОВКА — исходники PX4: обновления и сборка")
    print("=" * 60)

    if not os.path.isdir(os.path.join(src, ".git")) or not branch:
        print(f"  ⚠ исходники не найдены ({src}) — использую прошивку из комплекта")
        return True

    def git(*args, timeout=300):
        return subprocess.run(["git", "-C", src] + list(args),
                              capture_output=True, text=True, timeout=timeout)

    def norm(url):
        return url.rstrip("/").removesuffix(".git")

    # найти/завести remote с нужным URL
    remote = None
    if repo_url:
        for line in git("remote", "-v").stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and norm(parts[1]) == norm(repo_url):
                remote = parts[0]
                break
        if remote is None:
            git("remote", "add", "sverk-src", repo_url)
            remote = "sverk-src"
    else:
        remote = "origin"

    # есть ли обновления
    print(f"  проверяю обновления: {remote}/{branch} ...")
    fetched = git("fetch", remote, branch).returncode == 0
    if not fetched:
        print("  ⚠ git недоступен (нет сети?)")
        if os.path.exists(fw):
            print("  использую уже собранную прошивку")
    head = git("rev-parse", "HEAD").stdout.strip()
    cur_branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    want = git("rev-parse", f"{remote}/{branch}").stdout.strip() \
        if fetched else head

    need_build = not os.path.exists(fw)
    if cur_branch != branch or (want and head != want):
        if cur_branch != branch:
            print(f"  переключаюсь на ветку {branch} (была {cur_branch})")
        else:
            print(f"  есть обновления: {head[:9]} → {want[:9]}")
        if git("checkout", "-B", branch, f"{remote}/{branch}").returncode != 0:
            print("  [ОШИБКА] не удалось переключиться на ветку")
            return False
        print("  обновляю сабмодули (может занять несколько минут)...")
        git("submodule", "sync", "--recursive")
        if git("submodule", "update", "--init", "--recursive",
               timeout=3600).returncode != 0:
            print("  [ОШИБКА] сабмодули не обновились")
            return False
        need_build = True
    elif need_build:
        print("  исходники актуальны, но собранной прошивки нет — соберу")
    else:
        print(f"  ✓ исходники актуальны ({head[:9]}), пересборка не нужна")

    if need_build:
        print(f"  собираю прошивку: make {target} — при первой сборке "
              "это может занять 10–15 минут...")
        p = subprocess.Popen(["make", target], cwd=src,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        for line in p.stdout:
            line = _strip_ansi(line.rstrip())
            if line:
                print(f"    {line}")
        if p.wait() != 0:
            print("  [ОШИБКА] сборка не удалась — прошивать нечем")
            return False
        print("  ✓ сборка успешна")

    if not os.path.exists(fw):
        print(f"  [ОШИБКА] после сборки нет файла {fw}")
        return False

    # подставить свежие артефакты
    cfg["firmware"] = fw
    dfu_bin = os.path.join(src, "build", target, f"{target}.dfu.bin")
    if _extract_bin_from_px4(fw, dfu_bin):
        cfg["firmware_bin"] = dfu_bin
    bl = os.path.join(src, "boards", "matek", "h743-slim", "extras",
                      "matek_h743-slim_bootloader.bin")
    if os.path.exists(bl):
        cfg["bootloader"] = bl
    print(f"  использую прошивку из сборки: {fw}")
    return True


def _git_candidates(ver):
    """Вытащить git-хэш из AUTOPILOT_VERSION.flight_custom_version.
    PX4 кладёт его то ASCII-текстом, то бинарно задом наперёд
    (little-endian) — возвращаем оба прочтения."""
    if ver is None:
        return []
    try:
        raw = bytes(ver.flight_custom_version)
    except Exception:
        return []
    out = []
    txt = raw.split(b'\x00')[0].decode('ascii', 'replace').strip().lower()
    if re.fullmatch(r"[0-9a-f]{6,}", txt):
        out.append(txt)
    out.append(raw[::-1].rstrip(b'\x00').hex())   # бинарный вариант
    return [c for c in out if c]


def probe_running_px4(cfg, fw_path):
    """Опрос запущенной платы БЕЗ захода в DFU. Возвращает:
      'same'    — на плате PX4 и git-хэш совпал с нашим образом;
      'px4'     — на плате PX4, другая/неизвестная версия (загрузчик PX4
                  рабочий → шьём px_uploader'ом без BOOT);
      'foreign' — есть heartbeat, но это не PX4 (ArduPilot/Betaflight) →
                  перепрошивка только через DFU (BOOT);
      'silent'  — порт есть, но плата не отвечает по MAVLink (молчит,
                  завис, порт занят) — НЕ значит «чужая прошивка»."""
    try:
        from pymavlink import mavutil
    except ImportError:
        return "silent"
    port = find_tty()
    if not port:
        return "silent"
    try:
        m = mavutil.mavlink_connection(port, baud=int(cfg.get("baud", "57600")))
    except Exception:
        return "silent"
    try:
        hb = m.wait_heartbeat(timeout=10)
        if hb is None:
            return "silent"
        # MAV_AUTOPILOT_PX4 = 12; ArduPilot (=3) тоже говорит на MAVLink,
        # но его загрузчик px_uploader не поймёт — для него путь через DFU
        if getattr(hb, "autopilot", None) != 12:
            return "foreign"
        ts = m.target_system or 1
        tc = m.target_component or 1
        # запрос версии: сначала MAV_CMD_REQUEST_MESSAGE (512, id 148) —
        # на него PX4 v1.15 отвечает стабильно; 520 — запасной вариант
        m.mav.command_long_send(ts, tc, 512, 0, 148, 0, 0, 0, 0, 0, 0)
        ver = m.recv_match(type='AUTOPILOT_VERSION', blocking=True, timeout=5)
        if ver is None:
            m.mav.command_long_send(ts, tc, 520, 0, 1, 0, 0, 0, 0, 0, 0)
            ver = m.recv_match(type='AUTOPILOT_VERSION', blocking=True,
                               timeout=5)
        want_git = parse_px4_git(fw_path)
        for cand in _git_candidates(ver):
            if want_git and cand[:7] and cand[:7] in want_git:
                return "same"
        return "px4"
    finally:
        try:
            m.close()
        except Exception:
            pass


def verify_firmware_running(cfg, fw_path, port=None):
    """После прошивки подтвердить, что плата загрузилась в PX4.
    Проверяет: порт вернулся → heartbeat → AUTOPILOT_VERSION (+сверка git)."""
    try:
        from pymavlink import mavutil
    except ImportError:
        print("  ⚠ pymavlink не установлен — проверяю только факт появления порта")
        return wait_port(20) is not None

    port = port or wait_port(20)
    if not port:
        print("  ✗ порт не появился после прошивки — плата не загрузилась")
        return False
    try:
        m = mavutil.mavlink_connection(port, baud=int(cfg.get("baud", "57600")))
    except Exception as e:
        print(f"  ✗ не удалось открыть порт {port}: {e}")
        return False

    hb = m.wait_heartbeat(timeout=20)
    if hb is None:
        print("  ✗ нет heartbeat — PX4 не поднялся (возможен boot-loop)")
        m.close()
        return False
    print("  ✓ heartbeat получен — PX4 запущен")

    # запрос AUTOPILOT_VERSION: сначала REQUEST_MESSAGE (512), затем 520
    ts, tc = m.target_system or 1, m.target_component or 1
    m.mav.command_long_send(ts, tc, 512, 0, 148, 0, 0, 0, 0, 0, 0)
    ver = m.recv_match(type='AUTOPILOT_VERSION', blocking=True, timeout=5)
    if ver is None:
        m.mav.command_long_send(ts, tc, 520, 0, 1, 0, 0, 0, 0, 0, 0)
        ver = m.recv_match(type='AUTOPILOT_VERSION', blocking=True, timeout=5)
    cands = _git_candidates(ver)
    if ver is not None:
        print(f"  ✓ AUTOPILOT_VERSION получен (git на плате: "
              f"{cands[0] if cands else '?'})")
    else:
        print("  ⚠ AUTOPILOT_VERSION не пришёл, но heartbeat есть — считаю запуск успешным")
    m.close()

    # мягкая сверка git-хэша с образом (не блокирует — формат хэша PX4 капризный)
    want_git = parse_px4_git(fw_path)
    if cands and want_git:
        if any(c[:7] and c[:7] in want_git for c in cands):
            print(f"  ✓ git-хэш совпал с образом ({want_git})")
        else:
            print(f"  ⚠ git на плате ({cands[0]}) не найден в образе ({want_git}) —")
            print("     убедитесь, что залит нужный .px4")
    return True


# MAV_PARAM_TYPE: целочисленные типы (PX4 реально использует INT32=6 и REAL32=9)
INT_TYPES = (1, 2, 3, 4, 5, 6, 7, 8)


def _i32_to_wire(i):
    """Упаковать int32 побайтово во float — byte-wise кодировка параметров PX4
    (так делает QGC: биты int кладутся в float-поле PARAM_SET как есть)."""
    import struct
    return struct.unpack('<f', struct.pack('<i', int(i)))[0]


def _wire_to_i32(f):
    import struct
    return struct.unpack('<i', struct.pack('<f', f))[0]


def _decode_param_value(wire_float, wire_type):
    """PARAM_VALUE от PX4: int-параметры приходят битами внутри float-поля."""
    return _wire_to_i32(wire_float) if wire_type in INT_TYPES else wire_float


def _drain_param_values(m):
    while m.recv_match(type='PARAM_VALUE', blocking=False):
        pass


def _recv_param_named(m, name, timeout):
    """Ждать PARAM_VALUE именно для параметра name. Вернуть msg или None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        msg = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=timeout)
        if msg is None:
            return None
        pid = msg.param_id
        if isinstance(pid, bytes):
            pid = pid.split(b"\x00")[0].decode('ascii', 'replace')
        if pid.rstrip("\x00") == name:
            return msg
    return None


def param_read(m, name, retries=3, timeout=1.0):
    """Прочитать один параметр (PARAM_REQUEST_READ).
    Вернуть (значение, mav_тип) или (None, None)."""
    name_pad = name.encode()[:16].ljust(16, b"\x00")
    for _ in range(retries):
        _drain_param_values(m)
        m.mav.param_request_read_send(m.target_system or 1, m.target_component or 1,
                                      name_pad, -1)
        msg = _recv_param_named(m, name, timeout)
        if msg is not None:
            return _decode_param_value(msg.param_value, msg.param_type), msg.param_type
    return None, None


def fetch_all_params(m, stall_s=5, hard_s=90):
    """Скачать все параметры одним потоком (PARAM_REQUEST_LIST), как QGC при
    подключении. Вернуть словарь {имя: (значение, mav_тип)}."""
    _drain_param_values(m)
    m.mav.param_request_list_send(m.target_system or 1, m.target_component or 1)
    got, total = {}, None
    t0 = last = time.time()
    while time.time() - last < stall_s and time.time() - t0 < hard_s:
        msg = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=1)
        if msg is None:
            continue
        last = time.time()
        pid = msg.param_id
        if isinstance(pid, bytes):
            pid = pid.split(b"\x00")[0].decode('ascii', 'replace')
        pid = pid.rstrip("\x00")
        got[pid] = (_decode_param_value(msg.param_value, msg.param_type), msg.param_type)
        if 0 < msg.param_count < 65535:
            total = msg.param_count
        if total and len(got) >= total:
            break
    return got


def _value_matches(want, got, wire_type):
    if wire_type in INT_TYPES:
        return int(got) == int(round(float(want)))
    return abs(got - float(want)) <= max(1e-4, abs(float(want)) * 1e-4)


def set_param_verified(m, name, value, ptype=None, retries=3, timeout=0.5):
    """Записать параметр и ПОДТВЕРДИТЬ чтением ответного PARAM_VALUE.

    ВАЖНО: PX4 ОТВЕРГАЕТ PARAM_SET, если заявленный MAVLink-тип не совпадает с
    типом параметра ('param types mismatch' в mavlink_parameters.cpp), причём
    молча — без ответного PARAM_VALUE. А int-параметры кодируются побайтово
    (union) во float-поле — как это делает QGC. Поэтому тип обязателен: берём
    его из файла параметров (ptype), а если не задан — читаем с платы.
    Возврат: ('ok'|'mismatch'|'noresp'|'toolong', прочитанное_значение_или_None)."""
    name_b = name.encode()
    if len(name_b) > 16:
        return ('toolong', None)
    name_pad = name_b.ljust(16, b"\x00")

    if ptype is None:
        _, ptype = param_read(m, name)
        if ptype is None:
            return ('noresp', None)

    if ptype in INT_TYPES:
        send_type, wire_val = 6, _i32_to_wire(round(float(value)))
    else:
        send_type, wire_val = 9, float(value)

    last = None
    for _ in range(retries):
        # выгрести старые PARAM_VALUE из очереди, чтобы не поймать чужой ответ
        _drain_param_values(m)
        m.mav.param_set_send(m.target_system or 1, m.target_component or 1,
                             name_pad, wire_val, send_type)
        msg = _recv_param_named(m, name, timeout)
        if msg is None:
            continue
        last = _decode_param_value(msg.param_value, msg.param_type)
        if _value_matches(value, last, msg.param_type):
            return ('ok', last)
    if last is None:
        return ('noresp', None)
    return ('mismatch', last)


# ── шаги ──────────────────────────────────────────────────────────────

def step_mass_erase(cfg):
    """Шаг 0: mass-erase всей flash (требуется DFU-режим).

    Отдельный «только стереть» шаг. В обычном потоке НЕ нужен: шаг 1 уже делает
    mass-erase перед записью загрузчика одной командой (флаг bl_mass_erase).
    Используйте его, когда надо просто стереть чип (например, заводской
    ArduPilot). После него — прошить загрузчик и прошивку заново.
    """
    print("\n" + "=" * 60)
    print("ШАГ 0 — mass-erase (полное стирание flash)")
    print("=" * 60)
    print("  ВНИМАНИЕ: стирается ВСЯ flash, включая загрузчик.")
    print("  После этого шага нужно заново прошить и загрузчик, и PX4.")

    state = detect_board_state()
    if state != "dfu":
        print("  Плата не в режиме DFU.")
        print("  >>> Зажмите BOOT, подключите USB, отпустите BOOT. <<<")
        input("  Нажмите Enter, когда готово...")
        state = detect_board_state()
        if state != "dfu":
            print("[ОШИБКА] DFU устройство не обнаружено.")
            return False

    print("  выполняю mass-erase...")
    addr = cfg.get("dfu_address", "0x08000000")
    if not dfu_download(addr, "/tmp/obrik_empty.bin", "mass-erase",
                        extra="mass-erase:force"):
        return False

    # верификация: регион flash должен читаться как 0xFF (стёрт)
    e = verify_dfu_erased(addr)
    if e is True:
        print("  ✓ проверка: flash стёрт (0xFF)")
    elif e is None:
        print("  ⚠ readback-проверку стирания выполнить не удалось (dfu-util)")
    else:
        print("  [ОШИБКА] flash НЕ стёрт после mass-erase — повторите")
        return False

    print("  Плата в режиме DFU — можно прошивать загрузчик и PX4.")
    return True


def step_flash_bootloader(cfg):
    """Шаг 1: прошить загрузчик через DFU."""
    bl = cfg["bootloader"]
    addr = cfg["dfu_address"]
    if not os.path.exists(bl):
        print(f"[ОШИБКА] загрузчик не найден: {bl}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 1 — прошивка загрузчика (DFU)")
    print("=" * 60)

    # прошиваем ВСЕГДА через DFU — у новой платы может стоять заводская
    # прошивка (ArduPilot/Betaflight), «запущена» не значит «загрузчик наш»
    state = detect_board_state()

    # платы нет — просим просто подключить, БЕЗ кнопки: что делать дальше,
    # решим после опроса (PX4 на плате → BOOT вообще не понадобится)
    if state == "none":
        print("  Плата не обнаружена.")
        print("  >>> Подключите плату по USB. Кнопку BOOT зажимать НЕ нужно —")
        print("  >>> если она понадобится, скрипт попросит отдельно.")
        input("  Нажмите Enter, когда плата подключена...")
        time.sleep(1.5)   # дать плате время загрузиться/определиться
        state = detect_board_state()

    if state == "running":
        # плата запущена: если на ней PX4 — загрузчик исправен (плата же
        # загрузилась), перепрошивать его незачем, BOOT не нужен
        print("  Плата запущена — проверяю по MAVLink, что на ней стоит...")
        kind = probe_running_px4(cfg, cfg["firmware"])
        # молчит — не вывод «чужая прошивка»: дать переткнуть и опросить ещё раз
        if kind == "silent":
            print("  ⚠ плата определяется по USB, но не отвечает по MAVLink")
            print("  >>> Переткните USB (кнопку BOOT НЕ нажимать) и подождите "
                  "~15 секунд, пока плата загрузится.")
            input("  Нажмите Enter, когда переткнули...")
            time.sleep(3)
            if detect_board_state() == "running":
                kind = probe_running_px4(cfg, cfg["firmware"])
        if kind == "same":
            print("  ✓ на плате уже наша прошивка PX4 (git-хэш совпал)")
            print("  ✓ плата с ней загружается — значит загрузчик исправен")
            print("  шаги 1–2 пропущены, BOOT-режим не нужен")
            cfg["_fw_already_ok"] = True
            return True
        if kind == "px4":
            print("  ✓ на плате PX4 (другая версия) — загрузчик исправен, "
                  "перепрошивать его не нужно")
            print("  прошивка обновится на шаге 2 через px_uploader — "
                  "BOOT-режим не нужен")
            cfg["_fw_px4_running"] = True
            return True
        if kind == "foreign":
            print("  На плате чужая прошивка (не PX4) — загрузчик "
                  "прошивается только через DFU.")
        else:  # silent и после переткивания
            print("  Плата так и не ответила по MAVLink. Возможно, прошивка "
                  "повреждена — перепрошьём через DFU (BOOT).")

    if state == "dfu":
        print("  Плата обнаружена в режиме DFU.")
    else:
        print("  >>> ОТКЛЮЧИТЕ плату от USB.")
        print("  >>> Зажмите кнопку BOOT на плате.")
        print("  >>> Подключите USB (держа BOOT).")
        print("  >>> Отпустите BOOT через 1-2 сек после подключения.")
        input("  Нажмите Enter, когда готово...")

        # re-detect after user action
        state = detect_board_state()
        if state != "dfu":
            print("[ОШИБКА] DFU устройство не обнаружено. Убедитесь, что BOOT зажат при подключении.")
            return False

    # если на плате уже стоит ровно этот загрузчик — прошивать нечего
    print("  сравниваю загрузчик на плате с файлом...")
    if verify_dfu_write(addr, bl) is True:
        print("  ✓ загрузчик на плате уже совпадает байт-в-байт — шаг пропущен")
        print("  Плата остаётся в режиме DFU — можно сразу прошивать PX4 (шаг 2).")
        return True

    # mass-erase + запись загрузчика ОДНОЙ DFU-командой — перезагрузка между
    # ними не нужна, плата остаётся в DFU. Управляется флагом bl_mass_erase
    # (по умолчанию вкл): чистый старт при каждой прошивке загрузчика.
    combined = str(cfg.get("bl_mass_erase", "1")).strip().lower() \
        not in ("0", "false", "no", "off", "")
    extra = "mass-erase:force" if combined else ""
    what = "загрузчик + mass-erase" if combined else "загрузчик"
    print(f"  прошиваю {what} → {addr} из {bl}")
    if not dfu_download(addr, bl, what, extra=extra):
        return False

    # верификация записи: читаем flash обратно и сверяем с файлом
    v = verify_dfu_write(addr, bl)
    if v is True:
        print("  ✓ проверка: загрузчик совпал байт-в-байт")
    elif v is None:
        print("  ⚠ readback-проверку выполнить не удалось (dfu-util) — полагаюсь на код записи")
    else:
        print("  [ОШИБКА] readback НЕ совпал с файлом загрузчика — запись повреждена, повторите")
        return False

    print("  Плата остаётся в режиме DFU — можно сразу прошивать PX4 (шаг 2).")
    return True


def _flash_via_uploader(cfg, fw):
    """Прошить .px4 через px_uploader БЕЗ кнопки BOOT — для плат, на которых
    уже запущен PX4: его загрузчик понимает протокол px_uploader, плата
    перезагружается в него командой по MAVLink."""
    tools = cfg.get("px4_tools", "tools")
    print("  На плате PX4 — прошиваю через px_uploader, кнопка BOOT не нужна.")
    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    port = wait_port(15)
    if not port:
        print("[ОШИБКА] полётник не обнаружен.")
        return False

    # перезагрузка в загрузчик по MAVLink: MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
    # param1=3 (reboot and keep in bootloader) — px_uploader подхватит плату
    try:
        m = get_mavlink(cfg, wait_s=10)
        if m is not None:
            print("  перезагружаю плату в загрузчик (MAVLink, без кнопки BOOT)...")
            m.mav.command_long_send(m.target_system or 1, m.target_component or 1,
                                    246, 0, 3, 0, 0, 0, 0, 0, 0)
            time.sleep(0.5)
    except Exception as e:
        print(f"  ⚠ команда перезагрузки не отправлена ({e}) — px_uploader перезагрузит сам")
    close_mavlink()  # освободить порт для px_uploader
    time.sleep(2)
    port = wait_port(15) or port  # порт мог переподняться под тем же именем

    uploader = os.path.join(tools, "px_uploader.py")
    if not os.path.exists(uploader):
        uploader = os.path.join(tools, "px4_uploader.py")
    if not os.path.exists(uploader):
        uploader = "px_uploader.py"

    print(f"  прошиваю: {fw}")
    cmd = f'python3 "{uploader}" --port "{port}" "{fw}"'
    result = subprocess.run(cmd, shell=True, timeout=180,
                            capture_output=True, text=True)
    print(result.stdout)
    combined = result.stdout + result.stderr
    if result.returncode == 0 and ("Reboot" in combined or "Success" in combined
                                   or "done" in combined.lower()):
        print("  ✓ образ записан и проверен загрузчиком (CRC)")
    else:
        print(f"  [ОШИБКА] px_uploader завершился с кодом {result.returncode}")
        if result.stderr:
            print(result.stderr)
        return False

    print("  Жду перезагрузки полётника (8 сек)...")
    time.sleep(8)
    print("  проверка: подключаюсь к новой прошивке по MAVLink...")
    if not verify_firmware_running(cfg, fw):
        print("  [ОШИБКА] прошивка залита, но плата не подтвердила запуск PX4.")
        return False
    print("  ✓ прошивка подтверждена — PX4 запущен")
    return True


def step_flash_firmware(cfg):
    """Шаг 2: прошить основную прошивку.

    Плата с PX4 (запущена) — px_uploader без кнопки BOOT; совпавший git —
    пропуск. Чужая прошивка или плата в DFU после шага 1 — через DFU."""
    fw = cfg["firmware"]
    app_addr = cfg.get("app_address", "0x08020000")

    if not os.path.exists(fw):
        print(f"[ОШИБКА] прошивка не найдена: {fw}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 2 — прошивка PX4 (DFU)")
    print("=" * 60)

    if cfg.get("_fw_already_ok"):
        print("  ✓ прошивка уже подтверждена по MAVLink (шаг 1) — пропущено")
        return True

    state = detect_board_state()
    if state == "running":
        if cfg.get("_fw_px4_running"):
            kind = "px4"            # шаг 1 уже опросил плату
        else:
            print("  Плата запущена — проверяю по MAVLink, что на ней стоит...")
            kind = probe_running_px4(cfg, fw)
        if kind == "same":
            print("  ✓ на плате уже наша прошивка PX4 (git-хэш совпал) "
                  "— шаг пропущен, BOOT-режим не нужен")
            return True
        if kind == "px4":
            return _flash_via_uploader(cfg, fw)
        if kind == "foreign":
            print("  На плате чужая прошивка (не PX4) — прошиваем только "
                  "через DFU.")
        else:
            print("  Плата не отвечает по MAVLink — прошиваем через DFU.")
    if state != "dfu":
        if state == "none":
            print("  Плата не обнаружена.")
        print("  >>> ОТКЛЮЧИТЕ плату от USB.")
        print("  >>> Зажмите кнопку BOOT, подключите USB, отпустите BOOT.")
        input("  Нажмите Enter, когда готово...")
        state = detect_board_state()
        if state != "dfu":
            print("[ОШИБКА] DFU устройство не обнаружено. "
                  "Убедитесь, что BOOT зажат при подключении.")
            return False

    print("  Плата в режиме DFU — прошиваю напрямую через dfu-util.")
    fw_bin = cfg.get("firmware_bin", "")
    if not fw_bin:
        fw_bin = fw.replace(".px4", ".bin")
    if not os.path.exists(fw_bin):
        print(f"[ОШИБКА] .bin прошивка не найдена: {fw_bin}")
        print(f"  Укажите firmware_bin в конфиге или положите .bin рядом с .px4.")
        return False

    # если на плате уже ровно эта прошивка — заливать не нужно
    print("  сравниваю прошивку на плате с файлом (до минуты)...")
    if verify_dfu_write(app_addr, fw_bin) is True:
        print("  ✓ прошивка на плате уже совпадает байт-в-байт — заливка пропущена")
    else:
        print(f"  прошиваю: {fw_bin}")
        print(f"  адрес: {app_addr}")
        if not dfu_download(app_addr, fw_bin, "прошивка"):
            return False
    print("\n  >>> ОТКЛЮЧИТЕ USB, затем подключите заново БЕЗ BOOT. <<<")
    print("  >>> Плата загрузится в PX4. <<<")
    input("  Нажмите Enter, когда плата переподключена и загрузилась...")
    print("  проверка: подключаюсь к новой прошивке по MAVLink...")
    if not verify_firmware_running(cfg, fw):
        print("  [ОШИБКА] прошивка залита, но плата не подтвердила запуск PX4.")
        return False
    print("  ✓ прошивка подтверждена — PX4 запущен")
    return True


def _strip_ansi(s):
    s = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', s)
    return s.replace('\r', '')


def step_beacon_delay(cfg):
    """Шаг 4: записать Beacon Delay = Infinite во все ESC.

    Работает через ТО ЖЕ MAVLink-соединение, что и шаг 3 (nsh поверх
    SERIAL_CONTROL, device 10) — отдельный процесс mavlink_shell.py и гонки
    за порт больше не нужны."""
    beacon_val = cfg.get("beacon_value", "5")
    num = int(cfg.get("num_motors", "4"))

    print("\n" + "=" * 60)
    print("ШАГ 4 — отключение писка регуляторов (Beacon Delay)")
    print("=" * 60)
    print("  Требуется подключённый АКБ (регуляторы должны быть под питанием).")
    print("  Полётник должен быть подключён по USB (QGC закроется автоматически).")

    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    try:
        from pymavlink import mavutil
    except ImportError:
        print("[ОШИБКА] pymavlink не установлен. Установите: pip install pymavlink")
        return False

    m = get_mavlink(cfg, wait_s=60)
    if m is None:
        print("[ОШИБКА] полётник не обнаружен по USB.")
        return False

    # проверка АКБ; если не видно — попросить подключить и проверить ещё раз
    print("  проверка АКБ...")
    v = battery_voltage(m)
    if v is None or v < 3.0:
        cur = "не определяется" if v is None else f"{v:.1f}V"
        print(f"  ⚠ напряжение АКБ: {cur}")
        try:
            input("  Подключите АКБ и нажмите Enter...")
        except EOFError:
            pass
        v = battery_voltage(m)
    if v is not None and v >= 3.0:
        print(f"  ✓ батарея: {v:.1f}V")
    else:
        print("  ⚠ напряжение не подтверждено — продолжаю, но ESC могут не ответить")

    print("  останавливаю dshot...")
    nsh_send(m, "dshot stop", timeout_s=2)
    time.sleep(3)  # первому ESC после stop нужна пауза побольше

    esc_done = [False] * num
    for esc in range(num):
        for attempt in range(1, 4):  # до 3 попыток на ESC
            out = _strip_ansi(nsh_send(
                m, f"dshot_4way beacon {esc} {beacon_val}", timeout_s=40,
                stop=[f"ESC {esc}: OK", f"ESC{esc}: OK", f"ESC {esc}: already",
                      "no bootloader", "FAILED"]))
            if (f"ESC {esc}: OK" in out or f"ESC{esc}: OK" in out
                    or f"ESC {esc}: already" in out):
                esc_done[esc] = True
                print(f"  ✓ ESC {esc} — Beacon Delay = {beacon_val}"
                      + (f" (попытка {attempt})" if attempt > 1 else ""))
                break
            if "no bootloader" in out:
                print(f"  ⚠ ESC {esc} — no bootloader, повтор...")
                time.sleep(1)
                continue
            print(f"  ✗ ESC {esc} — нет подтверждения (попытка {attempt})")
            for line in out.splitlines():
                s = line.strip()
                if s and any(kw in s for kw in ("ESC", "Beacon", "bootloader",
                                                "FAILED", "ERROR", "signature")):
                    print(f"    | {s[:120]}")
            time.sleep(1)
        if not esc_done[esc]:
            print(f"  ✗ ESC {esc} — не удалось записать (проверьте АКБ и повторите)")

    print("  запускаю dshot...")
    nsh_send(m, "dshot start", timeout_s=2)

    success_count = sum(esc_done)
    print(f"  готово: {success_count}/{num} ESC настроено")
    return success_count == num


def step_load_params(cfg):
    """Шаг 3: загрузить параметры в полётник через MAVLink (param_set)."""
    params = cfg.get("params_file", "")
    if not params or not os.path.exists(params):
        print(f"[ОШИБКА] файл параметров не найден: {params}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 3 — загрузка параметров (MAVLink param_set)")
    print("=" * 60)
    print(f"  Файл параметров: {params}")
    print("  Полётник должен быть подключён по USB (QGC закроется автоматически).")

    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    try:
        from pymavlink import mavutil
    except ImportError:
        print("[ОШИБКА] pymavlink не установлен.")
        return False

    # читаем файл параметров; поддерживаются оба формата:
    #   QGC:    vehicle_id<TAB>component_id<TAB>NAME<TAB>VALUE<TAB>TYPE
    #   legacy: NAME<TAB>VALUE[<TAB>TYPE]
    params_to_set = []
    with open(params) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("%"):
                continue
            parts = line.split()
            pname = sval = ptype = None
            if len(parts) >= 5 and parts[0].isdigit() and parts[1].isdigit() \
                    and parts[4].isdigit():
                pname, sval, ptype = parts[2], parts[3], int(parts[4])
            elif len(parts) >= 2:
                pname, sval = parts[0], parts[1]
                if len(parts) >= 3 and parts[2].isdigit():
                    ptype = int(parts[2])
            if not pname:
                continue
            try:
                pval = float(sval)
            except ValueError:
                print(f"  ⚠ строка пропущена (не число): {line[:60]}")
                continue
            params_to_set.append((pname, pval, ptype))

    print(f"  параметров к загрузке: {len(params_to_set)}")
    if not params_to_set:
        print("  [ПРЕДУПРЕЖДЕНИЕ] файл параметров пуст")
        return True

    m = get_mavlink(cfg, wait_s=30)
    if m is None:
        print("[ОШИБКА] полётник не обнаружен по USB.")
        return False
    time.sleep(1)  # дать параметрическому серверу PX4 подняться

    # снимок текущих параметров: пишем только отличающиеся + узнаём типы
    print("  читаю текущие параметры с платы...")
    onboard = fetch_all_params(m)
    print(f"  считано с платы: {len(onboard)}")

    total = len(params_to_set)
    ok_list, mismatch, noresp, toolong = [], [], [], []
    skipped_same = 0

    for idx, (pname, pval, ptype) in enumerate(params_to_set):
        cur = onboard.get(pname)
        if cur is not None and _value_matches(pval, cur[0], cur[1]):
            ok_list.append(pname)
            skipped_same += 1
        else:
            if ptype is None and cur is not None:
                ptype = cur[1]
            # каждый param_set подтверждается ответным PARAM_VALUE (до 3 попыток)
            status, readback = set_param_verified(m, pname, pval, ptype)
            if status == 'ok':
                ok_list.append(pname)
            elif status == 'mismatch':
                mismatch.append((pname, pval, readback))
            elif status == 'toolong':
                toolong.append(pname)
            else:  # noresp
                noresp.append(pname)

        if idx % 50 == 49 or idx == total - 1:
            print(f"  прогресс {idx + 1}/{total}: "
                  f"ok={len(ok_list)} (без изменений {skipped_same}) "
                  f"mismatch={len(mismatch)} нет_ответа={len(noresp)}")

    # сохранить параметры в flash. PX4 и сам автосохраняет изменённые параметры
    # (autosave), 'param save' здесь — дублирующая страховка, поэтому пустой
    # ответ nsh (SERIAL_CONTROL на этой связке глючит) не считается ошибкой.
    print("  сохраняю параметры в flash (param save)...")
    save_out = nsh_send(m, "param save", timeout_s=6)
    time.sleep(2)
    if not save_out.strip():
        print("  ⚠ nsh не ответил на 'param save' — полагаюсь на автосохранение PX4")
        save_ok = True
    else:
        save_ok = not any(w in save_out.lower() for w in ("fail", "error", "invalid", "no such"))
        if save_ok:
            print("  ✓ param save выполнен")
        else:
            print("  ✗ param save вернул ошибку:")
            for line in save_out.splitlines():
                if line.strip():
                    print(f"    | {line.strip()[:130]}")

    # ── контрольная перезагрузка: доказать, что параметры СОХРАНИЛИСЬ ──
    print("  перезагружаю полётник и сверяю параметры после рестарта...")
    m.mav.command_long_send(m.target_system or 1, m.target_component or 1,
                            246, 0, 1, 0, 0, 0, 0, 0, 0)  # MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN
    close_mavlink()
    time.sleep(4)  # даём порту пропасть
    persist_ok = None
    m = get_mavlink(cfg, wait_s=40)
    if m is None:
        print("  ⚠ порт не вернулся после перезагрузки — переподключите USB и "
              "проверьте параметры в QGC вручную")
    else:
        try:
            time.sleep(1)
            after = fetch_all_params(m)
            print(f"  считано после перезагрузки: {len(after)}")
            confirmed = set(ok_list)
            bad = []
            skipped_auto = 0
            for pname, pval, ptype in params_to_set:
                if pname not in confirmed:
                    continue  # записать не удалось — сохранение не проверяем
                # PX4 пересчитывает смещения гироскопа при каждой загрузке —
                # после ребута они закономерно отличаются, это не ошибка
                if re.fullmatch(r"CAL_GYRO\d+_[XYZ]OFF", pname):
                    skipped_auto += 1
                    continue
                entry = after.get(pname)
                if entry is None:
                    val, ft = param_read(m, pname)  # добор потерянных в потоке
                    entry = (val, ft) if val is not None else None
                if entry is None:
                    bad.append((pname, pval, "нет ответа"))
                elif not _value_matches(pval, entry[0], entry[1]):
                    bad.append((pname, pval, entry[0]))
            # соединение НЕ закрываем — шаг 4 (beacon) использует тот же канал
            persist_ok = not bad
            if skipped_auto:
                print(f"  (не сверяю {skipped_auto} гиросмещений CAL_GYRO*OFF — "
                      f"PX4 пересчитывает их сам при каждой загрузке)")
            if persist_ok:
                print(f"  ✓ после перезагрузки все {len(confirmed)} записанных "
                      f"параметров на месте — сохранение подтверждено")
            else:
                print(f"  ✗ после перезагрузки НЕ совпало: {len(bad)} — "
                      f"параметры НЕ сохранились корректно")
                for pname, want, got in bad[:15]:
                    print(f"      {pname}: хотели {want}, на плате {got}")
                if len(bad) > 15:
                    print(f"      ... ещё {len(bad) - 15}")
        except Exception as e:
            print(f"  ⚠ сверка после перезагрузки не удалась: {e}")

    # ── отчёт ──
    print(f"\n  подтверждено (readback): {len(ok_list)}/{total} "
          f"(из них уже были верными: {skipped_same})")
    if mismatch:
        print(f"  ✗ значение НЕ совпало после записи: {len(mismatch)}")
        for pname, want, got in mismatch[:15]:
            print(f"      {pname}: хотели {want}, на плате {got}")
        if len(mismatch) > 15:
            print(f"      ... ещё {len(mismatch) - 15}")
    if noresp:
        print(f"  ⚠ без ответа (нет в этой прошивке или сбой связи): {len(noresp)}")
        for pname in noresp[:15]:
            print(f"      {pname}")
        if len(noresp) > 15:
            print(f"      ... ещё {len(noresp) - 15}")
    if toolong:
        print(f"  ⚠ пропущены (имя >16 символов): {len(toolong)} — {', '.join(toolong[:10])}")

    all_ok = save_ok and not mismatch and not noresp and not toolong \
        and persist_ok is not False
    if all_ok:
        print(f"  ✓ все {total} параметров записаны"
              + (" и подтверждены после перезагрузки" if persist_ok else
                 " (сохранение после перезагрузки проверьте вручную)"))
    else:
        print("  ⚠ не все параметры подтверждены — см. список выше")
    return all_ok


# ── dry-run ─────────────────────────────────────────────────────────────

def dry_run_checks(cfg):
    """Проверить файлы и зависимости, не выполняя прошивку."""
    print("=" * 60)
    print("DRY-RUN — проверка файлов и зависимостей")
    print("=" * 60)
    ok = True

    checks = [
        ("bootloader", cfg.get("bootloader", "")),
        ("firmware", cfg.get("firmware", "")),
        ("firmware_bin", cfg.get("firmware_bin", "") or cfg.get("firmware", "").replace(".px4", ".bin")),
        ("params_file", cfg.get("params_file", "")),
    ]
    for name, path in checks:
        if not path:
            print(f"  ✗ {name}: не указан в конфиге")
            ok = False
        elif os.path.exists(path):
            size = os.path.getsize(path)
            print(f"  ✓ {name}: {path} ({size:,} B)")
        else:
            print(f"  ✗ {name}: ФАЙЛ НЕ НАЙДЕН — {path}")
            ok = False

    import shutil
    if shutil.which("dfu-util"):
        print(f"  ✓ dfu-util: {shutil.which('dfu-util')}")
    else:
        print("  ✗ dfu-util: не установлен")
        ok = False

    for mod in [("pymavlink", "mavutil"), ("serial", None)]:
        try:
            __import__(mod[0])
            print(f"  ✓ python-{mod[0]}: OK")
        except ImportError:
            print(f"  ✗ python-{mod[0]}: не установлен (pip install {mod[0]})")
            ok = False

    print(f"\n  итог dry-run: {'✓ всё OK' if ok else '✗ есть проблемы'}")
    return ok


# ── main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="obrik_flash — прошивка и настройка дрона одной командой")
    parser.add_argument("--config", "-c", default=None,
                        help="путь к конфиг-файлу (по умолчанию ищет obrik_flash.cfg рядом со скриптом)")
    parser.add_argument("--steps", "-s", default="1,2,3,4",
                        help="шаги: 0=erase, 1=загрузчик, 2=прошивка, 3=параметры, 4=beacon. Или: all, fw, erase, beacon, params")
    parser.add_argument("--list", action="store_true",
                        help="показать текущий конфиг и выйти")
    parser.add_argument("--dry-run", action="store_true",
                        help="проверить файлы и зависимости без прошивки")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.list:
        print("Текущий конфиг:")
        for k in sorted(cfg):
            print(f"  {k} = {cfg[k]}")
        return

    if args.dry_run:
        dry_run_checks(cfg)
        return

    # создать пустой файл для mass-erase (нужен dfu-util для запуска)
    subprocess.run("dd if=/dev/zero of=/tmp/obrik_empty.bin bs=1 count=1 2>/dev/null",
                   shell=True)

    # разобрать --steps
    steps = args.steps.lower()
    if steps == "all":
        do = {1, 2, 3, 4}
    elif steps == "fw":
        do = {1, 2}
    elif steps == "beacon":
        do = {4}
    elif steps == "params":
        do = {3}
    elif steps == "erase":
        do = {0}
    else:
        do = set()
        for s in steps.split(","):
            s = s.strip()
            if s.isdigit():
                do.add(int(s))

    # перед шагами прошивки — проверить git исходников и пересобрать при
    # обновлениях (если в конфиге задан px4_src)
    if do & {1, 2}:
        if not ensure_firmware_fresh(cfg):
            print("\n[ОШИБКА] подготовка прошивки не удалась — прошивка отменена.")
            return

    success = True
    results = {}

    for step_num in sorted(do):
        if step_num == 0:
            results[0] = step_mass_erase(cfg)
        elif step_num == 1:
            results[1] = step_flash_bootloader(cfg)
        elif step_num == 2:
            results[2] = step_flash_firmware(cfg)
        elif step_num == 3:
            results[3] = step_load_params(cfg)
        elif step_num == 4:
            results[4] = step_beacon_delay(cfg)
        else:
            print(f"неизвестный шаг: {step_num}")
            continue

        if not results[step_num]:
            print(f"\n  ⚠ шаг {step_num} завершился с ошибкой.")
            try:
                ans = input("  Продолжить? [Y/n]: ").strip().lower()
                if ans == "n":
                    success = False
                    break
            except EOFError:
                # if stdin is closed (e.g. piped from `echo y | ...`),
                # default to continuing (same as pressing Enter)
                pass

    close_mavlink()

    print("\n" + "=" * 60)
    print("ИТОГ")
    names = {0: "mass-erase", 1: "загрузчик", 2: "прошивка", 3: "параметры", 4: "Beacon Delay"}
    all_ok = True
    for step_num in sorted(do):
        r = results.get(step_num, "пропущен")
        ok = r is True
        if not ok:
            all_ok = False
        print(f"  шаг {step_num} ({names.get(step_num, '?')}): {'✓ OK' if ok else '✗ ошибка'}")
    print(f"  итог: {'успешно' if all_ok else 'есть ошибки'}")

if __name__ == "__main__":
    main()

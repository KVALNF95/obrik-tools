#!/usr/bin/env python3
"""
obrik_flash.py — утилита одной командой для прошивки и настройки дрона «Обрик».

Что делает:
  0. Mass-erase (DFU) — полное стирание flash (для проблемных плат)
  1. Прошивает загрузчик (DFU) — требуется нажать кнопку BOOT
  2. Прошивает основную прошивку PX4 (DFU или px_uploader)
  3. Загружает параметры в полётник (через MAVLink param_set)
  4. Записывает Beacon Delay = Infinite во все ESC (требуется АКБ)

Шаг 2 автоматически выбирает способ прошивки:
  - Если плата в DFU — прошивает .bin напрямую через dfu-util
  - Если плата запущена — использует px_uploader.py (старый метод)

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


def nsh_send(m, cmd, timeout_s=6):
    """Отправить команду в nsh через SERIAL_CONTROL и вернуть вывод."""
    data = (cmd + "\n").encode()
    pad = data + b"\x00" * (70 - len(data))
    m.mav.serial_control_send(
        0,       # SERIAL_CONTROL_DEV_SHELL
        1,       # flags: SERIAL_CONTROL_FLAG_RESPOND (обязательно, иначе nsh не вернёт вывод)
        0, 0,    # timeout, baudrate
        len(data), pad)
    t0 = time.time()
    out = b""
    while time.time() - t0 < timeout_s:
        msg = m.recv_match(type='SERIAL_CONTROL', blocking=True, timeout=1)
        if msg is None:
            continue
        out += bytes(msg.data[:msg.count])
    return out.decode("ascii", "replace")


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


def parse_px4_git(fw_path):
    """Достать git_identity/version из .px4 (JSON), либо None."""
    try:
        with open(fw_path) as f:
            data = json.load(f)
        return data.get("git_identity") or data.get("version")
    except Exception:
        return None


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

    # запрос AUTOPILOT_VERSION (MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES = 520)
    m.mav.command_long_send(m.target_system or 1, m.target_component or 1,
                            520, 0, 1, 0, 0, 0, 0, 0, 0)
    ver = m.recv_match(type='AUTOPILOT_VERSION', blocking=True, timeout=5)
    running_git = None
    if ver is not None:
        try:
            running_git = bytes(ver.flight_custom_version).split(b'\x00')[0]\
                .decode('ascii', 'replace').strip()
        except Exception:
            running_git = None
        print(f"  ✓ AUTOPILOT_VERSION получен (git на плате: {running_git or '?'})")
    else:
        print("  ⚠ AUTOPILOT_VERSION не пришёл, но heartbeat есть — считаю запуск успешным")
    m.close()

    # мягкая сверка git-хэша с образом (не блокирует — формат хэша PX4 капризный)
    want_git = parse_px4_git(fw_path)
    if running_git and want_git:
        if running_git[:6] and running_git[:6] in want_git:
            print(f"  ✓ git-хэш совпал с образом ({running_git})")
        else:
            print(f"  ⚠ git на плате ({running_git}) не найден в образе ({want_git}) —")
            print("     убедитесь, что залит нужный .px4")
    return True


def set_param_verified(m, name, value, retries=3, timeout=0.5):
    """Записать параметр и ПОДТВЕРДИТЬ чтением ответного PARAM_VALUE.
    PX4 на каждый PARAM_SET отвечает PARAM_VALUE с фактическим значением.
    Возврат: ('ok'|'mismatch'|'noresp'|'toolong', прочитанное_значение_или_None)."""
    name_b = name.encode()
    if len(name_b) > 16:
        return ('toolong', None)
    name_pad = name_b.ljust(16, b"\x00")
    last = None

    for _ in range(retries):
        # выгрести старые PARAM_VALUE из очереди, чтобы не поймать чужой ответ
        while m.recv_match(type='PARAM_VALUE', blocking=False):
            pass
        # REAL32 (9): PX4 сам приведёт значение к реальному типу параметра
        m.mav.param_set_send(m.target_system or 1, m.target_component or 1,
                             name_pad, float(value), 9)
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=timeout)
            if msg is None:
                break
            pid = msg.param_id
            if isinstance(pid, bytes):
                pid = pid.split(b"\x00")[0].decode('ascii', 'replace')
            pid = pid.rstrip("\x00")
            if pid != name:
                continue  # ответ на другой параметр — ждём дальше
            last = msg.param_value
            if msg.param_type in (9, 10):  # REAL32 / REAL64 — сравнение с допуском
                if abs(msg.param_value - float(value)) <= max(1e-4, abs(float(value)) * 1e-4):
                    return ('ok', msg.param_value)
            else:  # целочисленные типы — сравнение по округлению
                if round(msg.param_value) == round(float(value)):
                    return ('ok', msg.param_value)
            break  # значение получено, но не совпало — новая попытка
    if last is None:
        return ('noresp', None)
    return ('mismatch', last)


# ── шаги ──────────────────────────────────────────────────────────────

def step_mass_erase(cfg):
    """Шаг 0: mass-erase всей flash (требуется DFU-режим).

    Стирает ВСЁ — и загрузчик, и прошивку. Нужен когда плата не перепрошивается
    обычным способом (например, при заводском ArduPilot).
    После mass-erase обязательно прошить загрузчик и прошивку заново.
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
    result = subprocess.run(
        f'dfu-util -a 0 -s {addr}:mass-erase:force -D /tmp/obrik_empty.bin',
        shell=True, capture_output=True, text=True, timeout=120
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"  [ОШИБКА] mass-erase завершился с кодом {result.returncode}")
        if result.stderr:
            print(result.stderr)
        return False
    print("  ✓ mass-erase выполнен")

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

    # проверить, не в DFU ли плата уже
    state = detect_board_state()
    if state == "running":
        print("  Плата уже запущена с прошивкой (ttyACM найден).")
        print("  Загрузчик уже прошит — шаг 1 пропускается.")
        return True

    if state == "none":
        print("  Плата не обнаружена.")
        print("  >>> ОТКЛЮЧИТЕ плату от USB.")
        print("  >>> Зажмите кнопку BOOT на плате.")
        print("  >>> Подключите USB (держа BOOT).")
        print("  >>> Отпустите BOOT через 1-2 сек после подключения.")

    else:  # state == "dfu"
        print("  Плата обнаружена в режиме DFU.")
    input("  Нажмите Enter, когда готово...")

    # re-detect after user action
    state = detect_board_state()
    if state != "dfu":
        print("[ОШИБКА] DFU устройство не обнаружено. Убедитесь, что BOOT зажат при подключении.")
        return False

    print(f"  прошиваю загрузчик → {addr} из {bl}")
    result = subprocess.run(
        f'dfu-util -a 0 --dfuse-address {addr} -D "{bl}"',
        shell=True, capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"  [ОШИБКА] dfu-util завершился с кодом {result.returncode}")
        if result.stderr:
            print(result.stderr)
        return False
    print("  ✓ загрузчик прошит")

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


def step_flash_firmware(cfg):
    """Шаг 2: прошить основную прошивку (DFU или px_uploader)."""
    fw = cfg["firmware"]
    tools = cfg["px4_tools"]
    app_addr = cfg.get("app_address", "0x08020000")

    if not os.path.exists(fw):
        print(f"[ОШИБКА] прошивка не найдена: {fw}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 2 — прошивка PX4")
    print("=" * 60)

    state = detect_board_state()

    # ── DFU-режим: прошиваем .bin напрямую через dfu-util ──
    if state == "dfu":
        print("  Плата в режиме DFU — прошиваю напрямую через dfu-util.")
        fw_bin = cfg.get("firmware_bin", "")
        if not fw_bin:
            fw_bin = fw.replace(".px4", ".bin")
        if not os.path.exists(fw_bin):
            print(f"[ОШИБКА] .bin прошивка не найдена: {fw_bin}")
            print(f"  Укажите firmware_bin в конфиге или положите .bin рядом с .px4.")
            return False

        print(f"  прошиваю: {fw_bin}")
        print(f"  адрес: {app_addr}")
        cmd = f'dfu-util -a 0 --dfuse-address {app_addr} -D "{fw_bin}"'
        result = subprocess.run(cmd, shell=True, timeout=300)
        if result.returncode == 0:
            print("  ✓ прошивка залита")
            print("\n  >>> ОТКЛЮЧИТЕ USB, затем подключите заново БЕЗ BOOT. <<<")
            print("  >>> Плата загрузится в PX4. <<<")
            input("  Нажмите Enter, когда плата переподключена и загрузилась...")
            print("  проверка: подключаюсь к новой прошивке по MAVLink...")
            if not verify_firmware_running(cfg, fw):
                print("  [ОШИБКА] прошивка залита, но плата не подтвердила запуск PX4.")
                return False
            print("  ✓ прошивка подтверждена — PX4 запущен")
            return True
        else:
            print(f"  [ОШИБКА] dfu-util завершился с кодом {result.returncode}")
            return False

    # ── Нет платы ──
    if state == "none":
        print("  Плата не обнаружена.")
        print("  >>> Подключите полётник по USB (кнопку BOOT НЕ нажимать).")
        print("  >>> Или запустите с BOOT для прошивки через DFU после загрузчика.")
        return False

    # ── Плата запущена (running): старый метод через px_uploader ──
    print("  Плата подключена и работает.")
    print("  Закройте QGroundControl (если открыт).")
    input("  Нажмите Enter, когда готово...")

    port = wait_port(15)
    if not port:
        print("[ОШИБКА] полётник не обнаружен.")
        return False

    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    uploader = os.path.join(tools, "px_uploader.py")
    if not os.path.exists(uploader):
        uploader = os.path.join(tools, "px4_uploader.py")
    if not os.path.exists(uploader):
        uploader = "px_uploader.py"

    print(f"  прошиваю: {fw}")
    cmd = f'python3 "{uploader}" --port "{port}" "{fw}"'
    result = subprocess.run(cmd, shell=True, timeout=120,
                            capture_output=True, text=True)
    print(result.stdout)
    combined = result.stdout + result.stderr
    if result.returncode == 0 and ("Reboot" in combined or "Success" in combined or "done" in combined.lower()):
        print("  ✓ образ записан и проверен загрузчиком (CRC)")
    else:
        print(f"  [ОШИБКА] px_uploader завершился с кодом {result.returncode}")
        if result.stdout:
            print(result.stdout)
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


def step_beacon_delay(cfg):
    """Шаг 4: записать Beacon Delay = Infinite во все ESC."""
    beacon_val = cfg.get("beacon_value", "5")
    num = int(cfg.get("num_motors", "4"))

    print("\n" + "=" * 60)
    print("ШАГ 4 — отключение писка регуляторов (Beacon Delay)")
    print("=" * 60)
    print("  Требуется подключённый АКБ (регуляторы должны быть под питанием).")
    print("  Полётник должен быть подключён по USB (питание).")
    print("  Закройте QGroundControl (если открыт).")

    input("  Нажмите Enter, когда АКБ и USB подключены...")

    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    print("  ожидание полётника (USB)...")
    port = wait_port(timeout_s=60)
    if not port:
        print("[ОШИБКА] полётник не обнаружен по USB.")
        return False

    try:
        from pymavlink import mavutil
    except ImportError:
        print("[ОШИБКА] pymavlink не установлен. Установите: pip install pymavlink")
        return False

    m = mavlink_connect(port, int(cfg.get("baud", "57600")))

    # проверка АКБ — предупреждение, не блокировка
    print("  проверка АКБ...")
    v = battery_voltage(m)
    if v is not None and v >= 3.0:
        print(f"  ✓ батарея: {v:.1f}V")
    elif v is not None:
        print(f"  ⚠ напряжение батареи: {v:.1f}V — низкое, но продолжаю")
    else:
        print("  ⚠ не удалось определить напряжение — убедитесь, что АКБ подключён")

    m.close()  # разрыв MAVLink-соединения — освобождаем порт для mavlink_shell

    # beacon пишем через mavlink_shell.py (SERIAL_CONTROL глючит)
    tools = cfg.get("px4_tools", "Tools")
    shell = os.path.join(tools, "mavlink_shell.py")
    if not os.path.exists(shell):
        # попробовать найти рядом со скриптом
        for prefix in ["", os.path.expanduser("~") + "/Documents/Applications/px4/PX4-Autopilot/"]:
            candidate = os.path.join(prefix, "Tools", "mavlink_shell.py")
            if os.path.exists(candidate):
                shell = candidate
                break

    if not os.path.exists(shell):
        print(f"[ОШИБКА] mavlink_shell.py не найден: {shell}")
        return False

    # строим список команд с повторами при "no bootloader"
    commands = ["dshot stop"]
    # первый ESC после stop требует паузы побольше (3 сек)
    commands.append("__PAUSE_3__")

    for esc in range(num):
        for attempt in range(1, 4):  # до 3 попыток на ESC
            commands.append(f"dshot_4way beacon {esc} {beacon_val}")
            if attempt < 3:
                commands.append(f"__RETRY_{esc}__")  # маркер: проверить и повторить
    commands.append("dshot start")

    print(f"  запускаю beacon через mavlink_shell ({port})...")

    import select
    proc = subprocess.Popen(
        ["python3", shell, port],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    all_output = ""
    esc_done = [False] * num  # какие ESC уже подтверждены

    for idx, cmd in enumerate(commands):
        if cmd.startswith("__PAUSE_"):
            time.sleep(3)
            continue
        if cmd.startswith("__RETRY_"):
            esc_n = int(cmd.replace("__RETRY_", "").replace("__", ""))
            if esc_done[esc_n]:
                continue  # уже OK, пропускаем повтор
            if "no bootloader" not in all_output.split(f"beacon {esc_n}")[-1] if len(all_output.split(f"beacon {esc_n}")) > 1 else True:
                continue  # нет ошибки bootloader — не повторяем
            print(f"    повтор ESC {esc_n} (no bootloader)...")

        proc.stdin.write((cmd + "\n").encode())
        proc.stdin.flush()

        if cmd.startswith("dshot_4way beacon"):
            esc_n = int(cmd.split()[-2])
            t0 = time.time()
            while time.time() - t0 < 40:
                if proc.poll() is not None:
                    break
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if r:
                    try:
                        chunk = proc.stdout.read(4096)
                        if not chunk:
                            break
                        all_output += chunk.decode("ascii", "replace")
                    except Exception:
                        break
                if f"ESC {esc_n}: OK" in all_output or f"ESC {esc_n}: already" in all_output:
                    esc_done[esc_n] = True
                    print(f"  ✓ ESC {esc_n} — готово")
                    break
                if "no bootloader" in all_output and f"beacon {esc_n}" in all_output:
                    print(f"  ⚠ ESC {esc_n} — no bootloader, будет повтор")
                    break
            else:
                print(f"  ? ESC {esc_n} — таймаут ({'OK' if esc_done[esc_n] else 'не подтверждён'})")
        elif cmd == "dshot stop":
            time.sleep(1)
        elif cmd == "dshot start":
            pass  # финальная команда, не ждём

    time.sleep(2)
    # дочитать остаток
    try:
        leftover = proc.stdout.read(8192)
        if leftover:
            all_output += leftover.decode("ascii", "replace")
    except Exception:
        pass
    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()

    out = all_output
    # удалить ANSI-escape последовательности
    import re
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    out = re.sub(r'\x1b\[\?25[hl]', '', out)
    out = re.sub(r'\r', '', out)

    success_count = 0
    for esc in range(num):
        # ищем "ESC N: OK, Beacon Delay" в любом месте вывода
        ok_patterns = [
            f"ESC {esc}: OK, Beacon Delay",
            f"ESC {esc}: already set",
            f"ESC{esc}: OK, Beacon Delay",
        ]
        found_ok = any(p in out for p in ok_patterns)
        no_resp = f"dshot_4way beacon {esc}" in out and "bootloader" in out

        if found_ok:
            print(f"  ✓ ESC {esc} — Beacon Delay = {beacon_val}")
            success_count += 1
        elif no_resp:
            print(f"  ⚠ ESC {esc} — no bootloader response (попробуйте ещё раз)")
        else:
            print(f"  ✗ ESC {esc} — не удалось записать (проверьте АКБ и повторите)")

    # покажем ключевые строки
    for line in out.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(kw in stripped for kw in ["ESC", "Beacon", "bootloader", "connected", "OK", "FAILED", "signature"]):
            print(f"    | {stripped[:130]}")

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
    print("  Полётник должен быть подключён по USB.")
    print("  Закройте QGroundControl (если открыт).")

    input("  Нажмите Enter, когда готово...")

    subprocess.run("pkill -9 -f QGroundControl 2>/dev/null", shell=True)
    time.sleep(1)

    port = wait_port(15)
    if not port:
        print("[ОШИБКА] полётник не обнаружен по USB.")
        return False

    try:
        from pymavlink import mavutil
    except ImportError:
        print("[ОШИБКА] pymavlink не установлен.")
        return False

    # читаем файл параметров (формат: имя<таб>значение)
    params_to_set = []
    with open(params) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("%"):
                continue
            # формат: NAME\tVALUE или NAME\tVALUE\tTYPE
            parts = line.split("\t")
            if len(parts) >= 2:
                pname = parts[0].strip()
                try:
                    pval = float(parts[1].strip())
                except ValueError:
                    pval = 0.0
                params_to_set.append((pname, pval))

    print(f"  параметров к загрузке: {len(params_to_set)}")
    if not params_to_set:
        print("  [ПРЕДУПРЕЖДЕНИЕ] файл параметров пуст")
        return True

    print(f"  порт: {port}")
    m = mavlink_connect(port, int(cfg.get("baud", "57600")))
    time.sleep(1)  # дать параметрическому серверу PX4 подняться

    total = len(params_to_set)
    ok_list, mismatch, noresp, toolong = [], [], [], []

    for idx, (pname, pval) in enumerate(params_to_set):
        # каждый param_set подтверждается ответным PARAM_VALUE (до 3 попыток)
        status, readback = set_param_verified(m, pname, pval)
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
                  f"ok={len(ok_list)} mismatch={len(mismatch)} нет_ответа={len(noresp)}")

    # сохранить параметры в flash и проверить, что save прошёл без ошибки
    print("  сохраняю параметры в flash (param save)...")
    save_out = nsh_send(m, "param save", timeout_s=6)
    time.sleep(2)
    save_ok = not any(w in save_out.lower() for w in ("fail", "error", "invalid", "no such"))
    if save_ok:
        print("  ✓ param save выполнен")
    else:
        print("  ✗ param save вернул ошибку:")
        for line in save_out.splitlines():
            if line.strip():
                print(f"    | {line.strip()[:130]}")

    m.close()

    # ── отчёт ──
    print(f"\n  подтверждено (readback): {len(ok_list)}/{total}")
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

    all_ok = save_ok and not mismatch and not noresp and not toolong
    print("  >>> ОТКЛЮЧИТЕ полётник от питания и подключите заново (перезагрузка). <<<")
    if all_ok:
        print(f"  ✓ все {total} параметров записаны и подтверждены")
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

    tools_dir = cfg.get("px4_tools", "")
    if not tools_dir or not os.path.isdir(tools_dir):
        print(f"  ✗ px4_tools: директория не найдена — {tools_dir}")
        ok = False
    else:
        for tool in ["px_uploader.py", "mavlink_shell.py"]:
            tp = os.path.join(tools_dir, tool)
            if os.path.exists(tp):
                print(f"  ✓ {tool}: {tp}")
            else:
                print(f"  ✗ {tool}: не найден в {tools_dir}")
                ok = False

    import shutil
    for dep in ["dfu-util", "arm-none-eabi-gcc"]:
        if shutil.which(dep):
            print(f"  ✓ {dep}: {shutil.which(dep)}")
        else:
            print(f"  ✗ {dep}: не установлен")
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

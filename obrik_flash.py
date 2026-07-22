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

import os, sys, time, re, glob, subprocess, argparse, struct, tempfile, threading, queue

# ── конфиг по умолчанию ──────────────────────────────────────────────
DEFAULT_CONFIG = {
    "bootloader":   "",
    "firmware":     "",
    "firmware_bin": "",
    "params_file":  "",
    "px4_tools":    "",
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
    try:
        result = subprocess.run(["dfu-util", "-l"], capture_output=True, text=True)
        out = result.stdout + result.stderr
    except FileNotFoundError:
        out = ""
    if re.search(r'Found DFU:\s*\[0483:', out) or re.search(r'0483.*df11', out, re.I):
        return "dfu"

    # USB serial-порт PX4?
    ports = serial_ports()
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
        config_dir = os.path.dirname(os.path.abspath(path))
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    key = k.strip()
                    value = os.path.expanduser(v.strip())
                    if key in {"bootloader", "firmware", "firmware_bin", "params_file", "px4_tools"} and value and not os.path.isabs(value):
                        value = os.path.normpath(os.path.join(config_dir, value))
                    cfg[key] = value
    return cfg


def serial_ports():
    """Список PX4 USB-портов на Windows, Linux и macOS."""
    patterns = [
        "/dev/ttyACM*", "/dev/serial/by-id/usb-Matek*",
        "/dev/cu.usbmodem*", "/dev/tty.usbmodem*",
    ]
    ports = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))
    try:
        from serial.tools import list_ports
        for port in list_ports.comports():
            text = " ".join(filter(None, (port.description, port.manufacturer, port.product))).lower()
            if port.vid in (0x1209, 0x26AC) or any(name in text for name in ("matek", "px4", "autopilot")):
                ports.append(port.device)
    except ImportError:
        pass
    return sorted(set(ports), key=lambda p: ("/dev/cu." not in p, p))


def close_qgroundcontrol():
    """Освободить serial-порт QGroundControl на Windows/POSIX."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/IM", "QGroundControl.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-9", "-f", "QGroundControl"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ProcessOutputReader:
    """Неблокирующее чтение stdout subprocess, включая Windows pipes."""
    def __init__(self, proc):
        self.proc = proc
        self.chunks = queue.Queue()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while True:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                break
            self.chunks.put(chunk)

    def collect(self, timeout, idle_timeout=1.0):
        output = ""
        deadline = time.time() + timeout
        last_data = time.time()
        while time.time() < deadline:
            wait = min(0.25, max(0.0, deadline - time.time()))
            try:
                chunk = self.chunks.get(timeout=wait)
                output += chunk.decode("ascii", "replace")
                last_data = time.time()
            except queue.Empty:
                if output and time.time() - last_data > idle_timeout:
                    break
                if self.proc.poll() is not None and self.chunks.empty():
                    break
        return output


def find_tty():
    """Вернуть первый USB serial-порт PX4 или None."""
    ports = serial_ports()
    return ports[0] if ports else None


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
    heartbeat = m.wait_heartbeat(timeout=15)
    if heartbeat is None:
        m.close()
        raise RuntimeError(f"heartbeat не получен на {port}")
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


def shell_tool(cfg):
    path = os.path.join(cfg.get("px4_tools", "tools"), "mavlink_shell.py")
    return path if os.path.exists(path) else None


def run_mavlink_shell(cfg, port, commands, timeout_per_command=8):
    """Выполнить NSH-команды штатным mavlink_shell и вернуть полный вывод."""
    shell = shell_tool(cfg)
    if not shell:
        raise RuntimeError("mavlink_shell.py не найден")
    proc = subprocess.Popen(
        [sys.executable, shell, port], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
    )
    reader = ProcessOutputReader(proc)
    output = reader.collect(5)
    for command in commands:
        proc.stdin.write((command + "\n").encode("ascii"))
        proc.stdin.flush()
        output += reader.collect(timeout_per_command)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    reader.thread.join(timeout=1)
    proc.stdout.close()
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", output).replace("\r", "")


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
    empty_file = os.path.join(tempfile.gettempdir(), "obrik_empty.bin")
    result = subprocess.run([
        "dfu-util", "-a", "0", "-s", "0x08000000:mass-erase:force",
        "-D", empty_file,
    ], timeout=60)
    if result.returncode == 0:
        print("  ✓ mass-erase завершён")
        print("  Плата в режиме DFU — можно прошивать загрузчик и PX4.")
    else:
        print(f"  [ОШИБКА] mass-erase завершился с кодом {result.returncode}")
        return False

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
    result = subprocess.run([
        "dfu-util", "-a", "0", "--dfuse-address", addr, "-D", bl,
    ])
    if result.returncode == 0:
        print("  ✓ загрузчик прошит")
        print("  Плата остаётся в режиме DFU — можно сразу прошивать PX4 (шаг 2).")
    else:
        print(f"  [ОШИБКА] dfu-util завершился с кодом {result.returncode}")
        return False

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
        result = subprocess.run([
            "dfu-util", "-a", "0", "--dfuse-address", app_addr,
            "-D", fw_bin,
        ], timeout=300)
        if result.returncode == 0:
            print("  ✓ прошивка залита")
            print("\n  >>> ОТКЛЮЧИТЕ USB, затем подключите заново БЕЗ BOOT. <<<")
            print("  >>> Плата загрузится в PX4. <<<")
            input("  Нажмите Enter, когда плата переподключена и загрузилась...")
            port = wait_port(30)
            if port:
                print(f"  ✓ плата доступна на {port}")
            else:
                print("  ? плата не обнаружена — проверьте подключение USB")
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

    close_qgroundcontrol()
    time.sleep(1)

    uploader = os.path.join(tools, "px_uploader.py")
    if not os.path.exists(uploader):
        uploader = os.path.join(tools, "px4_uploader.py")
    if not os.path.exists(uploader):
        uploader = "px_uploader.py"

    print(f"  прошиваю: {fw}")
    cmd = [sys.executable, uploader, "--port", port, fw]
    result = subprocess.run(cmd, timeout=120,
                            capture_output=True, text=True)
    print(result.stdout)
    combined = result.stdout + result.stderr
    if result.returncode == 0 and ("Reboot" in combined or "Success" in combined or "done" in combined.lower()):
        print("  ✓ прошивка залита — полётник перезагружается")
    else:
        print(f"  [ОШИБКА] px_uploader завершился с кодом {result.returncode}")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        return False

    print("  Жду перезагрузки полётника (8 сек)...")
    time.sleep(8)
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

    close_qgroundcontrol()
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
        print(f"  [ОШИБКА] ESC не запитаны: {v:.1f}V. Подключите АКБ.")
        m.close()
        return False
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

    print(f"  запускаю beacon через mavlink_shell ({port})...")

    proc = subprocess.Popen(
        [sys.executable, shell, port],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    reader = ProcessOutputReader(proc)

    def run_shell_command(command, timeout):
        """Отправить ровно одну NSH-команду и неблокирующе собрать её вывод."""
        proc.stdin.write((command + "\n").encode("ascii"))
        proc.stdin.flush()
        return reader.collect(timeout, idle_timeout=3.0)

    all_output = run_shell_command("dshot stop", 5)
    time.sleep(3)
    esc_done = [False] * num
    for esc in range(num):
        for attempt in range(1, 4):
            print(f"  ESC {esc}: попытка {attempt}/3...")
            attempt_output = run_shell_command(f"dshot_4way beacon {esc} {beacon_val}", 40)
            all_output += attempt_output
            clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", attempt_output).replace("\r", "")
            if f"ESC {esc}: OK" in clean or f"ESC {esc}: already" in clean or f"ESC{esc}: OK" in clean:
                esc_done[esc] = True
                print(f"  ✓ ESC {esc} — Beacon Delay записан")
                break
            if "no bootloader" in clean.lower():
                print(f"  ⚠ ESC {esc} — no bootloader response")
            else:
                print(f"  ⚠ ESC {esc} — подтверждение не получено")
            time.sleep(2)

    all_output += run_shell_command("dshot start", 4)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    reader.thread.join(timeout=1)
    proc.stdout.close()

    out = all_output
    # удалить ANSI-escape последовательности
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
        found_ok = esc_done[esc] or any(p in out for p in ok_patterns)
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


def parse_params_file(path):
    """Прочитать QGC/PX4 params: sys comp NAME VALUE TYPE или NAME VALUE [TYPE]."""
    result = []
    with open(path, encoding="utf-8") as source:
        for line_no, raw in enumerate(source, 1):
            line = raw.strip()
            if not line or line.startswith(("#", "%")):
                continue
            parts = line.split()
            try:
                if len(parts) >= 5 and parts[0].isdigit() and parts[1].isdigit():
                    name, value, param_type = parts[2], float(parts[3]), int(parts[4])
                elif len(parts) >= 2:
                    name, value = parts[0], float(parts[1])
                    param_type = int(parts[2]) if len(parts) >= 3 else 9
                else:
                    raise ValueError("недостаточно колонок")
            except ValueError as error:
                raise ValueError(f"{path}:{line_no}: {error}") from error
            if not name or len(name.encode("ascii")) > 16:
                raise ValueError(f"{path}:{line_no}: некорректное имя параметра {name!r}")
            result.append((name, value, param_type))
    return result


def encode_param_value(value, param_type):
    """Byte-wise MAVLink encoding для целых PARAM_SET (PX4 protocol)."""
    formats = {1: "B", 2: "b", 3: "H", 4: "h", 5: "I", 6: "i"}
    if param_type == 9:
        return float(value)
    if param_type not in formats:
        raise ValueError(f"тип MAVLink {param_type} пока не поддержан")
    return struct.unpack(">f", struct.pack(">" + formats[param_type], int(value)).rjust(4, b"\x00"))[0]


def decode_param_value(value, param_type):
    formats = {1: "B", 2: "b", 3: "H", 4: "h", 5: "I", 6: "i"}
    if param_type == 9:
        return float(value)
    if param_type not in formats:
        return float(value)
    raw = struct.pack(">f", float(value))
    size = struct.calcsize(formats[param_type])
    return struct.unpack(">" + formats[param_type], raw[-size:])[0]


def set_parameter(m, name, value, param_type, retries=3):
    """Записать один параметр и дождаться соответствующего PARAM_VALUE ACK."""
    target_system = m.target_system or 1
    target_component = m.target_component or 1
    wire_value = encode_param_value(value, param_type)
    for _ in range(retries):
        m.mav.param_set_send(target_system, target_component, name.encode("ascii"), wire_value, param_type)
        deadline = time.time() + 1.2
        while time.time() < deadline:
            ack = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.25)
            if ack and str(ack.param_id).rstrip("\x00") == name:
                actual = decode_param_value(ack.param_value, ack.param_type)
                tolerance = 1e-5 * max(1.0, abs(value)) if param_type == 9 else 0
                return abs(actual - value) <= tolerance
    return False


def read_parameter(m, name, timeout=1.5):
    m.mav.param_request_read_send(m.target_system or 1, m.target_component or 1,
                                  name.encode("ascii"), -1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.3)
        if msg and str(msg.param_id).rstrip("\x00") == name:
            return decode_param_value(msg.param_value, msg.param_type)
    return None


def step_load_params(cfg):
    """Шаг 3: типизированная загрузка, flash-save, reboot и проверка."""
    params = cfg.get("params_file", "")
    if not params or not os.path.exists(params):
        print(f"[ОШИБКА] файл параметров не найден: {params}")
        return False
    try:
        params_to_set = parse_params_file(params)
    except ValueError as error:
        print(f"[ОШИБКА] {error}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 3 — загрузка параметров с проверкой сохранения")
    print("=" * 60)
    print(f"  Файл: {params}")
    print(f"  параметров: {len(params_to_set)}")
    input("  Закройте QGroundControl и нажмите Enter...")
    close_qgroundcontrol()
    port = wait_port(15)
    if not port:
        return False

    # Эта сборка может хранить параметры на microSD. Проверяем backend ДО
    # отправки сотен PARAM_SET, иначе ACK будут успешными, но reboot всё сотрёт.
    try:
        storage_check = run_mavlink_shell(cfg, port, ["param save", "param status"], 12)
    except RuntimeError as error:
        print(f"[ОШИБКА] {error}")
        return False
    if "Param save failed" in storage_check or "param export failed" in storage_check:
        print("[ОШИБКА] хранилище параметров недоступно.")
        if "/fs/microsd" in storage_check:
            print("  PX4 ожидает microSD: вставьте исправную FAT32-карту и перезагрузите плату.")
        return False

    try:
        m = mavlink_connect(port, int(cfg.get("baud", "57600")))
    except (ImportError, RuntimeError) as error:
        print(f"[ОШИБКА] MAVLink: {error}")
        return False

    failed = []
    for idx, (name, value, param_type) in enumerate(params_to_set, 1):
        if not set_parameter(m, name, value, param_type):
            failed.append(name)
        if idx % 100 == 0 or idx == len(params_to_set):
            print(f"  подтверждено {idx - len(failed)}/{idx}...")
    if failed:
        print(f"[ОШИБКА] нет подтверждения для {len(failed)}: {', '.join(failed[:12])}")
        m.close()
        return False

    # MAV_CMD_PREFLIGHT_STORAGE ACK у PX4 приходит до фактического экспорта.
    # Используем NSH и анализируем реальную ошибку backend (например, missing SD).
    m.close()
    print("  сохраняю параметры и проверяю backend...")
    save_output = run_mavlink_shell(cfg, port, ["param save", "param status"], 15)
    if "Param save failed" in save_output or "param export failed" in save_output:
        print("[ОШИБКА] PX4 не сохранил параметры:")
        for line in save_output.splitlines():
            if "failed" in line.lower() or "file:" in line:
                print(f"    {line.strip()}")
        return False
    print("  ✓ param save завершён без ошибок")
    time.sleep(2)

    try:
        m = mavlink_connect(port, int(cfg.get("baud", "57600")))
    except RuntimeError as error:
        print(f"[ОШИБКА] {error}")
        return False

    # Реальный reboot обязателен: проверка до него доказывает только RAM-значения.
    from pymavlink import mavutil
    m.mav.command_long_send(m.target_system or 1, m.target_component or 1,
                            mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
                            0, 1, 0, 0, 0, 0, 0, 0)
    m.close()
    time.sleep(4)
    rebooted_port = wait_port(45)
    if not rebooted_port:
        print("[ОШИБКА] порт не появился после reboot")
        return False
    try:
        m = mavlink_connect(rebooted_port, int(cfg.get("baud", "57600")))
    except RuntimeError as error:
        print(f"[ОШИБКА] {error}")
        return False

    critical_names = [name for name, _, _ in params_to_set if name in {
        "BAT1_N_CELLS", "BAT1_V_DIV", "EKF2_OF_CTRL", "SENS_FLOW_MAXHGT",
        "COM_ARM_BAT_MIN", "SYS_AUTOSTART", "WV_YRATE_MAX",
    }]
    expected = {name: value for name, value, _ in params_to_set}
    mismatches = []
    for name in critical_names:
        actual = read_parameter(m, name)
        if actual is None or abs(actual - expected[name]) > 1e-4 * max(1, abs(expected[name])):
            mismatches.append((name, expected[name], actual))
        else:
            print(f"    ✓ после reboot: {name} = {actual}")
    m.close()
    if mismatches:
        for name, wanted, actual in mismatches:
            print(f"    ✗ после reboot: {name}: {actual}, ожидалось {wanted}")
        return False
    print(f"  ✓ {len(params_to_set)} параметров записаны; контроль после reboot пройден")
    return True


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
    for dep in ["dfu-util"]:
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

    # создать пустой файл для mass-erase кроссплатформенно
    with open(os.path.join(tempfile.gettempdir(), "obrik_empty.bin"), "wb") as empty:
        empty.write(b"\x00")

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

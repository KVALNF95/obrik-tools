#!/usr/bin/env python3
"""
obrik_flash.py — утилита одной командой для прошивки и настройки дрона «Обрик».

Что делает:
  1. Прошивает загрузчик (DFU) — требуется нажать кнопку BOOT
  2. Прошивает основную прошивку PX4 (px_uploader)
  3. Загружает параметры в полётник (через MAVLink param_set)
  4. Записывает Beacon Delay = Infinite во все ESC (требуется АКБ)

Использование:
  python3 obrik_flash.py                     # с конфигом по умолчанию
  python3 obrik_flash.py --config my.cfg     # с указанным конфигом
   python3 obrik_flash.py --steps 1,2         # только прошивка
   python3 obrik_flash.py --steps beacon       # только отключение писка
   python3 obrik_flash.py --steps params       # только загрузка параметров

Конфиг-файл (obrik_flash.cfg) — формат key=value, см. пример внизу.
"""

import os, sys, time, re, glob, subprocess, argparse

# ── конфиг по умолчанию ──────────────────────────────────────────────
DEFAULT_CONFIG = {
    "bootloader":   "",
    "firmware":     "",
    "params_file":  "",
    "px4_tools":    "",
    "dfu_address":  "0x08000000",
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


def prompt_yesno(msg, default="y"):
    """Спросить y/n, вернуть True если да."""
    try:
        d = "Y" if default == "y" else "N"
        ans = input(f"  {msg} [{d}]: ").strip().lower()
        if not ans:
            ans = default
        return ans in ("y", "yes", "да", "д")
    except EOFError:
        return default == "y"

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


# ── шаги ──────────────────────────────────────────────────────────────

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
        print("  Для прошивки загрузчика плата должна быть в режиме DFU.")
        if not prompt_yesno("  Пропустить шаг 1 (загрузчик уже прошит)?"):
            print("  >>> ОТКЛЮЧИТЕ плату от USB.")
            print("  >>> Зажмите BOOT, подключите USB, отпустите BOOT.")
            print("  >>> Запустите этот скрипт заново.")
        return True  # пропускаем — загрузчик уже стоит

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
    if result.returncode == 0 and "success" in result.stdout.lower():
        print("  ✓ загрузчик прошит")
        print("\n  >>> ОТКЛЮЧИТЕ полётник от USB перед следующим шагом. <<<")
    else:
        print(f"  [ОШИБКА] dfu-util завершился с кодом {result.returncode}")
        print(result.stderr)
        return False

    return True


def step_flash_firmware(cfg):
    """Шаг 2: прошить основную прошивку."""
    fw = cfg["firmware"]
    tools = cfg["px4_tools"]
    if not os.path.exists(fw):
        print(f"[ОШИБКА] прошивка не найдена: {fw}")
        return False

    print("\n" + "=" * 60)
    print("ШАГ 2 — прошивка PX4")
    print("=" * 60)

    # проверяем, подключена ли плата
    state = detect_board_state()
    if state == "none":
        print("  Плата не обнаружена.")
        print("  >>> Подключите полётник по USB (кнопку BOOT НЕ нажимать).")
    elif state == "dfu":
        print("  Плата в режиме DFU. Перезагрузите её:")
        print("  >>> ОТКЛЮЧИТЕ USB, затем подключите заново (без BOOT).")
    else:
        print("  Плата подключена и работает.")
        if prompt_yesno("  Прошивка уже установлена. Пропустить шаг 2?"):
            return True

    print("  Закройте QGroundControl (если открыт).")
    input("  Нажмите Enter, когда готово...")

    port = wait_port(15)
    if not port:
        print("[ОШИБКА] полётник не обнаружен.")
        return False

    # убить QGC если открыт (чтобы не перехватывал порт)
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

    if prompt_yesno("  Пропустить шаг 4 (beacon уже настроен)?"):
        return True

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

    if prompt_yesno("  Пропустить шаг 3 (параметры уже загружены)?"):
        return True

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

    sent = 0
    failed = 0

    for idx, (pname, pval) in enumerate(params_to_set):
        pname_b = pname.encode()
        if len(pname_b) > 16:
            print(f"  [ПРОПУСК] {pname} — имя длиннее 16 символов")
            failed += 1
            continue

        pname_b = pname_b.ljust(16, b"\x00")

        # MAV_PARAM_TYPE_FLOAT (9) — подходит для float и int параметров
        m.mav.param_set_send(
            1,                # target_system
            1,                # target_component
            pname_b,          # param_id
            pval,             # param_value
            9                 # MAV_PARAM_TYPE_FLOAT
        )
        sent += 1

        # небольшой темп, чтобы не забить очередь
        if idx % 20 == 19:
            time.sleep(0.2)
        if idx % 100 == 99:
            print(f"  отправлено {sent}/{len(params_to_set)}...")

    time.sleep(1)

    # сохранить параметры в flash через nsh
    print("  сохраняю параметры в flash...")
    nsh_send(m, "param save", timeout_s=5)
    time.sleep(3)

    # верификация критических параметров
    print("  проверка критических параметров...")
    critical = {pname for pname, _ in params_to_set if pname in (
        "BAT1_N_CELLS", "BAT1_V_DIV", "EKF2_OF_CTRL",
        "SENS_FLOW_MAXHGT", "COM_ARM_BAT_MIN", "SYS_AUTOSTART",
    )}
    verified = 0
    for pname in sorted(critical):
        got = None
        try:
            m.mav.param_request_read_send(1, 1, pname.encode().ljust(16, b'\x00'), -1)
            t0 = time.time()
            while time.time() - t0 < 2:
                msg = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=1)
                if msg and msg.param_id.rstrip('\x00') == pname:
                    got = msg.param_value
                    break
        except Exception:
            pass
        if got is not None:
            expected = next((v for n, v in params_to_set if n == pname), None)
            if expected is not None and abs(got - expected) < 1e-4:
                print(f"    ✓ {pname} = {got}")
                verified += 1
            else:
                print(f"    ⚠ {pname} = {got} (ожид: {expected})")
        else:
            print(f"    ? {pname} — не удалось прочитать")

    m.close()

    if failed == 0:
        print(f"  ✓ {sent} параметров отправлено и сохранено")
    else:
        print(f"  ⚠ {sent} отправлено, {failed} пропущено")
    print("  >>> ОТКЛЮЧИТЕ полётник от питания и подключите заново (перезагрузка). <<<")
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
                        help="какие шаги выполнить: 1=загрузчик, 2=прошивка, 3=параметры, 4=beacon, либо 'beacon', 'fw', 'params', 'all'")
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
    else:
        do = set()
        for s in steps.split(","):
            s = s.strip()
            if s.isdigit():
                do.add(int(s))

    success = True
    results = {}

    for step_num in sorted(do):
        if step_num == 1:
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
    names = {1: "загрузчик", 2: "прошивка", 3: "параметры", 4: "Beacon Delay"}
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

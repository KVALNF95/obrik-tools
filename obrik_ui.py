#!/usr/bin/env python3
"""obrik_ui — простой веб-интерфейс к obrik_flash.py.

Только стандартная библиотека Python — ничего устанавливать не нужно.

Запуск:
    python3 obrik_ui.py              # поднимет сервер и откроет браузер
    python3 obrik_ui.py --no-browser # только сервер (http://127.0.0.1:8765)

Что умеет:
  - кнопки: прошить полностью (шаги 1-4), каждый шаг отдельно,
    dry-run (проверка файлов), mass-erase (шаг 0);
  - статус платы: не подключена / в BOOT (DFU) / подключена и работает;
  - галочки по выполненным шагам (из ИТОГа obrik_flash.py);
  - журнал вывода скрипта в реальном времени;
  - кнопка «Продолжить», когда скрипт ждёт Enter (переподключение USB,
    подключение АКБ и т.п.).
"""
import argparse
import configparser
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FLASH_SCRIPT = os.path.join(HERE, "obrik_flash.py")
DRONES_CFG = os.path.join(HERE, "drones.cfg")
BASE_CFG = os.path.join(HERE, "obrik_flash.cfg")
PORT = 8765
LOG_CAP = 300_000  # максимум символов журнала в памяти

STEP_NAMES = {
    0: "Mass-erase (стирание flash)",
    1: "Шаг 1 — загрузчик (DFU)",
    2: "Шаг 2 — прошивка PX4",
    3: "Шаг 3 — параметры",
    4: "Шаг 4 — Beacon (писк ESC)",
}

_lock = threading.Lock()
_state = {
    "proc": None,          # Popen текущего запуска или None
    "log": "",             # накопленный вывод текущего/последнего запуска
    "steps": {},           # {номер: 'wait'|'run'|'ok'|'fail'}
    "label": "",           # что сейчас запущено («шаги 1-4», «dry-run», ...)
    "exit": None,          # код завершения последнего запуска
}


# ── определение состояния платы ──────────────────────────────────────

def board_state():
    """'dfu' | 'running' | 'none' — как detect_board_state() в obrik_flash."""
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        out = ""
    if re.search(r"0483:df11", out, re.I):
        return "dfu"
    if glob.glob("/dev/ttyACM*") or glob.glob("/dev/serial/by-id/usb-*Matek*"):
        return "running"
    return "none"


# ── дроны и полётники (drones.cfg) ───────────────────────────────────

def load_drones():
    """Прочитать drones.cfg → (drones, fcs). Пусто, если файла нет."""
    drones, fcs = {}, {}
    if not os.path.exists(DRONES_CFG):
        return drones, fcs
    cp = configparser.ConfigParser()
    cp.read(DRONES_CFG, encoding="utf-8")
    for sec in cp.sections():
        if sec.startswith("fc:"):
            fcs[sec[3:]] = dict(cp[sec])
        elif sec.startswith("drone:"):
            drones[sec[6:]] = dict(cp[sec])
    return drones, fcs


def build_cfg(drone_id, fc_id):
    """Собрать временный конфиг: базовый obrik_flash.cfg + выбранный
    дрон (params_file) + выбранный полётник (bootloader/firmware).
    Вернуть (путь, None) или (None, текст ошибки)."""
    drones, fcs = load_drones()
    if drone_id not in drones:
        return None, f"неизвестный дрон: {drone_id}"
    if fc_id not in fcs:
        return None, f"неизвестный полётник: {fc_id}"
    overridden = {"bootloader", "firmware", "firmware_bin", "params_file"}
    lines = []
    if os.path.exists(BASE_CFG):
        with open(BASE_CFG, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                s = line.strip()
                if s and not s.startswith("#") and "=" in s \
                        and s.split("=", 1)[0].strip() in overridden:
                    continue
                lines.append(line)
    fc, dr = fcs[fc_id], drones[drone_id]
    lines += [
        "",
        f"# ── подставлено obrik_ui: дрон «{dr.get('name', drone_id)}», "
        f"полётник «{fc.get('name', fc_id)}» ──",
        f"bootloader   = {fc.get('bootloader', '')}",
        f"firmware     = {fc.get('firmware', '')}",
        f"firmware_bin = {fc.get('firmware_bin', '')}",
        f"params_file  = {dr.get('params_file', '')}",
    ]
    path = os.path.join(tempfile.gettempdir(), "obrik_ui_run.cfg")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path, None


# ── запуск obrik_flash.py и чтение его вывода ────────────────────────

def _reader(proc):
    """Читает вывод скрипта кусками (чтобы ловить input()-подсказки без \\n)."""
    fd = proc.stdout.fileno()
    buf_line = ""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        with _lock:
            _state["log"] = (_state["log"] + text)[-LOG_CAP:]
        # построчный разбор для статусов шагов
        buf_line += text
        while "\n" in buf_line:
            line, buf_line = buf_line.split("\n", 1)
            _parse_line(line)
    proc.wait()
    with _lock:
        _state["exit"] = proc.returncode
        _state["proc"] = None
        # всё, что осталось «выполняющимся», при ненулевом коде — ошибка
        for n, st in _state["steps"].items():
            if st in ("run", "wait"):
                _state["steps"][n] = "fail" if proc.returncode else st


def _parse_line(line):
    m = re.search(r"ШАГ (\d)", line)
    if m:
        with _lock:
            _state["steps"][int(m.group(1))] = "run"
        return
    m = re.search(r"шаг (\d+) \([^)]*\): (✓ OK|✗ ошибка)", line)
    if m:
        with _lock:
            _state["steps"][int(m.group(1))] = \
                "ok" if "OK" in m.group(2) else "fail"
        return
    m = re.search(r"⚠ шаг (\d+) завершился с ошибкой", line)
    if m:
        with _lock:
            _state["steps"][int(m.group(1))] = "fail"


def start_run(steps_arg, label, extra_flags=(), cfg_path=None):
    """Запустить obrik_flash.py. Вернуть None при успехе или текст ошибки."""
    with _lock:
        if _state["proc"] is not None:
            return "уже выполняется — дождитесь завершения или прервите"
        cmd = [sys.executable, "-u", FLASH_SCRIPT]
        if steps_arg:
            cmd += ["--steps", steps_arg]
        if cfg_path:
            cmd += ["--config", cfg_path]
        cmd += list(extra_flags)
        try:
            proc = subprocess.Popen(
                cmd, cwd=HERE,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
        except Exception as e:
            return f"не удалось запустить: {e}"
        _state["proc"] = proc
        _state["log"] = f"$ {' '.join(cmd)}\n"
        _state["label"] = label
        _state["exit"] = None
        wanted = ({int(s) for s in steps_arg.split(",") if s.strip().isdigit()}
                  if steps_arg else set())
        _state["steps"] = {n: "wait" for n in sorted(wanted)}
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return None


def waiting_prompt():
    """Если скрипт ждёт ввода — вернуть текст подсказки, иначе ''."""
    with _lock:
        if _state["proc"] is None:
            return ""
        tail = _state["log"].rsplit("\n", 1)[-1].strip()
    if "Enter" in tail or tail.endswith("[Y/n]:"):
        return tail
    return ""


# ── HTTP-сервер ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # не засорять консоль
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/status":
            with _lock:
                st = {
                    "board": None,  # заполним ниже, вне lock
                    "running": _state["proc"] is not None,
                    "label": _state["label"],
                    "exit": _state["exit"],
                    "steps": dict(_state["steps"]),
                    "log": _state["log"],
                }
            st["board"] = board_state()
            st["prompt"] = waiting_prompt()
            self._json(st)
        elif self.path == "/config":
            drones, fcs = load_drones()
            self._json({"drones": drones, "fcs": fcs})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/run"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            steps = q.get("steps", [""])[0]
            dry = q.get("dry", [""])[0] == "1"
            drone = q.get("drone", [""])[0]
            fc = q.get("fc", [""])[0]

            cfg_path, suffix = None, ""
            if drone and fc:
                cfg_path, err = build_cfg(drone, fc)
                if err:
                    self._json({"error": err}, 409)
                    return
                drones, fcs = load_drones()
                suffix = (f" — {drones[drone].get('name', drone)}"
                          f" / {fcs[fc].get('name', fc)}")

            if dry:
                err = start_run("", "проверка (dry-run)" + suffix,
                                ("--dry-run",), cfg_path)
            else:
                label = {"1,2,3,4": "полная прошивка (шаги 1–4)",
                         "0": "mass-erase"}.get(steps, f"шаг {steps}")
                err = start_run(steps, label + suffix, (), cfg_path)
            self._json({"error": err} if err else {"ok": True},
                       409 if err else 200)
        elif self.path == "/enter":
            with _lock:
                proc = _state["proc"]
            if proc and proc.stdin:
                try:
                    proc.stdin.write(b"\n")
                    proc.stdin.flush()
                except Exception:
                    pass
            self._json({"ok": True})
        elif self.path == "/abort":
            with _lock:
                proc = _state["proc"]
            if proc:
                proc.terminate()
            self._json({"ok": True})
        else:
            self.send_error(404)


# ── страница ─────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Obrik Tools — прошивка полётника</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #f4f7f9;
         color: #333; display: flex; flex-direction: column; height: 100vh; }
  header { background: #05b9f0; color: #fff; padding: 10px 18px;
           font-size: 18px; font-weight: 600; }
  .wrap { display: flex; flex: 1; min-height: 0; }
  .panel { width: 340px; padding: 14px; overflow-y: auto; }
  .board { padding: 10px 12px; border-radius: 8px; margin-bottom: 12px;
           font-weight: 600; background: #e3e7ea; }
  .board.dfu     { background: #fff3cd; color: #7a5b00; }
  .board.running { background: #d9f2df; color: #1d6b32; }
  .board.none    { background: #e3e7ea; color: #666; }
  .sel { background: #fff; border-radius: 8px; padding: 10px 12px;
         margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .sel label { display: block; font-size: 12px; color: #888;
               margin: 6px 0 2px; }
  .sel label:first-child { margin-top: 0; }
  .sel select { width: 100%; font-size: 14px; padding: 7px 8px;
                border: 1px solid #d6dde2; border-radius: 6px;
                background: #fff; }
  button { font-size: 14px; border: 0; border-radius: 8px; padding: 10px 12px;
           cursor: pointer; }
  button:disabled { opacity: .45; cursor: default; }
  .big { width: 100%; background: #05b9f0; color: #fff; font-weight: 700;
         font-size: 16px; padding: 13px; margin-bottom: 12px; }
  .steps { background: #fff; border-radius: 8px; padding: 6px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .step { display: flex; align-items: center; gap: 8px; padding: 7px 8px;
          border-bottom: 1px solid #f0f2f4; }
  .step:last-child { border-bottom: 0; }
  .step .name { flex: 1; font-size: 13.5px; }
  .step button { background: #e8f7fd; color: #0788b3; padding: 6px 10px; }
  .badge { width: 26px; text-align: center; font-size: 16px; }
  .aux { display: flex; gap: 8px; margin-top: 12px; }
  .aux button { flex: 1; background: #e3e7ea; color: #444; font-size: 12.5px; }
  .right { flex: 1; display: flex; flex-direction: column; min-width: 0;
           padding: 14px 14px 14px 0; }
  .promptbar { display: none; background: #fff3cd; border: 1px solid #e6c200;
               border-radius: 8px; padding: 10px 12px; margin-bottom: 10px;
               align-items: center; gap: 12px; }
  .promptbar.show { display: flex; }
  .promptbar button { background: #e6a800; color: #fff; font-weight: 700; }
  #log { flex: 1; background: #14191e; color: #e4e6e7; border-radius: 8px;
         padding: 12px; font: 12px/1.5 monospace; overflow-y: auto;
         white-space: pre-wrap; word-break: break-all; }
  .runbar { display: none; margin-bottom: 10px; align-items: center; gap: 10px; }
  .runbar.show { display: flex; }
  .runbar .what { font-weight: 600; color: #0788b3; }
  .runbar button { background: #f6d2d2; color: #a33; }
  .spin { display: inline-block; animation: r 1s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
</style></head><body>
<header>Obrik Tools — прошивка полётника</header>
<div class="wrap">
  <div class="panel">
    <div id="board" class="board none">Полётник: …</div>
    <div class="sel" id="selectors" style="display:none">
      <label for="drone">Дрон</label>
      <select id="drone" onchange="droneChanged()"></select>
      <label for="fc">Полётник (по умолчанию — штатный для дрона)</label>
      <select id="fc"></select>
    </div>
    <button class="big" onclick="run('1,2,3,4')">▶ Прошить полностью (шаги 1–4)</button>
    <div class="steps" id="steps"></div>
    <div class="aux">
      <button onclick="runDry()">Проверка файлов (dry-run)</button>
      <button onclick="runErase()">Mass-erase (шаг 0)</button>
    </div>
  </div>
  <div class="right">
    <div class="runbar" id="runbar">
      <span class="spin">⏳</span> <span class="what" id="what"></span>
      <button onclick="post('/abort')">Прервать</button>
    </div>
    <div class="promptbar" id="promptbar">
      <span id="prompttext" style="flex:1"></span>
      <button onclick="post('/enter')">Продолжить (Enter)</button>
    </div>
    <div id="log"></div>
  </div>
</div>
<script>
const STEPS = {1:"Шаг 1 — загрузчик (DFU)", 2:"Шаг 2 — прошивка PX4",
               3:"Шаг 3 — параметры", 4:"Шаг 4 — Beacon (писк ESC)"};
const ICON = {wait:"▫", run:"<span class='spin'>⏳</span>", ok:"✅", fail:"❌"};
const BOARD = {
  dfu:     ["dfu",     "Полётник: в BOOT-режиме (DFU) — можно прошивать"],
  running: ["running", "Полётник: подключён, прошивка запущена"],
  none:    ["none",    "Полётник: не подключён"]};
let running = false;
let CFG = {drones: {}, fcs: {}};

async function loadConfig() {
  try { CFG = await (await fetch("/config")).json(); } catch (e) { return; }
  const dsel = document.getElementById("drone");
  const ids = Object.keys(CFG.drones);
  if (!ids.length) return;              // drones.cfg нет — работаем без выбора
  dsel.innerHTML = ids.map(id =>
    `<option value="${id}">${CFG.drones[id].name || id}</option>`).join("");
  document.getElementById("selectors").style.display = "block";
  droneChanged();
}
function droneChanged() {
  const d = CFG.drones[document.getElementById("drone").value] || {};
  const fsel = document.getElementById("fc");
  fsel.innerHTML = Object.keys(CFG.fcs).map(id =>
    `<option value="${id}">${CFG.fcs[id].name || id}</option>`).join("");
  if (d.fc && CFG.fcs[d.fc]) fsel.value = d.fc;   // штатный полётник дрона
}
function selArgs() {
  const d = document.getElementById("drone").value;
  const f = document.getElementById("fc").value;
  return (d && f) ? `&drone=${d}&fc=${f}` : "";
}

function render(st) {
  running = st.running;
  const b = BOARD[st.board] || BOARD.none;
  const bd = document.getElementById("board");
  bd.className = "board " + b[0]; bd.textContent = b[1];

  const box = document.getElementById("steps");
  box.innerHTML = "";
  for (const n of [1,2,3,4]) {
    const row = document.createElement("div"); row.className = "step";
    const state = st.steps[n];
    row.innerHTML = `<span class="badge">${state ? ICON[state] : "▫"}</span>
      <span class="name">${STEPS[n]}</span>
      <button ${running ? "disabled" : ""} onclick="run('${n}')">Выполнить</button>`;
    box.appendChild(row);
  }
  document.querySelectorAll(".big, .aux button, .sel select").forEach(
    el => el.disabled = running);

  document.getElementById("runbar").className =
    "runbar" + (running ? " show" : "");
  document.getElementById("what").textContent = st.label || "";

  const pb = document.getElementById("promptbar");
  pb.className = "promptbar" + (st.prompt ? " show" : "");
  document.getElementById("prompttext").textContent = st.prompt;

  const log = document.getElementById("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  if (log.textContent !== st.log) {
    log.textContent = st.log;
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
}
async function tick() {
  try { render(await (await fetch("/status")).json()); } catch (e) {}
}
async function post(url) { await fetch(url, {method:"POST"}); tick(); }
function run(steps) { post("/run?steps=" + steps + selArgs()); }
function runDry() { post("/run?dry=1" + selArgs()); }
function runErase() {
  if (confirm("Mass-erase сотрёт ВСЮ flash-память платы. Продолжить?"))
    post("/run?steps=0" + selArgs());
}
setInterval(tick, 1000); loadConfig(); tick();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="веб-интерфейс к obrik_flash.py")
    ap.add_argument("--no-browser", action="store_true",
                    help="не открывать браузер автоматически")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"obrik_ui: {url}  (Ctrl+C — выход)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with _lock:
            if _state["proc"]:
                _state["proc"].terminate()
        print("\nвыход")


if __name__ == "__main__":
    main()

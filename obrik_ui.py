#!/usr/bin/env python3
"""obrik_ui — графическое приложение для прошивки через obrik_flash.py.

Запуск:  python3 obrik_ui.py          (нужен пакет python3-tk)

Что умеет:
  - выбор дрона и полётника из drones.cfg (там же — как добавлять новые);
  - кнопки: прошить полностью, каждый шаг отдельно, dry-run, mass-erase;
  - живой статус платы: не подключена / в BOOT (DFU) / подключена;
  - статус текущего действия и прогресс вместо «терминала»
    (подробный вывод — в сворачиваемых «Подробностях»);
  - сам замечает, когда плата появилась в нужном режиме, и спрашивает
    окошком «Продолжить?» — Enter руками жать не нужно. Исключение —
    подключение АКБ: его с ноутбука не видно, там кнопка.
"""
import configparser
import glob
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    print("Нужен tkinter:  sudo apt install python3-tk")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
FLASH_SCRIPT = os.path.join(HERE, "obrik_flash.py")
DRONES_CFG = os.path.join(HERE, "drones.cfg")
BASE_CFG = os.path.join(HERE, "obrik_flash.cfg")

ACCENT = "#05b9f0"
STEPS = {1: "Загрузчик (DFU)", 2: "Прошивка PX4",
         3: "Параметры", 4: "Beacon (писк ESC)"}
BADGE = {"wait": "▫", "run": "⏳", "ok": "✅", "fail": "❌"}


# ── плата и конфиги ──────────────────────────────────────────────────

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


def load_drones():
    drones, fcs, softs = {}, {}, {}
    if os.path.exists(DRONES_CFG):
        cp = configparser.ConfigParser()
        cp.read(DRONES_CFG, encoding="utf-8")
        for sec in cp.sections():
            if sec.startswith("fc:"):
                fcs[sec[3:]] = dict(cp[sec])
            elif sec.startswith("soft:"):
                softs[sec[5:]] = dict(cp[sec])
            elif sec.startswith("drone:"):
                drones[sec[6:]] = dict(cp[sec])
    return drones, fcs, softs


def build_cfg(drone, soft, fc=None, all_fcs=None):
    """Временный конфиг: базовый + params дрона + файлы выбранного ПО +
    данные для сверки подключённой платы (usb_id)."""
    overridden = {"bootloader", "firmware", "firmware_bin", "params_file"}
    lines = []
    if os.path.exists(BASE_CFG):
        with open(BASE_CFG, encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if s and not s.startswith("#") and "=" in s \
                        and s.split("=", 1)[0].strip() in overridden:
                    continue
                lines.append(raw.rstrip("\n"))
    lines += [
        "",
        f"bootloader   = {soft.get('bootloader', '')}",
        f"firmware     = {soft.get('firmware', '')}",
        f"firmware_bin = {soft.get('firmware_bin', '')}",
        f"params_file  = {drone.get('params_file', '')}",
    ]
    # сборка из исходников (если задана у ПО)
    for k in ("px4_src", "px4_repo", "px4_branch", "px4_target"):
        if soft.get(k):
            lines.append(f"{k} = {soft[k]}")
    # сверка платы: что выбрано и карта «usb_id → имя платы» всех плат
    if fc:
        lines.append(f"expected_fc_name = {fc.get('name', '')}")
        if fc.get("usb_id"):
            lines.append(f"expected_usb_id = {fc['usb_id']}")
    if all_fcs:
        known = [f"{v['usb_id']}={v.get('name', k)}"
                 for k, v in all_fcs.items() if v.get("usb_id")]
        if known:
            lines.append(f"known_usb_ids = {';'.join(known)}")
    path = os.path.join(tempfile.gettempdir(), "obrik_ui_run.cfg")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ── классификация пауз скрипта ───────────────────────────────────────

def classify_prompt(tail, context):
    """Что скрипт ждёт: ('yn'|'mismatch'|'akb'|'dfu'|'running'|'plug'|'replug'
    |'manual', понятный текст)."""
    ctx = context + "\n" + tail
    if tail.endswith("[y/N]:") or "Подключена плата" in ctx or "а выбрана" in ctx:
        warn = next((l.strip() for l in reversed(context.splitlines())
                     if "Подключена плата" in l or "а выбрана" in l),
                    "Подключена не та плата, что выбрана.")
        return "mismatch", warn.lstrip("⚠ ").strip()
    if tail.endswith("[Y/n]:"):
        return "yn", "Шаг завершился с ошибкой."
    if "АКБ" in ctx:
        return "akb", "Подключите АКБ (регуляторы должны получить питание)."
    if "когда плата подключена" in tail or "зажимать НЕ нужно" in ctx:
        return "plug", ("Подключите плату по USB — кнопку BOOT зажимать "
                        "не нужно (если понадобится, спрошу отдельно).")
    if "когда переткнули" in tail or "не отвечает по MAVLink" in ctx:
        return "replug", ("Плата не отвечает по MAVLink. Переткните USB "
                          "(без BOOT) и подождите ~15 секунд.")
    if "загрузилась" in ctx or "переподключена" in ctx:
        return "running", ("Переподключите USB БЕЗ кнопки BOOT "
                           "и дождитесь загрузки платы.")
    if "BOOT" in ctx or "DFU" in ctx:
        return "dfu", ("Переподключите USB, ЗАЖАВ кнопку BOOT на плате "
                       "(отпустить через 1–2 с после подключения).")
    return "manual", tail


# ── приложение ───────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        root.title("Obrik Tools — прошивка полётника")
        root.minsize(620, 480)

        self.proc = None
        self.out_q = queue.Queue()
        self.log_lines = []
        self.cur_line = ""          # последняя строка вывода (живой статус)
        self.steps_state = {}
        self._step_frac = 0.0       # доля текущего шага для прогресс-бара
        self.prompt_key = None      # текущая пауза скрипта (текст)
        self.prompt_kind = None
        self.asked = False          # уже показывали окошко для этой паузы
        self.answered_key = None    # пауза, на которую уже ответили
        self.board = "none"
        self.drones, self.fcs, self.softs = load_drones()

        self._build_ui()
        threading.Thread(target=self._board_poller, daemon=True).start()
        root.after(150, self._tick)

    # ── интерфейс ────────────────────────────────────────────────

    def _build_ui(self):
        r = self.root
        r.configure(bg="#f4f7f9")
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure("TFrame", background="#f4f7f9")
        s.configure("TLabel", background="#f4f7f9")
        s.configure("Big.TButton", font=("", 12, "bold"), foreground="#fff",
                    background=ACCENT, padding=10)
        s.map("Big.TButton", background=[("active", "#049ed0"),
                                         ("disabled", "#9fd9ef")])
        s.configure("Head.TLabel", background=ACCENT, foreground="#fff",
                    font=("", 13, "bold"), padding=10)
        s.configure("Green.Horizontal.TProgressbar",
                    troughcolor="#e3e7ea", bordercolor="#e3e7ea",
                    background="#3bb24a", lightcolor="#3bb24a",
                    darkcolor="#3bb24a", thickness=18)

        ttk.Label(r, text="Obrik Tools — прошивка полётника",
                  style="Head.TLabel", anchor="w").pack(fill="x")

        top = ttk.Frame(r, padding=(12, 10, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Дрон:").grid(row=0, column=0, sticky="w")
        self.drone_var = tk.StringVar()
        self.drone_cb = ttk.Combobox(top, textvariable=self.drone_var,
                                     state="readonly", width=24)
        self.drone_cb.grid(row=0, column=1, padx=(6, 18))
        ttk.Label(top, text="Полётник:").grid(row=0, column=2, sticky="w")
        self.fc_var = tk.StringVar()
        self.fc_cb = ttk.Combobox(top, textvariable=self.fc_var,
                                  state="readonly", width=24)
        self.fc_cb.grid(row=0, column=3, padx=6)
        # ПО не выбирается руками — оно однозначно определяется платой
        # (Matek/MicoAir/Holybro → PX4, SpeedyBee → Betaflight).
        # Показываем его справочно рядом с платой.
        self.soft_lbl = ttk.Label(top, text="", foreground="#888")
        self.soft_lbl.grid(row=1, column=2, columnspan=2, sticky="w",
                           pady=(4, 0))

        self.board_lbl = tk.Label(r, text="Плата: …", font=("", 11, "bold"),
                                  bg="#e3e7ea", fg="#666", pady=8)
        self.board_lbl.pack(fill="x", padx=12, pady=10)

        mid = ttk.Frame(r, padding=(12, 0))
        mid.pack(fill="x")
        self.full_btn = ttk.Button(mid, text="▶  Прошить полностью",
                                   style="Big.TButton",
                                   command=lambda: self.run(self._full_steps()))
        self.full_btn.pack(fill="x")

        # шаги — только индикаторы хода: если что-то уже стоит на плате,
        # скрипт сам пропустит лишнее
        self.step_rows = {}
        steps_fr = tk.Frame(r, bg="#ffffff", bd=0, highlightthickness=1,
                            highlightbackground="#e0e6ea")
        steps_fr.pack(fill="x", padx=12, pady=10)
        for n, name in STEPS.items():
            row = tk.Frame(steps_fr, bg="#ffffff")
            row.pack(fill="x", padx=8, pady=3)
            badge = tk.Label(row, text="▫", width=2, bg="#ffffff")
            badge.pack(side="left")
            tk.Label(row, text=f"Шаг {n} — {name}", bg="#ffffff",
                     anchor="w").pack(side="left", fill="x", expand=True)
            self.step_rows[n] = (row, badge)

        aux = ttk.Frame(r, padding=(12, 0))
        aux.pack(fill="x")
        self.abort_btn = ttk.Button(aux, text="Прервать", command=self.abort,
                                    state="disabled")
        self.abort_btn.pack(side="right")

        # автоматическая проверка файлов выбранного дрона/полётника
        self.check_lbl = tk.Label(r, text="", bg="#f4f7f9", anchor="w",
                                  justify="left", wraplength=580,
                                  font=("", 9))
        self.check_lbl.pack(fill="x", padx=12, pady=(6, 0))

        # статус текущего действия + «что требуется»
        st = ttk.Frame(r, padding=(12, 10, 12, 0))
        st.pack(fill="x")
        self.action_lbl = tk.Label(st, text="Готов к работе.", bg="#f4f7f9",
                                   fg="#333", anchor="w", font=("", 10))
        self.action_lbl.pack(fill="x")
        self.progress = ttk.Progressbar(st, mode="determinate", maximum=100,
                                        style="Green.Horizontal.TProgressbar")
        # подсказка о действии — крупно и жёлтым, чтобы сразу бросалась в глаза
        self.need_lbl = tk.Label(st, text="", bg="#ffe169", fg="#5a4500",
                                 anchor="w", justify="left", padx=14, pady=14,
                                 font=("", 14, "bold"), wraplength=560,
                                 bd=2, relief="solid")
        self.need_btn = ttk.Button(st, text="Готово — продолжить",
                                   command=lambda: self.send_stdin("\n"))

        # сворачиваемые подробности
        bot = ttk.Frame(r, padding=(12, 8, 12, 10))
        bot.pack(fill="both", expand=True)
        self.details_btn = ttk.Button(bot, text="Подробности ▸",
                                      command=self.toggle_details)
        self.details_btn.pack(anchor="w")
        self.log_text = tk.Text(bot, height=10, bg="#14191e", fg="#e4e6e7",
                                font=("monospace", 9), state="disabled",
                                wrap="word")
        self.log_visible = False

        self._fill_selectors()
        self.fc_cb.bind("<<ComboboxSelected>>", self._fc_changed)

    def _fill_selectors(self):
        d_names = [v.get("name", k) for k, v in self.drones.items()]
        self.drone_cb["values"] = d_names
        f_names = [v.get("name", k) for k, v in self.fcs.items()]
        self.fc_cb["values"] = f_names
        if d_names:
            self.drone_cb.current(0)
        self.drone_cb.bind("<<ComboboxSelected>>", self._drone_changed)
        self._drone_changed()

    def _drone_changed(self, *_):
        dr = self._sel(self.drones, self.drone_var.get())
        if dr:
            fc_id = dr.get("fc", "")
            fc = self.fcs.get(fc_id)
            if fc:
                self.fc_var.set(fc.get("name", fc_id))
            elif self.fc_cb["values"]:
                self.fc_cb.current(0)
        self._apply_beacon()
        self._fc_changed()

    def _beacon_on(self):
        """Нужен ли шаг 4 (отключение писка) для выбранного дрона."""
        dr = self._sel(self.drones, self.drone_var.get())
        return bool(dr) and str(dr.get("beacon", "no")).strip().lower() \
            in ("yes", "y", "1", "true", "on", "да")

    def _full_steps(self):
        return "1,2,3,4" if self._beacon_on() else "1,2,3"

    def _apply_beacon(self):
        """Показать/скрыть строку шага 4 по типу дрона."""
        row, _ = self.step_rows[4]
        if self._beacon_on():
            row.pack(fill="x", padx=8, pady=3)
        else:
            row.pack_forget()

    def _current_soft(self):
        """ПО (стек) для выбранного полётника — определяется однозначно.
        Приоритет: штатное ПО дрона (если подходит плате), иначе первое ПО,
        собранное под эту плату. None — для платы нет ПО (напр. SpeedyBee)."""
        fc_id = self._id(self.fcs, self.fc_var.get())
        fitting = [v for v in self.softs.values() if v.get("fc", "") == fc_id]
        dr = self._sel(self.drones, self.drone_var.get())
        want = dr.get("soft", "") if dr else ""
        if want and want in self.softs and self.softs[want].get("fc") == fc_id:
            return self.softs[want]
        return fitting[0] if fitting else None

    def _fc_changed(self, *_):
        """Плата сменилась — пересчитать ПО (справочно) и перепроверить файлы."""
        soft = self._current_soft()
        self.soft_lbl.config(
            text=f"ПО: {soft.get('name', '?')}" if soft
            else "ПО: нет (для этой платы стек не заведён)")
        self._validate()

    def _validate(self):
        """Автопроверка готовности: есть ли ПО для платы, исходники/файлы
        прошивки и файл параметров дрона."""
        import shutil
        probs = []
        if not shutil.which("dfu-util"):
            probs.append("не установлен dfu-util")

        def exists(p):
            p = os.path.expanduser(p)
            full = p if os.path.isabs(p) else os.path.join(HERE, p)
            return os.path.exists(full)

        soft = self._current_soft()
        dr = self._sel(self.drones, self.drone_var.get())
        if soft is None:
            probs.append("для этой платы нет ПО (напр. Betaflight не заведён)")
        elif soft.get("px4_src"):
            # прошивка собирается из исходников — файлы появятся при сборке;
            # достаточно, чтобы исходники были на месте
            if not exists(os.path.join(soft["px4_src"], ".git")):
                probs.append(f"нет исходников PX4: {soft['px4_src']}")
        else:
            for label, key in (("загрузчик", "bootloader"),
                               ("прошивка", "firmware"),
                               ("прошивка (.bin)", "firmware_bin")):
                p = soft.get(key, "")
                if not p or not exists(p):
                    probs.append(f"{label}: нет файла {p or '—'}")
        if dr:
            pf = dr.get("params_file", "")
            if not pf or not exists(pf):
                probs.append(f"параметры: нет файла {pf or '—'}")

        self.files_ok = not probs
        if probs:
            self.check_lbl.config(text="⚠ " + "; ".join(probs), fg="#aa3333")
        elif soft and soft.get("px4_src"):
            self.check_lbl.config(
                text="✓ Готово (прошивка соберётся из исходников)",
                fg="#1d6b32")
        else:
            self.check_lbl.config(text="✓ Файлы прошивки и параметров на месте",
                                  fg="#1d6b32")
        if not self.proc:
            self.full_btn.config(
                state="normal" if self.files_ok else "disabled")

    @staticmethod
    def _sel(table, shown_name):
        for k, v in table.items():
            if v.get("name", k) == shown_name:
                return v
        return None

    @staticmethod
    def _id(table, shown_name):
        for k, v in table.items():
            if v.get("name", k) == shown_name:
                return k
        return None

    def toggle_details(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_text.pack(fill="both", expand=True, pady=(6, 0))
            self.details_btn.config(text="Подробности ▾")
        else:
            self.log_text.pack_forget()
            self.details_btn.config(text="Подробности ▸")

    # ── запуск/остановка ─────────────────────────────────────────

    def run(self, steps, dry=False):
        if self.proc or not getattr(self, "files_ok", True):
            return
        cmd = [sys.executable, "-u", FLASH_SCRIPT]
        if steps:
            cmd += ["--steps", steps]
        if dry:
            cmd += ["--dry-run"]
        dr = self._sel(self.drones, self.drone_var.get())
        soft = self._current_soft()
        fc = self._sel(self.fcs, self.fc_var.get())
        if dr and soft:
            cmd += ["--config", build_cfg(dr, soft, fc, self.fcs)]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить:\n{e}")
            return
        wanted = {int(x) for x in steps.split(",") if x.strip().isdigit()}
        self.steps_state = {n: "wait" for n in wanted}
        self.aborted = False
        self._step_frac = 0.0
        self.log_lines, self.cur_line = [], ""
        self._clear_prompt()
        what = "проверка файлов" if dry else \
            {"1,2,3,4": "полная прошивка"}.get(steps, f"шаг {steps}")
        if dr:
            what += f" — {dr.get('name', '?')}"
        self.action_lbl.config(text=f"Выполняется: {what}…")
        self.progress.pack(fill="x", pady=(4, 0))
        self.progress["value"] = 0
        threading.Thread(target=self._reader, args=(self.proc,),
                         daemon=True).start()
        self._set_running(True)

    def abort(self):
        if self.proc:
            self.aborted = True
            self.proc.terminate()

    def send_stdin(self, s):
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(s.encode())
                self.proc.stdin.flush()
            except Exception:
                pass
        # строка-приглашение ещё висит в выводе, пока скрипт не напечатает
        # что-то новое — запоминаем её, чтобы не принять за новую паузу
        self.answered_key = self.prompt_key
        self._clear_prompt()

    def _set_running(self, running):
        self.full_btn.config(state="disabled" if running else "normal")
        for cb in (self.drone_cb, self.fc_cb):
            cb.config(state="disabled" if running else "readonly")
        self.abort_btn.config(state="normal" if running else "disabled")
        if not running:
            self._validate()

    # ── чтение вывода скрипта ────────────────────────────────────

    def _reader(self, proc):
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.out_q.put(chunk.decode("utf-8", errors="replace"))
        proc.wait()
        self.out_q.put(("__EXIT__", proc.returncode))

    def _board_poller(self):
        while True:
            self.board = board_state()
            time.sleep(1.2)

    # ── главный цикл ─────────────────────────────────────────────

    def _tick(self):
        self._update_board_label()
        exited = None
        drained = False
        while True:
            try:
                item = self.out_q.get_nowait()
            except queue.Empty:
                break
            drained = True
            if isinstance(item, tuple):
                exited = item[1]
            else:
                self._consume_text(item)
        if drained:
            self._refresh_steps()
            self._refresh_action()
        if self.proc:
            self.progress["value"] = self._progress_value()
        if exited is not None:
            self._finished(exited)
        self._check_prompt()
        self.root.after(150, self._tick)

    def _consume_text(self, text):
        for ch in text:
            if ch == "\n":
                self.log_lines.append(self.cur_line)
                self._parse_line(self.cur_line)
                self.cur_line = ""
            elif ch == "\r":
                self.cur_line = ""
            else:
                self.cur_line += ch
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        tail = self.log_lines[-200:] + ([self.cur_line]
                                        if self.cur_line else [])
        self.log_text.insert("1.0", "\n".join(tail))
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _parse_line(self, line):
        m = re.search(r"ШАГ (\d)", line)
        if m:
            n = int(m.group(1))
            # предыдущий шаг дошёл до следующего без строки об ошибке —
            # значит, завершился успешно (не ждём финального ИТОГа)
            for k, st in list(self.steps_state.items()):
                if k != n and st == "run":
                    self.steps_state[k] = "ok"
            self.steps_state[n] = "run"
            self._step_frac = 0.0   # новый шаг — бар с нуля
        if "шаги 1–2 пропущены" in line:
            self.steps_state[1] = self.steps_state[2] = "ok"
        m = re.search(r"шаг (\d+) \([^)]*\): (✓ OK|✗ ошибка)", line)
        if m:
            self.steps_state[int(m.group(1))] = \
                "ok" if "OK" in m.group(2) else "fail"
        m = re.search(r"⚠ шаг (\d+) завершился с ошибкой", line)
        if m:
            self.steps_state[int(m.group(1))] = "fail"

    # ── обновление виджетов ──────────────────────────────────────

    def _update_board_label(self):
        # пока скрипт ждёт плату в другом режиме — пишем прямо, что не так
        if self.proc and self.prompt_kind == "dfu" and self.board == "running":
            self.board_lbl.config(
                text="Плата подключена БЕЗ кнопки BOOT — переподключите, "
                     "зажав BOOT",
                bg="#ffd9d9", fg="#aa3333")
            return
        if self.proc and self.prompt_kind == "running" \
                and self.board == "dfu":
            self.board_lbl.config(
                text="Плата в BOOT-режиме — переподключите БЕЗ кнопки BOOT",
                bg="#ffd9d9", fg="#aa3333")
            return
        txt, bg, fg = {
            "dfu": ("Плата: в BOOT-режиме (DFU) — готова к прошивке",
                    "#fff3cd", "#7a5b00"),
            "running": ("Плата: подключена, работает в обычном режиме",
                        "#d9f2df", "#1d6b32"),
            "none": ("Плата: не подключена", "#e3e7ea", "#666666"),
        }[self.board]
        self.board_lbl.config(text=txt, bg=bg, fg=fg)

    def _refresh_steps(self):
        for n, (row, badge) in self.step_rows.items():
            badge.config(text=BADGE.get(self.steps_state.get(n), "▫"))

    def _progress_value(self):
        """Заполнение бара в пределах ТЕКУЩЕГО шага (0–100%). Прогресс-строки
        (N/M, проценты) приходят как завершённые строки, поэтому ищем самую
        свежую среди cur_line и последних строк лога — не заглядывая в
        предыдущий шаг (до ближайшего заголовка «ШАГ N»)."""
        frac = None
        for line in [self.cur_line] + list(reversed(self.log_lines[-20:])):
            if re.search(r"ШАГ \d", line):
                break   # дальше — уже предыдущий шаг
            mp = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            mr = re.search(r"(\d+)\s*/\s*(\d+)", line)
            if mp:
                frac = min(1.0, float(mp.group(1)) / 100)
                break
            if mr and int(mr.group(2)) > 0:
                frac = min(1.0, int(mr.group(1)) / int(mr.group(2)))
                break
        if frac is None:
            frac = self._step_frac   # свежего числа нет — держим прежнее
        self._step_frac = frac
        return frac * 100

    def _refresh_action(self):
        line = self.cur_line.strip() or \
            next((l.strip() for l in reversed(self.log_lines)
                  if l.strip()), "")
        running = next((n for n, s in self.steps_state.items()
                        if s == "run"), None)
        pct = int(self.progress["value"])
        prefix = f"Шаг {running} — {pct}%  ·  " if running else ""
        if line and self.proc:
            body = (line[:90] + "…") if len(line) > 90 else line
            self.action_lbl.config(text=prefix + body)

    def _finished(self, code):
        self.proc = None
        try:   # полный лог последнего запуска — для разбора проблем
            with open("/tmp/obrik_ui_last.log", "w", encoding="utf-8") as f:
                f.write("\n".join(self.log_lines + [self.cur_line]))
        except OSError:
            pass
        self.progress.pack_forget()
        self._clear_prompt()
        self._set_running(False)
        if getattr(self, "aborted", False):
            # прервано пользователем: недоигранные шаги — не ошибки
            for n, st in list(self.steps_state.items()):
                if st in ("run", "wait"):
                    self.steps_state[n] = "wait"
            self._refresh_steps()
            done = [n for n, st in self.steps_state.items() if st == "ok"]
            self.action_lbl.config(
                text="⏹ Прервано"
                     + (f" (шаги {', '.join(map(str, sorted(done)))} успели "
                        f"выполниться)." if done else "."))
            return
        for n, st in list(self.steps_state.items()):
            if st in ("run", "wait") and code:
                self.steps_state[n] = "fail"
        self._refresh_steps()
        bad = [n for n, st in self.steps_state.items() if st == "fail"]
        if code == 0 and not bad:
            self.action_lbl.config(text="✅ Готово — всё выполнено успешно.")
        else:
            self.action_lbl.config(
                text="❌ Завершено с ошибками"
                     + (f" (шаги: {', '.join(map(str, sorted(bad)))})."
                        if bad else "."))

    # ── паузы скрипта: автодетект + окошко ───────────────────────

    def _check_prompt(self):
        if not self.proc:
            return
        tail = self.cur_line.strip()
        waiting = "Enter" in tail or tail.endswith("[Y/n]:") \
            or tail.endswith("[y/N]:")
        if not waiting:
            self.answered_key = None   # скрипт что-то напечатал — пауза ушла
            if self.prompt_key:
                self._clear_prompt()
            return
        if tail == self.answered_key:
            return                     # на эту паузу уже ответили
        if tail != self.prompt_key:
            # новая пауза
            self.prompt_key = tail
            self.asked = False
            self.replug_gone = False
            ctx = "\n".join(self.log_lines[-12:])
            self.prompt_kind, human = classify_prompt(tail, ctx)
            self.need_lbl.config(text="Требуется: " + human)
            self.need_lbl.pack(fill="x", pady=(6, 0))
            self.need_btn.pack(anchor="e", pady=(6, 0))
            if self.prompt_kind == "yn" and not self.asked:
                self.asked = True
                cont = messagebox.askyesno(
                    "Ошибка шага",
                    "Шаг завершился с ошибкой.\nПродолжить со "
                    "следующими шагами?")
                self.send_stdin("\n" if cont else "n\n")
                return
            if self.prompt_kind == "mismatch" and not self.asked:
                self.asked = True
                cont = messagebox.askyesno(
                    "Не та плата",
                    human + "\n\nВсё равно прошивать?", default="no",
                    icon="warning")
                self.send_stdin("y\n" if cont else "n\n")
                return
        # для «переткните» ждём, пока плата сначала пропадёт, потом вернётся
        if self.prompt_kind == "replug":
            if self.board == "none":
                self.replug_gone = True
            if getattr(self, "replug_gone", False) and self.board == "running" \
                    and not self.asked:
                self.asked = True
                if messagebox.askyesno("Плата переподключена",
                                       "Плата снова загрузилась.\nПродолжаем?"):
                    self.send_stdin("\n")
            return
        # автодетект готовности платы
        ready = (self.prompt_kind in ("dfu", "running")
                 and self.board == self.prompt_kind) \
            or (self.prompt_kind == "plug" and self.board != "none")
        if ready and not self.asked:
            self.asked = True
            if self.prompt_kind == "plug":
                name = ("подключена (в BOOT-режиме)" if self.board == "dfu"
                        else "подключена и загрузилась")
            else:
                name = {"dfu": "в BOOT-режиме (DFU)",
                        "running": "загрузилась в обычном режиме"
                        }[self.prompt_kind]
            if messagebox.askyesno("Плата обнаружена",
                                   f"Плата {name}.\nПродолжаем?"):
                self.send_stdin("\n")
            # при «Нет» остаётся кнопка «Готово — продолжить»

    def _clear_prompt(self):
        self.prompt_key = None
        self.prompt_kind = None
        self.asked = False
        self.need_lbl.pack_forget()
        self.need_btn.pack_forget()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

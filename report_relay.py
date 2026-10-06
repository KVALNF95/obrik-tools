#!/usr/bin/env python3
"""report_relay — крошечный приёмник отчётов для Sverk Tools.

Ставится на ПУБЛИЧНО ДОСТУПНЫЙ хост (ваш сервер), а НЕ на рабочий ноут.
Ноут шлёт сюда POST /report с заголовком X-Token и JSON {"text": "..."};
relay проверяет токен и пересылает текст на почту. Почтовые секреты лежат
ТОЛЬКО здесь, на сервере, — с ноута украсть нечего (там лишь URL и токен,
которым можно только отправить отчёт, и который легко сменить).

Конфиг relay.cfg рядом (key=value):
    token     = длинная_случайная_строка      # тот же, что в report.cfg ноута
    smtp_host = smtp.yandex.ru
    smtp_port = 465
    smtp_user = sender@yandex.ru
    smtp_pass = пароль_приложения
    mail_to   = kuznetsovvova95@gmail.com
    mail_from = sender@yandex.ru
    bind      = 0.0.0.0
    port      = 8787

Запуск:  python3 report_relay.py
TLS лучше повесить рядом (nginx/Caddy) и проксировать на этот порт.
Только стандартная библиотека.
"""
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "relay.cfg")


def load_cfg():
    cfg = {}
    with open(CFG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def deliver(cfg, text):
    """Переслать текст отчёта выбранным способом: telegram или email."""
    method = cfg.get("method", "email").lower()
    if method == "telegram":
        send_telegram(cfg, text)
    else:
        send_mail(cfg, text)


def send_telegram(cfg, text):
    token, chat = cfg["tg_token"], cfg["tg_chat"]
    data = json.dumps({"chat_id": chat, "text": text[:4000],
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        body = r.read().decode("utf-8", "replace")
        if '"ok":true' not in body:
            raise RuntimeError(f"telegram: {body[:200]}")


def send_mail(cfg, text):
    msg = EmailMessage()
    msg["Subject"] = "Sverk Tools — отчёт о проблеме"
    msg["From"] = cfg.get("mail_from", cfg["smtp_user"])
    msg["To"] = cfg["mail_to"]
    msg.set_content(text)
    port = int(cfg.get("smtp_port", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=20, context=ctx) as s:
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_host"], port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_POST(self):
        if self.path.rstrip("/") != "/report":
            return self._reply(404, "not found")
        try:
            cfg = load_cfg()
        except Exception as e:
            return self._reply(500, f"relay config error: {e}")
        if self.headers.get("X-Token", "") != cfg.get("token", ""):
            return self._reply(401, "bad token")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            text = str(body.get("text", ""))[:20000]
        except Exception as e:
            return self._reply(400, f"bad body: {e}")
        try:
            deliver(cfg, text)
        except Exception as e:
            return self._reply(502, f"delivery failed: {e}")
        return self._reply(200, "ok")


def main():
    cfg = load_cfg()
    bind = cfg.get("bind", "0.0.0.0")
    port = int(cfg.get("port", "8787"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    print(f"report_relay слушает {bind}:{port} (POST /report)")
    srv.serve_forever()


if __name__ == "__main__":
    main()

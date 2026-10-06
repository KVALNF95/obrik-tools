#!/usr/bin/env python3
"""Отправка отчёта о проблеме владельцу инструмента.

Канал настраивается в report.cfg (рядом со скриптом), формат key=value:

  # Telegram: создать бота у @BotFather, взять токен; chat_id — свой
  # (узнать: написать боту и открыть
  #  https://api.telegram.org/bot<TOKEN>/getUpdates)
  method   = telegram
  tg_token = 123456:ABC...
  tg_chat  = 111222333

  # либо произвольный вебхук (POST JSON {text: ...}):
  # method      = webhook
  # webhook_url = https://example.com/hook

Только стандартная библиотека.
"""
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_CFG = os.path.join(HERE, "report.cfg")


def load_report_cfg():
    cfg = {}
    if os.path.exists(REPORT_CFG):
        with open(REPORT_CFG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    return cfg


def _post(url, data, timeout=15):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url, data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode("utf-8", "replace")


def send_report(text):
    """Отправить текст отчёта. Вернуть (ok, сообщение-для-пользователя)."""
    cfg = load_report_cfg()
    method = cfg.get("method", "").lower()
    if not method:
        return False, ("Отправка не настроена. Заполните report.cfg "
                       "(см. report.cfg.example).")
    try:
        if method == "telegram":
            token, chat = cfg.get("tg_token"), cfg.get("tg_chat")
            if not token or not chat:
                return False, "В report.cfg нет tg_token или tg_chat."
            # Telegram ограничивает сообщение ~4096 символами
            status, body = _post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": text[:4000],
                 "disable_web_page_preview": True})
            ok = status == 200 and '"ok":true' in body
            return ok, ("Отчёт отправлен." if ok
                        else f"Telegram вернул ошибку: {body[:200]}")
        if method == "webhook":
            url = cfg.get("webhook_url")
            if not url:
                return False, "В report.cfg нет webhook_url."
            status, _ = _post(url, {"text": text})
            ok = 200 <= status < 300
            return ok, ("Отчёт отправлен." if ok
                        else f"Вебхук вернул код {status}.")
        if method == "email":
            return _send_email(cfg, text)
        return False, f"Неизвестный method в report.cfg: {method}"
    except Exception as e:
        return False, f"Не удалось отправить: {e}"


def _send_email(cfg, text):
    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port", "465"))
    user = cfg.get("smtp_user")
    pw = cfg.get("smtp_pass")
    to = cfg.get("mail_to") or user
    frm = cfg.get("mail_from") or user
    if not host or not user or not pw:
        return False, "В report.cfg нет smtp_host/smtp_user/smtp_pass."
    msg = EmailMessage()
    msg["Subject"] = "Sverk Tools — отчёт о проблеме"
    msg["From"] = frm
    msg["To"] = to
    msg.set_content(text)
    ctx = ssl.create_default_context()
    try:
        if port == 465:   # SSL
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:             # STARTTLS (587)
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
        return True, f"Отчёт отправлен на {to}."
    except Exception as e:
        return False, f"Почта не отправилась: {e}"

"""Отправка сообщений в Telegram обычным HTTP-запросом (без запуска бота).

Если токена или чата нет — сообщение печатается в лог. Так сервис можно гонять и без Telegram.
"""
from __future__ import annotations

import html
import logging
import os

import requests

from storage.db import connect, get_setting

log = logging.getLogger(__name__)


def to_html(text: str) -> str:
    """Моноширинный блок (чтобы колонки стояли ровно); строки с командами (→ /explain) — вне блока, кликабельные."""
    body = [l for l in text.split("\n") if not l.startswith("→")]
    tail = [l for l in text.split("\n") if l.startswith("→")]
    out = f"<pre>{html.escape(chr(10).join(body))}</pre>"
    return out + ("\n" + html.escape("\n".join(tail)) if tail else "")


def chat_id() -> str | None:
    if os.getenv("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    with connect() as con:
        return get_setting(con, "chat_id")


def send(text: str) -> str | None:
    """Возвращает chat_id, если сообщение ушло; None — если отправить не удалось или некуда."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), chat_id()
    if not token or not chat:
        log.info("Telegram не настроен, сообщение в лог:\n%s", text)
        return None
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": to_html(text), "parse_mode": "HTML"}, timeout=15)
        if resp.status_code != 200:
            log.error("Telegram: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        return str(chat)
    except requests.RequestException as e:
        log.error("Telegram недоступен: %s", e)
        return None

"""Telegram-бот: команды. Запускается внутри pipeline/scheduler.py (или отдельно: python -m bot.telegram)."""
from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from ai import digest as ai_digest
from ai import explain as ai_explain
from analysis.anomalies import ALERT_KEYS
from bot import messages
from bot.notify import chat_id, to_html
from pipeline.config import load_config, setup_logging
from pipeline.timeutil import utcnow
from storage.db import connect, set_setting

log = logging.getLogger(__name__)

HELP = """Funding Radar — мониторинг funding на Binance, Bybit, OKX
/signals — лучшие пары для funding carry после комиссий
/funding — таблица по всем биржам сейчас
/spread BTC — история расхождений за сутки
/oi ETH — open interest
/explain SOL — AI-объяснение по инструменту
/digest — сводка прямо сейчас
/alerts — пороги алертов (например /alerts carry_alert_threshold_pct 20)
/status — здоровье коллекторов
Не торговая рекомендация."""


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(to_html(text), parse_mode=ParseMode.HTML)


def _allowed(update: Update) -> bool:
    """Бот личный: отвечает только чату владельца (тому, кто первым написал /start, или TELEGRAM_CHAT_ID)."""
    owner = chat_id()
    return owner is not None and str(update.effective_chat.id) == str(owner)


def _symbol(context, cfg) -> str | None:
    if not context.args:
        return None
    s = context.args[0].upper().replace("USDT", "")
    return s if s in cfg["symbols"] else None


def owner_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            return
        try:
            await handler(update, context)
        except Exception:
            log.exception("ошибка в команде %s", update.effective_message.text)
            await _reply(update, "Что-то сломалось, подробности в логе. Проверь /status.")
    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if chat_id() is None:                       # первый, кто написал, становится владельцем
        with connect() as con:
            set_setting(con, "chat_id", update.effective_chat.id)
        log.info("зарегистрирован чат %s", update.effective_chat.id)
    if _allowed(update):
        await update.effective_message.reply_text(HELP)


@owner_only
async def signals(update, context):
    with connect() as con:
        text = messages.signals_report(con, load_config(), utcnow())
    await _reply(update, text)


@owner_only
async def funding(update, context):
    with connect() as con:
        text = messages.funding_table(con, utcnow())
    await _reply(update, text)


@owner_only
async def spread(update, context):
    cfg = load_config()
    symbol = _symbol(context, cfg)
    if not symbol:
        return await _reply(update, "Формат: /spread BTC. Монеты: " + ", ".join(cfg["symbols"]))
    with connect() as con:
        text = messages.spread_report(con, cfg, symbol, utcnow())
    await _reply(update, text)


@owner_only
async def oi(update, context):
    cfg = load_config()
    symbol = _symbol(context, cfg)
    if not symbol:
        return await _reply(update, "Формат: /oi ETH. Монеты: " + ", ".join(cfg["symbols"]))
    with connect() as con:
        text = messages.oi_report(con, symbol, utcnow())
    await _reply(update, text)


@owner_only
async def explain(update, context):
    cfg = load_config()
    symbol = _symbol(context, cfg)
    if not symbol:
        return await _reply(update, "Формат: /explain SOL. Монеты: " + ", ".join(cfg["symbols"]))
    # обращение к модели занимает секунды — уводим в отдельный поток, чтобы бот не «завис»
    facts, ex = await asyncio.to_thread(ai_explain.explain, symbol)
    await _reply(update, ai_explain.render(symbol, facts, ex))


@owner_only
async def digest(update, context):
    await _reply(update, await asyncio.to_thread(ai_digest.make_digest))


@owner_only
async def alerts(update, context):
    cfg = load_config()
    carry_keys = {f"carry_{k}": f"carry.{k}" for k in ("alert_threshold_pct", "alert_min_stability_h")}
    if len(context.args) == 2 and (context.args[0] in ALERT_KEYS or context.args[0] in carry_keys):
        try:
            value = float(context.args[1].replace(",", "."))
            if value <= 0:
                raise ValueError
        except ValueError:
            return await _reply(update, "Значение должно быть положительным числом.")
        with connect() as con:
            set_setting(con, carry_keys.get(context.args[0], f"alerts.{context.args[0]}"), value)
    elif context.args:
        return await _reply(update, "Формат: /alerts spread_bp 2\nКлючи: " + ", ".join([*ALERT_KEYS, *carry_keys]))
    with connect() as con:
        text = messages.thresholds_report(con, cfg)
    await _reply(update, text)


@owner_only
async def status(update, context):
    with connect() as con:
        text = messages.status_report(con, utcnow())
    await _reply(update, text)


def build_app() -> Application | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    for cmd, fn in [("signals", signals), ("funding", funding), ("spread", spread), ("oi", oi), ("explain", explain),
                    ("digest", digest), ("alerts", alerts), ("status", status)]:
        app.add_handler(CommandHandler(cmd, fn))
    return app


if __name__ == "__main__":
    setup_logging(load_config())
    application = build_app()
    if application is None:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env")
    application.run_polling()       # long polling: бот сам спрашивает у Telegram новые сообщения

"""Команды бота на поддельном Update: отвечает владельцу, молчит для чужих."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from analysis import spreads
from bot import telegram as tg
from pipeline import normalize as n
from pipeline.timeutil import floor_slot, utcnow
from storage.db import upsert


def fake_update(chat):
    message = SimpleNamespace(reply_text=AsyncMock(), text="/cmd")
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat), effective_message=message)


def run(handler, chat=111, args=()):
    update = fake_update(chat)
    asyncio.run(handler(update, SimpleNamespace(args=list(args))))
    calls = update.effective_message.reply_text.call_args_list
    return calls[-1].args[0] if calls else None


def test_commands(db, load, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    slot = floor_slot(utcnow())
    with db() as con:
        for exchange in n.EXCHANGES:
            rows, _ = n.normalize_funding(load(f"{exchange}_funding"), ["BTC", "ETH", "SOL"], slot)
            upsert(con, "funding", rows, ["exchange", "symbol", "ts"])
            oi = n.normalize_oi(load(f"{exchange}_oi"), ["BTC", "ETH"], slot)
            upsert(con, "open_interest", oi, ["exchange", "symbol", "ts"])
            upsert(con, "open_interest", [{**r, "ts": slot - timedelta(hours=1), "oi_usd": r["oi_usd"] / 1.041}
                                          for r in oi], ["exchange", "symbol", "ts"])
        spreads.compute_spreads(con)

    assert "+0.0312" in run(tg.funding)
    assert "2.14 bp" in run(tg.spread, args=["btc"])
    assert "Формат" in run(tg.spread)                           # без аргумента — подсказка
    assert "+4.1%" in run(tg.oi, args=["BTC"])
    assert "AI-объяснение недоступно" in run(tg.explain, args=["BTC"])
    assert "Market pulse" in run(tg.digest)
    assert "Коллекторы ещё не запускались" in run(tg.status)
    assert "spread_bp = 1.5" in run(tg.alerts)
    assert "spread_bp = 2" in run(tg.alerts, args=["spread_bp", "2"])
    assert "Формат" in run(tg.alerts, args=["bad_key", "1"])
    assert run(tg.funding, chat=999) is None                    # чужой чат — тишина


def test_start_registers_first_chat(db, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert "Funding Radar" in run(tg.start, chat=555)
    assert run(tg.start, chat=777) is None                      # второй человек владельцем не становится
    assert run(tg.status, chat=555) is not None


def test_app_builds_with_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    assert tg.build_app() is not None

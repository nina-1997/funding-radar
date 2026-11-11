"""Spread, upsert без дублей, эпизоды и дедупликация алертов."""
from datetime import datetime, timedelta

import pytest

from analysis import anomalies, spreads
from pipeline import normalize as n
from pipeline.config import load_config
from storage.db import upsert

SLOT = datetime(2026, 11, 11, 15, 0)
SYMBOLS = ["BTC", "ETH", "SOL"]
KEYS = ["exchange", "symbol", "ts"]


def load_all(con, load, slot=SLOT):
    for exchange in n.EXCHANGES:
        rows, _ = n.normalize_funding(load(f"{exchange}_funding"), SYMBOLS, slot)
        upsert(con, "funding", rows, KEYS)


def test_spread_from_rates():
    s = spreads.spread_from_rates({"binance": 0.0001, "bybit": 0.000312, "okx": 0.000098})
    assert s["spread_bp"] == pytest.approx(2.14)                 # (0.0312 % - 0.0098 %) = 2.14 б.п.
    assert (s["exchange_hi"], s["exchange_lo"]) == ("bybit", "okx")
    assert s["annualized_hi"] == pytest.approx(0.34164)          # 34.2 % годовых
    assert spreads.spread_from_rates({"binance": 0.0001}) is None


def test_upsert_twice_does_not_duplicate(db, load):
    """Сдача шага 2: загрузили дважды — COUNT(*) не изменился."""
    with db() as con:
        load_all(con, load)
        first = con.execute("SELECT count(*) FROM funding").fetchone()[0]
        load_all(con, load)
        assert con.execute("SELECT count(*) FROM funding").fetchone()[0] == first == 9


def test_upsert_updates_existing_row(db):
    row = {"exchange": "binance", "symbol": "BTC", "ts": SLOT, "funding_rate": 0.0001,
           "mark_price": 100.0, "index_price": None, "next_funding_ts": None, "raw_rate": 0.0001, "raw_interval_h": 8.0}
    with db() as con:
        upsert(con, "funding", [row], KEYS)
        upsert(con, "funding", [{**row, "funding_rate": 0.0002}], KEYS)
        assert con.execute("SELECT count(*), max(funding_rate) FROM funding").fetchone() == (1, 0.0002)
        assert con.execute("SELECT index_price FROM funding").fetchone()[0] is None    # NULL, а не NaN и не 0


def test_sql_spread_matches_python(db, load):
    with db() as con:
        load_all(con, load)
        assert spreads.compute_spreads(con) == 3
        assert spreads.compute_spreads(con) == 3                 # пересчёт не плодит строки
        row = con.execute("SELECT spread_bp, exchange_hi, exchange_lo FROM spreads WHERE symbol = 'BTC'").fetchone()
        rates = dict(con.execute("SELECT exchange, funding_rate FROM funding WHERE symbol = 'BTC'").fetchall())
    expected = spreads.spread_from_rates(rates)
    assert row[0] == pytest.approx(expected["spread_bp"])
    assert row[1:] == (expected["exchange_hi"], expected["exchange_lo"])


def test_spread_episode_opens_once_and_closes(db, load):
    """Один эпизод = одна строка в anomalies, сколько бы пятиминуток он ни длился."""
    cfg = load_config()
    with db() as con:
        for i in range(4):                                       # 20 минут spread BTC = 2.14 б.п. > порога 1.5
            now = SLOT + timedelta(minutes=5 * i)
            load_all(con, load, now)
            spreads.compute_spreads(con)
            anomalies.update_spread_episodes(con, cfg, now)
        rows = con.execute("SELECT symbol, ts, resolved_ts FROM anomalies WHERE type = 'spread'").fetchall()
        assert rows == [("BTC", SLOT, None)]

        later = SLOT + timedelta(minutes=20)                     # ставки сошлись
        calm = [{"exchange": e, "symbol": "BTC", "ts": later, "funding_rate": 0.0001, "mark_price": None,
                 "index_price": None, "next_funding_ts": None, "raw_rate": 0.0001, "raw_interval_h": 8.0}
                for e in n.EXCHANGES]
        upsert(con, "funding", calm, KEYS)
        spreads.compute_spreads(con)
        anomalies.update_spread_episodes(con, cfg, later)
        assert con.execute("SELECT resolved_ts FROM anomalies WHERE symbol = 'BTC'").fetchone()[0] == later


def test_alerts_are_deduplicated(db, load, monkeypatch):
    """Сдача шага 6: алерт об открытии один, потом одно «закрылся», лишних сообщений нет."""
    from bot import alerts
    cfg = load_config()
    monkeypatch.setitem(cfg["ai"], "explain_in_alerts", False)
    sent = []
    fake_send = lambda text: sent.append(text) or "chat"

    for i in range(6):                                           # 25 минут расхождения
        now = SLOT + timedelta(minutes=5 * i)
        with db() as con:
            load_all(con, load, now)
            spreads.compute_spreads(con)
            anomalies.update_spread_episodes(con, cfg, now)
        alerts.process(now, send=fake_send)
    assert len(sent) == 1                                        # первые 15 минут молчим, потом одно сообщение
    assert sent[0].startswith("⚡ Funding spread · BTC")
    assert "Spread Bybit–OKX: 2.14 bp" in sent[0]

    later = SLOT + timedelta(minutes=30)
    with db() as con:
        con.execute("UPDATE anomalies SET resolved_ts = ?", [later])
    alerts.process(later, send=fake_send)
    alerts.process(later + timedelta(minutes=5), send=fake_send)
    assert len(sent) == 2 and "закрылся" in sent[1]

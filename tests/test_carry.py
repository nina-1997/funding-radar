"""Carry: формулы сверены с ручным расчётом (acceptance из ТЗ), выбор пары, знаки, интервалы, алерты."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from analysis import carry
from pipeline.config import load_config

NOW = datetime(2026, 11, 11, 16, 10)


def test_manual_example_avax():
    """Реальные ставки AVAX 11.11 16:10: Binance +0.0059 %, Bybit −0.0134 %, OKX +0.0005 % за 8 ч.

    Ручной расчёт (лонг Bybit, шорт Binance):
      годовые: Bybit −0.0134 × 1095 = −14.673 %, Binance 0.0059 × 1095 = +6.4605 %
      gross = 6.4605 + 14.673 = 21.1335 %
      издержки = 2×5.5 + 2×5 + 3 = 24 bp = 0.24 %;  × 365 / 7 = 12.514 %
      нетто = 21.1335 − 12.514 = 8.619 %
    """
    cfg = load_config()
    p = carry.best_pair({"binance": 0.000059, "bybit": -0.000134, "okx": 0.000005}, cfg, holding_days=7)
    assert (p["long_ex"], p["short_ex"]) == ("bybit", "binance")
    assert p["gross_pct"] == pytest.approx(21.1335)
    assert p["cost_bps"] == pytest.approx(24)
    assert p["net_pct"] == pytest.approx(21.1335 - 0.24 * 365 / 7)
    assert p["spread_bp_8h"] == pytest.approx(1.93)


def test_longer_holding_makes_costs_lighter():
    cfg = load_config()
    rates = {"binance": 0.0001, "bybit": 0.0003}
    assert carry.best_pair(rates, cfg, 30)["net_pct"] > carry.best_pair(rates, cfg, 7)["net_pct"] \
        > carry.best_pair(rates, cfg, 1)["net_pct"]


def test_sign_short_the_expensive_leg():
    """funding > 0: лонги платят шортам. Шортим там, где ставка выше."""
    cfg = load_config()
    p = carry.best_pair({"binance": -0.0002, "okx": 0.0004}, cfg, 7)
    assert (p["long_ex"], p["short_ex"]) == ("binance", "okx")
    assert p["gross_pct"] > 0


def test_fees_can_change_best_pair(monkeypatch):
    """Крайняя пара не всегда лучшая: дорогая комиссия на «самой дешёвой» бирже перевешивает."""
    cfg = load_config()
    monkeypatch.setitem(cfg["carry"]["fees_bps"], "bybit", {"taker": 60.0, "maker": 2.0})
    p = carry.best_pair({"bybit": -0.0001, "okx": -0.00009, "binance": 0.0002}, cfg, 7)
    assert p["long_ex"] == "okx"


def test_annual_uses_normalized_rate():
    """В базе ставка уже за 8 ч: 0.01 % раз в 4 ч хранится как 0.02 % за 8 ч и даёт вдвое больше годовых."""
    assert carry.annual_pct(0.0002) == pytest.approx(2 * carry.annual_pct(0.0001))
    assert carry.annual_pct(0.0001) == pytest.approx(10.95)


def make_frames(minutes=60, short_rate=0.0006):
    """История: лонг binance 0.0001, шорт okx short_rate, снимки каждые 5 минут."""
    ts = [NOW - timedelta(minutes=5 * i) for i in range(minutes // 5, -1, -1)]
    hist = pd.DataFrame([{"ts": t, "exchange": e, "symbol": "BTC", "funding_rate": r}
                         for t in ts for e, r in (("binance", 0.0001), ("okx", short_rate))])
    latest = pd.DataFrame([{"exchange": e, "symbol": "BTC", "ts": NOW, "funding_rate": r, "raw_rate": r,
                            "raw_interval_h": 8.0, "mark_price": 100.0, "next_funding_ts": NOW + timedelta(minutes=10),
                            "oi_usd": 1e9} for e, r in (("binance", 0.0001), ("okx", short_rate))])
    return latest, hist


def test_signals_table():
    cfg = load_config()
    latest, hist = make_frames()
    df = carry.signals(latest, hist, {("okx", "BTC"): 4.2}, cfg, NOW, holding_days=7, min_net=0)
    r = df.iloc[0]
    assert (r.long_ex, r.short_ex) == ("binance", "okx")
    assert r.stability_h == pytest.approx(1.0)                  # 13 снимков подряд = 60 минут
    assert r.funding_soon                                       # фандинг через 10 минут < 15
    assert r.fresh and r.oi_24h_short_pct == 4.2


def test_min_net_and_min_oi_filter():
    cfg = load_config()
    latest, hist = make_frames(short_rate=0.00012)              # крошечный спред не переживает издержки
    assert carry.signals(latest, hist, {}, cfg, NOW, 7, min_net=0).empty
    latest, hist = make_frames()
    assert carry.signals(latest, hist, {}, cfg, NOW, 7, min_net=0, min_oi_usd=2e9).empty


def test_gap_breaks_stability():
    cfg = load_config()
    latest, hist = make_frames(minutes=120)
    hist = hist[(hist.ts > NOW - timedelta(minutes=30)) | (hist.ts < NOW - timedelta(minutes=60))]   # дыра 30 мин
    df = carry.signals(latest, hist, {}, cfg, NOW, 7, min_net=0)
    assert df.iloc[0].stability_h == pytest.approx(25 / 60)


def test_stale_data_is_flagged():
    cfg = load_config()
    latest, hist = make_frames()
    latest["ts"] = NOW - timedelta(minutes=40)
    assert not carry.signals(latest, hist, {}, cfg, NOW, 7, min_net=0).iloc[0].fresh


def test_carry_alert_sent_once_and_cooldown(db, monkeypatch):
    """Алерт приходит при превышении порога и не спамит: эпизод + cooldown 2 ч."""
    from bot import alerts
    from storage.db import upsert
    cfg = load_config()
    monkeypatch.setitem(cfg["ai"], "explain_in_alerts", False)
    sent = []

    def tick(now, short_rate):
        with db() as con:
            upsert(con, "funding", [
                {"exchange": e, "symbol": "BTC", "ts": now, "funding_rate": r, "mark_price": 100.0, "index_price": None,
                 "next_funding_ts": now + timedelta(hours=1), "raw_rate": r, "raw_interval_h": 8.0}
                for e, r in (("binance", 0.0001), ("okx", short_rate))], ["exchange", "symbol", "ts"])
            carry.update_carry_episodes(con, cfg, now)
        alerts.process(now, send=lambda text: sent.append(text) or "chat")

    monkeypatch.setitem(cfg["carry"], "alert_min_stability_h", 0.5)
    start = NOW - timedelta(hours=4)
    for i in range(13):                 # час сильного спреда: алерт после 30 мин устойчивости, один раз
        tick(start + timedelta(minutes=5 * i), 0.0006)
    assert len([t for t in sent if t.startswith("💰")]) == 1
    assert "Лонг Binance" in sent[0] and "Шорт OKX" in sent[0]

    t = start + timedelta(minutes=5 * 13)
    tick(t, 0.0001)                     # спред пропал -> «закрылся»
    assert "закрылся" in sent[-1]
    n = len(sent)
    for i in range(1, 10):              # вернулся и снова держится 40 мин, но с первого алерта < 2 ч — тишина
        tick(t + timedelta(minutes=5 * i), 0.0006)
    assert len(sent) == n
    t2 = t + timedelta(minutes=45)
    tick(t2, 0.0001)                    # подавленный эпизод закрывается молча
    assert len(sent) == n
    for i in range(12, 20):             # спустя 2+ ч после первого алерта — новый эпизод снова даёт алерт
        tick(t2 + timedelta(minutes=5 * i), 0.0006)
    assert len(sent) == n + 1 and sent[-1].startswith("💰")

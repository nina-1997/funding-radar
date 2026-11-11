"""Демо: СИНТЕТИЧЕСКИЕ данные за 7 дней в data/demo.duckdb — посмотреть бота и дашборд, не дожидаясь сбора.

  python -m scripts.seed_demo
  FR_DB=data/demo.duckdb python -m streamlit run dashboard/app.py

Числа случайные и к рынку отношения не имеют. В конец ряда подмешаны три «события»,
чтобы сработали алерты: spread BTC на Bybit, всплеск объёма DOGE, рост OI на Bybit.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import numpy as np

os.environ["FR_DB"] = "data/demo.duckdb"

from analysis import anomalies, spreads              # noqa: E402
from bot import alerts, messages                     # noqa: E402
from pipeline.config import load_config              # noqa: E402
from pipeline.timeutil import floor_slot, utcnow     # noqa: E402
from storage.db import connect, upsert               # noqa: E402

EXCHANGES = ["binance", "bybit", "okx"]
START_PRICE = {"BTC": 102000, "ETH": 3800, "SOL": 180, "XRP": 2.4, "DOGE": 0.21, "BNB": 940, "ADA": 0.62, "AVAX": 24}
OI_USD = {"BTC": 6e9, "ETH": 4e9, "SOL": 1.5e9, "XRP": 9e8, "DOGE": 7e8, "BNB": 5e8, "ADA": 3e8, "AVAX": 2e8}


def main() -> None:
    cfg = load_config()
    cfg["ai"]["explain_in_alerts"] = False           # демо не тратит деньги на модель
    Path(cfg["paths"]["db"]).unlink(missing_ok=True)
    rng = np.random.default_rng(7)
    now = floor_slot(utcnow())
    n = 7 * 288
    slots = [now - timedelta(minutes=5 * (n - 1 - i)) for i in range(n)]

    market = rng.normal(0, 0.0012, n)                # общий фактор: монеты ходят вместе
    funding, klines, oi = [], [], []
    for symbol in cfg["symbols"]:
        returns = 0.85 * market + rng.normal(0, 0.0008, n)
        price = START_PRICE[symbol] * np.exp(np.cumsum(returns))
        base_rate = 0.0001 + np.cumsum(rng.normal(0, 0.000004, n))      # медленно плавающая ставка
        for exchange in EXCHANGES:
            interval = 4.0 if (symbol, exchange) == ("SOL", "bybit") else 8.0
            rate = base_rate + rng.normal(0, 0.000022, n)
            volume = rng.lognormal(0, 0.2, n) * 3e6 / price
            if (symbol, exchange) == ("BTC", "bybit"):
                rate[-9:] += 0.00021                                     # spread последние 40 минут
            if (symbol, exchange) == ("AVAX", "bybit"):
                rate[-60:] -= 0.00035                                    # устойчивый carry последние 5 часов
            if symbol == "DOGE":
                volume[-2] *= 6                                          # всплеск в последней закрытой свече
                volume[-110] *= 5                                        # и один ночью
            for i, ts in enumerate(slots):
                step = int(interval)
                next_ts = ts.replace(minute=0, second=0) + timedelta(hours=step - ts.hour % step)
                funding.append({"exchange": exchange, "symbol": symbol, "ts": ts, "funding_rate": rate[i],
                                "mark_price": price[i], "index_price": price[i] * 0.9999, "next_funding_ts": next_ts,
                                "raw_rate": rate[i] * interval / 8, "raw_interval_h": interval})
                if i < n - 1:                                            # текущая свеча ещё не закрыта
                    klines.append({"exchange": exchange, "symbol": symbol, "ts": ts, "interval": "5m",
                                   "open": price[i - 1] if i else price[0], "high": price[i] * 1.0008,
                                   "low": price[i] * 0.9992, "close": price[i], "volume": volume[i],
                                   "quote_volume": volume[i] * price[i], "trades": None})
            level = OI_USD[symbol] * {"binance": 1.0, "bybit": 0.7, "okx": 0.45}[exchange]
            hours = [i for i, ts in enumerate(slots) if ts.minute == 0]
            walk = level * np.exp(np.cumsum(rng.normal(0.0002, 0.006, len(hours))))
            if (symbol, exchange) == ("BTC", "bybit"):
                walk[-1] = walk[-2] * 1.058
            for j, i in enumerate(hours):
                oi.append({"exchange": exchange, "symbol": symbol, "ts": slots[i],
                           "oi_contracts": walk[j] / price[i], "oi_usd": walk[j]})

    sent = []
    with connect() as con:
        upsert(con, "funding", funding, ["exchange", "symbol", "ts"])
        upsert(con, "klines", klines, ["exchange", "symbol", "ts", "interval"])
        upsert(con, "open_interest", oi, ["exchange", "symbol", "ts"])
        spreads.compute_spreads(con)
        for exchange in EXCHANGES:
            for kind in ("funding", "oi", "klines"):
                con.execute("INSERT INTO collector_runs (collector, started, finished, status, rows) "
                            "VALUES (?, ?, ?, 'ok', 8)", [f"{exchange}.{kind}", utcnow() - timedelta(seconds=40), utcnow() - timedelta(seconds=35)])
    # «проигрываем» последние 14 часов так, как это делал бы планировщик
    for ts in slots[-168:]:
        with connect() as con:
            anomalies.update_all(con, cfg, ts)
        alerts.process(ts, send=lambda text: sent.append((ts, text)) or "demo")

    with connect() as con:
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("funding", "klines", "open_interest", "spreads", "anomalies", "alerts")}
        blocks = [messages.funding_table(con, now), messages.spread_report(con, cfg, "BTC", now),
                  messages.oi_report(con, "BTC", now), messages.status_report(con, now)]
    print("База:", cfg["paths"]["db"], counts)
    print(f"\n===== алерты за последние 14 часов: {len(sent)} =====")
    for ts, text in sent[-4:]:
        print(f"\n[{ts:%d.%m %H:%M} UTC]\n{text}")
    with connect() as con:
        blocks.append(messages.signals_report(con, cfg, now))
    for title, block in zip(("/funding", "/spread BTC", "/oi BTC", "/status", "/signals"), blocks):
        print(f"\n===== {title} =====\n{block}")
    from ai.digest import make_digest
    print("\n===== утренняя сводка =====\n" + make_digest(now))


if __name__ == "__main__":
    main()

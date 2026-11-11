"""Расхождение funding между биржами. Аналитика не знает про API — только про таблицы."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from pipeline.timeutil import utcnow


def spread_from_rates(rates: dict[str, float]) -> dict | None:
    """{'binance': 0.0001, 'bybit': 0.000312, ...} -> spread в б.п., кто дороже, кто дешевле.

    Та же формула, что в SQL ниже, — на Python, чтобы её можно было проверить тестом и объяснить.
    """
    if len(rates) < 2:
        return None
    hi = max(rates, key=rates.get)
    lo = min(rates, key=rates.get)
    return {
        "max_rate": rates[hi], "min_rate": rates[lo],
        "spread_bp": (rates[hi] - rates[lo]) * 10_000,
        "exchange_hi": hi, "exchange_lo": lo,
        "annualized_hi": rates[hi] * 3 * 365, "annualized_lo": rates[lo] * 3 * 365,
    }


def compute_spreads(con, since: datetime | None = None) -> int:
    """Пересчитать spreads из funding начиная с since (по умолчанию — всё). Повторный запуск безопасен."""
    since = since or datetime(2000, 1, 1)
    con.execute(
        """
        INSERT OR REPLACE INTO spreads
        SELECT symbol, ts,
               max(funding_rate), min(funding_rate),
               (max(funding_rate) - min(funding_rate)) * 10000,
               arg_max(exchange, funding_rate), arg_min(exchange, funding_rate),
               max(funding_rate) * 3 * 365, min(funding_rate) * 3 * 365
        FROM funding
        WHERE ts >= ?
        GROUP BY symbol, ts
        HAVING count(*) >= 2            -- spread имеет смысл минимум для двух бирж
        """,
        [since],
    )
    return con.execute("SELECT count(*) FROM spreads WHERE ts >= ?", [since]).fetchone()[0]


def latest_slot(con, now: datetime | None = None) -> datetime | None:
    q = "SELECT max(ts) FROM funding" + (" WHERE ts <= ?" if now else "")
    return con.execute(q, [now] if now else []).fetchone()[0]


def current_funding(con, now: datetime | None = None) -> pd.DataFrame:
    """Funding по всем биржам на последнем слоте: symbol, exchange, funding_rate, mark_price, raw_interval_h."""
    slot = latest_slot(con, now)
    if slot is None:
        return pd.DataFrame()
    return con.execute(
        "SELECT symbol, exchange, ts, funding_rate, mark_price, raw_rate, raw_interval_h "
        "FROM funding WHERE ts = ? ORDER BY symbol, exchange", [slot]).df()


def current_spreads(con, now: datetime | None = None) -> pd.DataFrame:
    """Последний spread по каждой монете."""
    q = ("SELECT * FROM spreads {where} "
         "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = 1 ORDER BY spread_bp DESC")
    return con.execute(q.format(where="WHERE ts <= ?" if now else ""), [now] if now else []).df()


def spread_history(con, symbol: str, hours: int = 24, now: datetime | None = None) -> pd.DataFrame:
    now = now or utcnow()
    return con.execute(
        "SELECT * FROM spreads WHERE symbol = ? AND ts > ? AND ts <= ? ORDER BY ts",
        [symbol, now - timedelta(hours=hours), now]).df()


def spread_stats(con, symbol: str, days: int = 7) -> dict | None:
    """Типичный spread — чтобы понимать, много ли «сейчас» и выбирать порог по распределению."""
    row = con.execute(
        "SELECT count(*), median(spread_bp), quantile_cont(spread_bp, 0.95), max(spread_bp) "
        "FROM spreads WHERE symbol = ? AND ts > now()::TIMESTAMP - to_days(?)", [symbol, days]).fetchone()
    if not row or not row[0]:
        return None
    return {"n": row[0], "median_bp": row[1], "p95_bp": row[2], "max_bp": row[3]}

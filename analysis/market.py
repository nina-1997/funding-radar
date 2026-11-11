"""Цены и их изменения — из свечей той биржи, где данных больше всего."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from analysis.correlations import prices


def price_changes(con, now: datetime) -> pd.DataFrame:
    """symbol, price, chg_1h_pct, chg_24h_pct."""
    p = prices(con, days=2, now=now)
    rows = []
    if p.empty:
        return pd.DataFrame(columns=["symbol", "price", "chg_1h_pct", "chg_24h_pct"])
    for symbol in p.columns:
        s = p[symbol].dropna()
        if s.empty:
            continue
        last_ts, last = s.index[-1], s.iloc[-1]

        def change(delta: timedelta):
            past = s[s.index <= last_ts - delta]
            # прошлое значение должно быть действительно «около» нужного момента
            if past.empty or past.index[-1] < last_ts - delta - timedelta(minutes=30):
                return None
            return float((last / past.iloc[-1] - 1) * 100)

        rows.append({"symbol": symbol, "price": float(last),
                     "chg_1h_pct": change(timedelta(hours=1)), "chg_24h_pct": change(timedelta(hours=24))})
    return pd.DataFrame(rows)

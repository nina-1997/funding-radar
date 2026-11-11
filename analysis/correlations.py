"""Корреляции монет: Пирсон по 5-минутным доходностям за окно в N дней."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from pipeline.timeutil import utcnow


def prices(con, days: int = 7, now: datetime | None = None, exchange: str | None = None) -> pd.DataFrame:
    """Таблица close: строки — время, колонки — монеты. Берём одну биржу, где свечей больше всего."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    if exchange is None:
        row = con.execute(
            "SELECT exchange FROM klines WHERE ts > ? AND ts <= ? GROUP BY exchange ORDER BY count(*) DESC LIMIT 1",
            [since, now]).fetchone()
        if not row:
            return pd.DataFrame()
        exchange = row[0]
    df = con.execute(
        "SELECT ts, symbol, close FROM klines WHERE exchange = ? AND ts > ? AND ts <= ?",
        [exchange, since, now]).df()
    return df.pivot(index="ts", columns="symbol", values="close").sort_index() if not df.empty else df


def correlation_matrix(con, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    p = prices(con, days, now)
    if p.empty or len(p) < 30:
        return pd.DataFrame()
    returns = p.pct_change().dropna(how="all")       # доходность = (цена / предыдущая цена) - 1
    return returns.corr(method="pearson", min_periods=30)


def mean_pairwise(corr: pd.DataFrame) -> float | None:
    """Средняя корреляция по всем парам (без диагонали): «насколько рынок ходит вместе»."""
    n = len(corr)
    if n < 2:
        return None
    total = corr.values.sum() - n                    # убираем единицы на диагонали
    value = total / (n * (n - 1))
    return None if pd.isna(value) else float(value)


def pair(corr: pd.DataFrame, a: str, b: str) -> float | None:
    if a in corr.index and b in corr.columns and not pd.isna(corr.loc[a, b]):
        return float(corr.loc[a, b])
    return None

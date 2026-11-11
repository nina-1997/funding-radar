"""Аномалии и «эпизоды».

Эпизод — непрерывный отрезок, пока метрика выше порога. В таблице anomalies это одна строка:
открылась (resolved_ts IS NULL) -> обновляется каждые 5 минут -> закрылась (resolved_ts заполнен).
Благодаря этому алерт уходит один раз на эпизод, а не каждые 5 минут.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from storage.db import get_setting

log = logging.getLogger(__name__)
ALERT_KEYS = ("spread_bp", "spread_min_duration_min", "volume_zscore", "oi_change_1h_pct")


def threshold(con, cfg: dict, key: str) -> float:
    """Порог: сначала то, что задали через /alerts в боте, иначе config.yaml."""
    return float(get_setting(con, f"alerts.{key}", cfg["alerts"][key]))


# ---------- метрики ----------

def volume_zscores(con, now: datetime) -> pd.DataFrame:
    """z-score объёма последней закрытой 5-мин свечи к среднему за предыдущие 24 ч (288 свечей)."""
    return con.execute(
        """
        WITH w AS (
            SELECT exchange, symbol, ts, volume,
                   avg(volume)         OVER win AS mean,
                   stddev_samp(volume) OVER win AS sd,
                   count(*)            OVER win AS n
            FROM klines
            WHERE ts > ?::TIMESTAMP - INTERVAL 25 HOUR AND ts <= ?
            WINDOW win AS (PARTITION BY exchange, symbol ORDER BY ts
                           ROWS BETWEEN 288 PRECEDING AND 1 PRECEDING)   -- текущая свеча в среднее не входит
        )
        SELECT exchange, symbol, ts, volume, (volume - mean) / sd AS zscore
        FROM w
        WHERE n >= 50 AND sd > 0
        QUALIFY row_number() OVER (PARTITION BY exchange, symbol ORDER BY ts DESC) = 1
        """,
        [now, now],
    ).df()


def oi_changes(con, now: datetime) -> pd.DataFrame:
    """Изменение open interest за 1 ч и 24 ч, %: exchange, symbol, ts, oi_usd, chg_1h_pct, chg_24h_pct."""
    df = con.execute(
        "SELECT exchange, symbol, ts, coalesce(oi_usd, oi_contracts) AS oi FROM open_interest "
        "WHERE ts > ?::TIMESTAMP - INTERVAL 27 HOUR AND ts <= ? ORDER BY ts", [now, now]).df()
    rows = []
    for (exchange, symbol), g in df.groupby(["exchange", "symbol"]):
        cur = g.iloc[-1]

        def change(ago_from: timedelta, ago_to: timedelta):
            # ближайшая запись в окне [ts - ago_to, ts - ago_from]
            past = g[(g.ts <= cur.ts - ago_from) & (g.ts >= cur.ts - ago_to)]
            if past.empty or not past.iloc[-1].oi:
                return None
            return (cur.oi / past.iloc[-1].oi - 1) * 100

        rows.append({"exchange": exchange, "symbol": symbol, "ts": cur.ts, "oi_usd": cur.oi,
                     "chg_1h_pct": change(timedelta(minutes=55), timedelta(hours=2)),
                     "chg_24h_pct": change(timedelta(hours=23, minutes=30), timedelta(hours=26))})
    return pd.DataFrame(rows, columns=["exchange", "symbol", "ts", "oi_usd", "chg_1h_pct", "chg_24h_pct"])


# ---------- эпизоды ----------

def _sync_episode(con, type_: str, symbol: str, exchange: str | None, active: bool,
                  value: float, zscore: float | None, window_min: int, now: datetime) -> str | None:
    """Открыть / продлить / закрыть эпизод. Возвращает 'opened', 'closed' или None."""
    open_row = con.execute(
        "SELECT id FROM anomalies WHERE type = ? AND symbol = ? AND resolved_ts IS NULL",
        [type_, symbol]).fetchone()
    if active and open_row:
        con.execute("UPDATE anomalies SET value = ?, zscore = ?, exchange = ?, last_seen_ts = ? WHERE id = ?",
                    [value, zscore, exchange, now, open_row[0]])
    elif active:
        con.execute(
            "INSERT INTO anomalies (ts, symbol, exchange, type, value, zscore, window_min, last_seen_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [now, symbol, exchange, type_, value, zscore, window_min, now])
        return "opened"
    elif open_row:
        con.execute("UPDATE anomalies SET resolved_ts = ? WHERE id = ?", [now, open_row[0]])  # exchange/value не трогаем
        return "closed"
    return None


def update_spread_episodes(con, cfg: dict, now: datetime) -> None:
    limit = threshold(con, cfg, "spread_bp")
    fresh = now - timedelta(minutes=cfg["alerts"]["stale_after_min"])
    rows = con.execute(
        "SELECT symbol, spread_bp, exchange_hi FROM spreads WHERE ts <= ? AND ts >= ? "
        "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = 1", [now, fresh]).fetchall()
    for symbol, spread_bp, exchange_hi in rows:      # нет свежих данных -> эпизоды не трогаем
        _sync_episode(con, "spread", symbol, exchange_hi, spread_bp >= limit, spread_bp, None, 5, now)


def update_volume_episodes(con, cfg: dict, now: datetime) -> None:
    limit = threshold(con, cfg, "volume_zscore")
    z = volume_zscores(con, now)
    z = z[z.ts >= now - timedelta(minutes=cfg["alerts"]["stale_after_min"])]
    for symbol, g in z.groupby("symbol"):
        top = g.loc[g.zscore.idxmax()]               # одна монета — один эпизод, даже если всплеск на всех биржах
        _sync_episode(con, "volume", symbol, top.exchange, top.zscore >= limit,
                      float(top.volume), float(top.zscore), 24 * 60, now)


def update_oi_episodes(con, cfg: dict, now: datetime) -> None:
    limit = threshold(con, cfg, "oi_change_1h_pct")
    oi = oi_changes(con, now)
    if oi.empty:
        return
    oi = oi[(oi.ts >= now - timedelta(minutes=90)) & oi.chg_1h_pct.notna()]
    for symbol, g in oi.groupby("symbol"):
        top = g.loc[g.chg_1h_pct.abs().idxmax()]
        _sync_episode(con, "oi", symbol, top.exchange, abs(top.chg_1h_pct) >= limit,
                      float(top.chg_1h_pct), None, 60, now)


def update_all(con, cfg: dict, now: datetime) -> None:
    update_spread_episodes(con, cfg, now)
    update_volume_episodes(con, cfg, now)
    update_oi_episodes(con, cfg, now)
    from analysis.carry import update_carry_episodes
    update_carry_episodes(con, cfg, now)

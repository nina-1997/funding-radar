"""Funding carry: какая пара бирж по монете даёт максимальный нетто-доход после комиссий.

Идея (дельта-нейтральная позиция): лонг на бирже, где funding ниже, шорт там, где выше.
Цена страхуется (лонг и шорт гасят друг друга), а доход — разница funding-платежей.

Формулы (ставки в базе уже приведены к 8 ч, см. pipeline/normalize.py):
  funding_annual   = rate_8h * 3 * 365          ( = raw_rate * 24 / interval_h * 365 )
  gross_carry      = annual[short] - annual[long]                       % годовых
  cost_bps         = 2*taker[long] + 2*taker[short] + slippage          4 сделки: открыть и закрыть две ноги
  net_carry        = gross - cost_bps/100 * 365 / holding_days          комиссии разовые, carry — поток

Знак: funding > 0 — лонги платят шортам. Шорт на бирже с высоким положительным funding получает,
лонг на бирже с отрицательным funding тоже получает.

Это мониторинг, а не торговый совет: нет учёта ликвидации ноги, лимитов, задержек перевода маржи.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import permutations

import pandas as pd

from storage.db import get_setting

PERIODS_PER_YEAR_8H = 3 * 365


def annual_pct(rate_8h: float) -> float:
    """Ставка за 8 ч (доля) -> % годовых."""
    return rate_8h * PERIODS_PER_YEAR_8H * 100


def cost_bps(cfg: dict, long_ex: str, short_ex: str) -> float:
    fees = cfg["carry"]["fees_bps"]
    return 2 * fees[long_ex]["taker"] + 2 * fees[short_ex]["taker"] + cfg["carry"]["slippage_buffer_bps"]


def net_pct(gross_pct: float, cost: float, holding_days: float) -> float:
    """Разовые издержки, «размазанные» на год при удержании holding_days дней."""
    return gross_pct - cost / 100 * 365 / holding_days


def best_pair(rates: dict[str, float], cfg: dict, holding_days: float) -> dict | None:
    """rates: {биржа: ставка за 8 ч}. Перебираем все упорядоченные пары (лонг, шорт), берём максимум нетто.

    Не просто min/max: при разных комиссиях лучшей может оказаться не самая крайняя пара.
    """
    best = None
    for long_ex, short_ex in permutations(rates, 2):
        gross = annual_pct(rates[short_ex]) - annual_pct(rates[long_ex])
        cost = cost_bps(cfg, long_ex, short_ex)
        net = net_pct(gross, cost, holding_days)
        if best is None or net > best["net_pct"]:
            best = {"long_ex": long_ex, "short_ex": short_ex,
                    "long_annual_pct": annual_pct(rates[long_ex]), "short_annual_pct": annual_pct(rates[short_ex]),
                    "spread_bp_8h": (rates[short_ex] - rates[long_ex]) * 10_000,
                    "gross_pct": gross, "cost_bps": cost, "net_pct": net}
    return best


# ---------- данные ----------

def load_latest(con, now: datetime, exchanges: list[str] | None = None) -> pd.DataFrame:
    """Последняя ставка по каждой паре (биржа, монета) + mark price, следующий фандинг, OI."""
    df = con.execute(
        """
        SELECT f.exchange, f.symbol, f.ts, f.funding_rate, f.raw_rate, f.raw_interval_h, f.mark_price,
               f.next_funding_ts, oi.oi_usd
        FROM funding f
        LEFT JOIN (SELECT exchange, symbol, oi_usd FROM open_interest WHERE ts <= ?
                   QUALIFY row_number() OVER (PARTITION BY exchange, symbol ORDER BY ts DESC) = 1) oi
               USING (exchange, symbol)
        WHERE f.ts <= ? AND f.ts > ?::TIMESTAMP - INTERVAL 2 DAY
              AND f.next_funding_ts IS NOT NULL      -- только живые снимки, не догруженная история
        QUALIFY row_number() OVER (PARTITION BY f.exchange, f.symbol ORDER BY f.ts DESC) = 1
        """, [now, now, now]).df()
    return df[df.exchange.isin(exchanges)] if exchanges else df


def load_history(con, now: datetime, hours: int = 48) -> pd.DataFrame:
    """Живые 5-минутные снимки за hours часов: для «устойчивости»."""
    return con.execute(
        "SELECT ts, exchange, symbol, funding_rate FROM funding "
        "WHERE ts <= ? AND ts > ?::TIMESTAMP - to_hours(?) AND next_funding_ts IS NOT NULL ORDER BY ts",
        [now, now, hours]).df()


def stability_hours(history: pd.DataFrame, symbol: str, long_ex: str, short_ex: str,
                    cost: float, holding_days: float, threshold_pct: float, max_gap_min: float) -> float:
    """Сколько последних часов подряд эта же пара держит нетто выше порога.

    Отличает «липкий» спред от разового выброса. Дыра в данных больше max_gap_min рвёт серию.
    """
    h = history[(history.symbol == symbol) & history.exchange.isin([long_ex, short_ex])]
    wide = h.pivot_table(index="ts", columns="exchange", values="funding_rate").dropna()
    if wide.empty or long_ex not in wide or short_ex not in wide:
        return 0.0
    gross = (wide[short_ex] - wide[long_ex]) * PERIODS_PER_YEAR_8H * 100
    ok = net_pct(gross, cost, holding_days) > threshold_pct
    ts = list(wide.index)
    if not ok.iloc[-1]:
        return 0.0
    start = ts[-1]
    for i in range(len(ts) - 2, -1, -1):
        if not ok.iloc[i] or (ts[i + 1] - ts[i]) > timedelta(minutes=max_gap_min):
            break
        start = ts[i]
    return (ts[-1] - start).total_seconds() / 3600


def oi_change_24h(con, now: datetime) -> dict[tuple[str, str], float]:
    from analysis.anomalies import oi_changes
    oi = oi_changes(con, now)
    return {(r.exchange, r.symbol): r.chg_24h_pct for r in oi.itertuples() if r.chg_24h_pct == r.chg_24h_pct}


def signals(latest: pd.DataFrame, history: pd.DataFrame, oi24: dict, cfg: dict, now: datetime,
            holding_days: float, min_net: float, min_oi_usd: float = 0) -> pd.DataFrame:
    """Таблица экрана Signals: по строке на монету (лучшая пара бирж), сортировка по нетто."""
    c = cfg["carry"]
    rows = []
    for symbol, g in latest.groupby("symbol"):
        if min_oi_usd:
            g = g[g.oi_usd.fillna(0) >= min_oi_usd]           # неисполнимое объёмом не показываем
        rates = dict(zip(g.exchange, g.funding_rate))
        pair = best_pair(rates, cfg, holding_days)
        if pair is None:
            continue
        legs = g.set_index("exchange")
        L, S = legs.loc[pair["long_ex"]], legs.loc[pair["short_ex"]]
        next_ts = min(t for t in (L.next_funding_ts, S.next_funding_ts) if pd.notna(t))
        minutes_to_funding = (next_ts - now).total_seconds() / 60
        oldest = min(L.ts, S.ts)
        stale_leg = pair["long_ex"] if L.ts <= S.ts else pair["short_ex"]
        rows.append({
            "symbol": symbol, **pair,
            "next_funding_ts": next_ts,
            "minutes_to_funding": minutes_to_funding,
            "funding_soon": 0 <= minutes_to_funding < c["time_to_funding_buffer_min"],
            "stability_h": stability_hours(history, symbol, pair["long_ex"], pair["short_ex"], pair["cost_bps"],
                                           holding_days, min_net, c["max_gap_min"]),
            "oi_24h_short_pct": oi24.get((pair["short_ex"], symbol)),
            "min_leg_oi_usd": min(L.oi_usd, S.oi_usd) if pd.notna(L.oi_usd) and pd.notna(S.oi_usd) else None,
            "data_ts": oldest,
            "data_age_min": (now - oldest).total_seconds() / 60,
            "oldest_leg": stale_leg,
            "fresh": (now - oldest) <= timedelta(minutes=c["freshness_min"]),
            "intervals_h": f"{L.raw_interval_h:g}/{S.raw_interval_h:g}",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df[df.net_pct > min_net].sort_values("net_pct", ascending=False).reset_index(drop=True)


def compute(con, cfg: dict, now: datetime, holding_days: float | None = None, min_net: float | None = None,
            exchanges: list[str] | None = None, min_oi_usd: float | None = None) -> pd.DataFrame:
    c = cfg["carry"]
    return signals(load_latest(con, now, exchanges), load_history(con, now), oi_change_24h(con, now), cfg, now,
                   holding_days or c["holding_days"], c["min_net_pct"] if min_net is None else min_net,
                   c["min_oi_usd"] if min_oi_usd is None else min_oi_usd)


# ---------- эпизоды для алертов ----------

def alert_setting(con, cfg: dict, key: str) -> float:
    return float(get_setting(con, f"carry.{key}", cfg["carry"][key]))


def update_carry_episodes(con, cfg: dict, now: datetime) -> None:
    """Эпизод «carry» открыт, пока нетто > порога алерта и держится ≥ K часов. Логика — как у других аномалий."""
    from analysis.anomalies import _sync_episode
    threshold = alert_setting(con, cfg, "alert_threshold_pct")
    min_h = alert_setting(con, cfg, "alert_min_stability_h")
    table = compute(con, cfg, now, min_net=threshold)          # устойчивость считается относительно порога алерта
    active = {r.symbol: r for r in table.itertuples() if r.fresh and r.stability_h >= min_h}
    symbols = set(con.execute("SELECT DISTINCT symbol FROM anomalies WHERE type = 'carry' AND resolved_ts IS NULL")
                  .df().symbol) | set(active)
    latest = load_latest(con, now)
    if latest.empty or not (now - latest.ts.max() <= timedelta(minutes=cfg["carry"]["freshness_min"])):
        return                                                  # данные стухли — эпизоды не трогаем
    for symbol in symbols:
        r = active.get(symbol)
        _sync_episode(con, "carry", symbol, f"{r.long_ex}/{r.short_ex}" if r else None, r is not None,
                      float(r.net_pct) if r else 0.0, None, int(min_h * 60), now)

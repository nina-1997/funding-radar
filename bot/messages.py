"""Тексты сообщений. Бот ничего не считает сам — только читает готовое из базы и форматирует."""
from __future__ import annotations

from datetime import datetime, timedelta

from analysis.anomalies import ALERT_KEYS, oi_changes, threshold
from analysis.spreads import current_funding, current_spreads, spread_history, spread_stats

EXCHANGE_ORDER = {"binance": 0, "bybit": 1, "okx": 2}
EXCHANGE_NAME = {"binance": "Binance", "bybit": "Bybit", "okx": "OKX"}


def name(exchange: str) -> str:
    return EXCHANGE_NAME.get(exchange, exchange.capitalize())


def pct(rate: float, digits: int = 4) -> str:
    """Доля -> проценты со знаком: 0.0001 -> +0.0100%"""
    return f"{rate * 100:+.{digits}f}%"


def _ago(ts: datetime, now: datetime) -> str:
    minutes = max(0, int((now - ts).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes < 48 * 60 else f"{minutes // 1440} дн"


def _funding_lines(con, symbol: str, now: datetime) -> list[str]:
    f = current_funding(con, now)
    f = f[f.symbol == symbol].sort_values("exchange", key=lambda s: s.map(EXCHANGE_ORDER)) if not f.empty else f
    lines = []
    for r in f.itertuples():
        note = "8h" if r.raw_interval_h == 8 else f"8h, биржа платит раз в {r.raw_interval_h:g}h"
        lines.append(f"{name(r.exchange):<8} {pct(r.funding_rate)} ({note})")
    return lines


# ---------- алерты ----------

def spread_alert(con, cfg: dict, anomaly: dict, now: datetime) -> str:
    symbol = anomaly["symbol"]
    s = current_spreads(con, now)
    s = s[s.symbol == symbol].iloc[0]
    lines = [f"⚡ Funding spread · {symbol}"] + _funding_lines(con, symbol, now)
    lines.append(f"Spread {name(s.exchange_hi)}–{name(s.exchange_lo)}: {s.spread_bp:.2f} bp")
    lines.append(f"Annualized: {s.annualized_hi * 100:.1f}% vs {s.annualized_lo * 100:.1f}%")
    oi = oi_changes(con, now)
    oi = oi[(oi.symbol == symbol) & (oi.exchange == s.exchange_hi) & oi.chg_1h_pct.notna()] if not oi.empty else oi
    if not oi.empty:
        lines.append(f"OI {name(s.exchange_hi)}: {oi.chg_1h_pct.iloc[0]:+.1f}% за час")
    lines.append(f"Длится: {_ago(anomaly['ts'], now)} · порог {threshold(con, cfg, 'spread_bp'):g} bp")
    lines.append(f"→ /explain {symbol}")
    return "\n".join(lines)


def volume_alert(con, cfg: dict, anomaly: dict, now: datetime) -> str:
    return "\n".join([
        f"📊 Всплеск объёма · {anomaly['symbol']}",
        f"{name(anomaly['exchange'])}: {anomaly['zscore']:.1f}σ к среднему за 24 ч (5-мин свеча)",
        f"Порог {threshold(con, cfg, 'volume_zscore'):g}σ · {anomaly['ts']:%H:%M} UTC",
        f"→ /explain {anomaly['symbol']}",
    ])


def oi_alert(con, cfg: dict, anomaly: dict, now: datetime) -> str:
    return "\n".join([
        f"🏦 Open interest · {anomaly['symbol']}",
        f"{name(anomaly['exchange'])}: {anomaly['value']:+.1f}% за час",
        f"Порог ±{threshold(con, cfg, 'oi_change_1h_pct'):g}% · {anomaly['ts']:%H:%M} UTC",
        f"→ /explain {anomaly['symbol']}",
    ])


def _countdown(minutes: float) -> str:
    if minutes < 0:
        return "—"
    return f"{int(minutes)} мин" if minutes < 60 else f"{int(minutes // 60)} ч {int(minutes % 60):02d} мин"


def carry_line(r) -> list[str]:
    return [f"Лонг {name(r.long_ex)} ({r.long_annual_pct:+.1f}% год.) / Шорт {name(r.short_ex)} ({r.short_annual_pct:+.1f}% год.)",
            f"Нетто: {r.net_pct:.1f}% год. (gross {r.gross_pct:.1f}%, издержки {r.cost_bps:.0f} bp)",
            f"Фандинг через {_countdown(r.minutes_to_funding)}" + (" · можно не успеть" if r.funding_soon else "")
            + f" · держится {r.stability_h:.1f} ч"]


def carry_alert(con, cfg: dict, anomaly: dict, now: datetime) -> str:
    from analysis import carry
    # устойчивость в алерте — относительно порога алерта, как и при открытии эпизода
    table = carry.compute(con, cfg, now, min_net=carry.alert_setting(con, cfg, "alert_threshold_pct"))
    row = table[table.symbol == anomaly["symbol"]]
    lines = [f"💰 Carry · {anomaly['symbol']}"]
    if row.empty:
        lines.append(f"Нетто {anomaly['value']:.1f}% год. ({anomaly['exchange']})")
    else:
        lines += carry_line(row.iloc[0])
    lines.append(f"Удержание {cfg['carry']['holding_days']} д · не торговая рекомендация")
    lines.append(f"→ /explain {anomaly['symbol']}")
    return "\n".join(lines)


def signals_report(con, cfg: dict, now: datetime, top: int = 5) -> str:
    from analysis import carry
    table = carry.compute(con, cfg, now)
    if table.empty:
        return "Пар с нетто-carry выше порога сейчас нет."
    lines = [f"💰 Signals · удержание {cfg['carry']['holding_days']} д · {now:%H:%M} UTC"]
    for r in table.head(top).itertuples():
        stale = "" if r.fresh else " · ⚠️ данные стухли"
        lines += ["", f"{r.symbol}{stale}"] + carry_line(r)
    lines += ["", "Мониторинг, не торговая рекомендация."]
    return "\n".join(lines)


ALERT_BUILDERS = {"spread": spread_alert, "volume": volume_alert, "oi": oi_alert, "carry": carry_alert}
TYPE_TITLE = {"spread": "Funding spread", "volume": "Всплеск объёма", "oi": "Open interest", "carry": "Carry"}


def closed_alert(anomaly: dict) -> str:
    return (f"✅ {TYPE_TITLE[anomaly['type']]} · {anomaly['symbol']} — закрылся\n"
            f"Длился {_ago(anomaly['ts'], anomaly['resolved_ts'])}")


# ---------- команды ----------

def funding_table(con, now: datetime) -> str:
    f = current_funding(con, now)
    if f.empty:
        return "Данных пока нет. Проверь /status."
    exchanges = sorted(f.exchange.unique(), key=lambda e: EXCHANGE_ORDER.get(e, 9))
    table = f.pivot(index="symbol", columns="exchange", values="funding_rate")
    spreads = current_spreads(con, now).set_index("symbol")
    lines = [f"Funding за 8 ч · {f.ts.iloc[0]:%d.%m %H:%M} UTC",
             f"{'':<5}" + "".join(f"{name(e):>10}" for e in exchanges) + f"{'bp':>7}"]
    order = table.index.map(lambda s: -spreads.spread_bp.get(s, 0)).argsort()
    for symbol in table.index[order]:
        cells = "".join(f"{table.loc[symbol, e] * 100:>+10.4f}" if table.loc[symbol, e] == table.loc[symbol, e]
                        else f"{'—':>10}" for e in exchanges)
        bp = spreads.spread_bp.get(symbol)
        lines.append(f"{symbol:<5}{cells}" + (f"{bp:>7.2f}" if bp is not None else ""))
    lines.append("Значения в %, bp — расхождение между биржами")
    return "\n".join(lines)


def spread_report(con, cfg: dict, symbol: str, now: datetime) -> str:
    h = spread_history(con, symbol, 24, now)
    if h.empty:
        return f"По {symbol} нет истории spread."
    limit = threshold(con, cfg, "spread_bp")
    last = h.iloc[-1]
    lines = [f"Spread · {symbol} · 24 ч",
             f"Сейчас:  {last.spread_bp:.2f} bp ({name(last.exchange_hi)} дороже {name(last.exchange_lo)})",
             f"Среднее: {h.spread_bp.mean():.2f} bp · медиана {h.spread_bp.median():.2f}",
             f"Максимум: {h.spread_bp.max():.2f} bp в {h.loc[h.spread_bp.idxmax(), 'ts']:%H:%M} UTC",
             f"Выше порога {limit:g} bp: {(h.spread_bp >= limit).mean() * 100:.0f}% времени"]
    week = spread_stats(con, symbol, 7)
    if week:
        lines.append(f"За 7 дней: медиана {week['median_bp']:.2f}, p95 {week['p95_bp']:.2f}, макс {week['max_bp']:.2f}")
    hourly = h.set_index("ts").spread_bp.resample("3h").mean().dropna()
    lines.append("По 3 часа (UTC): " + " ".join(f"{t:%H}:{v:.1f}" for t, v in hourly.items()))
    return "\n".join(lines)


def oi_report(con, symbol: str, now: datetime) -> str:
    oi = oi_changes(con, now)
    oi = oi[oi.symbol == symbol] if not oi.empty else oi
    if oi.empty:
        return f"По {symbol} нет данных open interest."
    lines = [f"Open interest · {symbol}", f"{'':<8}{'млн $':>10}{'1 ч':>9}{'24 ч':>9}"]
    fmt = lambda v: f"{v:>+8.1f}%" if v is not None and v == v else f"{'—':>9}"
    for r in oi.sort_values("exchange", key=lambda s: s.map(EXCHANGE_ORDER)).itertuples():
        lines.append(f"{name(r.exchange):<8}{r.oi_usd / 1e6:>10,.0f}{fmt(r.chg_1h_pct)}{fmt(r.chg_24h_pct)}")
    lines.append("OI↑ и цена↑ — новые лонги; OI↑ и цена↓ — новые шорты")
    return "\n".join(lines)


def status_report(con, now: datetime) -> str:
    rows = con.execute(
        """SELECT collector,
                  max(finished) FILTER (WHERE status = 'ok') AS last_ok,
                  arg_max(status, finished) AS last_status,
                  arg_max(error, finished) AS last_error,
                  count(*) FILTER (WHERE status = 'error' AND finished > ?) AS errors_24h
           FROM collector_runs GROUP BY collector ORDER BY collector""",
        [now - timedelta(hours=24)]).fetchall()
    if not rows:
        return "Коллекторы ещё не запускались."
    lines = ["Коллекторы (последний успех)"]
    for collector, last_ok, last_status, last_error, errors in rows:
        mark = "🟢" if last_status == "ok" else "🔴"
        ago = _ago(last_ok, now) + " назад" if last_ok else "ни разу"
        lines.append(f"{mark} {collector:<16} {ago}" + (f" · ошибок за 24 ч: {errors}" if errors else ""))
        if last_status != "ok" and last_error:
            lines.append(f"   {last_error[:120]}")
    n = con.execute("SELECT count(*) FROM anomalies WHERE resolved_ts IS NULL").fetchone()[0]
    lines.append(f"Открытых эпизодов: {n}")
    return "\n".join(lines)


def thresholds_report(con, cfg: dict) -> str:
    lines = ["Пороги алертов"]
    lines += [f"{k} = {threshold(con, cfg, k):g}" for k in ALERT_KEYS]
    from analysis.carry import alert_setting
    lines += [f"carry_{k} = {alert_setting(con, cfg, k):g}" for k in ("alert_threshold_pct", "alert_min_stability_h")]
    lines.append("Изменить: /alerts spread_bp 2")
    return "\n".join(lines)

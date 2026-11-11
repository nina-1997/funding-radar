"""/explain: собрать цифры по инструменту из базы и попросить модель их истолковать."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ai import llm
from analysis import correlations
from analysis.anomalies import oi_changes, volume_zscores
from analysis.market import price_changes
from analysis.spreads import current_funding, spread_stats
from pipeline.config import load_config
from pipeline.timeutil import utcnow
from storage.db import connect


class Explanation(BaseModel):
    summary: str = Field(min_length=20)
    hypotheses: list[str] = Field(min_length=1, max_length=4)
    confidence: Literal["low", "medium", "high"]


def _r(x, digits=4):
    return None if x is None or x != x else round(float(x), digits)     # x != x — это проверка на NaN


def build_facts(con, cfg: dict, symbol: str, now: datetime) -> dict | None:
    """Все числа, которые увидит модель. Ничего, кроме этого словаря, у неё нет."""
    funding = current_funding(con, now)
    funding = funding[funding.symbol == symbol] if not funding.empty else funding
    if funding.empty:
        return None
    rates = {r.exchange: r.funding_rate for r in funding.itertuples()}
    hi, lo = max(rates, key=rates.get), min(rates, key=rates.get)

    oi = oi_changes(con, now)
    oi = oi[oi.symbol == symbol] if not oi.empty else oi
    vol = volume_zscores(con, now)
    vol = vol[vol.symbol == symbol] if not vol.empty else vol
    px = price_changes(con, now)
    px = px[px.symbol == symbol]
    corr = correlations.correlation_matrix(con, cfg["regime"]["corr_window_days"], now)
    six_h_ago = con.execute(
        "SELECT spread_bp FROM spreads WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        [symbol, now - timedelta(hours=6)]).fetchone()
    episodes = con.execute(
        "SELECT type, exchange, ts, value, zscore FROM anomalies "
        "WHERE symbol = ? AND resolved_ts IS NULL ORDER BY ts", [symbol]).fetchall()

    return {
        "symbol": symbol,
        "slot_utc": str(funding.ts.iloc[0]),
        "funding_8h_pct": {ex: _r(rate * 100, 4) for ex, rate in rates.items()},
        "funding_annualized_pct": {ex: _r(rate * 3 * 365 * 100, 1) for ex, rate in rates.items()},
        "spread_bp": _r((rates[hi] - rates[lo]) * 10_000, 2),
        "spread_most_expensive": hi, "spread_cheapest": lo,
        "spread_bp_6h_ago": _r(six_h_ago[0], 2) if six_h_ago else None,
        "spread_7d_typical": {k: _r(v, 2) for k, v in (spread_stats(con, symbol) or {}).items()},
        "oi_change_1h_pct": {r.exchange: _r(r.chg_1h_pct, 2) for r in oi.itertuples()},
        "oi_change_24h_pct": {r.exchange: _r(r.chg_24h_pct, 2) for r in oi.itertuples()},
        "price": _r(px.price.iloc[0], 6) if not px.empty else None,
        "price_change_1h_pct": _r(px.chg_1h_pct.iloc[0], 2) if not px.empty else None,
        "price_change_24h_pct": _r(px.chg_24h_pct.iloc[0], 2) if not px.empty else None,
        "volume_zscore_last_5m": {r.exchange: _r(r.zscore, 1) for r in vol.itertuples()},
        "corr_with_btc_7d": _r(correlations.pair(corr, symbol, "BTC"), 2) if symbol != "BTC" else None,
        "open_episodes": [
            {"type": t, "exchange": ex, "since_utc": str(ts), "value": _r(v, 2), "zscore": _r(z, 1)}
            for t, ex, ts, v, z in episodes],
        "alert_thresholds": cfg["alerts"],
    }


def explain(symbol: str, now: datetime | None = None) -> tuple[dict | None, Explanation | None]:
    """-> (факты, объяснение). Объяснение = None, если AI недоступен; факты всё равно возвращаются."""
    cfg, now = load_config(), now or utcnow()
    with connect() as con:              # соединение закрываем ДО обращения к модели: оно может длиться секунды
        facts = build_facts(con, cfg, symbol, now)
    if facts is None:
        return None, None
    return facts, llm.ask_json("explain", facts, Explanation)


CONFIDENCE = {"low": "низкая", "medium": "средняя", "high": "высокая"}


def render(symbol: str, facts: dict | None, ex: Explanation | None) -> str:
    if facts is None:
        return f"По {symbol} пока нет данных."
    lines = [f"🔎 {symbol} · {facts['slot_utc'][:16]} UTC"]
    for exch, pct in facts["funding_8h_pct"].items():
        lines.append(f"{exch.capitalize():<8} {pct:+.4f}% (8h) · {facts['funding_annualized_pct'][exch]:+.1f}% год.")
    lines.append(f"Spread: {facts['spread_bp']:.2f} bp ({facts['spread_most_expensive']} vs {facts['spread_cheapest']})")
    if facts["price"] is not None:
        ch = facts["price_change_24h_pct"]
        lines.append(f"Цена: {facts['price']:g}" + (f" ({ch:+.1f}% / 24h)" if ch is not None else ""))
    lines.append("")
    if ex is None:
        lines.append("AI-объяснение недоступно (нет ключа, исчерпан бюджет или модель не ответила).")
    else:
        lines.append(ex.summary)
        lines += [f"• {h}" for h in ex.hypotheses]
        lines.append(f"Уверенность: {CONFIDENCE[ex.confidence]}")
    lines.append("Не торговая рекомендация.")
    return "\n".join(lines)

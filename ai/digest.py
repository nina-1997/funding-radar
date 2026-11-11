"""Утренняя сводка. Цифры считает и подставляет код, модель добавляет только комментарий и оценку режима."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ai import llm
from analysis import correlations, regime
from analysis.anomalies import oi_changes
from analysis.market import price_changes
from analysis.spreads import current_funding
from pipeline.config import load_config
from pipeline.timeutil import utcnow
from storage.db import connect


class DigestNote(BaseModel):
    headline: str = Field(min_length=5, max_length=160)
    comment: str = Field(min_length=20)
    regime: Literal["neutral", "long_overheat", "short_overheat", "divergence"]


def _r(x, digits=2):
    return None if x is None or x != x else round(float(x), digits)


def _oi_change_over(con, symbol: str, now: datetime, hours: int) -> float | None:
    """Суммарный OI по всем биржам: сейчас против hours часов назад, %."""
    def total(at: datetime):
        row = con.execute(
            """SELECT sum(oi) FROM (
                   SELECT coalesce(oi_usd, oi_contracts) AS oi FROM open_interest
                   WHERE symbol = ? AND ts <= ? AND ts > ?::TIMESTAMP - INTERVAL 2 HOUR
                   QUALIFY row_number() OVER (PARTITION BY exchange ORDER BY ts DESC) = 1)""",
            [symbol, at, at]).fetchone()
        return row[0]
    cur, past = total(now), total(now - timedelta(hours=hours))
    return (cur / past - 1) * 100 if cur and past else None


def build_facts(con, cfg: dict, now: datetime, night_hours: int = 12) -> dict:
    since = now - timedelta(hours=night_hours)
    px = price_changes(con, now)
    funding = current_funding(con, now)
    corr = correlations.correlation_matrix(con, cfg["regime"]["corr_window_days"], now)
    m = regime.metrics(con, cfg, now)

    top = funding.loc[funding.funding_rate.idxmax()] if not funding.empty else None
    by_symbol = funding.groupby("symbol").funding_rate.mean() if not funding.empty else {}
    volume_spikes = con.execute(
        "SELECT symbol, exchange, ts, zscore FROM anomalies WHERE type = 'volume' AND ts > ? AND ts <= ? "
        "ORDER BY zscore DESC LIMIT 3", [since, now]).fetchall()
    alerts_n, closed_fast = con.execute(
        """SELECT count(*), count(*) FILTER (WHERE a.resolved_ts IS NOT NULL
                                              AND a.resolved_ts - a.ts < INTERVAL 1 HOUR)
           FROM alerts al JOIN anomalies a ON a.id = al.anomaly_id
           WHERE al.kind = 'open' AND al.sent_ts > ? AND al.sent_ts <= ?""", [since, now]).fetchone()

    return {
        "date_utc": now.strftime("%Y-%m-%d"),
        "night_hours": night_hours,
        "prices": {r.symbol: {"price": _r(r.price, 6), "chg_24h_pct": _r(r.chg_24h_pct)} for r in px.itertuples()},
        "funding_8h_pct_avg_by_symbol": {s: _r(v * 100, 4) for s, v in dict(by_symbol).items()},
        "funding_8h_pct_avg_all": _r(m["avg_funding"] * 100, 4) if m["avg_funding"] is not None else None,
        "most_expensive_leverage": None if top is None else {
            "symbol": top.symbol, "exchange": top.exchange, "annualized_pct": _r(top.funding_rate * 3 * 365 * 100, 1)},
        "oi_change_night_pct": {s: _r(_oi_change_over(con, s, now, night_hours)) for s in cfg["symbols"]},
        "oi_change_24h_pct_avg": _r(m["oi_change_24h_pct"]),
        "volume_spikes": [{"symbol": s, "exchange": e, "time_utc": t.strftime("%H:%M"), "zscore": _r(z, 1)}
                          for s, e, t, z in volume_spikes],
        "corr_btc_eth_7d": _r(correlations.pair(corr, "BTC", "ETH")),
        "mean_pairwise_corr_7d": _r(m["mean_corr"]),
        "alerts_night": alerts_n, "alerts_closed_within_1h": closed_fast,
        "_rule_regime": regime.classify(m, cfg),       # модели не показываем (см. make_digest)
        "_metrics": m,
    }


def _price(p: float) -> str:
    return f"{p:,.0f}".replace(",", " ") if p >= 1000 else f"{p:.4g}"


def render(facts: dict, note: DigestNote | None) -> str:
    """Структура фиксированная — чтобы сводки можно было сравнивать день к дню."""
    d = datetime.strptime(facts["date_utc"], "%Y-%m-%d")
    lines = [f"☀️ Market pulse · {d.day} {d.strftime('%b')}"]
    if note:
        lines.append(note.headline)

    parts = []
    for s in ("BTC", "ETH"):
        p = facts["prices"].get(s)
        if p and p["price"] is not None:
            ch = f" ({p['chg_24h_pct']:+.1f}% / 24h)" if p["chg_24h_pct"] is not None else ""
            parts.append(f"{s} {_price(p['price'])}{ch}")
    if parts:
        lines.append(", ".join(parts))

    rule = regime.REGIMES[facts["_rule_regime"]]
    btc_f = facts["funding_8h_pct_avg_by_symbol"].get("BTC")
    lines.append(f"Funding: {rule}" + (f", BTC avg {btc_f:.3f}%" if btc_f is not None else ""))

    top = facts["most_expensive_leverage"]
    if top:
        lines.append(f"Самое дорогое плечо: {top['symbol']} на {top['exchange'].capitalize()}, "
                     f"{top['annualized_pct']:.0f}% годовых")

    oi = [f"{s} {v:+.1f}%" for s, v in facts["oi_change_night_pct"].items() if v is not None and s in ("BTC", "ETH")]
    if oi:
        lines.append("OI за ночь: " + ", ".join(oi))

    for v in facts["volume_spikes"][:1]:
        lines.append(f"Аномалия объёма: {v['symbol']} {v['time_utc']} UTC, {v['zscore']:.1f}σ")

    c = facts["corr_btc_eth_7d"]
    if c is not None:
        lines.append(f"Корреляция BTC/ETH (7д): {c:.2f}")

    n, fast = facts["alerts_night"], facts["alerts_closed_within_1h"]
    lines.append(f"Алертов за ночь: {n}" + (f", из них закрылись < 1 ч: {fast}" if n else ""))

    if note:
        lines += ["", note.comment]
        if note.regime != facts["_rule_regime"]:
            lines.append(f"(Модель оценивает режим иначе: {regime.REGIMES[note.regime]}.)")
    return "\n".join(lines)


def make_digest(now: datetime | None = None) -> str:
    cfg, now = load_config(), now or utcnow()
    with connect() as con:
        facts = build_facts(con, cfg, now)
    # Модель не видит ответ правила — иначе эксперимент «правило против модели» теряет смысл
    visible = {k: v for k, v in facts.items() if not k.startswith("_")}
    note = llm.ask_json("digest", visible, DigestNote)
    m = facts["_metrics"]
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO regime_log VALUES (?, ?, ?, ?, ?, ?)",
                    [now, facts["_rule_regime"], note.regime if note else None,
                     m["avg_funding"], m["oi_change_24h_pct"], m["mean_corr"]])
    return render(facts, note)

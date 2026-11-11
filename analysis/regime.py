"""Режим рынка по простому правилу из трёх чисел: funding, OI, корреляции."""
from __future__ import annotations

from datetime import datetime

from analysis import correlations

from pipeline.timeutil import utcnow
from analysis.anomalies import oi_changes
from analysis.spreads import current_funding

REGIMES = {
    "neutral": "нейтрально",
    "long_overheat": "перегрев лонгов",
    "short_overheat": "перегрев шортов",
    "divergence": "разъезд",
}


def metrics(con, cfg: dict, now: datetime | None = None) -> dict:
    now = now or utcnow()
    funding = current_funding(con, now)
    oi = oi_changes(con, now)
    corr = correlations.correlation_matrix(con, cfg["regime"]["corr_window_days"], now)
    oi_24h = oi.chg_24h_pct.dropna() if not oi.empty else []
    return {
        "avg_funding": float(funding.funding_rate.mean()) if not funding.empty else None,
        "oi_change_24h_pct": float(oi_24h.mean()) if len(oi_24h) else None,
        "mean_corr": correlations.mean_pairwise(corr),
    }


def classify(m: dict, cfg: dict) -> str:
    """Правило читается сверху вниз, срабатывает первое подходящее условие."""
    r = cfg["regime"]
    funding, oi, corr = m.get("avg_funding"), m.get("oi_change_24h_pct") or 0.0, m.get("mean_corr")
    if funding is not None and funding >= r["long_overheat_funding"] and oi >= 0:
        return "long_overheat"      # лонги платят много и позиций становится больше
    if funding is not None and funding <= r["short_overheat_funding"] and oi >= 0:
        return "short_overheat"     # платят шорты, и позиций становится больше
    if corr is not None and corr < r["divergence_corr"]:
        return "divergence"         # монеты перестали ходить вместе
    return "neutral"

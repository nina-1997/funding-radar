"""Время. Везде в проекте — UTC без tzinfo (так его хранит DuckDB в TIMESTAMP)."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def floor_slot(ts: datetime, minutes: int = 5) -> datetime:
    """15:03:27 -> 15:00:00. Общий слот нужен, чтобы строки трёх бирж имели одинаковый ts."""
    return ts.replace(minute=ts.minute - ts.minute % minutes, second=0, microsecond=0)


def to_ms(ts: datetime) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000)

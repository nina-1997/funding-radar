"""Один проход пайплайна: сбор -> raw -> нормализация -> upsert -> аналитика -> алерты.

  python -m pipeline.run_collect --dry-run          проверить конфиг
  python -m pipeline.run_collect                    один полный проход
  python -m pipeline.run_collect --only funding     только funding
  python -m pipeline.run_collect --exchange okx     только одна биржа
  python -m pipeline.run_collect --backfill-days 7  догрузить историю funding и свечей
  python -m pipeline.run_collect --from-raw raw/binance/funding/2026-11-11T15-00-00.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from analysis import anomalies, spreads
from collectors import COLLECTORS
from pipeline import normalize
from pipeline.config import enabled_exchanges, load_config, setup_logging
from pipeline.timeutil import floor_slot, to_ms, utcnow
from storage.db import connect, upsert

log = logging.getLogger(__name__)
KINDS = ("funding", "oi", "klines")
KEYS = {
    "funding": ["exchange", "symbol", "ts"],
    "open_interest": ["exchange", "symbol", "ts"],
    "klines": ["exchange", "symbol", "ts", "interval"],
    "instruments": ["exchange", "symbol"],
}
_collectors: dict = {}


def get_collector(cfg: dict, exchange: str):
    """Один объект коллектора на процесс: у него HTTP-сессия и кэш справочников."""
    if exchange not in _collectors:
        _collectors[exchange] = COLLECTORS[exchange](cfg)
    return _collectors[exchange]


def save_raw(cfg: dict, bundle: dict, now: datetime, suffix: str = "") -> Path:
    """Ответ API как есть -> raw/<биржа>/<тип>/<время>.json. Сохраняем ДО нормализации."""
    folder = Path(cfg["paths"]["raw"]) / bundle["exchange"] / bundle["kind"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y-%m-%dT%H-%M-%S}{suffix}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def store(bundle: dict, cfg: dict, slot: datetime, now: datetime) -> int:
    """Нормализовать пакет и записать в базу. Возвращает число строк."""
    symbols, quote, kind = cfg["symbols"], cfg["quote"], bundle["kind"]
    with connect() as con:
        if kind == "funding":
            rows, instruments = normalize.normalize_funding(bundle, symbols, slot, quote)
            upsert(con, "instruments", instruments, KEYS["instruments"])
            return upsert(con, "funding", rows, KEYS["funding"])
        if kind == "funding_history":
            return upsert(con, "funding", normalize.normalize_funding_history(bundle, symbols, quote), KEYS["funding"])
        if kind == "oi":
            return upsert(con, "open_interest", normalize.normalize_oi(bundle, symbols, slot, quote),
                          KEYS["open_interest"])
        if kind == "klines":
            return upsert(con, "klines", normalize.normalize_klines(bundle, symbols, to_ms(now), quote), KEYS["klines"])
    raise ValueError(f"неизвестный тип пакета: {kind}")


def _klines_limit(cfg: dict, exchange: str, now: datetime) -> int:
    """Сколько свечей просить: ровно столько, чтобы закрыть дыру с последней сохранённой (догрузка пропусков)."""
    with connect() as con:
        last = con.execute(
            "SELECT min(last) FROM (SELECT max(ts) AS last FROM klines WHERE exchange = ? GROUP BY symbol)",
            [exchange]).fetchone()[0]
    if last is None:
        return cfg["klines"]["initial_limit"]
    missing = math.ceil((now - last).total_seconds() / 60 / cfg["klines"]["interval_min"]) + 2
    return max(3, missing)      # коллектор сам обрежет до максимума биржи; глубже — через --backfill-days


def run_collector(cfg: dict, exchange: str, kind: str, slot: datetime) -> bool:
    """Один коллектор от запроса до базы. Любая ошибка — в лог и в collector_runs, остальные продолжают."""
    started, rows, error = utcnow(), 0, None
    try:
        collector = get_collector(cfg, exchange)
        if kind == "funding":
            bundle = collector.fetch_funding(cfg["symbols"])
        elif kind == "oi":
            bundle = collector.fetch_oi(cfg["symbols"])
        else:
            bundle = collector.fetch_klines(cfg["symbols"], _klines_limit(cfg, exchange, started))
        save_raw(cfg, bundle, started)
        rows = store(bundle, cfg, slot, utcnow())
        log.info("%s.%s: %d строк", exchange, kind, rows)
    except Exception as e:      # намеренно широко: упавший коллектор не должен ронять сервис
        error = f"{type(e).__name__}: {e}"
        log.error("%s.%s: %s", exchange, kind, error)
    with connect() as con:
        con.execute(
            "INSERT INTO collector_runs (collector, started, finished, status, rows, error) VALUES (?, ?, ?, ?, ?, ?)",
            [f"{exchange}.{kind}", started, utcnow(), "error" if error else "ok", rows, error])
    return error is None


def run_analysis(cfg: dict, now: datetime) -> None:
    with connect() as con:
        spreads.compute_spreads(con, since=now - timedelta(hours=2))
        anomalies.update_all(con, cfg, now)


def run_cycle(cfg: dict, kinds=("funding", "klines"), exchanges: list[str] | None = None,
              send_alerts: bool = True) -> None:
    """Проход «слева направо»: коллекторы -> аналитика -> алерты."""
    slot = floor_slot(utcnow(), cfg["schedule"]["funding_every_min"])
    for exchange in exchanges or enabled_exchanges(cfg):
        for kind in kinds:
            run_collector(cfg, exchange, kind, slot)
    try:
        run_analysis(cfg, utcnow())
        if send_alerts:
            from bot import alerts
            alerts.process()
    except Exception:
        log.exception("аналитика/алерты упали")


def backfill(cfg: dict, days: int, exchanges: list[str] | None = None) -> None:
    """История: состоявшиеся funding и 5-мин свечи за days дней (свечи — страницами назад во времени)."""
    now = utcnow()
    target = now - timedelta(days=days)
    for exchange in exchanges or enabled_exchanges(cfg):
        collector = get_collector(cfg, exchange)
        try:
            bundle = collector.fetch_funding_history(cfg["symbols"], limit=days * 24)
            save_raw(cfg, bundle, now)
            log.info("%s: история funding, %d строк", exchange, store(bundle, cfg, now, now))
        except Exception as e:
            log.error("%s: история funding не загружена: %s", exchange, e)
        end_ms, total = None, 0
        for page in range(200):                 # предохранитель от бесконечного цикла
            try:
                bundle = collector.fetch_klines(cfg["symbols"], collector.MAX_KLINES, end_ms)
                save_raw(cfg, bundle, now, suffix=f"_p{page:03d}")
                rows = normalize.normalize_klines(bundle, cfg["symbols"], to_ms(now), cfg["quote"])
                with connect() as con:
                    total += upsert(con, "klines", rows, KEYS["klines"])
            except Exception as e:
                log.error("%s: история свечей прервана: %s", exchange, e)
                break
            if not rows:
                break
            oldest = min(r["ts"] for r in rows)
            if oldest <= target or (end_ms and to_ms(oldest) - 1 >= end_ms):
                break
            end_ms = to_ms(oldest) - 1
        log.info("%s: история свечей, %d строк", exchange, total)
    with connect() as con:
        log.info("spreads пересчитаны: %d строк", spreads.compute_spreads(con, since=target))


def load_from_raw(cfg: dict, path: str) -> None:
    """Перезалить сохранённый ответ (например, после починки нормализации)."""
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    fetched = normalize.to_ts(bundle["fetched_at_ms"])
    rows = store(bundle, cfg, floor_slot(fetched, cfg["schedule"]["funding_every_min"]), fetched)
    log.info("%s -> %d строк", path, rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Funding Radar: сбор данных")
    p.add_argument("--dry-run", action="store_true", help="показать конфиг и выйти")
    p.add_argument("--only", help="funding,oi,klines (через запятую)")
    p.add_argument("--exchange", help="binance | bybit | okx")
    p.add_argument("--backfill-days", type=int, help="догрузить историю за N дней")
    p.add_argument("--from-raw", help="загрузить сохранённый файл из raw/")
    p.add_argument("--no-alerts", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    setup_logging(cfg)
    exchanges = [args.exchange] if args.exchange else None

    if args.dry_run:
        print("Инструменты:", ", ".join(cfg["symbols"]), f"(к {cfg['quote']})")
        print("Биржи:      ", ", ".join(enabled_exchanges(cfg)))
        print("Расписание: ", cfg["schedule"])
        print("Пороги:     ", cfg["alerts"])
        print("База:       ", cfg["paths"]["db"])
        print("ok")
        return
    if args.from_raw:
        return load_from_raw(cfg, args.from_raw)
    if args.backfill_days:
        return backfill(cfg, args.backfill_days, exchanges)
    kinds = tuple(args.only.split(",")) if args.only else KINDS
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise SystemExit(f"неизвестный тип: {unknown}; доступны {KINDS}")
    run_cycle(cfg, kinds, exchanges, send_alerts=not args.no_alerts)


if __name__ == "__main__":
    main()

"""Весь сервис одним процессом: планировщик (в фоновых потоках) + Telegram-бот (в основном потоке).

  python -m pipeline.scheduler      или      make run

Один процесс — потому что DuckDB допускает только один пишущий процесс на файл.
"""
from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline.config import load_config, setup_logging
from pipeline.run_collect import run_cycle

log = logging.getLogger(__name__)


def job_fast() -> None:
    """Каждые 5 минут: funding + mark price, свечи, аналитика, алерты."""
    run_cycle(load_config(), kinds=("funding", "klines"))


def job_oi() -> None:
    """Раз в час: open interest (и сразу проверка аномалий OI)."""
    run_cycle(load_config(), kinds=("oi",))


def job_digest() -> None:
    from ai.digest import make_digest
    from bot.notify import send
    try:
        send(make_digest())
    except Exception:
        log.exception("утренняя сводка не собралась")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    s = cfg["schedule"]
    hour, minute = map(int, s["digest_time"].split(":"))

    # max_instances=1: если прошлый запуск ещё идёт, новый не стартует поверх.
    # coalesce: после «провала» (ноутбук спал) пропущенные запуски схлопываются в один.
    sched = BackgroundScheduler(timezone="UTC", job_defaults={
        "max_instances": 1, "coalesce": True, "misfire_grace_time": 120})
    sched.add_job(job_fast, CronTrigger(minute=f"*/{s['funding_every_min']}", second=20), id="fast")
    sched.add_job(job_oi, CronTrigger(minute=s["oi_minute"], second=50), id="oi")
    sched.add_job(job_digest, CronTrigger(hour=hour, minute=minute, timezone=s["timezone"]), id="digest")
    sched.start()
    log.info("планировщик запущен: сбор каждые %d мин, OI в :%02d, сводка в %s (%s)",
             s["funding_every_min"], s["oi_minute"], s["digest_time"], s["timezone"])

    sched.add_job(lambda: run_cycle(cfg, kinds=("funding", "oi", "klines")), id="startup")   # первый проход сразу

    from bot.telegram import build_app
    app = build_app()
    try:
        if app:
            log.info("Telegram-бот запущен")
            app.run_polling()           # блокирует основной поток до Ctrl+C
        else:
            log.warning("TELEGRAM_BOT_TOKEN не задан — работаю без бота, алерты пишу в лог")
            while True:
                time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        sched.shutdown(wait=False)
        log.info("остановлено")


if __name__ == "__main__":
    main()

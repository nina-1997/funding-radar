"""Загрузка config.yaml и .env, настройка логирования. Один раз на процесс."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def load_config() -> dict:
    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # FR_DB позволяет подменить файл базы (демо, тесты), не трогая конфиг
    if os.getenv("FR_DB"):
        cfg["paths"]["db"] = os.environ["FR_DB"]
    for key in ("db", "raw", "logs"):
        p = Path(cfg["paths"][key])
        cfg["paths"][key] = str(p if p.is_absolute() else ROOT / p)
    return cfg


def enabled_exchanges(cfg: dict) -> list[str]:
    return [name for name, ex in cfg["exchanges"].items() if ex.get("enabled", True)]


def setup_logging(cfg: dict) -> None:
    """Лог одновременно в консоль и в logs/funding_radar.log (с ротацией)."""
    root = logging.getLogger()
    if root.handlers:
        return
    Path(cfg["paths"]["logs"]).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file = RotatingFileHandler(
        Path(cfg["paths"]["logs"]) / "funding_radar.log",
        maxBytes=5_000_000, backupCount=5, encoding="utf-8",
    )
    file.setFormatter(fmt)
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("httpx", "apscheduler", "telegram", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

"""Общий интерфейс коллектора: HTTP с повторами и паузами между запросами.

Коллектор ничего не нормализует и не пишет в базу. Он возвращает «пакет»
(bundle) — ответы API как есть:

    {"exchange": "binance", "kind": "funding", "fetched_at_ms": ..., "responses": {...}}

Пакет сохраняется в raw/ и только потом отдаётся в pipeline/normalize.py.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {418, 429, 500, 502, 503, 504}


class CollectorError(Exception):
    """Биржа ответила ошибкой или не ответила после всех повторов."""


class ExchangeUnavailable(CollectorError):
    """Биржа недоступна целиком (сеть, блокировка, 5xx после повторов) — нет смысла спрашивать остальные тикеры."""


class BaseCollector:
    name = "base"

    def __init__(self, cfg: dict):
        ex = cfg["exchanges"][self.name]
        self.base_url = ex["base_url"].rstrip("/")
        self.min_interval_s = float(ex.get("min_interval_s", 0.1))
        self.timeout_s = cfg["http"]["timeout_s"]
        self.max_retries = cfg["http"]["max_retries"]
        self.backoff_base_s = cfg["http"]["backoff_base_s"]
        self.quote = cfg["quote"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "funding-radar/1.0 (pet project)"
        self._last_request = 0.0

    # ---------- HTTP ----------

    def _throttle(self) -> None:
        """Не чаще одного запроса в min_interval_s — чтобы не упереться в лимит."""
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = self.base_url + path
        last_error = "unknown"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = f"{type(e).__name__}: {e}"
                delay = self.backoff_base_s * 2 ** attempt
            else:
                if resp.status_code == 200:
                    try:
                        return self.check(resp.json(), path)
                    except ValueError as e:      # пришёл не JSON
                        raise CollectorError(f"{self.name} {path}: не JSON ({e})") from e
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in RETRY_STATUSES:
                    hint = " (похоже на гео-блокировку)" if resp.status_code in (403, 451) else ""
                    error = ExchangeUnavailable if hint else CollectorError
                    raise error(f"{self.name} {path}: {last_error}{hint}")
                # 429/418: биржа сама говорит, сколько ждать
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() \
                    else self.backoff_base_s * 2 ** attempt
            if attempt < self.max_retries:
                log.warning("%s %s: %s — повтор через %.0f с (%d/%d)",
                            self.name, path, last_error, delay, attempt + 1, self.max_retries)
                time.sleep(delay)
        raise ExchangeUnavailable(f"{self.name} {path}: не удалось после {self.max_retries} повторов: {last_error}")

    def check(self, payload: Any, path: str) -> Any:
        """Проверка «конверта» ответа. У каждой биржи свой код успеха."""
        return payload

    def _bundle(self, kind: str, responses: dict) -> dict:
        return {
            "exchange": self.name,
            "kind": kind,
            "fetched_at_ms": int(time.time() * 1000),
            "responses": responses,
        }

    def _per_symbol(self, symbols: list[str], fn) -> dict:
        """Запрос по каждому инструменту. Один сломанный тикер не роняет остальные,
        но если не ответил ни один — это ошибка коллектора."""
        out, errors = {}, []
        for base in symbols:
            ex_symbol = self.exchange_symbol(base)
            try:
                out[ex_symbol] = fn(ex_symbol)
            except ExchangeUnavailable:
                raise                                    # биржа лежит — не тратим время на остальные тикеры
            except CollectorError as e:
                errors.append(str(e))
                log.warning("пропускаю %s: %s", ex_symbol, e)
        if symbols and not out:
            raise CollectorError(f"{self.name}: ни один инструмент не получен: {errors[0]}")
        return out

    # ---------- интерфейс: одинаковый у всех бирж ----------

    def exchange_symbol(self, base: str) -> str:
        raise NotImplementedError

    def fetch_funding(self, symbols: list[str]) -> dict:
        raise NotImplementedError

    def fetch_oi(self, symbols: list[str]) -> dict:
        raise NotImplementedError

    def fetch_klines(self, symbols: list[str], limit: int, end_ms: int | None = None) -> dict:
        """limit последних 5-мин свечей; end_ms — «не позже этого момента» (для догрузки назад)."""
        raise NotImplementedError

    def fetch_funding_history(self, symbols: list[str], limit: int) -> dict:
        raise NotImplementedError

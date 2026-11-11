"""Bybit V5, категория linear (USDT-перпы). Документация: https://bybit-exchange.github.io/docs/v5/intro"""
from __future__ import annotations

import time

from collectors.base import BaseCollector, CollectorError


class BybitCollector(BaseCollector):
    name = "bybit"
    MAX_KLINES = 1000

    def __init__(self, cfg):
        super().__init__(cfg)
        self._instruments: dict = {}
        self._instruments_at = 0.0

    def exchange_symbol(self, base: str) -> str:
        return f"{base}{self.quote}"                     # BTCUSDT

    def check(self, payload, path):
        # У Bybit HTTP почти всегда 200, а ошибка — в retCode
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            code = payload.get("retCode") if isinstance(payload, dict) else "?"
            msg = payload.get("retMsg") if isinstance(payload, dict) else str(payload)[:200]
            raise CollectorError(f"bybit {path}: retCode={code} {msg}")
        return payload

    def _instruments_info(self, symbols):
        """Справочник инструментов (там интервал funding в минутах). Меняется редко — кэш на 6 часов."""
        if not self._instruments or time.time() - self._instruments_at > 6 * 3600:
            self._instruments = self._per_symbol(
                symbols, lambda s: self._get("/v5/market/instruments-info", {"category": "linear", "symbol": s}))
            self._instruments_at = time.time()
        return self._instruments

    def fetch_funding(self, symbols):
        return self._bundle("funding", {
            "tickers": self._get("/v5/market/tickers", {"category": "linear"}),   # все инструменты разом
            "instruments": self._instruments_info(symbols),
        })

    def fetch_oi(self, symbols):
        # open interest у Bybit приходит в том же /tickers
        return self._bundle("oi", {"tickers": self._get("/v5/market/tickers", {"category": "linear"})})

    def fetch_klines(self, symbols, limit, end_ms=None):
        def one(s):
            params = {"category": "linear", "symbol": s, "interval": "5", "limit": min(limit, self.MAX_KLINES)}
            if end_ms:
                params["end"] = end_ms
            return self._get("/v5/market/kline", params)
        return self._bundle("klines", {"kline": self._per_symbol(symbols, one)})

    def fetch_funding_history(self, symbols, limit):
        return self._bundle("funding_history", {
            "history": self._per_symbol(symbols, lambda s: self._get(
                "/v5/market/funding/history", {"category": "linear", "symbol": s, "limit": min(limit, 200)})),
            "instruments": self._instruments_info(symbols),
        })

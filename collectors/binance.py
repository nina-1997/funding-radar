"""Binance USDⓈ-M Futures. Документация: https://developers.binance.com/docs/derivatives/usds-margined-futures"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorError

log = logging.getLogger(__name__)


class BinanceCollector(BaseCollector):
    name = "binance"
    MAX_KLINES = 1500

    def exchange_symbol(self, base: str) -> str:
        return f"{base}{self.quote}"                     # BTCUSDT

    def check(self, payload, path):
        # Ошибка у Binance — объект {"code": -1121, "msg": "..."}
        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            raise CollectorError(f"binance {path}: {payload['code']} {payload['msg']}")
        return payload

    def fetch_funding(self, symbols):
        responses = {"premiumIndex": self._get("/fapi/v1/premiumIndex")}   # все инструменты одним запросом
        try:
            # интервал начисления: здесь только монеты с НЕстандартным интервалом (не 8 ч)
            responses["fundingInfo"] = self._get("/fapi/v1/fundingInfo")
        except CollectorError as e:
            log.warning("binance fundingInfo недоступен, считаю интервал 8 ч: %s", e)
            responses["fundingInfo"] = None
        return self._bundle("funding", responses)

    def fetch_oi(self, symbols):
        return self._bundle("oi", {
            "openInterest": self._per_symbol(
                symbols, lambda s: self._get("/fapi/v1/openInterest", {"symbol": s})),
            "premiumIndex": self._get("/fapi/v1/premiumIndex"),   # mark price, чтобы перевести OI в доллары
        })

    def fetch_klines(self, symbols, limit, end_ms=None):
        def one(s):
            params = {"symbol": s, "interval": "5m", "limit": min(limit, self.MAX_KLINES)}
            if end_ms:
                params["endTime"] = end_ms
            return self._get("/fapi/v1/klines", params)
        return self._bundle("klines", {"klines": self._per_symbol(symbols, one)})

    def fetch_funding_history(self, symbols, limit):
        return self._bundle("funding_history", {
            "fundingRate": self._per_symbol(
                symbols, lambda s: self._get("/fapi/v1/fundingRate", {"symbol": s, "limit": min(limit, 1000)})),
        })

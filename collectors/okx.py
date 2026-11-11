"""OKX V5, бессрочные свопы. Документация: https://www.okx.com/docs-v5/en/"""
from __future__ import annotations

from collectors.base import BaseCollector, CollectorError


class OkxCollector(BaseCollector):
    name = "okx"
    MAX_KLINES = 300

    def exchange_symbol(self, base: str) -> str:
        return f"{base}-{self.quote}-SWAP"               # BTC-USDT-SWAP

    def check(self, payload, path):
        # У OKX успех — code == "0" (строкой!)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            code = payload.get("code") if isinstance(payload, dict) else "?"
            msg = payload.get("msg") if isinstance(payload, dict) else str(payload)[:200]
            raise CollectorError(f"okx {path}: code={code} {msg}")
        return payload

    def fetch_funding(self, symbols):
        return self._bundle("funding", {
            # funding отдаётся только по одному инструменту за запрос
            "funding-rate": self._per_symbol(
                symbols, lambda s: self._get("/api/v5/public/funding-rate", {"instId": s})),
            "mark-price": self._get("/api/v5/public/mark-price", {"instType": "SWAP"}),
            "index-tickers": self._get("/api/v5/market/index-tickers", {"quoteCcy": self.quote}),
        })

    def fetch_oi(self, symbols):
        return self._bundle("oi", {
            "open-interest": self._get("/api/v5/public/open-interest", {"instType": "SWAP"}),
            "mark-price": self._get("/api/v5/public/mark-price", {"instType": "SWAP"}),
        })

    def fetch_klines(self, symbols, limit, end_ms=None):
        # /candles отдаёт только ~1440 последних свечей; глубже — /history-candles (не больше 100 за запрос)
        path = "/api/v5/market/history-candles" if end_ms else "/api/v5/market/candles"
        max_limit = 100 if end_ms else self.MAX_KLINES

        def one(s):
            params = {"instId": s, "bar": "5m", "limit": min(limit, max_limit)}
            if end_ms:
                params["after"] = end_ms          # у OKX after = «записи старше этого времени»
            return self._get(path, params)
        return self._bundle("klines", {"candles": self._per_symbol(symbols, one)})

    def fetch_funding_history(self, symbols, limit):
        return self._bundle("funding_history", {
            "history": self._per_symbol(symbols, lambda s: self._get(
                "/api/v5/public/funding-rate-history", {"instId": s, "limit": min(limit, 100)})),
            "funding-rate": self._per_symbol(
                symbols, lambda s: self._get("/api/v5/public/funding-rate", {"instId": s})),
        })

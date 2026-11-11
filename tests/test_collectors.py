"""Коллекторы без сети: подменяем HTTP и проверяем повторы и обработку ошибок."""
import pytest

from collectors import BinanceCollector, BybitCollector, OkxCollector
from collectors.base import CollectorError
from pipeline.config import load_config


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def make(cls, responses, monkeypatch):
    """Коллектор, у которого session.get по очереди отдаёт заготовленные ответы."""
    monkeypatch.setattr("time.sleep", lambda s: None)            # не ждём по-настоящему
    c = cls(load_config())
    queue, calls = list(responses), []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    c.session.get = fake_get
    return c, calls


def test_429_then_success_is_retried(monkeypatch):
    c, calls = make(BinanceCollector, [FakeResponse(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "1"}),
                                      FakeResponse(200, [{"symbol": "BTCUSDT"}])], monkeypatch)
    assert c._get("/fapi/v1/premiumIndex") == [{"symbol": "BTCUSDT"}]
    assert len(calls) == 2


def test_timeout_is_retried_then_fails_loudly(monkeypatch):
    import requests
    c, calls = make(BinanceCollector, [requests.Timeout("slow")] * 5, monkeypatch)
    with pytest.raises(CollectorError, match="не удалось после 4 повторов"):
        c._get("/fapi/v1/premiumIndex")
    assert len(calls) == 5                                       # первая попытка + 4 повтора


def test_geo_block_is_not_retried(monkeypatch):
    c, calls = make(BinanceCollector, [FakeResponse(451, {"msg": "restricted location"})], monkeypatch)
    with pytest.raises(CollectorError, match="гео-блокировк"):
        c._get("/fapi/v1/premiumIndex")
    assert len(calls) == 1


def test_bybit_error_inside_http_200(monkeypatch):
    c, _ = make(BybitCollector, [FakeResponse(200, {"retCode": 10001, "retMsg": "params error", "result": {}})], monkeypatch)
    with pytest.raises(CollectorError, match="10001"):
        c._get("/v5/market/tickers", {"category": "linear"})


def test_okx_error_inside_http_200(monkeypatch):
    c, _ = make(OkxCollector, [FakeResponse(200, {"code": "51001", "msg": "Instrument ID does not exist", "data": []})], monkeypatch)
    with pytest.raises(CollectorError, match="51001"):
        c._get("/api/v5/public/funding-rate", {"instId": "XXX-USDT-SWAP"})


def test_one_bad_symbol_does_not_break_others(monkeypatch):
    ok = FakeResponse(200, {"code": "0", "msg": "", "data": [{"instId": "BTC-USDT-SWAP"}]})
    bad = FakeResponse(200, {"code": "51001", "msg": "Instrument ID does not exist", "data": []})
    c, _ = make(OkxCollector, [ok, bad], monkeypatch)
    got = c._per_symbol(["BTC", "XXX"], lambda s: c._get("/api/v5/public/funding-rate", {"instId": s}))
    assert list(got) == ["BTC-USDT-SWAP"]


def test_exchange_symbols():
    cfg = load_config()
    assert BinanceCollector(cfg).exchange_symbol("BTC") == "BTCUSDT"
    assert BybitCollector(cfg).exchange_symbol("BTC") == "BTCUSDT"
    assert OkxCollector(cfg).exchange_symbol("BTC") == "BTC-USDT-SWAP"


def test_okx_backfill_uses_history_endpoint(monkeypatch):
    """Обычный /candles отдаёт только ~1440 последних свечей, для истории нужен /history-candles."""
    ok = lambda: FakeResponse(200, {"code": "0", "msg": "", "data": []})
    c, calls = make(OkxCollector, [ok(), ok()], monkeypatch)
    c.fetch_klines(["BTC"], 300)
    c.fetch_klines(["BTC"], 300, end_ms=1794409200000)
    assert calls[0][0].endswith("/api/v5/market/candles") and calls[0][1]["limit"] == 300
    assert calls[1][0].endswith("/api/v5/market/history-candles") and calls[1][1]["limit"] == 100

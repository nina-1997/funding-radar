"""Сквозной проход без сети: коллектор -> raw -> нормализация -> база -> collector_runs."""
import json
from pathlib import Path

from collectors.base import BaseCollector
from pipeline import run_collect
from pipeline.config import load_config
from tests.test_collectors import FakeResponse

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["responses"]


def fake_api(url, params=None, timeout=None):
    """Мини-«биржа»: по адресу запроса отдаёт кусок сохранённого ответа."""
    params = params or {}
    path = url.split("/", 3)[3]
    sym = params.get("symbol") or params.get("instId")
    routes = {
        "fapi/v1/premiumIndex": lambda: fixture("binance_funding")["premiumIndex"],
        "fapi/v1/fundingInfo": lambda: fixture("binance_funding")["fundingInfo"],
        "v5/market/tickers": lambda: fixture("bybit_funding")["tickers"],
        "v5/market/instruments-info": lambda: fixture("bybit_funding")["instruments"].get(sym),
        "api/v5/public/funding-rate": lambda: fixture("okx_funding")["funding-rate"].get(sym),
        "api/v5/public/mark-price": lambda: fixture("okx_funding")["mark-price"],
        "api/v5/market/index-tickers": lambda: fixture("okx_funding")["index-tickers"],
    }
    payload = routes[path]()
    if payload is None:         # инструмента нет в фикстуре — отвечаем, как биржа на неизвестный тикер
        payload = {"retCode": 10001, "retMsg": "symbol invalid"} if "v5/market" in path \
            else {"code": "51001", "msg": "Instrument ID does not exist", "data": []}
    return FakeResponse(200, payload)


def test_cycle_end_to_end(db, tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setitem(cfg["paths"], "raw", str(tmp_path / "raw"))
    monkeypatch.setattr(run_collect, "_collectors", {})
    monkeypatch.setattr("time.sleep", lambda s: None)
    original_init = BaseCollector.__init__

    def patched_init(self, cfg):
        original_init(self, cfg)
        self.session.get = fake_api
    monkeypatch.setattr(BaseCollector, "__init__", patched_init)

    for _ in range(2):          # два прохода подряд в одном слоте — дублей быть не должно
        run_collect.run_cycle(cfg, kinds=("funding",), send_alerts=False)

    with db() as con:
        rows = con.execute("SELECT exchange, symbol, funding_rate FROM funding ORDER BY 1, 2").fetchall()
        runs = con.execute("SELECT collector, status, rows FROM collector_runs ORDER BY id").fetchall()
        n_spreads = con.execute("SELECT count(*) FROM spreads").fetchone()[0]
        n_instruments = con.execute("SELECT count(*) FROM instruments").fetchone()[0]

    assert len(rows) == 9                                        # 3 биржи * 3 монеты из фикстур
    assert len({r[:2] for r in rows}) == 9
    assert [r[:2] for r in runs] == [(f"{e}.funding", "ok") for e in ("binance", "bybit", "okx")] * 2
    assert n_spreads == 3 and n_instruments == 9
    raw_files = list((tmp_path / "raw").rglob("*.json"))
    assert len(raw_files) >= 3                                   # сырое сохранено по каждой бирже
    assert json.loads(raw_files[0].read_text(encoding="utf-8"))["kind"] == "funding"


def test_failed_collector_is_logged_and_others_continue(db, tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setitem(cfg["paths"], "raw", str(tmp_path / "raw"))
    monkeypatch.setattr(run_collect, "_collectors", {})
    monkeypatch.setattr("time.sleep", lambda s: None)
    original_init = BaseCollector.__init__

    def patched_init(self, cfg):
        original_init(self, cfg)
        down = FakeResponse(503, {"msg": "maintenance"})
        self.session.get = (lambda *a, **k: down) if self.name == "bybit" else fake_api
    monkeypatch.setattr(BaseCollector, "__init__", patched_init)

    run_collect.run_cycle(cfg, kinds=("funding",), send_alerts=False)
    with db() as con:
        runs = dict(con.execute("SELECT collector, status FROM collector_runs").fetchall())
        exchanges = {r[0] for r in con.execute("SELECT DISTINCT exchange FROM funding").fetchall()}
        error = con.execute("SELECT error FROM collector_runs WHERE status = 'error'").fetchone()[0]
    assert runs == {"binance.funding": "ok", "bybit.funding": "error", "okx.funding": "ok"}
    assert exchanges == {"binance", "okx"}                       # Bybit лежит, остальные собраны
    assert "503" in error

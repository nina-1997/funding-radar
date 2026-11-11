"""Нормализация: три формата -> одна схема. Запуск: pytest -q"""
import copy
from datetime import datetime

import pytest

from pipeline import normalize as n

SLOT = datetime(2026, 11, 11, 15, 0)
NOW_MS = 1794409225000          # 15:00:25 UTC — свеча, открытая в 15:00, ещё не закрыта
SYMBOLS = ["BTC", "ETH", "SOL"]


def by_symbol(rows):
    return {r["symbol"]: r for r in rows}


# ---------- кирпичики ----------

@pytest.mark.parametrize("exchange, raw, expected", [
    ("binance", "BTCUSDT", "BTC"),
    ("bybit", "DOGEUSDT", "DOGE"),
    ("okx", "BTC-USDT-SWAP", "BTC"),
    ("okx", "BTC-USD-SWAP", None),          # инверсный контракт — не наш
    ("binance", "BTCUSDC", None),
    ("binance", "BTCUSDT_261225", None),    # квартальный фьючерс
    ("bybit", "BTCPERP", None),
])
def test_to_symbol(exchange, raw, expected):
    assert n.to_symbol(exchange, raw) == expected


def test_time_number_and_string_give_same_result():
    assert n.to_ts(1794409200000) == n.to_ts("1794409200000") == SLOT


def test_rate_to_8h():
    assert n.rate_to_8h(0.0001, 8) == pytest.approx(0.0001)
    assert n.rate_to_8h(0.0001, 4) == pytest.approx(0.0002)     # платят вдвое чаще -> за 8 ч вдвое больше
    assert n.rate_to_8h(0.0001, 1) == pytest.approx(0.0008)
    with pytest.raises(n.NormalizeError):
        n.rate_to_8h(0.0001, 0)


def test_annualize():
    assert n.annualize(0.0001) == pytest.approx(0.1095)          # 0.01 % * 3 * 365 = 10.95 % годовых


# ---------- funding ----------

@pytest.mark.parametrize("exchange, btc_rate, btc_mark", [
    ("binance", 0.0001, 102410.1),
    ("bybit", 0.000312, 102415.5),
    ("okx", 0.000098, 102408.3),
])
def test_funding_btc(load, exchange, btc_rate, btc_mark):
    rows, instruments = n.normalize_funding(load(f"{exchange}_funding"), SYMBOLS, SLOT)
    btc = by_symbol(rows)["BTC"]
    assert btc["exchange"] == exchange
    assert btc["ts"] == SLOT                                     # общий слот, а не время биржи
    assert btc["funding_rate"] == pytest.approx(btc_rate)
    assert btc["raw_interval_h"] == 8
    assert btc["mark_price"] == pytest.approx(btc_mark)
    assert btc["next_funding_ts"] == datetime(2026, 11, 11, 16, 0)
    assert isinstance(btc["funding_rate"], float)                # число, не строка
    assert {r["symbol"] for r in rows} == set(SYMBOLS)           # чужие инструменты отфильтрованы
    assert {i["symbol"] for i in instruments} == set(SYMBOLS)


@pytest.mark.parametrize("exchange, raw, normalized", [
    ("binance", 0.00005, 0.0001),       # интервал 4 ч из fundingInfo
    ("bybit", 0.00004, 0.00008),        # интервал 4 ч из тикера / справочника
    ("okx", 0.00003, 0.00006),          # интервал 4 ч = nextFundingTime - fundingTime
])
def test_funding_4h_is_scaled_to_8h(load, exchange, raw, normalized):
    rows, instruments = n.normalize_funding(load(f"{exchange}_funding"), SYMBOLS, SLOT)
    sol = by_symbol(rows)["SOL"]
    assert sol["raw_interval_h"] == 4
    assert sol["raw_rate"] == pytest.approx(raw)
    assert sol["funding_rate"] == pytest.approx(normalized)
    assert by_symbol(instruments)["SOL"]["funding_interval_h"] == 4


def test_exchange_symbols_are_kept(load):
    _, instruments = n.normalize_funding(load("okx_funding"), SYMBOLS, SLOT)
    assert by_symbol(instruments)["BTC"]["exchange_symbol"] == "BTC-USDT-SWAP"


def test_bybit_interval_falls_back_to_instruments(load):
    """У ETH в тикере нет fundingIntervalHour — берём из справочника (480 минут)."""
    rows, _ = n.normalize_funding(load("bybit_funding"), SYMBOLS, SLOT)
    assert by_symbol(rows)["ETH"]["raw_interval_h"] == 8


def test_three_exchanges_are_comparable(load):
    """Сдача шага 3: BTC на трёх биржах — три строки с одинаковым ts и сравнимыми ставками."""
    rows = [by_symbol(n.normalize_funding(load(f"{e}_funding"), SYMBOLS, SLOT)[0])["BTC"] for e in n.EXCHANGES]
    assert len({r["ts"] for r in rows}) == 1
    assert all(abs(r["funding_rate"]) < 0.01 for r in rows)     # все в долях, никто не в процентах


# ---------- сценарии «сломать и починить» ----------

def test_missing_funding_field_fails_loudly(load):
    bundle = copy.deepcopy(load("binance_funding"))
    del bundle["responses"]["premiumIndex"][0]["lastFundingRate"]   # биржа переименовала поле
    with pytest.raises(n.NormalizeError, match="lastFundingRate"):
        n.normalize_funding(bundle, SYMBOLS, SLOT)


def test_garbage_instead_of_number_fails_loudly(load):
    bundle = copy.deepcopy(load("bybit_funding"))
    bundle["responses"]["tickers"]["result"]["list"][0]["fundingRate"] = "n/a"
    with pytest.raises(n.NormalizeError):
        n.normalize_funding(bundle, SYMBOLS, SLOT)


def test_number_instead_of_string_is_fine(load):
    bundle = copy.deepcopy(load("okx_funding"))
    bundle["responses"]["funding-rate"]["BTC-USDT-SWAP"]["data"][0]["fundingRate"] = 0.000098   # число вместо строки
    rows, _ = n.normalize_funding(bundle, SYMBOLS, SLOT)
    assert by_symbol(rows)["BTC"]["funding_rate"] == pytest.approx(0.000098)


def test_renamed_ticker_fails_loudly(load):
    bundle = copy.deepcopy(load("binance_funding"))
    for item in bundle["responses"]["premiumIndex"]:
        item["symbol"] = item["symbol"].replace("USDT", "-USDT")    # тикеры переименовали
    with pytest.raises(n.NormalizeError, match="нет ни одного"):
        n.normalize_funding(bundle, SYMBOLS, SLOT)


def test_missing_optional_price_is_null_not_zero(load):
    bundle = copy.deepcopy(load("binance_funding"))
    bundle["responses"]["premiumIndex"][0]["markPrice"] = ""
    rows, _ = n.normalize_funding(bundle, SYMBOLS, SLOT)
    assert by_symbol(rows)["BTC"]["mark_price"] is None


# ---------- open interest ----------

def test_oi_in_usd_on_all_exchanges(load):
    binance = by_symbol(n.normalize_oi(load("binance_oi"), SYMBOLS, SLOT))
    bybit = by_symbol(n.normalize_oi(load("bybit_oi"), SYMBOLS, SLOT))
    okx = by_symbol(n.normalize_oi(load("okx_oi"), SYMBOLS, SLOT))
    assert binance["BTC"]["oi_usd"] == pytest.approx(80512.345 * 102410.1)     # монеты * mark price
    assert bybit["BTC"]["oi_usd"] == pytest.approx(6_267_000_000)
    assert okx["BTC"]["oi_usd"] == pytest.approx(3_021_000_000)
    assert okx["BTC"]["oi_contracts"] == pytest.approx(29500)                  # oiCcy (монеты), а не oi (контракты)
    assert okx["ETH"]["oi_usd"] == pytest.approx(410000 * 3860.11)             # oiUsd пустой -> считаем сами
    assert set(okx) == {"BTC", "ETH"}                                          # BTC-USD-SWAP отброшен


# ---------- свечи ----------

@pytest.mark.parametrize("exchange, first_open", [("binance", 102300.0), ("bybit", 102305.0), ("okx", 102302.0)])
def test_klines(load, exchange, first_open):
    rows = sorted(n.normalize_klines(load(f"{exchange}_klines"), SYMBOLS, NOW_MS), key=lambda r: r["ts"])
    assert len(rows) == 3                                        # четвёртая свеча не закрыта — отброшена
    assert rows[0]["ts"] == datetime(2026, 11, 11, 14, 45)
    assert rows[-1]["ts"] == datetime(2026, 11, 11, 14, 55)
    assert rows[0]["open"] == pytest.approx(first_open)
    assert rows[0]["interval"] == "5m"
    assert rows[0]["volume"] > 0 and rows[0]["quote_volume"] > 1_000_000


def test_okx_volume_is_in_coins_not_contracts(load):
    rows = n.normalize_klines(load("okx_klines"), SYMBOLS, NOW_MS)
    assert rows[0]["volume"] == pytest.approx(74.2)              # volCcy, а не vol=7420 контрактов

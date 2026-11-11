"""Нормализация: три формата бирж -> одна схема.

Здесь только чистые функции: на входе пакет ответов API (как лежит в raw/),
на выходе список словарей под таблицы. Никакой сети и базы — поэтому модуль
легко тестировать на сохранённых ответах (tests/fixtures).

Что приводим:
  тикер     BTCUSDT / BTC-USDT-SWAP          -> BTC
  время     мс числом / мс строкой           -> datetime UTC
  числа     "0.0001" строкой                 -> float
  funding   ставка за 1/4/8 ч                -> ставка за 8 ч
Если нужного поля нет или оно не число — NormalizeError. Нули молча не пишем.
"""
from __future__ import annotations

from datetime import datetime, timezone

EXCHANGES = ("binance", "bybit", "okx")
KLINE_MS = 5 * 60 * 1000


class NormalizeError(Exception):
    pass


# ---------- кирпичики ----------

def to_ts(ms) -> datetime:
    """Миллисекунды Unix (числом или строкой) -> datetime в UTC (без tzinfo, как хранит DuckDB)."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError) as e:
        raise NormalizeError(f"не время: {ms!r}") from e


def to_float(value, field: str) -> float:
    """Обязательное число. Пустая строка / None / мусор -> ошибка."""
    try:
        if value is None or value == "":
            raise ValueError("пусто")
        return float(value)
    except (TypeError, ValueError) as e:
        raise NormalizeError(f"поле {field}: ожидалось число, пришло {value!r}") from e


def opt_float(value) -> float | None:
    """Необязательное число: нет значения -> None (в базе будет NULL, а не 0)."""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def to_symbol(exchange: str, exchange_symbol: str, quote: str = "USDT") -> str | None:
    """BTCUSDT -> BTC, BTC-USDT-SWAP -> BTC. Чужой инструмент (не USDT-перп) -> None."""
    if exchange == "okx":
        parts = exchange_symbol.split("-")
        return parts[0] if len(parts) == 3 and parts[1] == quote and parts[2] == "SWAP" else None
    if exchange_symbol.endswith(quote) and len(exchange_symbol) > len(quote):
        return exchange_symbol[: -len(quote)]
    return None


def rate_to_8h(raw_rate: float, interval_h: float) -> float:
    """Ставка за interval_h часов -> эквивалент за 8 часов. 0.01 % за 4 ч = 0.02 % за 8 ч."""
    if not interval_h or interval_h <= 0:
        raise NormalizeError(f"некорректный интервал funding: {interval_h!r}")
    return raw_rate * 8.0 / interval_h


def annualize(rate_8h: float) -> float:
    """Годовая ставка: 3 начисления в сутки * 365 дней."""
    return rate_8h * 3 * 365


def _require(payload, exchange: str, what: str):
    if payload is None:
        raise NormalizeError(f"{exchange}: в пакете нет ответа {what}")
    return payload


def _funding_row(exchange, symbol, slot_ts, raw_rate, interval_h, mark, index, next_ms):
    return {
        "exchange": exchange, "symbol": symbol, "ts": slot_ts,
        "funding_rate": rate_to_8h(raw_rate, interval_h),
        "mark_price": mark, "index_price": index,
        "next_funding_ts": to_ts(next_ms) if next_ms not in (None, "", 0, "0") else None,
        "raw_rate": raw_rate, "raw_interval_h": float(interval_h),
    }


def _instrument_row(exchange, symbol, quote, exchange_symbol, interval_h):
    return {
        "exchange": exchange, "symbol": symbol, "base": symbol, "quote": quote,
        "exchange_symbol": exchange_symbol, "funding_interval_h": float(interval_h), "is_active": True,
    }


# ---------- интервалы funding ----------

def binance_intervals(funding_info) -> dict[str, float]:
    """fundingInfo перечисляет только монеты с нестандартным интервалом; остальных там нет (= 8 ч)."""
    return {i["symbol"]: float(i["fundingIntervalHours"]) for i in (funding_info or [])
            if "fundingIntervalHours" in i}


def bybit_intervals(tickers_list, instruments: dict | None) -> dict[str, float]:
    out = {}
    for sym, resp in (instruments or {}).items():
        for item in resp.get("result", {}).get("list", []):
            if item.get("fundingInterval"):                 # в минутах
                out[sym] = float(item["fundingInterval"]) / 60.0
    for t in tickers_list or []:
        if t.get("fundingIntervalHour"):                    # в часах, строкой
            out[t["symbol"]] = float(t["fundingIntervalHour"])
    return out


def okx_interval(item: dict) -> float:
    """У OKX интервал = расстояние между текущим и следующим расчётом."""
    hours = (int(item["nextFundingTime"]) - int(item["fundingTime"])) / 3_600_000
    if hours <= 0:
        raise NormalizeError(f"okx: странный интервал funding {hours} ч у {item.get('instId')}")
    return hours


# ---------- funding ----------

def normalize_funding(bundle: dict, symbols: list[str], slot_ts: datetime, quote: str = "USDT"):
    """-> (строки funding, строки instruments). slot_ts — общий 5-минутный слот для всех бирж."""
    exchange, r = bundle["exchange"], bundle["responses"]
    wanted = set(symbols)
    funding, instruments = [], []

    if exchange == "binance":
        intervals = binance_intervals(r.get("fundingInfo"))
        for item in _require(r.get("premiumIndex"), exchange, "premiumIndex"):
            symbol = to_symbol(exchange, item.get("symbol", ""), quote)
            if symbol not in wanted:
                continue
            interval_h = intervals.get(item["symbol"], 8.0)
            funding.append(_funding_row(
                exchange, symbol, slot_ts,
                to_float(item.get("lastFundingRate"), "lastFundingRate"), interval_h,
                opt_float(item.get("markPrice")), opt_float(item.get("indexPrice")),
                item.get("nextFundingTime")))
            instruments.append(_instrument_row(exchange, symbol, quote, item["symbol"], interval_h))

    elif exchange == "bybit":
        tickers = _require(r.get("tickers"), exchange, "tickers")["result"]["list"]
        intervals = bybit_intervals(tickers, r.get("instruments"))
        for item in tickers:
            symbol = to_symbol(exchange, item.get("symbol", ""), quote)
            if symbol not in wanted:
                continue
            if item["symbol"] not in intervals:
                raise NormalizeError(f"bybit: неизвестен интервал funding для {item['symbol']}")
            interval_h = intervals[item["symbol"]]
            funding.append(_funding_row(
                exchange, symbol, slot_ts,
                to_float(item.get("fundingRate"), "fundingRate"), interval_h,
                opt_float(item.get("markPrice")), opt_float(item.get("indexPrice")),
                item.get("nextFundingTime")))
            instruments.append(_instrument_row(exchange, symbol, quote, item["symbol"], interval_h))

    elif exchange == "okx":
        marks = {m["instId"]: opt_float(m.get("markPx"))
                 for m in (r.get("mark-price") or {}).get("data", [])}
        indexes = {i["instId"]: opt_float(i.get("idxPx"))
                   for i in (r.get("index-tickers") or {}).get("data", [])}
        for inst_id, resp in _require(r.get("funding-rate"), exchange, "funding-rate").items():
            symbol = to_symbol(exchange, inst_id, quote)
            if symbol not in wanted or not resp.get("data"):
                continue
            item = resp["data"][0]
            interval_h = okx_interval(item)
            funding.append(_funding_row(
                exchange, symbol, slot_ts,
                to_float(item.get("fundingRate"), "fundingRate"), interval_h,
                marks.get(inst_id), indexes.get(f"{symbol}-{quote}"),
                item.get("fundingTime")))          # fundingTime у OKX = ближайший расчёт
            instruments.append(_instrument_row(exchange, symbol, quote, inst_id, interval_h))
    else:
        raise NormalizeError(f"неизвестная биржа: {exchange}")

    if not funding:
        raise NormalizeError(f"{exchange}: в ответе нет ни одного из инструментов {sorted(wanted)}")
    return funding, instruments


def normalize_funding_history(bundle: dict, symbols: list[str], quote: str = "USDT") -> list[dict]:
    """Состоявшиеся начисления (для догрузки истории). ts = момент расчёта."""
    exchange, r = bundle["exchange"], bundle["responses"]
    wanted, rows = set(symbols), []

    def add(symbol, ms, rate, interval_h, mark=None):
        raw = to_float(rate, "fundingRate")
        ts = to_ts(ms)
        # у Binance время начисления «плавает» на несколько мс — округляем до минуты
        ts = ts.replace(second=0, microsecond=0)
        rows.append({"exchange": exchange, "symbol": symbol, "ts": ts,
                     "funding_rate": rate_to_8h(raw, interval_h), "mark_price": mark,
                     "index_price": None, "next_funding_ts": None,
                     "raw_rate": raw, "raw_interval_h": float(interval_h)})

    if exchange == "binance":
        for ex_symbol, items in r["fundingRate"].items():
            symbol = to_symbol(exchange, ex_symbol, quote)
            if symbol not in wanted:
                continue
            items = sorted(items, key=lambda i: i["fundingTime"])
            for prev, item in zip([None] + items[:-1], items):
                # интервал берём из самой истории: расстояние до предыдущего начисления
                gap_h = round((item["fundingTime"] - prev["fundingTime"]) / 3_600_000) if prev else 8
                add(symbol, item["fundingTime"], item["fundingRate"],
                    gap_h if gap_h in (1, 2, 4, 8) else 8, opt_float(item.get("markPrice")))
    elif exchange == "bybit":
        intervals = bybit_intervals(None, r.get("instruments"))
        for ex_symbol, resp in r["history"].items():
            symbol = to_symbol(exchange, ex_symbol, quote)
            if symbol in wanted:
                for item in resp["result"]["list"]:
                    add(symbol, item["fundingRateTimestamp"], item["fundingRate"], intervals.get(ex_symbol, 8.0))
    elif exchange == "okx":
        for inst_id, resp in r["history"].items():
            symbol = to_symbol(exchange, inst_id, quote)
            if symbol not in wanted:
                continue
            current = (r.get("funding-rate") or {}).get(inst_id, {}).get("data") or []
            interval_h = okx_interval(current[0]) if current else 8.0
            for item in resp["data"]:
                add(symbol, item["fundingTime"], item.get("realizedRate") or item["fundingRate"], interval_h)
    else:
        raise NormalizeError(f"неизвестная биржа: {exchange}")
    return rows


# ---------- open interest ----------

def normalize_oi(bundle: dict, symbols: list[str], slot_ts: datetime, quote: str = "USDT") -> list[dict]:
    """oi_contracts — в базовой монете у всех трёх бирж, oi_usd — в долларах."""
    exchange, r = bundle["exchange"], bundle["responses"]
    wanted, rows = set(symbols), []

    def add(symbol, amount, usd):
        rows.append({"exchange": exchange, "symbol": symbol, "ts": slot_ts,
                     "oi_contracts": amount, "oi_usd": usd})

    if exchange == "binance":
        marks = {p["symbol"]: opt_float(p.get("markPrice")) for p in r.get("premiumIndex") or []}
        for ex_symbol, item in _require(r.get("openInterest"), exchange, "openInterest").items():
            symbol = to_symbol(exchange, ex_symbol, quote)
            if symbol in wanted:
                amount = to_float(item.get("openInterest"), "openInterest")
                mark = marks.get(ex_symbol)
                add(symbol, amount, amount * mark if mark else None)
    elif exchange == "bybit":
        for item in _require(r.get("tickers"), exchange, "tickers")["result"]["list"]:
            symbol = to_symbol(exchange, item.get("symbol", ""), quote)
            if symbol in wanted:
                add(symbol, to_float(item.get("openInterest"), "openInterest"),
                    opt_float(item.get("openInterestValue")))
    elif exchange == "okx":
        marks = {m["instId"]: opt_float(m.get("markPx"))
                 for m in (r.get("mark-price") or {}).get("data", [])}
        for item in _require(r.get("open-interest"), exchange, "open-interest")["data"]:
            symbol = to_symbol(exchange, item.get("instId", ""), quote)
            if symbol in wanted:
                amount = to_float(item.get("oiCcy"), "oiCcy")
                usd = opt_float(item.get("oiUsd"))
                if usd is None and marks.get(item["instId"]):
                    usd = amount * marks[item["instId"]]
                add(symbol, amount, usd)
    else:
        raise NormalizeError(f"неизвестная биржа: {exchange}")

    if not rows:
        raise NormalizeError(f"{exchange}: в ответе нет open interest по {sorted(wanted)}")
    return rows


# ---------- свечи ----------

def normalize_klines(bundle: dict, symbols: list[str], now_ms: int, quote: str = "USDT") -> list[dict]:
    """5-минутные свечи. Незакрытую (текущую) свечу отбрасываем: её объём ещё растёт."""
    exchange, r = bundle["exchange"], bundle["responses"]
    wanted, rows = set(symbols), []

    def add(symbol, ms, o, h, l, c, vol, qvol, trades=None):
        if int(ms) + KLINE_MS > now_ms:
            return
        rows.append({"exchange": exchange, "symbol": symbol, "ts": to_ts(ms), "interval": "5m",
                     "open": to_float(o, "open"), "high": to_float(h, "high"),
                     "low": to_float(l, "low"), "close": to_float(c, "close"),
                     "volume": to_float(vol, "volume"), "quote_volume": opt_float(qvol),
                     "trades": int(trades) if trades is not None else None})

    if exchange == "binance":
        # [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, ...]
        for ex_symbol, candles in r["klines"].items():
            symbol = to_symbol(exchange, ex_symbol, quote)
            if symbol in wanted:
                for k in candles:
                    add(symbol, k[0], k[1], k[2], k[3], k[4], k[5], k[7], k[8])
    elif exchange == "bybit":
        # [startTime, open, high, low, close, volume, turnover] — всё строками, новые сверху
        for ex_symbol, resp in r["kline"].items():
            symbol = to_symbol(exchange, ex_symbol, quote)
            if symbol in wanted:
                for k in resp["result"]["list"]:
                    add(symbol, k[0], k[1], k[2], k[3], k[4], k[5], k[6])
    elif exchange == "okx":
        # [ts, o, h, l, c, vol(контракты), volCcy(монета), volCcyQuote(USDT), confirm]
        for inst_id, resp in r["candles"].items():
            symbol = to_symbol(exchange, inst_id, quote)
            if symbol in wanted:
                for k in resp["data"]:
                    add(symbol, k[0], k[1], k[2], k[3], k[4], k[6], k[7] if len(k) > 7 else None)
    else:
        raise NormalizeError(f"неизвестная биржа: {exchange}")
    return rows

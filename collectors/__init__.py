from collectors.binance import BinanceCollector
from collectors.bybit import BybitCollector
from collectors.okx import OkxCollector

COLLECTORS = {"binance": BinanceCollector, "bybit": BybitCollector, "okx": OkxCollector}

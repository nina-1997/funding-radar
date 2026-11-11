import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load():
    """load('binance_funding') -> пакет из tests/fixtures/binance_funding.json"""
    return lambda name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Чистая временная база на каждый тест."""
    monkeypatch.setenv("FR_DB", str(tmp_path / "test.duckdb"))
    from pipeline.config import load_config
    load_config.cache_clear()
    from storage.db import connect
    yield connect
    load_config.cache_clear()
    os.environ.pop("FR_DB", None)

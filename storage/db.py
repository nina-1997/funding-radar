"""Работа с DuckDB: подключение, схема, upsert.

DuckDB — один файл и один пишущий процесс. Поэтому соединения короткие:
открыли, сделали дело, закрыли. Если файл занят другим процессом (например,
дашборд читает) — ждём и пробуем ещё раз.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

from pipeline.config import load_config

SCHEMA = Path(__file__).with_name("schema.sql")
_lock = threading.RLock()          # внутри процесса пишем по очереди
_schema_ready: set[str] = set()


@contextmanager
def connect(read_only: bool = False, wait_s: float = 30.0):
    """with connect() as con: ...  — соединение закрывается само."""
    path = load_config()["paths"]["db"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                con = duckdb.connect(path, read_only=read_only)
                break
            except duckdb.IOException:      # файл заблокирован другим процессом
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        try:
            if not read_only and path not in _schema_ready:
                con.execute(SCHEMA.read_text(encoding="utf-8"))
                _schema_ready.add(path)
            yield con
        finally:
            con.close()


def upsert(con, table: str, rows: list[dict], keys: list[str]) -> int:
    """Вставить строки; если строка с таким ключом уже есть — обновить её.

    Возвращает число обработанных строк. Повторный вызов с теми же данными
    не меняет COUNT(*) — это и есть идемпотентность.
    """
    if not rows:
        return 0
    # В одной пачке ключ должен встречаться один раз (иначе DuckDB откажет) — берём последнюю версию
    unique = {tuple(r[k] for k in keys): r for r in rows}
    _batch = pd.DataFrame(list(unique.values()))     # DuckDB видит эту переменную по имени в запросе ниже
    cols = list(_batch.columns)
    q = lambda c: f'"{c}"'
    updates = ", ".join(f"{q(c)} = excluded.{q(c)}" for c in cols if c not in keys)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    con.execute(
        f"INSERT INTO {table} ({', '.join(map(q, cols))}) "
        f"SELECT {', '.join(map(q, cols))} FROM _batch "
        f"ON CONFLICT ({', '.join(map(q, keys))}) {action}"
    )
    return len(_batch)


def get_setting(con, key: str, default=None):
    row = con.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
    return row[0] if row else default


def set_setting(con, key: str, value) -> None:
    con.execute(
        "INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [key, str(value)],
    )

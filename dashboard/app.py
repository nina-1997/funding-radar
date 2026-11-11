"""Дашборд Funding Radar. Запуск: make dashboard  (python -m streamlit run dashboard/app.py)

Дашборд только читает. Соединение открывается на время запроса и сразу закрывается,
чтобы не мешать сервису писать (DuckDB: один пишущий процесс на файл).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from pipeline.config import load_config  # noqa: E402

st.set_page_config(page_title="Funding Radar", layout="wide")

if not Path(load_config()["paths"]["db"]).exists():
    st.error(f"Базы {load_config()['paths']['db']} ещё нет. Запусти `make collect` или `make demo`.")
    st.stop()

page = st.navigation([
    st.Page("signals.py", title="Signals", icon="💰", default=True),
    st.Page("overview.py", title="Обзор", icon="📈"),
])
page.run()

"""Страница «Обзор»: funding по биржам, тепловая карта spread, OI и цена, корреляции."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from analysis import correlations
from bot.messages import name
from pipeline.config import load_config
from pipeline.timeutil import utcnow
from storage.db import connect

COLORS = {"binance": "#C98A00", "bybit": "#2B6CB0", "okx": "#4A5568"}
cfg = load_config()


@st.cache_data(ttl=60)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect(read_only=True) as con:
        return con.execute(sql, list(params)).df()


@st.cache_data(ttl=300)
def corr_matrix(days: int) -> pd.DataFrame:
    with connect(read_only=True) as con:
        return correlations.correlation_matrix(con, days)


if not Path(cfg["paths"]["db"]).exists():
    st.error(f"Базы {cfg['paths']['db']} ещё нет. Запусти `make collect` или `make demo`.")
    st.stop()

st.title("Funding Radar")
st.caption("Funding, open interest и цены с Binance, Bybit и OKX. Мониторинг, не торговая рекомендация. Время — UTC.")

with st.sidebar:
    symbol = st.selectbox("Инструмент", cfg["symbols"])
    days = st.slider("Период, дней", 1, 30, 7)
    st.caption(f"База: {Path(cfg['paths']['db']).name}")
since = utcnow() - timedelta(days=days)

# ---------- сводка ----------
latest = query("SELECT * FROM spreads QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = 1")
if latest.empty:
    st.info("Данных пока нет.")
    st.stop()
row = latest[latest.symbol == symbol]
if not row.empty:
    r = row.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Spread {symbol}", f"{r.spread_bp:.2f} bp")
    c2.metric(f"Дороже всего · {r.annualized_hi * 100:.1f}% годовых", name(r.exchange_hi))
    c3.metric(f"Дешевле всего · {r.annualized_lo * 100:.1f}% годовых", name(r.exchange_lo))
    c4.metric("Обновлено", f"{r.ts:%d.%m %H:%M}")

left, right = st.columns(2)

# ---------- 1. funding по биржам во времени ----------
with left:
    st.subheader(f"Funding по биржам · {symbol}")
    f = query("SELECT ts, exchange, funding_rate * 100 AS pct FROM funding WHERE symbol = ? AND ts > ? ORDER BY ts",
              (symbol, since))
    fig = px.line(f, x="ts", y="pct", color="exchange", color_discrete_map=COLORS,
                  labels={"ts": "", "pct": "% за 8 ч", "exchange": ""})
    fig.for_each_trace(lambda t: t.update(name=name(t.name)))
    fig.update_traces(line_width=2)
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=1.1),
                      hovermode="x unified")
    st.plotly_chart(fig)

# ---------- 2. тепловая карта spread «монета × час» ----------
with right:
    st.subheader("Spread: монета × час суток")
    h = query("SELECT symbol, hour(ts) AS hour, avg(spread_bp) AS bp FROM spreads WHERE ts > ? GROUP BY ALL", (since,))
    # все 24 часа: пустые клетки = в этот час данных ещё нет (история funding есть только на 00/08/16)
    grid = h.pivot(index="symbol", columns="hour", values="bp").reindex(index=cfg["symbols"], columns=range(24))
    fig = px.imshow(grid, aspect="auto", color_continuous_scale="Blues",
                    labels=dict(x="час (UTC)", y="", color="bp"))
    fig.update_xaxes(dtick=1)
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig)

left, right = st.columns(2)

# ---------- 3. OI против цены ----------
with left:
    st.subheader(f"Open interest и цена · {symbol}")
    oi = query("SELECT ts, sum(oi_usd) / 1e9 AS oi FROM open_interest WHERE symbol = ? AND ts > ? "
               "GROUP BY ts HAVING count(*) = (SELECT count(DISTINCT exchange) FROM open_interest) ORDER BY ts",
               (symbol, since))
    price = query("SELECT ts, avg(close) AS close FROM klines WHERE symbol = ? AND ts > ? AND minute(ts) = 0 "
                  "GROUP BY ts ORDER BY ts", (symbol, since))
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=oi.ts, y=oi.oi, name="OI, млрд $ (все биржи)", line=dict(color="#2B6CB0", width=2)))
    fig.add_trace(go.Scatter(x=price.ts, y=price.close, name="Цена, $", line=dict(color="#C98A00", width=2)),
                  secondary_y=True)
    fig.update_yaxes(title_text="OI, млрд $", secondary_y=False)
    fig.update_yaxes(title_text="Цена, $", secondary_y=True, showgrid=False)
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=1.1),
                      hovermode="x unified")
    st.plotly_chart(fig)

# ---------- 4. матрица корреляций ----------
with right:
    window = cfg["regime"]["corr_window_days"]
    st.subheader(f"Корреляции 5-мин доходностей · {window} д")
    corr = corr_matrix(window)
    if corr.empty:
        st.info("Мало свечей для корреляций.")
    else:
        fig = px.imshow(corr.round(2), text_auto=True, zmin=-1, zmax=1, color_continuous_scale="RdBu_r", aspect="auto")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig)

# ---------- таблицы ----------
left, right = st.columns(2)
with left:
    st.subheader("Spread сейчас")
    t = latest.sort_values("spread_bp", ascending=False)[["symbol", "spread_bp", "exchange_hi", "exchange_lo", "ts"]]
    st.dataframe(t.rename(columns={"symbol": "монета", "spread_bp": "bp", "exchange_hi": "дороже",
                                   "exchange_lo": "дешевле", "ts": "время"}), hide_index=True,
                 column_config={"bp": st.column_config.NumberColumn(format="%.2f")})
with right:
    st.subheader("Коллекторы")
    runs = query("SELECT collector, max(finished) FILTER (WHERE status = 'ok') AS last_ok, "
                 "count(*) FILTER (WHERE status = 'error' AND finished > ?) AS errors_24h "
                 "FROM collector_runs GROUP BY collector ORDER BY collector", (utcnow() - timedelta(hours=24),))
    st.dataframe(runs.rename(columns={"collector": "коллектор", "last_ok": "последний успех",
                                      "errors_24h": "ошибок за 24 ч"}), hide_index=True)

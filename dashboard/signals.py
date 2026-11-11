"""Страница «Signals / Что торговать сейчас»: лучшая пара бирж по каждой монете для funding carry.

Лонг на бирже, где funding ниже, шорт там, где выше. Нетто = разница годовых ставок
минус комиссии за 4 сделки, растянутые на срок удержания. Формулы — analysis/carry.py.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from analysis import carry
from bot.messages import name
from pipeline.config import enabled_exchanges, load_config
from pipeline.timeutil import utcnow
from storage.db import connect

cfg = load_config()
c = cfg["carry"]

st.title("Signals · что торговать сейчас")
st.caption("Funding carry: лонг дешёвой ноги, шорт дорогой. Нетто — после комиссий за вход и выход "
           "на выбранном сроке удержания. Мониторинг, не торговая рекомендация: дельта-нейтраль страхует цену, "
           "но не ликвидацию одной из ног при резком движении.")

with st.sidebar:
    holding_days = st.slider("Срок удержания, дней", 1, 30, int(c["holding_days"]))
    min_net = st.slider("Порог нетто, % годовых", -20.0, 50.0, float(c["min_net_pct"]), 0.5)
    exchanges = st.multiselect("Биржи", enabled_exchanges(cfg), default=enabled_exchanges(cfg), format_func=name)
    min_oi_m = st.number_input("Мин. OI на каждой ноге, млн $", 0.0, 5000.0, c["min_oi_usd"] / 1e6, 10.0)


@st.cache_data(ttl=30)
def load(holding_days: int, min_net: float, exchanges: tuple, min_oi_usd: float):
    now = utcnow()
    with connect(read_only=True) as con:
        return carry.compute(con, cfg, now, holding_days, min_net, list(exchanges), min_oi_usd), now


def countdown(minutes: float) -> str:
    if minutes is None or minutes != minutes or minutes < 0:
        return "—"
    return f"{int(minutes)} мин" if minutes < 60 else f"{int(minutes // 60)} ч {int(minutes % 60):02d} м"


@st.fragment(run_every="60s")       # таблица и отсчёт до фандинга обновляются раз в минуту сами
def table() -> None:
    if len(exchanges) < 2:
        st.warning("Нужно минимум две биржи.")
        return
    df, now = load(holding_days, min_net, tuple(exchanges), min_oi_m * 1e6)
    if df.empty:
        st.info("Нет пар с нетто-carry выше порога. Уменьши порог или увеличь срок удержания.")
        return

    top = df.iloc[0]
    stale = int((~df.fresh).sum())
    stale_rows = df[~df.fresh]
    c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
    c1.metric("Лучшая монета", top.symbol, help=f"Пар выше порога: {len(df)}")
    c2.metric("Лонг → шорт", f"{name(top.long_ex)} → {name(top.short_ex)}",
              help=f"Лонг на {name(top.long_ex)} ({top.long_annual_pct:+.1f}% год.), "
                   f"шорт на {name(top.short_ex)} ({top.short_annual_pct:+.1f}% год.)")
    c3.metric("Нетто, % годовых", f"{top.net_pct:.1f}%", help=f"Срок удержания {holding_days} дн.")
    stale_list = ", ".join(f"{r.symbol} ({name(r.oldest_leg)}, {int(r.data_age_min)} мин)"
                           for r in stale_rows.itertuples())
    c4.metric("Свежесть", "🟢 ок" if not stale else f"🔴 {stale} из {len(df)}",
              help=f"Свежими считаются данные моложе {c['freshness_min']} мин по обеим ногам пары. "
                   + (f"Устарели: {stale_list}." if stale else "Все ноги свежие."))
    if stale:
        st.error(f"Устаревшие данные: {stale_list}. Значит, коллектор этой биржи не отрабатывает: "
                 "проверь окно с сервисом сбора на строки ERROR или команду /status в боте.")

    # главное — слева: монета, нетто, тайминг, устойчивость; детали расчёта — правее
    view = pd.DataFrame({
        "Монета": df.symbol,
        "Нетто % год.": df.net_pct,
        "Лонг": [f"{name(e)} {v:+.1f}%" for e, v in zip(df.long_ex, df.long_annual_pct)],
        "Шорт": [f"{name(e)} {v:+.1f}%" for e, v in zip(df.short_ex, df.short_annual_pct)],
        "Фандинг через": [("⏰ " if soon else "") + countdown(m) for m, soon in zip(df.minutes_to_funding, df.funding_soon)],
        "Устойч., ч": df.stability_h,
        "ΔOI 24ч, %": pd.to_numeric(df.oi_24h_short_pct, errors="coerce"),   # None -> NaN, иначе в таблице «None»
        "Gross %": df.gross_pct,
        "Издержки, bp": df.cost_bps,
        "Спред, bp/8ч": df.spread_bp_8h,
        "Свежесть": ["🟢" if f else f"🔴 {int(a)} мин ({name(e)})" for f, a, e in zip(df.fresh, df.data_age_min, df.oldest_leg)],
        "Интервалы, ч": df.intervals_h,
    })
    styled = (view.style
              .format({"Спред, bp/8ч": "{:.2f}", "Gross %": "{:.1f}", "Издержки, bp": "{:.0f}",
                       "Нетто % год.": "{:.1f}", "Устойч., ч": "{:.1f}", "ΔOI 24ч, %": "{:+.1f}"}, na_rep="—")
              .background_gradient(subset=["Нетто % год."], cmap="Greens")
              .set_properties(subset=["Нетто % год."], **{"font-weight": "bold"}))
    col = st.column_config
    st.dataframe(styled, hide_index=True, width="stretch", column_config={
        "Монета": col.TextColumn(pinned=True, width="small"),
        "Нетто % год.": col.Column(help="Gross минус издержки, растянутые на срок удержания. Главный столбец."),
        "Лонг": col.Column(help="Биржа длинной ноги и её funding в % годовых"),
        "Шорт": col.Column(help="Биржа короткой ноги и её funding в % годовых"),
        "Фандинг через": col.Column(help=f"До ближайшего снимка funding по любой из ног. ⏰ — меньше "
                                         f"{c['time_to_funding_buffer_min']} мин, войти до снимка можно не успеть."),
        "Устойч., ч": col.Column(help="Сколько часов подряд эта пара держит нетто выше порога. "
                                     "Отличает устойчивый спред от разового выброса."),
        "ΔOI 24ч, %": col.Column(help="Изменение open interest за сутки на бирже короткой (дорогой) ноги"),
        "Интервалы, ч": col.Column(help="Частота фандинга: лонг / шорт"),
    })
    st.caption(f"Расчёт на {now:%d.%m %H:%M:%S} UTC · пар выше порога: {len(df)} · наведи на заголовок столбца, "
               "чтобы увидеть пояснение.")
    st.download_button("Скачать CSV", df.to_csv(index=False).encode("utf-8"),
                       f"signals_{now:%Y%m%d_%H%M}.csv", "text/csv")


table()


@st.fragment(run_every="60s")
def recent_alerts() -> None:
    import os
    st.subheader("Алерты")
    tg = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    st.caption(("🟢 Telegram подключён" if tg else "⚪ Telegram не подключён: алерты пишутся только в лог")
               + f" · carry-алерт: нетто > {c['alert_threshold_pct']:g}% год. и держится ≥ "
               f"{c['alert_min_stability_h']:g} ч, повтор не чаще раза в {c['alert_cooldown_h']:g} ч")
    with connect(read_only=True) as con:
        log = con.execute(
            """SELECT al.sent_ts AS "Время, UTC", an.type AS "Тип", an.symbol AS "Монета",
                      CASE al.kind WHEN 'open' THEN 'открылся' WHEN 'close' THEN 'закрылся' ELSE 'подавлен (cooldown)' END
                          AS "Событие",
                      CASE WHEN al.chat_id IS NOT NULL THEN 'Telegram' ELSE 'лог' END AS "Куда",
                      split_part(al.message, chr(10), 1) AS "Сообщение"
               FROM alerts al JOIN anomalies an ON an.id = al.anomaly_id
               ORDER BY al.sent_ts DESC LIMIT 15""").df()
    if log.empty:
        st.info("Алертов пока не было. Они появятся, когда сервис сбора поработает и условие продержится нужное время.")
    else:
        st.dataframe(log, hide_index=True, width="stretch")


recent_alerts()

with st.expander("Как считается"):
    fees = ", ".join(f"{name(e)} {f['taker']:g} bp" for e, f in c["fees_bps"].items())
    st.markdown(f"""
- **Годовая ставка ноги** = ставка за интервал × (24 / интервал, ч) × 365. Интервалы бирж разные (1/4/8 ч), поэтому сырые ставки напрямую не сравниваются.
- **Gross** = годовая ставка шорта − годовая ставка лонга. Funding > 0 значит, что лонги платят шортам.
- **Издержки** = 2 × taker лонг-биржи + 2 × taker шорт-биржи + {c['slippage_buffer_bps']:g} bp на проскальзывание. Taker: {fees}.
- **Нетто** = gross − издержки (в %) × 365 / срок удержания. Издержки разовые, а carry идёт потоком: чем дольше держишь, тем меньше их вес.
- Перебираются все пары бирж, а не только «самая дешёвая против самой дорогой»: при разных комиссиях лучшей может оказаться другая пара.
- Не учтено: ликвидация одной ноги, перевод маржи между биржами, изменение ставки до следующего снимка.
""")

"""Алерты: один эпизод — одно сообщение об открытии и одно о закрытии.

Что уже отправлено, помнит таблица alerts (kind = open / close). Поэтому перезапуск сервиса
не приводит к повторным сообщениям.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ai import explain as ai_explain
from analysis.anomalies import threshold
from bot import messages, notify
from pipeline.config import load_config
from pipeline.timeutil import utcnow
from storage.db import connect

log = logging.getLogger(__name__)
COLS = ["id", "ts", "symbol", "exchange", "type", "value", "zscore", "resolved_ts"]


def _pending(con, cfg: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    select = f"SELECT {', '.join('a.' + c for c in COLS)} FROM anomalies a "
    # открытые эпизоды, по которым ещё не было сообщения
    opened = [dict(zip(COLS, r)) for r in con.execute(
        select + "WHERE a.resolved_ts IS NULL "
                 "AND NOT EXISTS (SELECT 1 FROM alerts WHERE anomaly_id = a.id AND kind IN ('open', 'suppressed'))").fetchall()]
    # spread должен продержаться N минут — иначе это шум, а не расхождение
    hold = timedelta(minutes=threshold(con, cfg, "spread_min_duration_min"))
    opened = [a for a in opened if a["type"] != "spread" or now - a["ts"] >= hold]
    # carry: тот же сигнал не чаще раза в cooldown, даже если эпизод закрылся и открылся снова
    cooldown = timedelta(hours=cfg["carry"]["alert_cooldown_h"])
    suppressed = [a for a in opened if a["type"] == "carry" and con.execute(
        "SELECT 1 FROM alerts al JOIN anomalies an ON an.id = al.anomaly_id "
        "WHERE an.type = 'carry' AND an.symbol = ? AND al.kind = 'open' AND al.sent_ts > ?",
        [a["symbol"], now - cooldown]).fetchone()]
    for a in suppressed:     # помечаем, чтобы не пересматривать; «закрылся» по нему тоже не придёт
        con.execute("INSERT INTO alerts (anomaly_id, kind, sent_ts) VALUES (?, 'suppressed', ?)", [a["id"], now])
    opened = [a for a in opened if a not in suppressed]
    # закрывшиеся эпизоды: сообщаем, только если об открытии писали
    closed = [dict(zip(COLS, r)) for r in con.execute(
        select + "WHERE a.resolved_ts IS NOT NULL "
                 "AND EXISTS (SELECT 1 FROM alerts WHERE anomaly_id = a.id AND kind = 'open') "
                 "AND NOT EXISTS (SELECT 1 FROM alerts WHERE anomaly_id = a.id AND kind = 'close')").fetchall()]
    return opened, closed


def process(now: datetime | None = None, send=notify.send) -> int:
    """Проверить эпизоды и отправить, что нужно. Возвращает число сообщений."""
    cfg, now = load_config(), now or utcnow()
    with connect() as con:
        opened, closed = _pending(con, cfg, now)
        outbox = [(a, "open", messages.ALERT_BUILDERS[a["type"]](con, cfg, a, now)) for a in opened]
        outbox += [(a, "close", messages.closed_alert(a)) for a in closed]

    sent = 0
    for anomaly, kind, text in outbox:
        ai_text = None
        if kind == "open" and cfg["ai"].get("explain_in_alerts"):
            _, ex = ai_explain.explain(anomaly["symbol"], now)     # None, если AI недоступен — алерт уйдёт без него
            if ex:
                ai_text = ex.summary
                head, _, tail = text.rpartition("\n")              # вставляем перед строкой «→ /explain»
                text = f"{head}\n\n{ai_text}\n{tail}"
        chat = send(text)
        with connect() as con:      # записываем в любом случае: иначе при выключенном Telegram лог забьётся повторами
            con.execute(
                "INSERT INTO alerts (anomaly_id, kind, sent_ts, chat_id, message, ai_explanation) "
                "VALUES (?, ?, ?, ?, ?, ?)", [anomaly["id"], kind, now, chat, text, ai_text])
        sent += 1
    if sent:
        log.info("алерты: отправлено %d", sent)
    return sent

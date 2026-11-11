"""Один вход в модель: кэш, проверка ответа по схеме, повтор, учёт стоимости, дневной бюджет.

Если ключа нет, бюджет исчерпан или модель дважды вернула мусор — возвращаем None.
Вызывающий код обязан уметь жить без AI-части (алерт уйдёт просто без объяснения).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Type

from pydantic import BaseModel, ValidationError

from pipeline.config import load_config
from pipeline.timeutil import utcnow
from storage.db import connect

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).with_name("prompts")


def available() -> bool:
    return bool(load_config()["ai"].get("enabled")) and bool(os.getenv("ANTHROPIC_API_KEY"))


def spent_today() -> float:
    with connect() as con:
        return con.execute(
            "SELECT coalesce(sum(cost_usd), 0) FROM ai_calls WHERE ts >= date_trunc('day', ?::TIMESTAMP)",
            [utcnow()]).fetchone()[0]


def _extract_json(text: str) -> dict:
    """Модель иногда оборачивает JSON в ```json ... ``` — берём всё от первой { до последней }."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    return json.loads(text[start:end + 1])


def _call_model(system: str, user: str) -> tuple[str, int, int]:
    import anthropic                    # импорт здесь: без AI-слоя библиотека не нужна
    cfg = load_config()["ai"]
    client = anthropic.Anthropic()      # ключ берётся из ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=cfg["model"], max_tokens=cfg["max_tokens"], system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def ask_json(kind: str, facts: dict, schema: Type[BaseModel], call=_call_model) -> BaseModel | None:
    """facts (цифры из базы) + промпт ai/prompts/<kind>.md -> проверенный объект schema или None."""
    cfg = load_config()["ai"]
    system = (PROMPTS / f"{kind}.md").read_text(encoding="utf-8")
    user = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    key = hashlib.sha256(f"{cfg['model']}\n{system}\n{user}".encode()).hexdigest()

    # 1. кэш: тот же вход -> тот же ответ, второй раз не платим
    with connect() as con:
        cached = con.execute("SELECT response FROM ai_calls WHERE hash = ?", [key]).fetchone()
    if cached:
        try:
            return schema.model_validate(json.loads(cached[0]))
        except (ValidationError, ValueError):
            pass
    if not available() and call is _call_model:
        return None
    if spent_today() >= cfg["daily_budget_usd"]:
        log.warning("AI: дневной бюджет $%.2f исчерпан, пропускаю %s", cfg["daily_budget_usd"], kind)
        return None

    # 2. вызов, проверка по схеме, одна повторная попытка
    result, tokens_in, tokens_out, prompt = None, 0, 0, user
    for attempt in (1, 2):
        try:
            text, t_in, t_out = call(system, prompt)
        except Exception as e:          # сеть, лимиты, неверный ключ — AI не должен ронять сервис
            log.error("AI %s: ошибка вызова: %s", kind, e)
            break
        tokens_in, tokens_out = tokens_in + t_in, tokens_out + t_out
        try:
            result = schema.model_validate(_extract_json(text))
            break
        except (ValidationError, ValueError) as e:
            log.warning("AI %s: ответ не прошёл проверку (попытка %d): %s", kind, attempt, str(e)[:200])
            prompt = user + "\n\nПредыдущий ответ не прошёл проверку. Верни строго JSON по схеме из инструкции."

    # 3. лог стоимости (даже если ответ негодный — деньги уже потрачены)
    cost = tokens_in / 1e6 * cfg["price_in_per_mtok"] + tokens_out / 1e6 * cfg["price_out_per_mtok"]
    if tokens_in or tokens_out:
        with connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO ai_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [key if result else key + ":failed", utcnow(), kind, cfg["model"],
                 result.model_dump_json() if result else None, tokens_in, tokens_out, cost])
        log.info("AI %s: %d+%d токенов, $%.4f", kind, tokens_in, tokens_out, cost)
    return result

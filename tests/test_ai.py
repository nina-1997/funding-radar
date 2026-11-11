"""AI-слой без обращения к модели: проверка ответа по схеме, повтор, кэш, жизнь без AI."""
from ai import llm
from ai.explain import Explanation, render

FACTS = {"symbol": "BTC", "spread_bp": 2.14}
GOOD = '```json\n{"summary": "Funding на Bybit заметно выше, чем на двух других биржах.", ' \
       '"hypotheses": ["Spread 2.14 bp: локальный спрос на лонги на Bybit."], "confidence": "low"}\n```'


def fake_model(answers):
    calls = []

    def call(system, user):
        calls.append(user)
        return answers[len(calls) - 1], 900, 120
    return call, calls


def test_valid_answer_is_parsed_and_cached(db):
    call, calls = fake_model([GOOD])
    first = llm.ask_json("explain", FACTS, Explanation, call=call)
    second = llm.ask_json("explain", FACTS, Explanation, call=call)     # тот же вход -> из кэша
    assert first.confidence == "low" and second == first
    assert len(calls) == 1
    with db() as con:
        cost = con.execute("SELECT sum(cost_usd) FROM ai_calls").fetchone()[0]
    assert cost > 0                                                     # стоимость записана


def test_invalid_answer_is_retried_once(db):
    call, calls = fake_model(['{"summary": "коротко"}', GOOD])          # первый ответ не по схеме
    result = llm.ask_json("explain", FACTS, Explanation, call=call)
    assert result is not None and len(calls) == 2
    assert "не прошёл проверку" in calls[1]


def test_garbage_twice_gives_none(db):
    call, calls = fake_model(["не могу ответить", "всё ещё не JSON"])
    assert llm.ask_json("explain", FACTS, Explanation, call=call) is None
    assert len(calls) == 2


def test_no_api_key_gives_none(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.ask_json("explain", FACTS, Explanation) is None


def test_budget_stops_calls(db):
    call, calls = fake_model([GOOD])
    with db() as con:
        con.execute("INSERT INTO ai_calls VALUES ('x', now()::TIMESTAMP, 'explain', 'm', NULL, 1, 1, 99.0)")
    assert llm.ask_json("explain", FACTS, Explanation, call=call) is None
    assert calls == []


def test_render_without_ai_still_shows_numbers():
    facts = {"slot_utc": "2026-11-11 15:00:00", "funding_8h_pct": {"binance": 0.01, "bybit": 0.0312},
             "funding_annualized_pct": {"binance": 11.0, "bybit": 34.2}, "spread_bp": 2.12,
             "spread_most_expensive": "bybit", "spread_cheapest": "binance", "price": 102410.0,
             "price_change_24h_pct": 1.8}
    text = render("BTC", facts, None)
    assert "2.12 bp" in text and "AI-объяснение недоступно" in text

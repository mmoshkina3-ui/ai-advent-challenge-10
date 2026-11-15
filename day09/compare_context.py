"""Один и тот же диалог из 30 сообщений: без сжатия и со сжатием.

Список деталей задан заранее. После диалога спрашиваем каждую.
Числа токенов берутся из usage, не из длины текста.
"""

import json
import sys
from pathlib import Path

from agent import Agent, load_settings, money

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

FACTS = [
    {
        "id": "имя",
        "say": "Заказчика зовут Максим. Ответь одним словом: принято.",
        "ask": "Как зовут заказчика? Ответь только именем.",
        "needles": ["максим"],
    },
    {
        "id": "срок",
        "say": "Срок сдачи проекта: 15 декабря 2026. Ответь одним словом: принято.",
        "ask": "Какой срок сдачи? Ответь датой.",
        "needles": ["15 декабря", "15.12.2026"],
    },
    {
        "id": "бюджет",
        "say": "Бюджет проекта 120000 рублей. Ответь одним словом: принято.",
        "ask": "Какой бюджет в рублях? Ответь числом.",
        "needles": ["120000", "120 000", "120 тыс"],
    },
    {
        "id": "ограничение",
        "say": "Ограничение: нельзя использовать облако. Ответь одним словом: принято.",
        "ask": "Какое ограничение по облаку? Ответь коротко.",
        "needles": ["нельзя использовать облако", "не использовать облако", "без облака", "облако запрещ"],
    },
    {
        "id": "решение",
        "say": "Решение: пишем на Python. Ответь одним словом: принято.",
        "ask": "На каком языке пишем? Ответь одним словом.",
        "needles": ["python", "питон"],
    },
    {
        "id": "город",
        "say": "Запуск будет в городе Казань. Ответь одним словом: принято.",
        "ask": "В каком городе запуск? Ответь только городом.",
        "needles": ["казан"],
    },
    {
        "id": "команда",
        "say": "В команде 4 человека. Ответь одним словом: принято.",
        "ask": "Сколько человек в команде? Ответь числом.",
        "needles": ["4 человек", "четыре человек", "четырех человек", "четырёх человек", "команда из 4", "команда: 4", "четыре"],
    },
    {
        "id": "встречи",
        "say": "Встречи по вторникам в 10:00. Ответь одним словом: принято.",
        "ask": "Когда встречи? Назови день и время.",
        "needles": ["вторник"],
    },
    {
        "id": "база",
        "say": "Нельзя менять базу данных. Ответь одним словом: принято.",
        "ask": "Можно ли менять базу данных? Ответь коротко.",
        "needles": ["нельзя менять", "не менять базу", "менять нельзя", "запрет на изменение", "нельзя"],
    },
    {
        "id": "отчёт",
        "say": "Отчёт сдаём в формате PDF. Ответь одним словом: принято.",
        "ask": "В каком формате отчёт? Ответь одним словом.",
        "needles": ["pdf"],
    },
    {
        "id": "порядок",
        "say": "Порядок работ: сначала поиск, потом оплата. Ответь одним словом: принято.",
        "ask": "Что делаем сначала, поиск или оплату?",
        "needles": ["сначала поиск", "поиск, потом", "поиск потом", "поиск"],
    },
    {
        "id": "почта",
        "say": "Почта для связи max@example.com. Ответь одним словом: принято.",
        "ask": "Какая почта для связи? Ответь адресом.",
        "needles": ["max@example.com"],
    },
    {
        "id": "версия",
        "say": "Делаем версию 3 продукта. Ответь одним словом: принято.",
        "ask": "Какую версию продукта делаем? Ответь коротко.",
        "needles": ["версия 3", "версии 3", "версию 3", "version 3"],
    },
    {
        "id": "пароль",
        "say": "Пароль не короче 12 символов. Ответь одним словом: принято.",
        "ask": "Какая минимальная длина пароля? Ответь числом.",
        "needles": ["12 символ", "12 знак"],
    },
    {
        "id": "язык",
        "say": "Интерфейс только на русском языке. Ответь одним словом: принято.",
        "ask": "На каком языке интерфейс? Ответь одним словом.",
        "needles": ["русск"],
    },
]


def remembered(answer: str, needles: list[str]) -> bool:
    folded = answer.lower().replace(" ", "").replace("\u00a0", "")
    plain = answer.strip().lower()
    if plain in {"4", "четыре", "12"} and any(needle.strip().lower() in {"четыре", "12 символ", "12 знак"} for needle in needles):
        return True
    for needle in needles:
        if needle.lower().replace(" ", "") in folded:
            return True
    return False


def totals(calls: list[dict]) -> dict:
    def take(kind: str | None) -> list[dict]:
        if kind is None:
            return calls
        return [call for call in calls if call["kind"] == kind]

    def pack(rows: list[dict]) -> dict:
        return {
            "calls": len(rows),
            "request_tokens": sum(row["request_tokens"] for row in rows),
            "answer_tokens": sum(row["answer_tokens"] for row in rows),
            "cost_usd": sum(row["cost_usd"] for row in rows),
        }

    return {
        "chat": pack(take("chat")),
        "summary": pack(take("summary")),
        "all": pack(take(None)),
        "last_request_tokens": calls[-1]["request_tokens"] if calls else 0,
    }


def run_mode(api_key: str, model: str, compress: bool) -> dict:
    path = ROOT / ("compact-history.json" if compress else "plain-history.json")
    if path.exists():
        path.unlink()
    agent = Agent(api_key, model, path, compress=compress)
    print(f"\n=== {'Со сжатием' if compress else 'Без сжатия'} ===")
    for index, fact in enumerate(FACTS, start=1):
        answer = agent.ask(fact["say"], max_tokens=40)
        print(f"  факт {index}/{len(FACTS)}: {fact['id']} -> {answer[:40]}")
    plant_messages = len(agent.messages)
    covered_before_quiz = agent.covered
    quiz = []
    for fact in FACTS:
        covered_now = agent.covered
        answer = agent.ask(fact["ask"], max_tokens=80)
        ok = remembered(answer, fact["needles"])
        user_index = FACTS.index(fact) * 2
        quiz.append(
            {
                "id": fact["id"],
                "answer": answer,
                "ok": ok,
                "only_in_summary": user_index < covered_now,
            }
        )
        print(f"  вопрос {fact['id']}: {'помнит' if ok else 'потерял'} | {answer[:80]}")
    return {
        "compress": compress,
        "plant_messages": plant_messages,
        "covered_before_quiz": covered_before_quiz,
        "covered_after": agent.covered,
        "summary": agent.summary,
        "quiz": quiz,
        "remembered": sum(1 for row in quiz if row["ok"]),
        "totals": totals(agent.calls),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    plain = run_mode(api_key, model, compress=False)
    compact = run_mode(api_key, model, compress=True)
    payload = {
        "model": model,
        "facts": [{"id": fact["id"], "say": fact["say"], "ask": fact["ask"]} for fact in FACTS],
        "keep_last": 6,
        "batch": 10,
        "plain": plain,
        "compact": compact,
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nБез сжатия:", plain["remembered"], "/", len(FACTS), money(plain["totals"]["all"]["cost_usd"]))
    print("Со сжатием:", compact["remembered"], "/", len(FACTS), money(compact["totals"]["all"]["cost_usd"]))
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

"""Диалог со сменой 4 фактов, вопросы про актуальное, очистка рабочей памяти.

python check_memory.py  настоящая модель, пишет runs.json
"""

import json
import sys
from pathlib import Path

from agent import MemoryAgent, deepseek_llm, load_settings, stale_values

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"
MEMORY_PATH = ROOT / "check-memory.json"
DIALOG_PATH = ROOT / "check-dialog.json"

START = (
    "Начинаем задачу: подготовить оплату заказов в магазине. "
    "Срок — 1 декабря 2026. "
    "Требование: двухфакторная аутентификация при оплате. "
    "Подход: синхронный REST без очередей. "
    "Версия релиза 2.1.0. Статус: в работе."
)

SCRIPT = [
    ("старт", START, None, None),
    (
        "срок сдвинулся",
        "Срок сдвинулся: дедлайн теперь 15 декабря 2026.",
        "Какой сейчас срок сдачи?",
        "срок",
    ),
    (
        "требование отменили",
        "Требование про двухфакторную аутентификацию при оплате отменили, его больше нет.",
        "Какое сейчас требование? Оно действует?",
        "требование",
    ),
    (
        "другой подход",
        "Выбрали другой подход: асинхронная обработка через очередь сообщений, не синхронный REST.",
        "Какой сейчас подход?",
        "подход",
    ),
    (
        "новая версия",
        "Версия релиза теперь 2.2.0. Решили использовать вебхуки провайдера для подтверждения платежа.",
        "Какая сейчас версия релиза? Какие решения уже приняты?",
        "версия",
    ),
]


def current_map(agent: MemoryAgent) -> dict:
    store = agent.store
    return {
        "scalars": {key: fact["value"] for key, fact in store.scalars.items()},
        "working": {key: fact["value"] for key, fact in store.working.items()},
        "решения": [item["value"] for item in store.lists.get("решения") or []],
        "changes": store.changes,
        "stale": stale_values(store),
    }


def row_for(agent: MemoryAgent, label: str, text: str, expect_key: str | None) -> dict:
    reply = agent.handle(text)
    last = agent.last
    applied = last.get("applied") or []
    row = {
        "label": label,
        "input": text,
        "reply": reply,
        "command": bool(last.get("command")),
        "extracted": last.get("extracted") or [],
        "applied": applied,
        "stale_in_prompt": last.get("stale_in_prompt") or [],
        "draft_stale": last.get("draft_stale") or [],
        "regenerated": bool(last.get("regenerated")),
        "retry_stale": last.get("retry_stale") or [],
        "reply_stale": last.get("reply_stale") or [],
        "expect_key": expect_key,
        "current": current_map(agent),
        "prompt": last.get("prompt") or "",
    }
    mark = "устарело в ответе: " + ", ".join(row["reply_stale"]) if row["reply_stale"] else "устаревших в ответе нет"
    prompt = "промпт чистый" if not row["stale_in_prompt"] else "в промпте: " + ", ".join(row["stale_in_prompt"])
    print(f"{label}: {mark}; {prompt}")
    return row


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for path in (MEMORY_PATH, DIALOG_PATH):
        if path.exists():
            path.unlink()
    api_key, model = load_settings()
    agent = MemoryAgent(deepseek_llm(api_key, model), MEMORY_PATH, DIALOG_PATH)

    turns: list[dict] = []
    questions: list[dict] = []
    for label, change, question, key in SCRIPT:
        turns.append(row_for(agent, label, change, None if question else key))
        if question:
            row = row_for(agent, f"вопрос после: {label}", question, key)
            turns.append(row)
            questions.append(row)

    before_done = current_map(agent)
    done = row_for(agent, "завершение задачи", "/done", None)
    turns.append(done)
    after_done = current_map(agent)
    after_question = row_for(agent, "после /done", "Какая сейчас задача? Какой срок и какой статус?", None)
    turns.append(after_question)

    payload = {
        "model": model,
        "turns": turns,
        "questions": questions,
        "facts_changed": sum(1 for row in turns if any(item.get("action") == "replaced" for item in row.get("applied") or [] if item)),
        "question_count": len(questions),
        "stale_answers": sum(1 for row in questions if row["reply_stale"]),
        "stale_answers_all": sum(1 for row in turns if row["reply_stale"]),
        "stale_drafts": sum(1 for row in turns if row["draft_stale"]),
        "stale_in_prompt_turns": sum(1 for row in turns if row["stale_in_prompt"]),
        "cleanup": {
            "working_before": before_done["working"],
            "working_after": after_done["working"],
            "long_after": after_done["scalars"],
            "decisions_after": after_done["решения"],
            "changes_kept": after_done["changes"],
            "working_cleared": after_done["working"] == {},
            "long_kept": bool(after_done["scalars"]),
            "after_question_stale": after_question["reply_stale"],
            "after_prompt_stale": after_question["stale_in_prompt"],
            "after_reply": after_question["reply"],
        },
        "final_memory": after_done,
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Смен фактов (replaced):", payload["facts_changed"])
    print("Вопросов про актуальное:", payload["question_count"])
    print("Агент назвал устаревшее в ответе на вопрос:", payload["stale_answers"])
    print("Черновики с устаревшим:", payload["stale_drafts"])
    print("Ходов, где устаревшее попало в промпт:", payload["stale_in_prompt_turns"])
    print("Рабочая память очищена:", payload["cleanup"]["working_cleared"], "было:", before_done["working"])
    print("Долговременная сохранилась:", after_done["scalars"], "решения:", after_done["решения"])
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

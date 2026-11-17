"""Проверка слоёв: куда пишется факт, сколько токенов он добавляет, что пропадает после очистки."""

import json
import sys
from pathlib import Path

from agent import MEMORY_PATH, MemoryAgent, load_settings

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

SEEDS = [
    "Запомни навсегда: заказчика зовут Максим, бюджет 120000 рублей.",
    "Текущая задача: подготовить отчёт к завтра.",
    "Служебная реплика 1. Ответь: ок.",
    "Служебная реплика 2. Ответь: ок.",
    "Служебная реплика 3. Ответь: ок.",
    "Служебная реплика 4. Ответь: ок.",
    "Служебная реплика 5. Ответь: ок.",
    "В этом диалоге короткий код 4815. Ответь одним словом: принято.",
]
QUESTIONS = {
    "short": "Какой короткий код я назвал в этом диалоге?",
    "working": "Какая текущая задача?",
    "long": "Как зовут заказчика и какой у проекта бюджет?",
}


def has_code(answer: str) -> bool:
    return "4815" in answer


def has_task(answer: str) -> bool:
    folded = answer.lower()
    return "отч" in folded


def has_long(answer: str) -> bool:
    folded = answer.lower().replace(" ", "")
    return "максим" in folded and "120000" in folded


CHECKS = {"short": has_code, "working": has_task, "long": has_long}


def ask_all(agent: MemoryAgent) -> dict:
    result = {}
    for layer, question in QUESTIONS.items():
        answer = agent.ask(question, max_tokens=80)
        result[layer] = {"question": question, "answer": answer, "ok": CHECKS[layer](answer)}
        print(f"  {layer}: {'да' if result[layer]['ok'] else 'нет'} | {answer[:120]}")
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if MEMORY_PATH.exists():
        MEMORY_PATH.unlink()
    api_key, model = load_settings()
    agent = MemoryAgent(api_key, model)
    placed = []
    print("=== Запись в слои ===")
    for text in SEEDS:
        saved = agent.route(text)
        placed.append({"text": text, "layers": saved})
        print(f"  {saved}: {text}")
    seeded = agent.snapshot()
    print("=== Токены слоёв ===")
    sizes = agent.measure()
    print(sizes)
    print("=== Все слои на месте ===")
    before = ask_all(agent)
    cleared = {}
    for layer in ("short", "working", "long"):
        print(f"=== Очищен слой {layer} ===")
        agent.clear(layer)
        cleared[layer] = ask_all(agent)
    print("=== Изолированная очистка до вопросов ===")
    isolated = {}
    for layer in ("short", "working", "long"):
        if MEMORY_PATH.exists():
            MEMORY_PATH.unlink()
        fresh = MemoryAgent(api_key, model)
        for text in SEEDS:
            fresh.route(text)
        fresh.clear(layer)
        answer = fresh.ask(QUESTIONS[layer], max_tokens=80)
        isolated[layer] = {"answer": answer, "ok": CHECKS[layer](answer)}
        print(f"  без {layer}: {'ещё отвечает' if isolated[layer]['ok'] else 'перестал'} | {answer[:120]}")
    payload = {
        "isolated_clear": isolated,
        "model": model,
        "rules": {
            "short": "каждая реплика, в промпт только последние 6",
            "working": "задача, сейчас нужно, временно, к завтра",
            "long": "запомни или явное имя, бюджет, срок, решение",
        },
        "placed": placed,
        "seeded": seeded,
        "tokens": sizes,
        "before_clear": before,
        "after_clear": cleared,
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

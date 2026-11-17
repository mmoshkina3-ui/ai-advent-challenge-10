"""Один вопрос трём профилям и 10 вопросов подряд в профиле «кратко»."""

import json
import re
import sys
from pathlib import Path

from agent import DEFAULT_PROFILES, PROFILES_PATH, ProfileAgent, DAY11

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"
MEMORY_PATH = ROOT / "memory.json"
QUESTION = "Объясни новичку, как в Python оставить в списке только чётные числа."
SERIES = [
    "Что такое переменная?",
    "Что такое цикл?",
    "Что такое функция?",
    "Что такое список?",
    "Что такое словарь?",
    "Что такое условие if?",
    "Что такое строка?",
    "Что такое ошибка в программе?",
    "Что такое комментарий в коде?",
    "Что такое модуль?",
]


def words(text: str) -> int:
    return len(text.split())


def has_code(text: str) -> bool:
    lowered = text.lower()
    return "```" in text or "print(" in lowered or " for " in f" {lowered}" or "def " in lowered


def tone_of(text: str) -> str:
    lowered = f" {text.lower()} "
    formal = bool(re.search(r"\bвы\b|\bвам\b|\bваш", lowered))
    informal = bool(re.search(r"\bты\b|\bтебе\b|\bтвой|\bдавай\b", lowered))
    if formal and not informal:
        return "формально"
    if informal and not formal:
        return "неформально"
    return "неясно"


def follows(answer: str, profile: dict) -> dict:
    count = words(answer)
    code = has_code(answer)
    tone = tone_of(answer)
    style_ok = count <= 70 if profile["style"] == "кратко" else count >= 90
    format_ok = code if profile["format"] == "с кодом" else not code
    tone_ok = tone == profile["tone"]
    return {
        "words": count,
        "code": code,
        "tone": tone,
        "style_ok": style_ok,
        "format_ok": format_ok,
        "tone_ok": tone_ok,
        "all_ok": style_ok and format_ok and tone_ok,
    }


def reset_memory() -> None:
    if MEMORY_PATH.exists():
        MEMORY_PATH.unlink()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if PROFILES_PATH.exists():
        PROFILES_PATH.unlink()
    reset_memory()
    api_key, model = DAY11.load_settings()
    agent = ProfileAgent(api_key, model)
    learned = agent.ask("Дальше отвечай подробно, формально и с примером кода.", max_tokens=40)
    learned_profile = dict(agent.profiles["кратко"])
    print("Профиль после фразы:", learned_profile, learned)
    agent.profiles = {key: dict(value) for key, value in DEFAULT_PROFILES.items()}
    agent._write()

    same = []
    for name, profile in DEFAULT_PROFILES.items():
        reset_memory()
        agent.memory = DAY11.MemoryAgent(api_key, model, MEMORY_PATH)
        agent.set_profile(name)
        answer = agent.ask(QUESTION)
        grade = follows(answer, profile)
        same.append({"profile": name, "settings": profile, "answer": answer, **grade})
        print(f"{name}: слов {grade['words']}, код {grade['code']}, тон {grade['tone']}, всё {grade['all_ok']}")

    reset_memory()
    agent.memory = DAY11.MemoryAgent(api_key, model, MEMORY_PATH)
    agent.set_profile("кратко")
    series = []
    first_break = None
    for index, question in enumerate(SERIES, start=1):
        answer = agent.ask(question, max_tokens=180)
        grade = follows(answer, DEFAULT_PROFILES["кратко"])
        series.append({"n": index, "question": question, "answer": answer, **grade})
        print(f"  {index}: слов {grade['words']}, всё {grade['all_ok']}")
        if first_break is None and not grade["all_ok"]:
            first_break = index
    payload = {
        "model": model,
        "learned_profile": learned_profile,
        "same_question": same,
        "same_question_followed": sum(1 for row in same if row["all_ok"]),
        "series_profile": "кратко",
        "series": series,
        "first_break": first_break,
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Соблюдено профилей:", payload["same_question_followed"], "/ 3")
    print("Первый сбой серии:", first_break)
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

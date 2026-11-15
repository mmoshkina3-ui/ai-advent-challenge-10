"""Три стратегии на сценарии дня 9 и две ветки от одного checkpoint.

Список деталей берётся из day09/compare_context.py, не придумывается заново.
"""

import importlib.util
import json
import sys
from pathlib import Path

from agent import Agent, load_settings, money

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"
STRATEGIES = ("sliding", "facts", "summary")


def day09_checks():
    spec = importlib.util.spec_from_file_location(
        "day09_compare",
        ROOT.parent / "day09" / "compare_context.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FACTS, module.remembered


def totals(calls: list[dict]) -> dict:
    def pack(kind: str | None) -> dict:
        rows = calls if kind is None else [call for call in calls if call["kind"] == kind]
        return {
            "calls": len(rows),
            "request_tokens": sum(row["request_tokens"] for row in rows),
            "answer_tokens": sum(row["answer_tokens"] for row in rows),
            "cost_usd": sum(row["cost_usd"] for row in rows),
        }

    return {
        "chat": pack("chat"),
        "facts": pack("facts"),
        "summary": pack("summary"),
        "all": pack(None),
        "last_request_tokens": calls[-1]["request_tokens"] if calls else 0,
    }


def run_strategy(api_key: str, model: str, strategy: str, facts: list[dict], remembered) -> dict:
    path = ROOT / f"{strategy}-history.json"
    if path.exists():
        path.unlink()
    agent = Agent(api_key, model, path, strategy=strategy)
    print(f"\n=== {strategy} ===")
    for index, fact in enumerate(facts, start=1):
        answer = agent.ask(fact["say"], max_tokens=40)
        print(f"  факт {index}/{len(facts)}: {fact['id']} -> {answer[:40]}")
    quiz = []
    for fact in facts:
        answer = agent.ask(fact["ask"], max_tokens=80)
        ok = remembered(answer, fact["needles"])
        quiz.append({"id": fact["id"], "answer": answer, "ok": ok})
        print(f"  вопрос {fact['id']}: {'помнит' if ok else 'потерял'} | {answer[:80]}")
    return {
        "strategy": strategy,
        "remembered": sum(1 for row in quiz if row["ok"]),
        "quiz": quiz,
        "summary": agent.summary,
        "facts": agent.facts,
        "covered": agent.covered,
        "totals": totals(agent.calls),
    }


def contains_number(answer: str, number: str) -> bool:
    return number in answer.replace(" ", "").replace("\u00a0", "")


def run_branches(api_key: str, model: str) -> dict:
    path = ROOT / "branch-history.json"
    if path.exists():
        path.unlink()
    agent = Agent(api_key, model, path, strategy="facts")
    setup = agent.ask("Заказчика зовут Максим. Бюджет 120000 рублей. Ответь одним словом: принято.", max_tokens=40)
    agent.checkpoint("развилка")
    agent.fork("развилка", "дороже")
    expensive = agent.ask("Бюджет теперь 200000 рублей. Ответь одним словом: принято.", max_tokens=40)
    agent.fork("развилка", "дешевле")
    cheap = agent.ask("Бюджет теперь 50000 рублей. Ответь одним словом: принято.", max_tokens=40)
    agent.switch("дороже")
    answer_expensive = agent.ask("Какой бюджет в рублях? Ответь числом.", max_tokens=40)
    facts_expensive = dict(agent.facts)
    agent.switch("дешевле")
    answer_cheap = agent.ask("Какой бюджет в рублях? Ответь числом.", max_tokens=40)
    facts_cheap = dict(agent.facts)
    print("\n=== Ветки ===")
    print("  дороже:", answer_expensive, facts_expensive)
    print("  дешевле:", answer_cheap, facts_cheap)
    return {
        "setup": setup,
        "expensive_ack": expensive,
        "cheap_ack": cheap,
        "answer_expensive": answer_expensive,
        "answer_cheap": answer_cheap,
        "facts_expensive": facts_expensive,
        "facts_cheap": facts_cheap,
        "expensive_ok": contains_number(answer_expensive, "200000"),
        "cheap_ok": contains_number(answer_cheap, "50000"),
        "isolated": "50000" not in answer_expensive.replace(" ", "") and "200000" not in answer_cheap.replace(" ", ""),
    }


def load_runs() -> dict:
    if not RUNS_PATH.exists():
        return {"strategies": {}, "branches": None}
    return json.loads(RUNS_PATH.read_text(encoding="utf-8"))


def save_runs(payload: dict) -> None:
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    facts, remembered = day09_checks()
    api_key, model = load_settings()
    payload = load_runs()
    payload["model"] = model
    payload.setdefault("strategies", {})
    for strategy in STRATEGIES:
        if strategy in payload["strategies"]:
            print(f"Уже есть прогон: {strategy}")
            continue
        payload["strategies"][strategy] = run_strategy(api_key, model, strategy, facts, remembered)
        save_runs(payload)
    if not payload.get("branches"):
        payload["branches"] = run_branches(api_key, model)
        save_runs(payload)
    print("\nИтог")
    for strategy in STRATEGIES:
        row = payload["strategies"][strategy]
        print(
            strategy,
            row["remembered"],
            "/",
            len(facts),
            "запросы",
            row["totals"]["all"]["request_tokens"],
            money(row["totals"]["all"]["cost_usd"]),
        )
    branches = payload["branches"]
    print(
        "ветки:",
        "дороже" if branches["expensive_ok"] else "дороже ошибка",
        "дешевле" if branches["cheap_ok"] else "дешевле ошибка",
    )
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

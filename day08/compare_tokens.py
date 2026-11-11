"""Три диалога: короткий, длинный и тот, что не влезает в лимит.

Пишет runs.json. Числа берёт из поля usage ответа DeepSeek, не из оценки длины текста.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

from agent import (
    CONTEXT_LIMIT,
    SYSTEM_MESSAGE,
    Agent,
    LimitError,
    load_settings,
    money,
    post_chat,
)

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"


def balance_usd(api_key: str) -> str:
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8").lstrip())
    for info in body.get("balance_infos") or []:
        if info.get("currency") == "USD":
            return str(info.get("total_balance"))
    return json.dumps(body, ensure_ascii=False)[:180]


def run_dialog(agent: Agent, questions: list[str]) -> list[dict]:
    rows = []
    for question in questions:
        start = len(agent.calls)
        try:
            answer = agent.ask(question)
            error = ""
        except LimitError as error_info:
            answer = ""
            error = f"HTTP {error_info.status}: {error_info.detail[:400]}"
        calls = agent.new_calls_since(start)
        rows.append(
            {
                "question": question if len(question) < 80 else question[:77] + "...",
                "chars": len(question),
                "answer": answer[:240],
                "error": error,
                "trace": list(agent.trace),
                "calls": calls,
            }
        )
        if error:
            break
    return rows


def parse_limit(detail: str) -> dict:
    found = {
        "maximum": None,
        "requested": None,
        "messages": None,
        "completion": None,
    }
    maximum = re.search(r"maximum context length is (\d+)", detail)
    requested = re.search(r"requested (\d+) tokens \((\d+) in the messages, (\d+) in the completion\)", detail)
    if maximum:
        found["maximum"] = int(maximum.group(1))
    if requested:
        found["requested"] = int(requested.group(1))
        found["messages"] = int(requested.group(2))
        found["completion"] = int(requested.group(3))
    return found


def overflow(api_key: str, model: str) -> dict:
    before = ""
    try:
        before = balance_usd(api_key)
    except Exception as error:
        before = f"не прочитался: {error}"

    small = post_chat(api_key, model, [SYSTEM_MESSAGE, {"role": "user", "content": "hello " * 2000}], max_tokens=16)
    large = post_chat(api_key, model, [SYSTEM_MESSAGE, {"role": "user", "content": "hello " * 4000}], max_tokens=16)
    small_prompt = int(small["usage"].get("prompt_tokens") or 0)
    large_prompt = int(large["usage"].get("prompt_tokens") or 0)
    rate = (large_prompt - small_prompt) / 2000
    if rate <= 0:
        raise SystemExit("Не удалось понять, сколько токенов в одном 'hello '.")
    overhead = small_prompt - 2000 * rate
    repeats = int((CONTEXT_LIMIT - overhead) / rate) + 50
    text = "hello " * repeats
    estimated = int(overhead + rate * repeats)
    print(f"Проба: 2000 повторов = {small_prompt} токенов запроса, 4000 = {large_prompt}")
    print(f"Отправляю {repeats} повторов, оценка {estimated} токенов при лимите {CONTEXT_LIMIT}")

    status = 0
    detail = ""
    usage = {}
    try:
        result = post_chat(
            api_key,
            model,
            [SYSTEM_MESSAGE, {"role": "user", "content": text}],
            max_tokens=16,
            timeout=180,
        )
        status = 200
        usage = result["usage"]
        detail = "Запрос принят, лимит не сломался."
    except LimitError as error:
        status = error.status
        detail = error.detail[:800]

    after = ""
    try:
        after = balance_usd(api_key)
    except Exception as error:
        after = f"не прочитался: {error}"

    return {
        "status": status,
        "detail": detail,
        "parsed": parse_limit(detail),
        "repeats": repeats,
        "estimated_tokens": estimated,
        "rate": rate,
        "probe_small": small_prompt,
        "probe_large": large_prompt,
        "usage": {
            "request_tokens": int(usage.get("prompt_tokens") or 0),
            "answer_tokens": int(usage.get("completion_tokens") or 0),
        },
        "balance_before": before,
        "balance_after": after,
    }


def totals(rows: list[dict]) -> dict:
    calls = [call for row in rows for call in row["calls"]]
    request = sum(call["request_tokens"] for call in calls)
    answer = sum(call["answer_tokens"] for call in calls)
    cost = sum(call["cost_usd"] for call in calls)
    last = calls[-1] if calls else {}
    return {
        "calls": len(calls),
        "request_tokens": request,
        "answer_tokens": answer,
        "history_tokens": last.get("history_tokens", 0),
        "last_request_tokens": last.get("request_tokens", 0),
        "cost_usd": cost,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    for name in ("short-history.json", "long-history.json"):
        path = ROOT / name
        if path.exists():
            path.unlink()

    print("=== Короткий диалог ===")
    short_agent = Agent(api_key, model, ROOT / "short-history.json")
    short_rows = run_dialog(
        short_agent,
        ["Сколько будет 15 долларов в рублях? Напиши только итог."],
    )
    print_rows(short_rows)

    print("\n=== Длинный диалог ===")
    long_agent = Agent(api_key, model, ROOT / "long-history.json")
    filler = []
    for index in range(1, 5):
        block = f"Строка журнала {index}. " * 400
        filler.append(block + "Ответь одним словом: принято.")
    long_rows = run_dialog(
        long_agent,
        [
            "Меня зовут Максим. Секретное число 42. Ответь одним словом: запомнил.",
            *filler,
            "Как меня зовут и какое секретное число? Ответь одной короткой фразой.",
        ],
    )
    print_rows(long_rows)

    print("\n=== Диалог больше лимита ===")
    broken = overflow(api_key, model)
    print(f"HTTP {broken['status']}")
    print(broken["detail"][:500])
    print(f"Баланс до: {broken['balance_before']}, после: {broken['balance_after']}")

    payload = {
        "model": model,
        "context_limit": CONTEXT_LIMIT,
        "price": "off-peak deepseek-flash: hit $0.003, miss $0.15, out $0.60 за 1M",
        "short": {"rows": short_rows, "totals": totals(short_rows)},
        "long": {"rows": long_rows, "totals": totals(long_rows)},
        "overflow": broken,
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено: {RUNS_PATH}")


def print_rows(rows: list[dict]) -> None:
    for index, row in enumerate(rows, start=1):
        print(f"Ход {index}: {row['question']}")
        if row["error"]:
            print(" ", row["error"])
        else:
            print(" ", " | ".join(row["trace"]))
            print("  Ответ:", row["answer"].replace("\n", " "))
        for call in row["calls"]:
            print(
                "  "
                f"текущий запрос {call['request_tokens']}, "
                f"вся история {call['history_tokens']}, "
                f"ответ {call['answer_tokens']}, "
                f"{money(call['cost_usd'])}"
            )


if __name__ == "__main__":
    main()

"""День 3. Четыре способа промпта и два уровня рассуждения.

Минимум: thinking выключен.
Максимум: thinking включен, reasoning_effort=max.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
RUNS_PATH = ROOT / "runs.json"
REPORT_PATH = ROOT / "report.md"

METHODS = (
    ("direct", "Прямой ответ"),
    ("steps", "Пошагово"),
    ("meta", "Свой промпт"),
    ("experts", "Эксперты"),
)
EFFORTS = (
    ("min", "минимум"),
    ("max", "максимум"),
)


def has_number(text: str, expected: int, window: int = 250) -> bool:
    numbers = [int(item) for item in re.findall(r"\d+", text[-window:])]
    return bool(numbers) and numbers[-1] == expected


def last_fraction(text: str, window: int = 400) -> str:
    tail = text[-window:]
    found = re.findall(
        r"\\frac\{\s*(\d+)\s*\}\{\s*(\d+)\s*\}|(\d+)\s*/\s*(\d+)",
        tail,
    )
    if not found:
        return ""
    left, right, plain_left, plain_right = found[-1]
    if left:
        return f"{left}/{right}"
    return f"{plain_left}/{plain_right}"


def last_yes_no(text: str, window: int = 200) -> str:
    found = re.findall(r"\b(да|нет)\b", text[-window:].lower())
    return found[-1] if found else ""


def syllogism_ok(text: str) -> bool:
    tail = text[-500:].lower().replace("ё", "е")
    if re.search(r"\bда\b", tail) and not re.search(
        r"не следует|нельзя вывести|неверн|не гарантир|не является логически|итог:\s*не",
        tail,
    ):
        return False
    if re.search(
        r"не следует|нельзя вывести|неверн|не гарантир|не является логически|итог:\s*не",
        tail,
    ):
        return True
    return last_yes_no(text) == "нет"


def clock_ok(text: str) -> bool:
    tail = text[-500:]
    if re.search(r"(?i)ответ[^0-9]{0,40}50", tail):
        return True
    if re.search(r"=\s*50\s*(?:\\circ|°|градус)", tail):
        return True
    return has_number(text, 50, window=180)


def ball_ok(text: str) -> bool:
    tail = text[-300:].lower().replace("ё", "е")
    if re.search(r"0[.,]05", tail):
        return True
    if re.search(r"(?<!\d)5\s*копе", tail):
        return True
    numbers = [int(item) for item in re.findall(r"\d+", tail[-120:])]
    return bool(numbers) and numbers[-1] == 5


TASKS = [
    {
        "id": "mul",
        "title": "17 умножить на 23",
        "text": "Сколько будет 17 умножить на 23?",
        "answer": "391",
        "check": lambda text: has_number(text, 391),
    },
    {
        "id": "fib",
        "title": "10-й член Фибоначчи",
        "text": "Чему равен десятый член последовательности Фибоначчи, если первый член равен 1 и второй член равен 1?",
        "answer": "55",
        "check": lambda text: has_number(text, 55),
    },
    {
        "id": "ball",
        "title": "Мяч и бита",
        "text": "Мяч и бита вместе стоят 1 рубль 10 копеек. Бита стоит на 1 рубль дороже мяча. Сколько копеек стоит мяч?",
        "answer": "5",
        "check": ball_ok,
    },
    {
        "id": "weekday",
        "title": "День через 100 дней",
        "text": "Сегодня среда. Какой день недели будет через 100 дней?",
        "answer": "пятница",
        "check": lambda text: "пятниц" in text[-250:].lower(),
    },
    {
        "id": "lcm",
        "title": "Наименьшее число больше 100",
        "text": "Какое наименьшее целое число больше 100 делится и на 7, и на 9?",
        "answer": "126",
        "check": lambda text: has_number(text, 126),
    },
    {
        "id": "syllogism",
        "title": "Следует ли вывод",
        "text": "Все кошки являются животными. Мурка является животным. Следует ли из этих двух утверждений, что Мурка является кошкой?",
        "answer": "нет",
        "check": syllogism_ok,
    },
    {
        "id": "clock",
        "title": "Угол стрелок в 2:20",
        "text": "Сколько градусов между часовой и минутной стрелками в 2:20?",
        "answer": "50",
        "check": clock_ok,
    },
    {
        "id": "monty",
        "title": "Вероятность при смене двери",
        "text": (
            "В игре три двери, приз за одной из них. Игрок выбрал первую дверь. "
            "Ведущий открыл третью дверь, за ней пусто. "
            "Какова вероятность выигрыша, если игрок сменит выбор на вторую дверь? "
            "Запиши ответ дробью."
        ),
        "answer": "2/3",
        "check": lambda text: last_fraction(text) == "2/3",
    },
]


def load_settings() -> tuple[str, str]:
    env: dict[str, str] = {}
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day03/.env или day02/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


def prompt_for(method: str, task_text: str, composed: str = "") -> str:
    if method == "direct":
        return task_text
    if method == "steps":
        return f"Решай пошагово.\n\nЗадача:\n{task_text}"
    if method == "meta":
        return composed
    return (
        "Решите задачу тремя экспертами. Каждый пишет свой ответ отдельно.\n"
        "1. Аналитик разбирает условие и даёт ответ.\n"
        "2. Инженер проверяет расчёт и даёт ответ.\n"
        "3. Критик ищет ошибку и даёт свой ответ.\n"
        "Последняя строка ответа должна быть строго такой: Итог: <ответ>\n\n"
        f"Задача:\n{task_text}"
    )


def compose_prompt(task_text: str) -> str:
    return (
        "Составь один промпт, по которому другая модель лучше всего решит задачу ниже. "
        "Верни только готовый промпт, без пояснений и без решения задачи.\n\n"
        f"Задача:\n{task_text}"
    )


def effort_payload(effort: str) -> dict:
    if effort == "min":
        return {"thinking": {"type": "disabled"}, "max_tokens": 1024}
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "max_tokens": 8192,
    }


def ask(api_key: str, model: str, prompt: str, effort: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        **effort_payload(effort),
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8").lstrip()
            body = json.loads(raw)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        return {
            "text": "",
            "usage": {},
            "seconds": time.perf_counter() - started,
            "error": f"HTTP {error.code}: {details[:300]}",
            "finish_reason": None,
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return {
            "text": "",
            "usage": {},
            "seconds": time.perf_counter() - started,
            "error": str(error),
            "finish_reason": None,
        }

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    return {
        "text": text.strip(),
        "usage": body.get("usage") or {},
        "seconds": time.perf_counter() - started,
        "error": None,
        "finish_reason": choice.get("finish_reason"),
    }


def ask_until(api_key: str, model: str, prompt: str, effort: str) -> dict:
    result = ask(api_key, model, prompt, effort)
    for attempt in range(1, 4):
        error = result["error"] or ""
        if not any(code in error for code in ("429", "500", "503")):
            return result
        time.sleep(3 * attempt)
        result = ask(api_key, model, prompt, effort)
    return result


def tokens_of(usage: dict) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return int(prompt) + int(completion)


def load_runs() -> list[dict]:
    if not RUNS_PATH.exists():
        return []
    data = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
    return data.get("runs", [])


def save_runs(model: str, runs: list[dict]) -> None:
    RUNS_PATH.write_text(
        json.dumps({"model": model, "runs": runs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def done_keys(runs: list[dict]) -> set[tuple[str, str, str]]:
    return {(row["task_id"], row["method"], row["effort"]) for row in runs}


def solve_one(api_key: str, model: str, task: dict, method: str, effort: str) -> dict:
    calls = []
    if method == "meta":
        draft = ask_until(api_key, model, compose_prompt(task["text"]), effort)
        calls.append(draft)
        composed = draft["text"].strip()
        if draft["error"] or not composed:
            return pack(task, method, effort, calls, "", False, draft["error"] or "пустой промпт")
        final = ask_until(api_key, model, composed, effort)
        calls.append(final)
        answer = final["text"]
        error = final["error"]
    else:
        final = ask_until(api_key, model, prompt_for(method, task["text"]), effort)
        calls.append(final)
        answer = final["text"]
        error = final["error"]

    correct = bool(answer) and not error and task["check"](answer)
    return pack(task, method, effort, calls, answer, correct, error)


def pack(task: dict, method: str, effort: str, calls: list[dict], answer: str, correct: bool, error: str | None) -> dict:
    return {
        "task_id": task["id"],
        "title": task["title"],
        "expected": task["answer"],
        "method": method,
        "effort": effort,
        "correct": correct,
        "tokens": sum(tokens_of(call["usage"]) for call in calls),
        "seconds": round(sum(call["seconds"] for call in calls), 2),
        "finish_reason": calls[-1].get("finish_reason"),
        "error": error,
        "tail": (answer or "")[-300:],
    }


def write_report(model: str, runs: list[dict]) -> None:
    lines = [
        "# День 3. Разные способы рассуждения",
        "",
        f"Модель: `{model}`.",
        "Минимум: `thinking` выключен.",
        "Максимум: `thinking` включен, `reasoning_effort` равен `max`.",
        "Задач: 8. У каждой известный ответ, проверка автоматическая.",
        "Способ «свой промпт» делает два запроса: сначала промпт, потом решение. Токены и время суммируются.",
        "Ключ в репозиторий не входит.",
        "",
        "| Способ | Рассуждение | Верных | Токены | Время, с |",
        "| --- | --- | --- | --- | --- |",
    ]
    summary = []
    for method, method_title in METHODS:
        for effort, effort_title in EFFORTS:
            rows = [row for row in runs if row["method"] == method and row["effort"] == effort]
            correct = sum(1 for row in rows if row["correct"])
            total_tokens = sum(row["tokens"] for row in rows)
            total_seconds = round(sum(row["seconds"] for row in rows), 1)
            lines.append(
                f"| {method_title} | {effort_title} | {correct} из {len(rows)} | {total_tokens} | {total_seconds} |"
            )
            summary.append((method, effort, correct, len(rows), total_tokens, total_seconds))

    lines.extend(["", "## По задачам", ""])
    for task in TASKS:
        lines.append(f"### {task['title']}")
        lines.append("")
        lines.append(f"Ожидается: {task['answer']}.")
        lines.append("")
        for method, method_title in METHODS:
            for effort, effort_title in EFFORTS:
                row = next(
                    item
                    for item in runs
                    if item["task_id"] == task["id"] and item["method"] == method and item["effort"] == effort
                )
                mark = "верно" if row["correct"] else "неверно"
                lines.append(
                    f"- {method_title}, {effort_title}: {mark}. Токены {row['tokens']}, {row['seconds']} с."
                )
        lines.append("")

    lines.extend(["## Вывод", "", conclusion(summary), ""])
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def conclusion(summary: list[tuple]) -> str:
    by_key = {
        (method, effort): (correct, total, tokens, seconds)
        for method, effort, correct, total, tokens, seconds in summary
    }
    direct_min = by_key[("direct", "min")][0]
    direct_max = by_key[("direct", "max")][0]
    direct_tokens_max = by_key[("direct", "max")][2]
    lines = [
        f"Прямой ответ без инструкций: {direct_min} верных при выключенном рассуждении и {direct_max} при максимальном.",
    ]
    stopped = []
    still_helps = []
    for method, title in METHODS:
        if method == "direct":
            continue
        gain_min = by_key[(method, "min")][0] - direct_min
        gain_max = by_key[(method, "max")][0] - direct_max
        tokens_max = by_key[(method, "max")][2]
        if gain_min > 0 and gain_max <= 0:
            stopped.append(
                f"{title}: при минимуме даёт на {gain_min} верных больше прямого ответа, "
                f"при максимуме прибавки нет ({gain_max}). На максимуме токены {tokens_max}, "
                f"у прямого ответа {direct_tokens_max}."
            )
        elif gain_max > 0:
            still_helps.append(
                f"{title} добавляет {gain_max} верных даже при максимальном рассуждении."
            )
        else:
            stopped.append(
                f"{title}: прибавки нет ни при минимуме ({gain_min}), ни при максимуме ({gain_max}). "
                f"На максимуме способ тратит {tokens_max} токенов, прямой ответ тратит {direct_tokens_max}."
            )
    if stopped:
        lines.append("Когда рассуждение максимальное, эти приёмы не добавляют верных ответов:")
        lines.extend(stopped)
    if still_helps:
        lines.append("Эти приёмы добавляют верные ответы даже при максимальном рассуждении:")
        lines.extend(still_helps)
    return "\n".join(lines)


def self_test() -> None:
    samples = {
        "mul": "Значит, ответ 391.",
        "fib": "Десятый член равен 55.",
        "ball": "Мяч стоит 5 копеек, а не 10.",
        "weekday": "Через 100 дней будет пятница.",
        "lcm": "Наименьшее такое число равно 126.",
        "syllogism": "Из этих утверждений вывод не следует. Нет.",
        "clock": "Угол равен 50 градусов.",
        "monty": "Сначала кажется 1/2, но верная вероятность 2/3.",
    }
    bad = {
        "ball": "Мяч стоит 10 копеек.",
        "syllogism": "Да, Мурка кошка.",
        "monty": "Вероятность равна 1/2.",
        "clock": "Угол равен 60 градусов.",
    }
    by_id = {task["id"]: task for task in TASKS}
    for task_id, text in samples.items():
        if not by_id[task_id]["check"](text):
            raise SystemExit(f"Проверка не узнала верный ответ: {task_id}")
    for task_id, text in bad.items():
        if by_id[task_id]["check"](text):
            raise SystemExit(f"Проверка приняла неверный ответ: {task_id}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    self_test()
    api_key, model = load_settings()
    runs = load_runs()
    finished = done_keys(runs)
    print(f"Модель: {model}. Уже готово строк: {len(runs)}", flush=True)

    for task in TASKS:
        for method, method_title in METHODS:
            for effort, effort_title in EFFORTS:
                key = (task["id"], method, effort)
                if key in finished:
                    continue
                print(f"{task['title']} | {method_title} | {effort_title}", flush=True)
                row = solve_one(api_key, model, task, method, effort)
                runs.append(row)
                save_runs(model, runs)
                mark = "верно" if row["correct"] else "неверно"
                print(f"  {mark}, токены {row['tokens']}, {row['seconds']} с", flush=True)
                time.sleep(0.4)

    write_report(model, runs)
    print("Готово: day03/report.md и day03/runs.json", flush=True)


if __name__ == "__main__":
    main()

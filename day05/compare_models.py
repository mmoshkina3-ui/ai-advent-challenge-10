"""День 5. Три модели: две DeepSeek и одна бесплатная.

Маленькая дешёвая: deepseek-flash.
Флагман: deepseek-v4-pro.
Бесплатная: google/gemma-4-31b-it:free на OpenRouter.
Рассуждение выключено, temperature 0, чтобы сравнивать сами модели.
Цены DeepSeek взяты для обычных часов, не пиковых.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"
REPORT_PATH = ROOT / "report.md"

# Доллары за 1 токен, обычные часы. Пик у DeepSeek в два раза дороже.
PRICES = {
    "deepseek-flash": {"hit": 0.003 / 1_000_000, "miss": 0.15 / 1_000_000, "out": 0.60 / 1_000_000},
    "deepseek-v4-pro": {"hit": 0.022 / 1_000_000, "miss": 0.66 / 1_000_000, "out": 1.98 / 1_000_000},
}

MODELS = (
    {
        "id": "deepseek-flash",
        "title": "Маленькая, DeepSeek Flash",
        "provider": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "key": "deepseek",
    },
    {
        "id": "google/gemma-4-31b-it:free",
        "title": "Бесплатная, Gemma 31B",
        "provider": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key": "openrouter",
    },
    {
        "id": "deepseek-v4-pro",
        "title": "Флагман, DeepSeek V4 Pro",
        "provider": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "key": "deepseek",
    },
)


def has_number(text: str, expected: int, window: int = 250) -> bool:
    numbers = [int(item) for item in re.findall(r"\d+", text[-window:])]
    return bool(numbers) and numbers[-1] == expected


def last_fraction(text: str, window: int = 400) -> str:
    found = re.findall(r"\\frac\{\s*(\d+)\s*\}\{\s*(\d+)\s*\}|(\d+)\s*/\s*(\d+)", text[-window:])
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
    negative = r"не следует|нельзя вывести|неверн|не гарантир|не является логически|итог:\s*не"
    if re.search(r"\bда\b", tail) and not re.search(negative, tail):
        return False
    if re.search(negative, tail):
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
    {"id": "mul", "title": "17 умножить на 23", "text": "Сколько будет 17 умножить на 23?", "answer": "391", "check": lambda text: has_number(text, 391)},
    {"id": "fib", "title": "10-й член Фибоначчи", "text": "Чему равен десятый член последовательности Фибоначчи, если первый член равен 1 и второй член равен 1?", "answer": "55", "check": lambda text: has_number(text, 55)},
    {"id": "ball", "title": "Мяч и бита", "text": "Мяч и бита вместе стоят 1 рубль 10 копеек. Бита стоит на 1 рубль дороже мяча. Сколько копеек стоит мяч?", "answer": "5", "check": ball_ok},
    {"id": "weekday", "title": "День через 100 дней", "text": "Сегодня среда. Какой день недели будет через 100 дней?", "answer": "пятница", "check": lambda text: "пятниц" in text[-250:].lower()},
    {"id": "lcm", "title": "Наименьшее число больше 100", "text": "Какое наименьшее целое число больше 100 делится и на 7, и на 9?", "answer": "126", "check": lambda text: has_number(text, 126)},
    {"id": "syllogism", "title": "Следует ли вывод", "text": "Все кошки являются животными. Мурка является животным. Следует ли из этих двух утверждений, что Мурка является кошкой?", "answer": "нет", "check": syllogism_ok},
    {"id": "clock", "title": "Угол стрелок в 2:20", "text": "Сколько градусов между часовой и минутной стрелками в 2:20?", "answer": "50", "check": clock_ok},
    {"id": "monty", "title": "Вероятность при смене двери", "text": "В игре три двери, приз за одной из них. Игрок выбрал первую дверь. Ведущий открыл третью дверь, за ней пусто. Какова вероятность выигрыша, если игрок сменит выбор на вторую дверь? Запиши ответ дробью.", "answer": "2/3", "check": lambda text: last_fraction(text) == "2/3"},
]


def load_keys() -> dict[str, str]:
    env: dict[str, str] = {}
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env", ROOT.parent / "day01" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    keys = {
        "deepseek": env.get("DEEPSEEK_API_KEY", ""),
        "openrouter": env.get("OPENROUTER_API_KEY", ""),
    }
    if not keys["deepseek"] or keys["deepseek"] == "сюда_ключ":
        raise SystemExit("Нет ключа DeepSeek. Положи DEEPSEEK_API_KEY в day02/.env.")
    if not keys["openrouter"] or keys["openrouter"] == "сюда_ключ":
        raise SystemExit("Нет ключа OpenRouter. Положи OPENROUTER_API_KEY в day01/.env.")
    return keys


def ask(api_key: str, model: dict, prompt: str) -> dict:
    payload = {
        "model": model["id"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 800,
    }
    if model["provider"] == "DeepSeek":
        payload["thinking"] = {"type": "disabled"}
    request = urllib.request.Request(
        model["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8").lstrip())
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        return {"text": "", "usage": {}, "seconds": round(time.perf_counter() - started, 2), "error": f"HTTP {error.code}: {details[:300]}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return {"text": "", "usage": {}, "seconds": round(time.perf_counter() - started, 2), "error": str(error)}
    message = ((body.get("choices") or [{}])[0].get("message") or {})
    content = message.get("content")
    return {
        "text": content.strip() if isinstance(content, str) else "",
        "usage": body.get("usage") or {},
        "seconds": round(time.perf_counter() - started, 2),
        "error": None,
    }


def ask_until(api_key: str, model: dict, prompt: str) -> dict:
    result = ask(api_key, model, prompt)
    for attempt in range(1, 6):
        error = result["error"] or ""
        if not any(code in error for code in ("429", "500", "503")):
            return result
        time.sleep(4 * attempt)
        result = ask(api_key, model, prompt)
    return result


def tokens_of(usage: dict) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def cost_of(model_id: str, usage: dict) -> float:
    if model_id not in PRICES:
        reported = usage.get("cost")
        return float(reported) if isinstance(reported, (int, float)) else 0.0
    price = PRICES[model_id]
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if not isinstance(hit, int) or not isinstance(miss, int):
        details = usage.get("prompt_tokens_details") or {}
        hit = details.get("cached_tokens") if isinstance(details, dict) else 0
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = int(hit or 0)
        miss = max(prompt - hit, 0)
    output = int(usage.get("completion_tokens") or 0)
    return hit * price["hit"] + miss * price["miss"] + output * price["out"]


def load_runs() -> list[dict]:
    if not RUNS_PATH.exists():
        return []
    return json.loads(RUNS_PATH.read_text(encoding="utf-8")).get("runs", [])


def save_runs(runs: list[dict]) -> None:
    RUNS_PATH.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report(runs: list[dict]) -> None:
    lines = [
        "# День 5. Версии моделей",
        "",
        "Одни и те же 8 задач из дня 3. Прямой вопрос, temperature 0, рассуждение выключено.",
        "Провайдеры разные, это разрешено заданием.",
        "Маленькая дешёвая: `deepseek-flash`, DeepSeek.",
        "Бесплатная: `google/gemma-4-31b-it:free`, OpenRouter.",
        "Флагман: `deepseek-v4-pro`, DeepSeek.",
        "Цены DeepSeek посчитаны по обычным часам. Попадание в кэш и промах берутся из ответа API.",
        "Выход флагмана стоит $1.98 за 1 миллион токенов, выход Flash стоит $0.60. Это в 3.3 раза дороже.",
        "Ключ в репозиторий не входит.",
        "",
        "| Модель | Верных | Время, с | Токены | Стоимость |",
        "| --- | --- | --- | --- | --- |",
    ]
    totals = []
    for model in MODELS:
        rows = [row for row in runs if row["model_id"] == model["id"]]
        correct = sum(1 for row in rows if row["correct"])
        seconds = round(sum(row["seconds"] for row in rows), 1)
        tokens = sum(row["tokens"] for row in rows)
        cost = sum(row["cost"] for row in rows)
        lines.append(f"| {model['title']} | {correct} из {len(rows)} | {seconds} | {tokens} | ${cost:.6f} |")
        totals.append((model, correct, len(rows), seconds, tokens, cost))

    lines.extend(["", "## По задачам", ""])
    for task in TASKS:
        lines.append(f"### {task['title']}")
        lines.append("")
        lines.append(f"Ожидается: {task['answer']}.")
        lines.append("")
        for model in MODELS:
            row = next(item for item in runs if item["task_id"] == task["id"] and item["model_id"] == model["id"])
            mark = "верно" if row["correct"] else "неверно"
            extra = f" Ошибка: {row['error']}" if row["error"] else ""
            lines.append(f"- {model['title']}: {mark}. {row['seconds']} с, токены {row['tokens']}, ${row['cost']:.6f}.{extra}")
            if row["tail"]:
                lines.append(f"  Ответ: {row['tail'][:220]}")
        lines.append("")

    small_rows = [row for row in runs if row["model_id"] == MODELS[0]["id"]]
    enough = [task["title"] for task in TASKS if any(row["task_id"] == task["id"] and row["correct"] for row in small_rows)]
    missing = [task["title"] for task in TASKS if task["title"] not in enough]
    flash_cost = totals[0][5]
    pro_cost = totals[2][5]
    ratio = (pro_cost / flash_cost) if flash_cost else 0
    lines.extend([
        "## Вывод",
        "",
        f"Flash решил {totals[0][1]} из {totals[0][2]}, Gemma {totals[1][1]} из {totals[1][2]}, V4 Pro {totals[2][1]} из {totals[2][2]}.",
        f"Время на 8 задач: Flash {totals[0][3]} с, Gemma {totals[1][3]} с, V4 Pro {totals[2][3]} с.",
        f"Стоимость прогона: Flash ${flash_cost:.6f}, Gemma ${totals[1][5]:.6f}, V4 Pro ${pro_cost:.6f}.",
        f"По этому прогону флагман дороже Flash в {ratio:.1f} раза." if flash_cost else "Стоимость Flash в этом прогоне нулевая, отношение посчитать нельзя.",
        "По тарифу выход флагмана дороже выхода Flash в 3.3 раза.",
        "Маленькой модели хватило на: " + (", ".join(enough) if enough else "ни на одну задачу") + ".",
        "Маленькой модели не хватило на: " + (", ".join(missing) if missing else "все 8 задач она решила") + ".",
        "",
    ])
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    keys = load_keys()
    runs = load_runs()
    done = {(row["task_id"], row["model_id"]) for row in runs}
    print(f"Уже готово строк: {len(runs)}", flush=True)
    for task in TASKS:
        for model in MODELS:
            if (task["id"], model["id"]) in done:
                continue
            print(f"{task['title']} | {model['title']}", flush=True)
            result = ask_until(keys[model["key"]], model, task["text"])
            correct = bool(result["text"]) and not result["error"] and task["check"](result["text"])
            row = {
                "task_id": task["id"],
                "title": task["title"],
                "expected": task["answer"],
                "model_id": model["id"],
                "model_title": model["title"],
                "correct": correct,
                "tokens": tokens_of(result["usage"]),
                "cost": cost_of(model["id"], result["usage"]),
                "seconds": result["seconds"],
                "error": result["error"],
                "tail": (result["text"] or "")[-400:],
            }
            runs.append(row)
            save_runs(runs)
            mark = "верно" if correct else "неверно"
            print(f"  {mark}, токены {row['tokens']}, {row['seconds']} с, ${row['cost']:.6f}", flush=True)
            time.sleep(0.4)
    write_report(runs)
    print("Готово: day05/report.md и day05/runs.json", flush=True)


if __name__ == "__main__":
    main()

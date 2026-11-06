"""День 4. Один и тот же запрос при temperature 0, 0.7 и 1.2.

Рассуждение выключено: в документации DeepSeek temperature в thinking-режиме не действует.
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
TEMPERATURES = (0, 0.7, 1.2)
REPEATS = 5

FACT_PROMPT = "Сколько будет 17 умножить на 23? Ответь одной строкой строго в виде: Ответ: <число>"
IDEA_PROMPT = (
    "Придумай ровно 5 коротких названий для мобильного приложения учёта личных финансов. "
    "Каждое название с новой строки. Без нумерации, пояснений и пустых строк."
)


def load_settings() -> tuple[str, str]:
    env: dict[str, str] = {}
    for path in (ROOT / ".env", ROOT.parent / "day03" / ".env", ROOT.parent / "day02" / ".env"):
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
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day04/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


def ask(api_key: str, model: str, prompt: str, temperature: float) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "thinking": {"type": "disabled"},
        "max_tokens": 300,
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
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8").lstrip())
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        return {"text": "", "seconds": round(time.perf_counter() - started, 2), "error": f"HTTP {error.code}: {details[:240]}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return {"text": "", "seconds": round(time.perf_counter() - started, 2), "error": str(error)}

    message = ((body.get("choices") or [{}])[0].get("message") or {})
    text = message.get("content")
    return {
        "text": text.strip() if isinstance(text, str) else "",
        "seconds": round(time.perf_counter() - started, 2),
        "error": None,
    }


def fact_checks(text: str) -> tuple[bool, bool]:
    line = text.strip()
    format_ok = bool(re.fullmatch(r"Ответ:\s*\d+\.?", line))
    numbers = re.findall(r"\d+", line)
    correct = bool(numbers) and int(numbers[-1]) == 391
    return correct, format_ok


def idea_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def idea_format_ok(text: str) -> bool:
    lines = idea_lines(text)
    if len(lines) != 5:
        return False
    return all(not re.match(r"^(\d+[\).\s]|[-*]\s)", line) for line in lines)


def unique_count(values: list[str]) -> int:
    return len(set(values))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    runs: list[dict] = []
    print(f"Модель: {model}. Рассуждение выключено.", flush=True)

    for kind, prompt in (("fact", FACT_PROMPT), ("ideas", IDEA_PROMPT)):
        for temperature in TEMPERATURES:
            for attempt in range(1, REPEATS + 1):
                print(f"{kind} | temperature {temperature} | {attempt}/{REPEATS}", flush=True)
                result = ask(api_key, model, prompt, temperature)
                row = {
                    "kind": kind,
                    "temperature": temperature,
                    "attempt": attempt,
                    "text": result["text"],
                    "seconds": result["seconds"],
                    "error": result["error"],
                }
                if kind == "fact":
                    correct, format_ok = fact_checks(result["text"]) if result["text"] else (False, False)
                    row["correct"] = correct
                    row["format_ok"] = format_ok
                else:
                    row["format_ok"] = idea_format_ok(result["text"]) if result["text"] else False
                    row["names"] = idea_lines(result["text"])
                runs.append(row)
                print(f"  {result['text'][:120].replace(chr(10), ' | ')}", flush=True)
                time.sleep(0.3)

    (ROOT / "runs.json").write_text(
        json.dumps({"model": model, "runs": runs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ROOT / "report.md").write_text(render_report(model, runs), encoding="utf-8")
    print("Готово: day04/report.md и day04/runs.json", flush=True)


def render_report(model: str, runs: list[dict]) -> str:
    lines = [
        "# День 4. Температура",
        "",
        f"Модель: `{model}`.",
        "Рассуждение выключено: в документации DeepSeek параметр `temperature` в thinking-режиме не действует.",
        "Допустимый диапазон temperature: от 0 до 2. Значение 1.2 внутри диапазона.",
        "Каждая пара «задача + temperature» запущена 5 раз.",
        "Ключ в репозиторий не входит.",
        "",
        "## Задача с одним ответом",
        "",
        f"Запрос: {FACT_PROMPT}",
        "",
        "Верный ответ: 391. Формат соблюдён, если весь ответ состоит из одной строки `Ответ: <число>`.",
        "",
        "| temperature | Верных | Формат сломан | Одинаковых ответов |",
        "| --- | --- | --- | --- |",
    ]
    for temperature in TEMPERATURES:
        group = [row for row in runs if row["kind"] == "fact" and row["temperature"] == temperature]
        correct = sum(1 for row in group if row["correct"])
        broken = sum(1 for row in group if not row["format_ok"])
        same = unique_count([row["text"] for row in group])
        lines.append(f"| {temperature} | {correct} из 5 | {broken} из 5 | уникальных текстов: {same} |")

    lines.extend(["", "Примеры:", ""])
    for temperature in TEMPERATURES:
        lines.append(f"### temperature {temperature}")
        lines.append("")
        for row in runs:
            if row["kind"] == "fact" and row["temperature"] == temperature:
                mark = "верно" if row["correct"] else "неверно"
                form = "формат ок" if row["format_ok"] else "формат сломан"
                lines.append(f"{row['attempt']}. {mark}, {form}: `{row['text']}`")
        lines.append("")

    lines.extend([
        "## Задача на генерацию",
        "",
        f"Запрос: {IDEA_PROMPT}",
        "",
        "Формат соблюдён, если в ответе ровно 5 непустых строк без нумерации.",
        "",
        "| temperature | Формат сломан | Уникальных наборов из 5 |",
        "| --- | --- | --- |",
    ])
    for temperature in TEMPERATURES:
        group = [row for row in runs if row["kind"] == "ideas" and row["temperature"] == temperature]
        broken = sum(1 for row in group if not row["format_ok"])
        sets = unique_count(["\n".join(row["names"]) for row in group])
        lines.append(f"| {temperature} | {broken} из 5 | {sets} |")

    lines.extend(["", "Примеры:", ""])
    for temperature in TEMPERATURES:
        lines.append(f"### temperature {temperature}")
        lines.append("")
        for row in runs:
            if row["kind"] == "ideas" and row["temperature"] == temperature:
                form = "формат ок" if row["format_ok"] else "формат сломан"
                shown = " / ".join(row["names"]) if row["names"] else row["text"]
                lines.append(f"{row['attempt']}. {form}: {shown}")
        lines.append("")

    lines.extend(["## Вывод", "", build_conclusion(runs), ""])
    return "\n".join(lines)


def build_conclusion(runs: list[dict]) -> str:
    parts = []
    fact_zero = [row["text"] for row in runs if row["kind"] == "fact" and row["temperature"] == 0]
    idea_zero = ["\n".join(row["names"]) for row in runs if row["kind"] == "ideas" and row["temperature"] == 0]
    if unique_count(fact_zero) == 1:
        parts.append("При temperature 0 задача с одним ответом дала один и тот же текст во всех 5 прогонах.")
    else:
        parts.append(
            f"При temperature 0 задача с одним ответом дала {unique_count(fact_zero)} разных текста из 5. "
            "Одинаковый ответ каждый раз не гарантирован."
        )
    if unique_count(idea_zero) == 1:
        parts.append("При temperature 0 генерация названий тоже повторила один и тот же набор.")
    else:
        parts.append(
            f"При temperature 0 генерация названий дала {unique_count(idea_zero)} разных набора из 5. "
            "Даже нулевая температура не зафиксировала текст полностью."
        )

    fact_wrong = {
        temperature: sum(1 for row in runs if row["kind"] == "fact" and row["temperature"] == temperature and not row["correct"])
        for temperature in TEMPERATURES
    }
    fact_broken = {
        temperature: sum(1 for row in runs if row["kind"] == "fact" and row["temperature"] == temperature and not row["format_ok"])
        for temperature in TEMPERATURES
    }
    idea_sets = {
        temperature: unique_count(["\n".join(row["names"]) for row in runs if row["kind"] == "ideas" and row["temperature"] == temperature])
        for temperature in TEMPERATURES
    }
    idea_broken = {
        temperature: sum(1 for row in runs if row["kind"] == "ideas" and row["temperature"] == temperature and not row["format_ok"])
        for temperature in TEMPERATURES
    }
    parts.append(
        "Для задачи с одним правильным ответом лучше temperature 0: "
        f"неверных ответов {fact_wrong[0]} из 5, сломанный формат {fact_broken[0]} из 5. "
        f"При 0.7 неверных {fact_wrong[0.7]}, сломанный формат {fact_broken[0.7]}. "
        f"При 1.2 неверных {fact_wrong[1.2]}, сломанный формат {fact_broken[1.2]}."
    )
    parts.append(
        "Для генерации названий разнообразие полезно. "
        f"Уникальных наборов: при 0 их {idea_sets[0]}, при 0.7 их {idea_sets[0.7]}, при 1.2 их {idea_sets[1.2]}. "
        f"Сломанный формат: {idea_broken[0]}, {idea_broken[0.7]} и {idea_broken[1.2]} из 5."
    )
    parts.append(
        "Итог: temperature 0 подходит для счёта, проверки и строгого формата. "
        "temperature 0.7 подходит, когда нужны разные формулировки, но ответ ещё должен оставаться аккуратным. "
        "temperature 1.2 подходит для поиска идей и вредит там, где есть единственный правильный ответ или жёсткий формат."
    )
    return "\n".join(parts)


if __name__ == "__main__":
    main()

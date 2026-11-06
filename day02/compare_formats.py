"""День 2. Один и тот же вопрос с разным контролем формата ответа.

Режимы:
1. без ограничений
2. формат описан словами в запросе
3. короткий ответ, max_tokens
4. обрыв по stop sequence
5. JSON-режим json_object
6. строгая JSON Schema, чтобы проверить, принимает ли её API
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
REPEATS = 3
QUESTION = (
    "Перечисли три причины, почему ответ языковой модели нужно проверять программой."
)
FORMAT_INSTRUCTION = (
    "Ответь только json-объектом, без пояснений и без markdown. "
    'Пример: {"reasons":[{"title":"короткое название","detail":"одно предложение"}]}. '
    "В массиве reasons ровно 3 элемента. Других полей нет."
)
EXPECTED_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasons": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        }
    },
    "required": ["reasons"],
}


def load_settings() -> tuple[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env[name.strip()] = value.strip().strip('"').strip("'")

    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env.")

    model = env.get("LLM_MODEL") or DEFAULT_MODEL
    return api_key, model


def can_parse(text: str) -> tuple[bool, str]:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as error:
        return False, f"json.loads не смог прочитать ответ: {error.msg}"

    if not isinstance(data, dict):
        return False, "корень ответа не объект"
    reasons = data.get("reasons")
    if not isinstance(reasons, list):
        return False, "нет массива reasons"
    if len(reasons) != 3:
        return False, f"в reasons {len(reasons)} элементов, нужно 3"
    for index, item in enumerate(reasons, start=1):
        if not isinstance(item, dict):
            return False, f"элемент {index} не объект"
        title = item.get("title")
        detail = item.get("detail")
        if not isinstance(title, str) or not title.strip():
            return False, f"у элемента {index} нет title"
        if not isinstance(detail, str) or not detail.strip():
            return False, f"у элемента {index} нет detail"
        if set(item) != {"title", "detail"}:
            return False, f"у элемента {index} лишние поля: {sorted(set(item) - {'title', 'detail'})}"
    if set(data) != {"reasons"}:
        return False, f"в корне лишние поля: {sorted(set(data) - {'reasons'})}"
    return True, "ответ совпал со схемой"


def short_error(raw: str) -> str:
    if "429" in raw:
        return "Модель занята, ответ не пришёл (429)."
    return raw[:240]


def ask(api_key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8").lstrip()
            body = json.loads(raw)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        return {
            "text": "",
            "finish_reason": None,
            "model": None,
            "error": f"HTTP {error.code}: {details}",
        }
    except urllib.error.URLError as error:
        return {"text": "", "finish_reason": None, "model": None, "error": f"сеть: {error.reason}"}

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = content if isinstance(content, str) else ""
    return {
        "text": text,
        "finish_reason": choice.get("finish_reason"),
        "model": body.get("model"),
        "error": None,
    }


def ask_until_answer(api_key: str, payload: dict) -> dict:
    result = {"text": "", "finish_reason": None, "model": None, "error": "нет ответа"}
    for attempt in range(1, 6):
        result = ask(api_key, payload)
        if not (result["error"] and "429" in result["error"]):
            return result
        time.sleep(3 * attempt)
    if result["error"]:
        result["error"] = short_error(result["error"])
    return result


def modes() -> list[dict]:
    question = [{"role": "user", "content": QUESTION}]
    with_format = [
        {"role": "system", "content": FORMAT_INSTRUCTION},
        {"role": "user", "content": QUESTION},
    ]
    return [
        {
            "id": "free",
            "title": "Без ограничений",
            "messages": question,
            "extra": {},
        },
        {
            "id": "format",
            "title": "Формат описан словами",
            "messages": with_format,
            "extra": {},
        },
        {
            "id": "max_tokens",
            "title": "Лимит max_tokens=40",
            "messages": question,
            "extra": {"max_tokens": 40},
        },
        {
            "id": "stop",
            "title": "Stop sequence «2.»",
            "messages": question,
            "extra": {"stop": ["2."]},
        },
        {
            "id": "json_object",
            "title": "JSON-режим json_object",
            "messages": with_format,
            "extra": {"response_format": {"type": "json_object"}},
        },
        {
            "id": "json_schema",
            "title": "Строгая JSON Schema",
            "messages": with_format,
            "extra": {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "reasons",
                        "strict": True,
                        "schema": EXPECTED_SCHEMA,
                    },
                }
            },
        },
    ]


def shorten(text: str, limit: int = 400) -> str:
    if not text:
        return "(пустой ответ)"
    if not text.strip():
        return f"(только пробелы, {len(text)} символов)"
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def write_report(model: str, rows: list[dict]) -> None:
    lines = [
        "# День 2. Формат ответа",
        "",
        "Один и тот же вопрос отправлен в каждом режиме.",
        f"Вопрос: {QUESTION}",
        f"Модель: `{model}`. Размышление выключено, чтобы лимит длины резал ответ, а не цепочку мыслей.",
        "Stop и лимит стоят на той же модели: DeepSeek принимает параметр stop.",
        "Строгая JSON Schema в справочнике DeepSeek не описана. Допустимые response_format: text и json_object.",
        f"Каждый режим запущен {REPEATS} раза.",
        "Ключ в репозиторий не входит.",
        "",
        "Проверка одна и та же: `json.loads`, затем сверка полей `reasons[].title` и `reasons[].detail`.",
        "",
    ]
    for mode in modes():
        mode_rows = [row for row in rows if row["mode_id"] == mode["id"]]
        ok = sum(1 for row in mode_rows if row["parsed"])
        lines.append(f"## {mode['title']}")
        lines.append("")
        lines.append(f"Разобрано кодом: {ok} из {len(mode_rows)}.")
        lines.append("")
        for row in mode_rows:
            status = "разобран" if row["parsed"] else "не разобран"
            lines.append(f"Попытка {row['attempt']}: {status}. {row['parse_note']}")
            if row["finish_reason"]:
                lines.append(f"finish_reason: `{row['finish_reason']}`.")
            if row["error"]:
                lines.append(f"Ошибка API: {short_error(row['error'])}")
            lines.append(f"Ответ: {shorten(row['text'])}")
            lines.append("")
    (ROOT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (ROOT / "runs.json").write_text(
        json.dumps({"model": model, "question": QUESTION, "runs": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key, model = load_settings()
    rows: list[dict] = []
    print(f"Модель: {model}")
    print(f"Вопрос: {QUESTION}")

    for mode in modes():
        for attempt in range(1, REPEATS + 1):
            print(f"{mode['title']}: попытка {attempt}/{REPEATS}")
            payload = {
                "model": model,
                "messages": mode["messages"],
                "thinking": {"type": "disabled"},
                **mode["extra"],
            }
            result = ask_until_answer(api_key, payload)
            if result["error"]:
                result["error"] = short_error(result["error"])

            parsed, note = (False, result["error"]) if result["error"] else can_parse(result["text"])
            row = {
                "mode_id": mode["id"],
                "title": mode["title"],
                "attempt": attempt,
                "text": result["text"],
                "finish_reason": result["finish_reason"],
                "error": result["error"],
                "parsed": parsed,
                "parse_note": note,
            }
            rows.append(row)
            print(f"  {note}")
            time.sleep(1)

    write_report(model, rows)
    print("Готово: day02/report.md и day02/runs.json")


if __name__ == "__main__":
    main()

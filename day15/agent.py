"""День 15. Устаревание и конфликты памяти.

Память лежит в memory.json, отдельно от диалога (dialog.json).
Правила записи задаёт код, не модель:
- скалярные факты (срок, статус, версия, требование, подход) новое значение перезаписывает;
- принятые решения накапливаются списком;
- рабочая память (задача, шаг, заметка) удаляется командой /done;
у каждого факта есть время записи.

При конфликте старое значение заменяется, а не добавляется рядом.
Факт с более поздним временем записи имеет приоритет.
Замена пишется в журнал: было → стало.
В промпт попадают только актуальные значения: устаревшие код вычищает и из истории диалога.
Извлечение фактов из реплики может делать модель, применение правил — только код.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
MEMORY_PATH = ROOT / "memory.json"
DIALOG_PATH = ROOT / "dialog.json"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
KEEP_LAST = 8

PROJECT = "Релиз оплаты заказов в интернет-магазине."

# Долговременные скаляры: новое значение заменяет старое.
SCALAR = {
    "срок": "дата сдачи или дедлайн",
    "статус": "статус задачи или релиза",
    "версия": "номер версии релиза",
    "требование": "функциональное требование; если его отменили, значение ровно «отменено»",
    "подход": "выбранный технический подход",
}

# Долговременные списки: новые пункты добавляются, старые не затираются.
LISTS = {
    "решения": "принятые решения",
}

# Рабочая память текущей задачи: исчезает по /done, долговременная при этом остаётся.
WORKING = {
    "задача": "формулировка текущей задачи",
    "шаг": "текущий шаг",
    "заметка": "черновая заметка по задаче",
}

ALL_KEYS = {**SCALAR, **LISTS, **WORKING}

BASE_SYSTEM = (
    "Ты ассистент проекта. " + PROJECT + " "
    "Ниже только актуальная память: её ведёт код. Опирайся исключительно на блок «Актуальная память». "
    "Если спрашивают текущее значение факта, назови только актуальное, без предыдущих формулировок и без слов «раньше было». "
    "Устаревшие значения из диалога код уже убрал. Не восстанавливай их и не цитируй. "
    "Если рабочей памяти нет, так и скажи. Долговременные факты после завершения задачи сохраняются."
)

EXTRACT_SYSTEM = (
    "Из сообщения пользователя извлеки факты для памяти проекта. Верни только JSON вида "
    '{"facts": [{"key": "срок", "value": "15 декабря 2026", "time": null}]}. '
    "Допустимые ключи и правила:\n"
    + "\n".join(f"- {key} (скаляр, перезапись): {hint}" for key, hint in SCALAR.items())
    + "\n"
    + "\n".join(f"- {key} (список, накапливается): {hint}" for key, hint in LISTS.items())
    + "\n"
    + "\n".join(f"- {key} (рабочая память, удаляется по /done): {hint}" for key, hint in WORKING.items())
    + "\n"
    "time оставляй null, если пользователь не назвал момент записи явно. "
    "Не выдумывай факты, не переноси старые значения из этого задания. "
    "Если требование отменяют, value должен быть ровно «отменено». "
    "Вопрос без новых данных — это {\"facts\": []}. Нельзя класть в факты сам вопрос."
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
    for name in ("DEEPSEEK_API_KEY", "LLM_MODEL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Задай DEEPSEEK_API_KEY в окружении или положи в day02/.env или day15/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


def post_chat(api_key: str, model: str, messages: list[dict], max_tokens: int = 200) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None
    body = None
    for attempt in range(4):
        request = urllib.request.Request(
            API_URL,
            data=data,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8").lstrip())
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {error.code}: {detail[:300]}")
            if error.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
        except urllib.error.URLError as error:
            last_error = RuntimeError(str(error))
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
    if body is None:
        raise last_error or RuntimeError("нет ответа")
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = body.get("usage") or {}
    return {
        "content": (message.get("content") or "").strip(),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "answer_tokens": int(usage.get("completion_tokens") or 0),
    }


def parse_json_object(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("ожидался объект JSON")
    return data


def fallback_facts(text: str) -> list[dict]:
    """Явные формулировки, если модель не вернула разбор. Код не угадывает скрытый смысл."""
    found: list[dict] = []

    def add(key: str, value: str) -> None:
        value = value.strip().rstrip(".")
        if value:
            found.append({"key": key, "value": value, "time": None})

    patterns = [
        (r"срок сдвинулся:?\s*(?:дедлайн теперь\s+)?(.+?)(?:\.|$)", "срок"),
        (r"дедлайн теперь\s+(.+?)(?:\.|$)", "срок"),
        (r"срок\s+[—–-]\s+(.+?)(?:\.|$)", "срок"),
        (r"версия релиза теперь\s+([\w.\-]+)", "версия"),
        (r"версия релиза\s+([\w.\-]+)", "версия"),
        (r"выбрали другой подход:\s*(.+?)(?:\.|$)", "подход"),
        (r"подход:\s*(.+?)(?:\.|$)", "подход"),
        (r"статус:\s*(.+?)(?:\.|$)", "статус"),
        (r"начинаем задачу:\s*(.+?)(?:\.|$)", "задача"),
        (r"требование:\s*(.+?)(?:\.|$)", "требование"),
        (r"решили\s+(.+?)(?:\.|$)", "решения"),
    ]
    if re.search(r"требование\s+про\s+.+?отменили|требование\s+отменили", text, flags=re.IGNORECASE):
        add("требование", "отменено")
    for pattern, key in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if key == "требование" and re.search(r"отменили", text, flags=re.IGNORECASE):
                continue
            add(key, value)
    return found


def merge_facts(model_facts: list[dict], text: str) -> list[dict]:
    seen = {item["key"] for item in model_facts if item.get("key") in ALL_KEYS and item.get("value")}
    merged = [item for item in model_facts if item.get("key") in ALL_KEYS and item.get("value")]
    for item in fallback_facts(text):
        if item["key"] not in seen:
            merged.append(item)
            seen.add(item["key"])
    return merged


def current_values(store: "MemoryStore") -> set[str]:
    values: set[str] = set()
    for fact in store.scalars.values():
        if fact.get("value"):
            values.add(fact["value"])
    for fact in store.working.values():
        if fact.get("value"):
            values.add(fact["value"])
    for items in store.lists.values():
        for item in items:
            if item.get("value"):
                values.add(item["value"])
    return values


def stale_values(store: "MemoryStore") -> list[str]:
    current = current_values(store)
    stale: list[str] = []
    for change in store.changes:
        old = change.get("old") or ""
        if old and old not in current and old not in stale:
            stale.append(old)
    return stale


def mentions(text: str, values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value and value in text and value not in found:
            found.append(value)
    return found


def redact(text: str, values: list[str]) -> str:
    for value in sorted(values, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, "«снято, см. актуальную память»")
    return text


def prompt_text(messages: list[dict]) -> str:
    return "\n".join(item.get("content") or "" for item in messages)


class MemoryStore:
    """Долговременная и рабочая память плюс журнал замен. Диалог сюда не пишет."""

    def __init__(self, path: Path = MEMORY_PATH) -> None:
        self.path = path
        self.scalars: dict[str, dict] = {}
        self.lists: dict[str, list[dict]] = {key: [] for key in LISTS}
        self.working: dict[str, dict] = {}
        self.changes: list[dict] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.scalars = data.get("scalars") or {}
            loaded = data.get("lists") or {}
            self.lists = {key: list(loaded.get(key) or []) for key in LISTS}
            self.working = data.get("working") or {}
            self.changes = data.get("changes") or []
        else:
            self._write()

    def get_scalar(self, key: str) -> dict | None:
        return self.scalars.get(key)

    def get_working(self, key: str) -> dict | None:
        return self.working.get(key)

    def apply(self, key: str, value: str, when: str | None = None) -> dict:
        value = value.strip()
        if not value:
            raise ValueError("Пустое значение факта")
        if key not in ALL_KEYS:
            raise ValueError(f"Неизвестный факт {key}")
        stamp = when or now_iso()
        if key in LISTS:
            return self._append(key, value, stamp)
        if key in WORKING:
            return self._put(self.working, key, value, stamp, layer="working")
        return self._put(self.scalars, key, value, stamp, layer="long")

    def apply_facts(self, facts: list[dict]) -> list[dict]:
        results = []
        for item in facts:
            key, value = item.get("key", ""), item.get("value", "")
            when = item.get("time") or None
            if when is not None and not isinstance(when, str):
                when = str(when)
            try:
                results.append(self.apply(key, str(value), when))
            except ValueError as error:
                results.append({"action": "error", "key": key, "value": value, "error": str(error)})
        return results

    def finish(self) -> dict:
        """Завершить задачу: рабочая память очищается, долговременная остаётся."""
        cleared = {key: dict(item) for key, item in self.working.items()}
        stamp = now_iso()
        for key, item in cleared.items():
            self.changes.append({"time": stamp, "key": key, "old": item["value"], "new": None})
        self.working = {}
        status = None
        if self.scalars.get("статус") and self.scalars["статус"]["value"] != "завершено":
            status = self.apply("статус", "завершено")
        else:
            self._write()
        return {"cleared": cleared, "status": status}

    def prompt_block(self) -> str:
        lines = ["Актуальная память (только текущие значения, устаревшие сюда не попадают):", "Долговременная:"]
        if not self.scalars:
            lines.append("- (пусто)")
        for key in SCALAR:
            fact = self.scalars.get(key)
            if fact:
                lines.append(f"- {key} (записано {fact['time']}): {fact['value']}")
        lines.append("Принятые решения (список накапливается):")
        items = self.lists.get("решения") or []
        if not items:
            lines.append("- (пока нет)")
        for item in items:
            lines.append(f"- {item['value']} (записано {item['time']})")
        lines.append("Рабочая память (очищается командой /done):")
        if not self.working:
            lines.append("- (пусто)")
        for key in WORKING:
            fact = self.working.get(key)
            if fact:
                lines.append(f"- {key} (записано {fact['time']}): {fact['value']}")
        return "\n".join(lines)

    def display(self) -> str:
        return self.prompt_block() + "\nЖурнал замен: " + (self.log_text() if self.changes else "записей нет")

    def log_text(self) -> str:
        if not self.changes:
            return "Журнал замен пуст."
        lines = ["Журнал замен (было → стало):"]
        for change in self.changes:
            gone = "удалено" if change["new"] is None else change["new"]
            lines.append(f"- {change['time']} · {change['key']}: было «{change['old']}» → стало «{gone}»")
        return "\n".join(lines)

    def snapshot(self) -> dict:
        return {
            "scalars": self.scalars,
            "lists": self.lists,
            "working": self.working,
            "changes": self.changes,
        }

    def _put(self, bucket: dict[str, dict], key: str, value: str, stamp: str, layer: str) -> dict:
        old = bucket.get(key)
        if old and old["time"] > stamp:
            return {
                "action": "rejected-older",
                "key": key,
                "layer": layer,
                "value": value,
                "time": stamp,
                "kept": old["value"],
                "kept_time": old["time"],
            }
        if old and old["value"] == value:
            return {"action": "unchanged", "key": key, "layer": layer, "value": value, "time": old["time"]}
        if old:
            self.changes.append({"time": stamp, "key": key, "old": old["value"], "new": value})
            action = "replaced"
        else:
            action = "set"
        bucket[key] = {"key": key, "layer": layer, "value": value, "time": stamp}
        self._write()
        return {"action": action, "key": key, "layer": layer, "value": value, "time": stamp, "old": old["value"] if old else None}

    def _append(self, key: str, value: str, stamp: str) -> dict:
        items = self.lists.setdefault(key, [])
        for item in items:
            if item["value"] == value:
                return {"action": "unchanged", "key": key, "layer": "long", "value": value, "time": item["time"]}
        items.append({"value": value, "time": stamp})
        self._write()
        return {"action": "appended", "key": key, "layer": "long", "value": value, "time": stamp}

    def _write(self) -> None:
        self.path.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")


class MemoryAgent:
    def __init__(
        self,
        llm: Callable[[list[dict], int], str],
        memory_path: Path = MEMORY_PATH,
        dialog_path: Path = DIALOG_PATH,
    ) -> None:
        self.llm = llm
        self.store = MemoryStore(memory_path)
        self.dialog_path = dialog_path
        self.history: list[dict] = []
        self.last: dict = {}
        if dialog_path.exists():
            self.history = json.loads(dialog_path.read_text(encoding="utf-8")).get("history") or []

    def handle(self, text: str, max_tokens: int = 700) -> str:
        text = text.strip()
        self.last = {"input": text, "command": text.startswith("/")}
        if text in ("/memory", "/facts"):
            reply = self.store.display()
            self.last.update({"reply": reply, "applied": [], "stale_in_prompt": [], "reply_stale": []})
            return reply
        if text == "/log":
            reply = self.store.log_text()
            self.last.update({"reply": reply, "applied": [], "stale_in_prompt": [], "reply_stale": []})
            return reply
        if text in ("/done", "/finish"):
            result = self.store.finish()
            names = ", ".join(result["cleared"]) or "не было записей"
            reply = (
                f"[код] Задача завершена. Рабочая память очищена ({names}). "
                "Долговременная память сохранена: скалярные факты, принятые решения и журнал замен."
            )
            if result["status"] and result["status"]["action"] == "replaced":
                reply += f" Статус: было «{result['status']['old']}» → стало «завершено»."
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            self._write_dialog()
            self.last.update({"reply": reply, "applied": [result["status"]] if result["status"] else [], "cleared": result["cleared"], "stale_in_prompt": [], "reply_stale": []})
            return reply
        return self._ask(text, max_tokens)

    def build_messages(self, text: str) -> list[dict]:
        stale = stale_values(self.store)
        messages = [{"role": "system", "content": BASE_SYSTEM + "\n\n" + self.store.prompt_block()}]
        for turn in self.history[-KEEP_LAST:]:
            messages.append({"role": turn["role"], "content": redact(turn["content"], stale)})
        messages.append({"role": "user", "content": redact(text, stale)})
        return messages

    def extract_facts(self, text: str) -> tuple[list[dict], str]:
        raw = ""
        model_facts: list[dict] = []
        try:
            raw = self.llm([{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": text}], 400)
            data = parse_json_object(raw)
            model_facts = data.get("facts") or []
            if not isinstance(model_facts, list):
                model_facts = []
        except (ValueError, TypeError, json.JSONDecodeError):
            model_facts = []
        return merge_facts(model_facts, text), raw

    def _ask(self, text: str, max_tokens: int) -> str:
        extracted, raw = self.extract_facts(text)
        applied = self.store.apply_facts(extracted)
        messages = self.build_messages(text)
        stale = stale_values(self.store)
        in_prompt = mentions(prompt_text(messages), stale)
        draft = self.llm(messages, max_tokens)
        first = mentions(draft, stale)
        final, retry, retry_found = draft, "", []
        if first:
            hint = (
                "В ответе есть устаревшие значения: "
                + "; ".join(first)
                + ". Перепиши ответ, опираясь только на актуальную память, и не называй предыдущие значения."
            )
            retry = self.llm(messages + [{"role": "assistant", "content": draft}, {"role": "user", "content": hint}], max_tokens)
            retry_found = mentions(retry, stale)
            final = retry
        reply_stale = mentions(final, stale)
        self.last.update(
            {
                "extract_raw": raw,
                "extracted": extracted,
                "applied": applied,
                "messages": messages,
                "prompt": prompt_text(messages),
                "stale": stale,
                "stale_in_prompt": in_prompt,
                "draft": draft,
                "draft_stale": first,
                "regenerated": bool(first),
                "retry": retry,
                "retry_stale": retry_found,
                "reply_stale": reply_stale,
                "reply": final,
            }
        )
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": final})
        self._write_dialog()
        return final

    def _write_dialog(self) -> None:
        self.dialog_path.write_text(json.dumps({"history": self.history}, ensure_ascii=False, indent=2), encoding="utf-8")


def deepseek_llm(api_key: str, model: str) -> Callable[[list[dict], int], str]:
    def call(messages: list[dict], max_tokens: int) -> str:
        return post_chat(api_key, model, messages, max_tokens=max_tokens)["content"]

    return call


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    text = " ".join(sys.argv[1:]).strip() or input("Запрос или команда: ").strip()

    def llm(messages: list[dict], max_tokens: int) -> str:
        api_key, model = load_settings()
        return deepseek_llm(api_key, model)(messages, max_tokens)

    agent = MemoryAgent(llm)
    print(agent.handle(text))
    if agent.last.get("applied"):
        print("\n[код] Память:", ", ".join(f"{item['action']} {item['key']}" for item in agent.last["applied"] if item))
    if agent.last.get("stale_in_prompt"):
        print("[код] В промпт попали устаревшие значения:", "; ".join(agent.last["stale_in_prompt"]))
    if agent.last.get("reply_stale"):
        print("[код] В ответе названы устаревшие значения:", "; ".join(agent.last["reply_stale"]))


if __name__ == "__main__":
    main()

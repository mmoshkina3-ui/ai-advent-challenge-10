"""День 11. Три слоя памяти ассистента.

short: текущий диалог, в промпт идут последние 6 реплик.
working: данные текущей задачи, в промпт идёт весь блок.
long: долговременные факты из дня 10, в промпт идёт весь блок.
Куда писать, решает явное правило в route(), а не модель.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEMORY_PATH = ROOT / "memory.json"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
SHORT_LIMIT = 6
LAYERS = ("short", "working", "long")

BASE_SYSTEM = (
    "Ты помощник проекта. Отвечай только по тем слоям памяти, которые есть в запросе. "
    "Не выдумывай имя, бюджет, задачу и коды. Если данных нет, так и скажи."
)


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
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day11/.env.")
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


def known_facts(text: str) -> dict[str, str]:
    if "?" in text:
        return {}
    found: dict[str, str] = {}
    patterns = {
        "имя": r"(?:заказчика зовут|имя(?: заказчика)?[:\s]+)\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]+)",
        "бюджет": r"бюджет(?: проекта)?[:\s]+(\d[\d ]*)",
        "срок": r"срок(?: сдачи)?[:\s]+(\d{1,2}\s+[А-ЯЁа-яё]+\s+\d{4})",
        "решение": r"(?:решение|пишем на|язык проекта)[:\s]+([A-Za-zА-Яа-яЁё]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            found[key] = match.group(1).strip()
    if re.search(r"\bpython\b|питон", text, flags=re.IGNORECASE):
        found["решение"] = "Python"
    return found


class MemoryAgent:
    def __init__(self, api_key: str, model: str, path: Path = MEMORY_PATH) -> None:
        self.api_key = api_key
        self.model = model
        self.path = path
        self.short: list[dict] = []
        self.working: dict[str, str] = {}
        self.long: dict[str, str] = {}
        self.extra_system = ""
        self.last_prompt_tokens = 0
        self._load()

    def route(self, text: str, role: str = "user") -> list[str]:
        """Явно решает, в какие слои записать реплику. Возвращает имена слоёв."""
        saved = ["short"]
        self.short.append({"role": role, "content": text})
        if role != "user" or "?" in text:
            self._write()
            return saved
        lowered = text.lower()
        if any(word in lowered for word in ("задача", "сейчас нужно", "временно", "к завтра")):
            self.working["задача"] = text.split("Ответь", 1)[0].strip()
            saved.append("working")
        facts = known_facts(text)
        if facts or "запомни" in lowered:
            self.long.update(facts)
            if "запомни" in lowered and "заметка" not in facts and not facts:
                self.long["заметка"] = text.split("Ответь", 1)[0].strip()
            if facts or "запомни" in lowered:
                saved.append("long")
        self._write()
        return saved

    def ask(self, text: str, max_tokens: int = 120) -> str:
        self.route(text)
        result = post_chat(self.api_key, self.model, self.prompt_messages(), max_tokens=max_tokens)
        self.last_prompt_tokens = result["prompt_tokens"]
        self.route(result["content"], role="assistant")
        return result["content"]

    def clear(self, layer: str) -> None:
        if layer == "short":
            self.short = []
        elif layer == "working":
            self.working = {}
        elif layer == "long":
            self.long = {}
        else:
            raise ValueError("Неизвестный слой")
        self._write()

    def prompt_messages(self, include: set[str] | None = None) -> list[dict]:
        include = include or set(LAYERS)
        blocks = [BASE_SYSTEM]
        if self.extra_system:
            blocks.append(self.extra_system)
        if "long" in include and self.long:
            blocks.append("Долговременная память:\n" + self._lines(self.long))
        if "working" in include and self.working:
            blocks.append("Рабочая память текущей задачи:\n" + self._lines(self.working))
        messages = [{"role": "system", "content": "\n\n".join(blocks)}]
        if "short" in include:
            messages.extend(self.short[-SHORT_LIMIT:])
        return messages

    def measure(self) -> dict[str, int]:
        """Сколько токенов промпта добавляет каждый слой. Ответы в память не пишутся."""
        full = post_chat(self.api_key, self.model, self.prompt_messages(), max_tokens=8)
        sizes = {"full": full["prompt_tokens"]}
        for layer in LAYERS:
            included = set(LAYERS) - {layer}
            result = post_chat(self.api_key, self.model, self.prompt_messages(included), max_tokens=8)
            sizes[layer] = full["prompt_tokens"] - result["prompt_tokens"]
        return sizes

    def snapshot(self) -> dict:
        return {
            "short": list(self.short),
            "working": dict(self.working),
            "long": dict(self.long),
        }

    def _lines(self, data: dict[str, str]) -> str:
        return "\n".join(f"{key}: {value}" for key, value in data.items())

    def _load(self) -> None:
        if not self.path.exists():
            self._write()
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.short = data.get("short") or []
        self.working = data.get("working") or {}
        self.long = data.get("long") or {}

    def _write(self) -> None:
        self.path.write_text(
            json.dumps({"short": self.short, "working": self.working, "long": self.long}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--new" in sys.argv:
        if MEMORY_PATH.exists():
            MEMORY_PATH.unlink()
        sys.argv.remove("--new")
    api_key, model = load_settings()
    agent = MemoryAgent(api_key, model)
    if "--clear" in sys.argv:
        index = sys.argv.index("--clear")
        agent.clear(sys.argv[index + 1])
        print(f"Слой очищен: {sys.argv[index + 1]}")
        return
    question = " ".join(sys.argv[1:]).strip() or input("Запрос: ").strip()
    print("Ответ:", agent.ask(question))
    print("Куда записано последнее пользовательское сообщение: смотри слои ниже")
    print(json.dumps(agent.snapshot(), ensure_ascii=False, indent=2))
    print(f"Токены последнего промпта: {agent.last_prompt_tokens}")


if __name__ == "__main__":
    main()

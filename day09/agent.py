"""День 9. Агент сжимает старую историю.

Последние KEEP_LAST сообщений уходят в запрос как есть.
Каждые BATCH более старых сообщений заменяются памяткой.
Памятка лежит отдельно и подставляется в запрос вместо полной истории.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "history.json"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
KEEP_LAST = 6
BATCH = 10
PRICE_HIT = 0.003 / 1_000_000
PRICE_MISS = 0.15 / 1_000_000
PRICE_OUT = 0.60 / 1_000_000

BASE_SYSTEM = (
    "Ты помощник проекта. Отвечай по сообщениям диалога и по памятке, если она есть. "
    "Не выдумывай имена, даты, деньги, запреты и решения. "
    "Если факта нет ни в памятке, ни в последних сообщениях, так и скажи."
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
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day09/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


def cost_usd(usage: dict) -> float:
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if not isinstance(hit, int) or not isinstance(miss, int):
        details = usage.get("prompt_tokens_details") or {}
        hit = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        miss = max(prompt - hit, 0)
    answer = int(usage.get("completion_tokens") or 0)
    return hit * PRICE_HIT + miss * PRICE_MISS + answer * PRICE_OUT


def money(value: float) -> str:
    return f"${value:.6f}"


class LimitError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def post_chat(api_key: str, model: str, messages: list[dict], max_tokens: int = 400, timeout: int = 90) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: LimitError | None = None
    body = None
    for attempt in range(4):
        request = urllib.request.Request(
            API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8").lstrip())
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = LimitError(error.code, detail)
            if error.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
        except urllib.error.URLError as error:
            last_error = LimitError(0, str(error))
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
    if body is None:
        raise last_error or LimitError(0, "нет ответа")
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "message": {"role": "assistant", "content": message.get("content") or ""},
        "usage": body.get("usage") or {},
        "finish_reason": choice.get("finish_reason") or "",
    }


class Agent:
    """Полный журнал хранится локально. В модель уходит памятка и только хвост."""

    def __init__(
        self,
        api_key: str,
        model: str,
        history_path: Path = HISTORY_PATH,
        compress: bool = True,
        keep_last: int = KEEP_LAST,
        batch: int = BATCH,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.history_path = history_path
        self.compress = compress
        self.keep_last = keep_last
        self.batch = batch
        self.summary = ""
        self.covered = 0
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self._load()

    def ask(self, user_text: str, max_tokens: int = 120) -> str:
        self.messages.append({"role": "user", "content": user_text})
        self._compress()
        result = post_chat(self.api_key, self.model, self._request_messages(), max_tokens=max_tokens)
        self._note(result, "chat")
        answer = (result["message"].get("content") or "").strip()
        self.messages.append({"role": "assistant", "content": answer})
        self._compress()
        self._write()
        return answer

    def _request_messages(self) -> list[dict]:
        system = BASE_SYSTEM
        if self.compress and self.summary:
            system += "\n\nПамятка ранней части диалога:\n" + self.summary
        tail = self.messages[self.covered :] if self.compress else self.messages
        return [{"role": "system", "content": system}, *tail]

    def _compress(self) -> None:
        if not self.compress:
            return
        while len(self.messages) - self.covered - self.keep_last >= self.batch:
            batch = self.messages[self.covered : self.covered + self.batch]
            self._fold(batch)

    def _fold(self, batch: list[dict]) -> None:
        lines = [f"{message['role']}: {message['content']}" for message in batch]
        prompt = (
            "Обнови памятку диалога. Сохрани все конкретные факты: "
            "имена, даты, деньги, числа, запреты, решения, почту, город. "
            "Не выдумывай и не выбрасывай факт только потому, что он старый. "
            "Пиши коротким списком.\n\n"
            f"Старая памятка:\n{self.summary or 'пусто'}\n\n"
            "Новые реплики:\n" + "\n".join(lines)
        )
        result = post_chat(
            self.api_key,
            self.model,
            [{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        self.summary = (result["message"].get("content") or "").strip()
        self.covered += len(batch)
        self._note(result, "summary")
        self._write()

    def _note(self, result: dict, kind: str) -> None:
        usage = result["usage"]
        request_tokens = int(usage.get("prompt_tokens") or 0)
        answer_tokens = int(usage.get("completion_tokens") or 0)
        previous = self.calls[-1]["history_tokens"] if self.calls else 0
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if not isinstance(hit, int) or not isinstance(miss, int):
            details = usage.get("prompt_tokens_details") or {}
            hit = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
            miss = max(request_tokens - hit, 0)
        self.calls.append(
            {
                "kind": kind,
                "request_tokens": request_tokens,
                "answer_tokens": answer_tokens,
                "history_tokens": previous + request_tokens,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "cost_usd": cost_usd(usage),
                "sent_messages": len(self._request_messages()) if kind == "chat" else 1,
                "covered": self.covered,
            }
        )

    def _load(self) -> None:
        if not self.history_path.exists():
            self._write()
            return
        data = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.summary = data.get("summary") or ""
        self.covered = int(data.get("covered") or 0)
        self.messages = data.get("messages") or []
        self.calls = data.get("calls") or []

    def _write(self) -> None:
        self.history_path.write_text(
            json.dumps(
                {
                    "summary": self.summary,
                    "covered": self.covered,
                    "keep_last": self.keep_last,
                    "batch": self.batch,
                    "compress": self.compress,
                    "messages": self.messages,
                    "calls": self.calls,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    compress = "--plain" not in sys.argv
    if "--plain" in sys.argv:
        sys.argv.remove("--plain")
    if "--new" in sys.argv:
        if HISTORY_PATH.exists():
            HISTORY_PATH.unlink()
        sys.argv.remove("--new")
    api_key, model = load_settings()
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        question = input("Запрос: ").strip()
    agent = Agent(api_key, model, compress=compress)
    print(f"Модель: {model}")
    print(f"Сжатие: {'да' if compress else 'нет'}, хвост {KEEP_LAST}, пачка {BATCH}")
    print(f"В памятке уже закрыто сообщений: {agent.covered}")
    answer = agent.ask(question)
    last = agent.calls[-1]
    print("Ответ:", answer)
    print(f"текущий запрос: {last['request_tokens']} токенов")
    print(f"вся история запросов: {last['history_tokens']} токенов")
    print(f"ответ модели: {last['answer_tokens']} токенов")
    print(f"стоимость: {money(last['cost_usd'])}")
    if agent.summary:
        print("Памятка:", agent.summary)


if __name__ == "__main__":
    main()

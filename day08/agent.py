"""День 8. Агент считает токены.

Каждый вызов модели даёт три числа:
- текущий запрос: prompt_tokens, всё, что ушло в модель сейчас, включая прошлый чат
- вся история: сумма таких запросов за диалог, потому что прошлое отправляется снова
- ответ модели: completion_tokens

Лимит DeepSeek: сообщения плюс max_tokens не больше 1 048 576.
Если запрос больше, API отвечает 400 и текста нет.
"""

import ast
import json
import operator
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "history.json"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
USD_TO_RUB = 90
MAX_STEPS = 6
CONTEXT_LIMIT = 1_048_576
PRICE_HIT = 0.003 / 1_000_000
PRICE_MISS = 0.15 / 1_000_000
PRICE_OUT = 0.60 / 1_000_000

SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "Ты агент с инструментами и памятью прошлого диалога. "
        "Если для ответа нужны данные, вызови подходящий инструмент. "
        "Не выдумывай время, курс и результат счёта. "
        "На длинные журналы отвечай коротко, как просит пользователь."
    ),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Возвращает текущие локальные дату и время компьютера.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Считает арифметическое выражение из чисел и знаков + - * / и скобок.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Выражение, например 17 * 23"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_usd_to_rub",
            "description": f"Переводит доллары в рубли по зашитому курсу {USD_TO_RUB} рублей за 1 доллар.",
            "parameters": {
                "type": "object",
                "properties": {"usd": {"type": "number", "description": "Сумма в долларах"}},
                "required": ["usd"],
            },
        },
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
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day08/.env.")
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


def get_current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def calculate(expression: str) -> str:
    operations = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            return operations[type(node.op)](walk(node.left), walk(node.right))
        raise ValueError("можно только числа и знаки + - * /")

    try:
        result = walk(ast.parse(expression, mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as error:
        return f"Не смог посчитать: {error}"
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)


def convert_usd_to_rub(usd: float) -> str:
    rub = float(usd) * USD_TO_RUB
    return f"{usd} USD = {rub:g} RUB по курсу {USD_TO_RUB}"


FUNCTIONS = {
    "get_current_time": lambda args: get_current_time(),
    "calculate": lambda args: calculate(str(args.get("expression", ""))),
    "convert_usd_to_rub": lambda args: convert_usd_to_rub(float(args.get("usd", 0))),
}


class LimitError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def post_chat(api_key: str, model: str, messages: list[dict], max_tokens: int = 800, timeout: int = 90) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": max_tokens,
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8").lstrip())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise LimitError(error.code, detail) from error
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    clean = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    usage = body.get("usage") or {}
    return {
        "message": clean,
        "usage": usage,
        "finish_reason": choice.get("finish_reason") or "",
    }


class Agent:
    """Хранит чат и после каждого вызова записывает токены запроса, истории и ответа."""

    def __init__(self, api_key: str, model: str, history_path: Path = HISTORY_PATH) -> None:
        self.api_key = api_key
        self.model = model
        self.history_path = history_path
        self.trace: list[str] = []
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self._load()

    def ask(self, user_text: str) -> str:
        self.trace = []
        started = len(self.calls)
        self._remember({"role": "user", "content": user_text})
        try:
            for step in range(1, MAX_STEPS + 1):
                result = post_chat(self.api_key, self.model, self.messages)
                self._note(result["usage"], result["finish_reason"])
                message = result["message"]
                self._remember(message)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    answer = (message.get("content") or "").strip()
                    self.trace.append(f"Шаг {step}: ответ без инструментов")
                    return answer
                for call in tool_calls:
                    result_text = self._run_tool(call)
                    name = call.get("function", {}).get("name", "")
                    arguments = call.get("function", {}).get("arguments", "")
                    self.trace.append(f"Шаг {step}: {name}({arguments}) -> {result_text}")
                    self._remember(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": result_text,
                        }
                    )
        except LimitError as error:
            self._drop_unanswered_user()
            self.trace.append(f"Запрос не принят: HTTP {error.status}")
            raise
        if len(self.calls) == started:
            return "Агент остановился до вызова модели."
        return "Агент остановился: слишком много вызовов инструментов."

    def new_calls_since(self, start: int) -> list[dict]:
        return self.calls[start:]

    def _note(self, usage: dict, finish_reason: str) -> None:
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
                "request_tokens": request_tokens,
                "answer_tokens": answer_tokens,
                "history_tokens": previous + request_tokens,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "cost_usd": cost_usd(usage),
                "finish_reason": finish_reason,
            }
        )
        self._write()

    def _drop_unanswered_user(self) -> None:
        if self.messages and self.messages[-1].get("role") == "user":
            self.messages.pop()
            self._write()

    def _load(self) -> None:
        if not self.history_path.exists():
            self.messages = [SYSTEM_MESSAGE]
            self.calls = []
            self._write()
            return
        data = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.messages = data.get("messages") or []
        self.calls = data.get("calls") or []
        if not self.messages or self.messages[0].get("role") != "system":
            self.messages.insert(0, SYSTEM_MESSAGE)
            self._write()

    def _remember(self, message: dict) -> None:
        self.messages.append(message)
        self._write()

    def _write(self) -> None:
        self.history_path.write_text(
            json.dumps({"messages": self.messages, "calls": self.calls}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _run_tool(self, call: dict) -> str:
        function = call.get("function") or {}
        name = function.get("name", "")
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return "Аргументы инструмента не являются JSON"
        else:
            arguments = raw_arguments
        runner = FUNCTIONS.get(name)
        if runner is None:
            return f"Неизвестный инструмент: {name}"
        try:
            return runner(arguments)
        except (TypeError, ValueError) as error:
            return f"Ошибка инструмента {name}: {error}"


def format_call(call: dict) -> list[str]:
    return [
        f"текущий запрос: {call['request_tokens']} токенов",
        f"вся история: {call['history_tokens']} токенов",
        f"ответ модели: {call['answer_tokens']} токенов",
        (
            f"стоимость хода: {money(call['cost_usd'])} "
            f"(кэш {call['cache_hit_tokens']}, новые {call['cache_miss_tokens']})"
        ),
    ]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--new" in sys.argv:
        if HISTORY_PATH.exists():
            HISTORY_PATH.unlink()
        sys.argv.remove("--new")
    api_key, model = load_settings()
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        question = input("Запрос: ").strip()
    agent = Agent(api_key, model)
    print(f"Модель: {model}")
    print(f"Лимит контекста: {CONTEXT_LIMIT} токенов, вместе с резервом ответа")
    print(f"Уже накоплено запросов: {agent.calls[-1]['history_tokens'] if agent.calls else 0}")
    print(f"Запрос: {question}")
    start = len(agent.calls)
    try:
        answer = agent.ask(question)
    except LimitError as error:
        print(f"Модель не ответила. HTTP {error.status}")
        print(error.detail[:500])
        return
    print("Ход агента:")
    for line in agent.trace:
        print(" ", line)
    for call in agent.new_calls_since(start):
        for line in format_call(call):
            print(" ", line)
    print("Ответ:", answer)


if __name__ == "__main__":
    main()

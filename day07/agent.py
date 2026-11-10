"""День 7. Агент помнит диалог между запусками.

История лежит в history.json: реплики, вызовы инструментов и их результаты.
При новом запуске агент читает файл и продолжает разговор.
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

SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "Ты агент с инструментами и памятью прошлого диалога. "
        "Если для ответа нужны данные, вызови подходящий инструмент. "
        "Не выдумывай время, курс и результат счёта. "
        "Помни имя пользователя и прошлые результаты инструментов из истории."
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
                    "expression": {
                        "type": "string",
                        "description": "Выражение, например 17 * 23",
                    }
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
                "properties": {
                    "usd": {"type": "number", "description": "Сумма в долларах"}
                },
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
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day07/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


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


class Agent:
    """Отдельная сущность: сама ходит в API, вызывает инструменты и хранит историю."""

    def __init__(self, api_key: str, model: str, history_path: Path = HISTORY_PATH) -> None:
        self.api_key = api_key
        self.model = model
        self.history_path = history_path
        self.trace: list[str] = []
        self.messages = self._load()
        self.loaded_count = sum(1 for message in self.messages if message.get("role") != "system")

    def ask(self, user_text: str) -> str:
        self.trace = []
        self._remember({"role": "user", "content": user_text})
        for step in range(1, MAX_STEPS + 1):
            message = self._complete()
            self._remember(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = message.get("content") or ""
                self.trace.append(f"Шаг {step}: ответ без инструментов")
                return answer.strip()
            for call in tool_calls:
                result = self._run_tool(call)
                name = call.get("function", {}).get("name", "")
                arguments = call.get("function", {}).get("arguments", "")
                self.trace.append(f"Шаг {step}: {name}({arguments}) -> {result}")
                self._remember(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    }
                )
        return "Агент остановился: слишком много вызовов инструментов."

    def _load(self) -> list[dict]:
        if not self.history_path.exists():
            messages = [SYSTEM_MESSAGE]
            self._write(messages)
            return messages
        data = json.loads(self.history_path.read_text(encoding="utf-8"))
        messages = data.get("messages") or []
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, SYSTEM_MESSAGE)
        return messages

    def _remember(self, message: dict) -> None:
        self.messages.append(message)
        self._write(self.messages)

    def _write(self, messages: list[dict]) -> None:
        self.history_path.write_text(
            json.dumps({"messages": messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _complete(self) -> dict:
        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": TOOLS,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 800,
        }
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8").lstrip())
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise SystemExit(f"API ответил {error.code}: {details[:400]}") from error
        message = ((body.get("choices") or [{}])[0].get("message") or {})
        clean = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            clean["tool_calls"] = message["tool_calls"]
        return clean

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
    print(f"В памяти сообщений: {agent.loaded_count}")
    print(f"Запрос: {question}")
    answer = agent.ask(question)
    print("Ход агента:")
    for line in agent.trace:
        print(" ", line)
    print("Ответ:", answer)


if __name__ == "__main__":
    main()

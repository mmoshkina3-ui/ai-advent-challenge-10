"""День 16. Агент дня 6, к которому подключены инструменты готового MCP-сервера.

Соединение — stdio с @modelcontextprotocol/server-filesystem (npx).
Список инструментов приходит от сервера, схемы уходят в запрос к модели.
Модель выбирает инструмент и аргументы, код либо вызывает локальную функцию
дня 6, либо передаёт вызов в MCP и возвращает результат в модель.
"""

from __future__ import annotations

import ast
import atexit
import asyncio
import json
import operator
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "sandbox"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
USD_TO_RUB = 90
MAX_STEPS = 8
MCP_PACKAGE = "@modelcontextprotocol/server-filesystem"

# Локальные инструменты — те же, что в дне 6.
LOCAL_TOOLS = [
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
                        "description": "Выражение, например 17 * 23 или (17 * 23) + 8",
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
    for name in ("DEEPSEEK_API_KEY", "LLM_MODEL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Задай DEEPSEEK_API_KEY в окружении или положи в day02/.env или day16/.env.")
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


LOCAL_RUNNERS: dict[str, Callable[[dict], str]] = {
    "get_current_time": lambda args: get_current_time(),
    "calculate": lambda args: calculate(str(args.get("expression", ""))),
    "convert_usd_to_rub": lambda args: convert_usd_to_rub(float(args.get("usd", 0))),
}


class McpError(RuntimeError):
    """MCP-сервер не запустился или вызов инструмента не удался."""


def npx_command() -> str:
    """На Windows npx — это npx.cmd, иначе subprocess его не находит."""
    if os.name == "nt":
        try:
            from mcp.os.win32.utilities import get_windows_executable_command

            return get_windows_executable_command("npx")
        except Exception:
            return shutil.which("npx.cmd") or shutil.which("npx") or "npx.cmd"
    return shutil.which("npx") or "npx"


def ensure_mcp() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as error:
        raise McpError(f"Пакет mcp не установлен ({error}).\n{install_hint()}") from error


def install_hint() -> str:
    return (
        "Чтобы подключить MCP-сервер, нужны две вещи:\n"
        "1) пакет Python: pip install mcp  (см. day16/requirements.txt)\n"
        "2) Node.js с командой npx, чтобы запустить готовый сервер "
        f"{MCP_PACKAGE}. На Windows поставь LTS с https://nodejs.org/ и проверь, "
        "что `npx -v` работает в том же терминале, откуда запускаешь агента или страницу.\n"
        f"Первый запуск скачает пакет: npx -y {MCP_PACKAGE} <папка-песочницы>."
    )


def mcp_to_openai(tool: Any) -> dict:
    """Схема инструмента MCP → формат tools[] для DeepSeek / OpenAI."""
    params = dict(getattr(tool, "input_schema", None) or {"type": "object", "properties": {}})
    params.pop("$schema", None)
    if "type" not in params:
        params["type"] = "object"
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or tool.name).strip(),
            "parameters": params,
        },
    }


def format_mcp_result(result: Any) -> str:
    if getattr(result, "is_error", False):
        prefix = "Ошибка MCP: "
    else:
        prefix = ""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    if not parts and getattr(result, "structured_content", None):
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return prefix + ("\n".join(parts) if parts else str(result))


def parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("аргументы инструмента не являются JSON")
        if isinstance(data, dict):
            return data
    raise ValueError("аргументы инструмента должны быть объектом")


class McpBridge:
    """Долгоживущее stdio-соединение с готовым filesystem MCP-сервером."""

    def __init__(self, sandbox: Path = SANDBOX) -> None:
        self.sandbox = Path(sandbox).resolve()
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.tools: list[Any] = []
        self.server_name = ""
        self.error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._lock = threading.Lock()

    def start(self, timeout: float = 60) -> "McpBridge":
        with self._lock:
            if self._client is not None:
                return self
            ensure_mcp()
            command = npx_command()
            if not shutil.which(command) and not Path(command).exists():
                raise McpError(
                    f"Не найден npx ({command}). {install_hint()}"
                )
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-stdio")
            self._thread.start()
            future = asyncio.run_coroutine_threadsafe(self._connect(command), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception as error:
                self.close()
                text = str(error) or error.__class__.__name__
                raise McpError(f"Не удалось установить MCP-соединение: {text}\n{install_hint()}") from error
            atexit.register(self.close)
            return self

    def list_tools(self) -> list[Any]:
        if self._client is None:
            raise McpError("MCP-соединение не установлено")
        result = self._submit(self._client.list_tools(), timeout=30)
        self.tools = list(result.tools)
        return self.tools

    def call_tool(self, name: str, arguments: dict) -> str:
        if self._client is None:
            raise McpError("MCP-соединение не установлено")
        result = self._submit(self._client.call_tool(name, arguments), timeout=60)
        return format_mcp_result(result)

    def schemas(self) -> list[dict]:
        return [mcp_to_openai(tool) for tool in self.tools]

    def summary(self) -> dict:
        return {
            "connected": self._client is not None,
            "server": self.server_name or MCP_PACKAGE,
            "sandbox": str(self.sandbox),
            "tools": [
                {
                    "name": tool.name,
                    "description": (tool.description or "").split("\n", 1)[0],
                    "source": "mcp",
                }
                for tool in self.tools
            ],
            "error": self.error,
        }

    def close(self) -> None:
        loop, stack, thread = self._loop, self._stack, self._thread
        self._client = None
        self._stack = None
        if loop and stack is not None:
            try:
                asyncio.run_coroutine_threadsafe(stack.aclose(), loop).result(timeout=8)
            except Exception:
                pass
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._loop = None
        self._thread = None

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()

    async def _connect(self, command: str) -> None:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=command,
            args=["-y", MCP_PACKAGE, str(self.sandbox)],
        )
        stack = AsyncExitStack()
        client = await stack.enter_async_context(Client(params))
        listed = await client.list_tools()
        info = getattr(client, "server_info", None)
        self.server_name = getattr(info, "name", "") or MCP_PACKAGE
        self._stack = stack
        self._client = client
        self.tools = list(listed.tools)
        self.error = None

    def _submit(self, coro: Any, timeout: float) -> Any:
        if self._loop is None:
            raise McpError("MCP-соединение не установлено")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)


_BRIDGE: McpBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def get_bridge(sandbox: Path = SANDBOX, start: bool = True) -> McpBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = McpBridge(sandbox)
        if start and _BRIDGE._client is None:
            _BRIDGE.start()
        return _BRIDGE


def build_catalog(
    mcp_tools: list[Any] | None,
    local_names: list[str] | None = None,
    mcp_names: list[str] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Схемы для модели и реестр источников (local / mcp). Модель видит только схемы."""
    tools: list[dict] = []
    sources: dict[str, str] = {}
    wanted_local = set(local_names) if local_names is not None else {item["function"]["name"] for item in LOCAL_TOOLS}
    for item in LOCAL_TOOLS:
        name = item["function"]["name"]
        if name in wanted_local:
            tools.append(item)
            sources[name] = "local"
    if mcp_tools:
        wanted_mcp = set(mcp_names) if mcp_names is not None else None
        for tool in mcp_tools:
            if wanted_mcp is not None and tool.name not in wanted_mcp:
                continue
            if tool.name in sources:
                continue
            tools.append(mcp_to_openai(tool))
            sources[tool.name] = "mcp"
    return tools, sources


class Agent:
    def __init__(
        self,
        api_key: str,
        model: str,
        tools: list[dict],
        sources: dict[str, str],
        bridge: McpBridge | None = None,
        sandbox: Path = SANDBOX,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.tools = tools
        self.sources = sources
        self.bridge = bridge
        self.sandbox = Path(sandbox).resolve()
        local = ", ".join(name for name, source in sources.items() if source == "local") or "нет"
        mcp = ", ".join(name for name, source in sources.items() if source == "mcp") or "нет"
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "Ты агент с инструментами. Если для ответа нужны данные, вызови подходящий инструмент. "
                    "Не выдумывай время, курс, результат счёта и содержимое файлов. "
                    "После результата инструмента ответь пользователю обычным текстом. "
                    f"Локальные инструменты: {local}. "
                    f"Инструменты внешнего MCP-сервера filesystem: {mcp}. "
                    f"Песочница MCP (только её можно читать и писать): {self.sandbox}. "
                    "Для файловых инструментов передавай полный путь внутри этой папки."
                ),
            }
        ]
        self.trace: list[str] = []
        self.calls: list[dict] = []
        self.payloads: list[dict] = []
        self.last_payload: dict | None = None
        self.answer = ""

    def ask(self, user_text: str) -> str:
        self.trace = []
        self.calls = []
        self.payloads = []
        self.last_payload = None
        self.messages.append({"role": "user", "content": user_text})
        for step in range(1, MAX_STEPS + 1):
            message = self._complete()
            self.messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = (message.get("content") or "").strip()
                self.trace.append(f"Шаг {step}: ответ без инструментов")
                self.answer = answer
                return answer
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                raw_arguments = function.get("arguments") or "{}"
                source = self.sources.get(name, "unknown")
                result = self._run_tool(name, raw_arguments)
                self.trace.append(f"Шаг {step}: [{source}] {name}({raw_arguments}) -> {result}")
                self.calls.append(
                    {
                        "step": step,
                        "name": name,
                        "source": source,
                        "arguments": raw_arguments,
                        "result": result,
                    }
                )
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    }
                )
        self.answer = "Агент остановился: слишком много вызовов инструментов."
        return self.answer

    def _complete(self) -> dict:
        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 800,
        }
        self.last_payload = payload
        self.payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
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
        except urllib.error.URLError as error:
            raise SystemExit(f"Сеть: {error}") from error
        message = ((body.get("choices") or [{}])[0].get("message") or {})
        clean = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            clean["tool_calls"] = message["tool_calls"]
        return clean

    def _run_tool(self, name: str, raw_arguments: Any) -> str:
        source = self.sources.get(name)
        try:
            arguments = parse_arguments(raw_arguments)
        except ValueError as error:
            return f"Ошибка аргументов {name}: {error}"
        if source == "local":
            runner = LOCAL_RUNNERS.get(name)
            if runner is None:
                return f"Неизвестный локальный инструмент: {name}"
            try:
                return runner(arguments)
            except (TypeError, ValueError) as error:
                return f"Ошибка инструмента {name}: {error}"
        if source == "mcp":
            if self.bridge is None:
                return "MCP-соединение не установлено"
            try:
                return self.bridge.call_tool(name, arguments)
            except Exception as error:
                return f"Ошибка MCP {name}: {error}"
        return f"Неизвестный инструмент: {name}"


def open_agent(
    mixed: bool = False,
    bridge: McpBridge | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> Agent:
    if api_key is None or model is None:
        loaded_key, loaded_model = load_settings()
        api_key = api_key or loaded_key
        model = model or loaded_model
    if bridge is None:
        bridge = get_bridge()
    if mixed:
        tools, sources = build_catalog(bridge.tools, local_names=["calculate"], mcp_names=["read_text_file"])
    else:
        tools, sources = build_catalog(bridge.tools)
    return Agent(api_key, model, tools, sources, bridge=bridge, sandbox=bridge.sandbox)


def payload_for_print(payload: dict | None) -> dict:
    """Тело запроса к модели без ключа и заголовков — их там и так нет, это страховка."""
    if not payload:
        return {}
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    data.pop("api_key", None)
    data.pop("Authorization", None)
    return data


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    mixed = "--mixed" in sys.argv
    args = [item for item in sys.argv[1:] if item != "--mixed"]
    question = " ".join(args).strip()
    if not question:
        question = input("Запрос: ").strip()
    started = time.time()
    bridge = get_bridge()
    print("MCP-соединение:", "установлено" if bridge._client else "нет")
    print("Сервер:", bridge.server_name, "песочница:", bridge.sandbox)
    print("Инструменты MCP:", ", ".join(tool.name for tool in bridge.tools) or "—")
    agent = open_agent(mixed=mixed, bridge=bridge)
    print("В запросе к модели:", ", ".join(f"{name} [{source}]" for name, source in agent.sources.items()))
    print(f"Запрос: {question}")
    answer = agent.ask(question)
    print("Ход агента:")
    for line in agent.trace:
        print(" ", line)
    print("Ответ:", answer)
    if mixed and agent.last_payload:
        print("\nТело запроса к модели (без ключа и заголовков):")
        print(json.dumps(payload_for_print(agent.last_payload), ensure_ascii=False, indent=2))
    print(f"Секунд: {time.time() - started:.1f}")


if __name__ == "__main__":
    main()

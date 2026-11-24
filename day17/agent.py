"""День 17. Свой MCP-сервер и агент, который вызывает его инструмент.

Клиент и диспетчер — те же, что в дне 16: соединение по stdio, схемы инструментов
в запросе к модели, модель выбирает имя и аргументы, код вызывает MCP и возвращает
результат. Отличие: сервер свой, запускается тем же интерпретатором Python
(`sys.executable`), без npx. Инструменты ходят в локальный HTTP API трекера заказов.
"""

from __future__ import annotations

import atexit
import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "server.py"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
MAX_STEPS = 8


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
        raise SystemExit("Нет ключа. Задай DEEPSEEK_API_KEY в окружении или положи в day02/.env или day17/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


class McpError(RuntimeError):
    """MCP-сервер не запустился или вызов инструмента не удался."""


def install_hint() -> str:
    return (
        "Чтобы запустить свой MCP-сервер дня 17, нужен пакет Python: pip install -U \"mcp>=2\". "
        "Сервер стартует тем же интерпретатором, что и агент (sys.executable), "
        "и сам поднимает локальный HTTP API трекера заказов. Node.js не нужен."
    )


def ensure_mcp() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as error:
        raise McpError(f"Пакет mcp не установлен ({error}).\n{install_hint()}") from error


def mcp_to_openai(tool: Any) -> dict:
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


def tool_schema_view(tool: Any) -> dict:
    schema = mcp_to_openai(tool)
    function = schema["function"]
    params = function.get("parameters") or {}
    return {
        "name": function["name"],
        "description": function["description"],
        "parameters": params,
        "required": params.get("required") or [],
        "properties": params.get("properties") or {},
        "source": "mcp",
    }


class McpBridge:
    """Долгоживущее stdio-соединение со своим MCP-сервером (sys.executable server.py)."""

    def __init__(self, command: str | None = None, args: list[str] | None = None) -> None:
        self.command = command or sys.executable
        self.args = args if args is not None else [str(SERVER_PATH)]
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
            if not Path(self.command).exists() and not self.command:
                raise McpError(f"Не найден интерпретатор для MCP-сервера.\n{install_hint()}")
            if not SERVER_PATH.exists():
                raise McpError(f"Нет файла сервера: {SERVER_PATH}")
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-stdio-day17")
            self._thread.start()
            future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
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
            "server": self.server_name or "order-tracker",
            "command": self.command,
            "args": self.args,
            "tools": [tool_schema_view(tool) for tool in self.tools],
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

    async def _connect(self) -> None:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            cwd=str(ROOT),
        )
        stack = AsyncExitStack()
        client = await stack.enter_async_context(Client(params))
        listed = await client.list_tools()
        info = getattr(client, "server_info", None)
        self.server_name = getattr(info, "name", "") or "order-tracker"
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


def get_bridge(start: bool = True) -> McpBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = McpBridge()
        if start and _BRIDGE._client is None:
            _BRIDGE.start()
        return _BRIDGE


def build_catalog(mcp_tools: list[Any] | None) -> tuple[list[dict], dict[str, str]]:
    tools: list[dict] = []
    sources: dict[str, str] = {}
    for tool in mcp_tools or []:
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
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.tools = tools
        self.sources = sources
        self.bridge = bridge
        names = ", ".join(self.sources) or "нет"
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "Ты агент с инструментами своего MCP-сервера «трекер заказов». "
                    "Если нужны данные о заказе, вызови инструмент. Не выдумывай статус, сумму и состав заказа. "
                    "После результата инструмента ответь пользователю обычным текстом и опирайся только на него. "
                    f"Доступные инструменты: {names}."
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
        if source != "mcp" or self.bridge is None:
            return f"Неизвестный инструмент: {name}"
        try:
            return self.bridge.call_tool(name, arguments)
        except Exception as error:
            return f"Ошибка MCP {name}: {error}"


def open_agent(
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
    tools, sources = build_catalog(bridge.tools)
    return Agent(api_key, model, tools, sources, bridge=bridge)


def payload_for_print(payload: dict | None) -> dict:
    if not payload:
        return {}
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    data.pop("api_key", None)
    data.pop("Authorization", None)
    return data


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        question = input("Запрос: ").strip()
    started = time.time()
    bridge = get_bridge()
    print("MCP-соединение:", "установлено" if bridge._client else "нет")
    print("Сервер:", bridge.server_name, "команда:", bridge.command)
    print("Инструменты:")
    for tool in bridge.summary()["tools"]:
        required = ", ".join(tool["required"]) or "—"
        print(f"  {tool['name']}  обязательные: {required}  — {tool['description'][:120]}")
    agent = open_agent(bridge=bridge)
    print(f"Запрос: {question}")
    answer = agent.ask(question)
    print("Ход агента:")
    for line in agent.trace:
        print(" ", line)
    print("Ответ:", answer)
    print(f"Секунд: {time.time() - started:.1f}")


if __name__ == "__main__":
    main()

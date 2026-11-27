"""День 19. Агент и клиент MCP для пайплайна search → summarize → save_to_file.

Клиент и диспетчер — те же, что в днях 16–18: соединение по stdio, схемы
инструментов в запросе к модели, модель выбирает имя и аргументы, код вызывает
MCP. Сервер свой, запускается тем же интерпретатором Python (`sys.executable`).
Цепочка выполняется оркестратором автоматически и только через MCP. Диспетчер
не даёт вызвать следующий инструмент после пустого результата или ошибки.
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import corpus  # noqa: E402
from pipeline import (  # noqa: E402
    PipelineGuard,
    looks_invented,
    run_pipeline,
    safe_answer,
)

SERVER_PATH = ROOT / "server.py"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
MAX_STEPS = 8


def load_settings() -> tuple[str, str]:
    corpus.load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Задай DEEPSEEK_API_KEY в окружении или положи в day02/.env или day19/.env.")
    return api_key, os.environ.get("LLM_MODEL") or DEFAULT_MODEL


class McpError(RuntimeError):
    """MCP-сервер не запустился или вызов инструмента не удался."""


def install_hint() -> str:
    return (
        "Чтобы запустить MCP-сервер дня 19, нужен пакет Python: pip install -U \"mcp>=2\". "
        "Сервер стартует тем же интерпретатором, что и агент (sys.executable). "
        "Внешний API не нужен: корпус заметок локальный, результаты пишутся в папку out."
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

    def __init__(self, command: str | None = None, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command or sys.executable
        self.args = args if args is not None else [str(SERVER_PATH)]
        self.env = env
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
            corpus.load_env()
            if not Path(self.command).exists() and not self.command:
                raise McpError(f"Не найден интерпретатор для MCP-сервера.\n{install_hint()}")
            if not SERVER_PATH.exists():
                raise McpError(f"Нет файла сервера: {SERVER_PATH}")
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-stdio-day19")
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
            "server": self.server_name or "notes-pipeline",
            "command": self.command,
            "args": self.args,
            "out": str(corpus.output_dir()),
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

        env = dict(self.env) if self.env is not None else dict(os.environ)
        env.setdefault("PIPELINE_OUT", str(corpus.output_dir()))
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            cwd=str(ROOT),
            env=env,
        )
        stack = AsyncExitStack()
        client = await stack.enter_async_context(Client(params))
        listed = await client.list_tools()
        info = getattr(client, "server_info", None)
        self.server_name = getattr(info, "name", "") or "notes-pipeline"
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


def reset_bridge(env: dict[str, str] | None = None) -> McpBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is not None:
            _BRIDGE.close()
            _BRIDGE = None
        _BRIDGE = McpBridge(env=env)
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
                    "Ты агент пайплайна из MCP-инструментов: search, summarize, save_to_file. "
                    "Цепочка идёт строго по шагам. Если search вернул пустой список, нельзя вызывать "
                    "summarize и save_to_file. Если summarize вернул ошибку, нельзя вызывать save_to_file. "
                    "Не выдавай итоговую сводку, которой нет. Если цепочка остановилась, скажи шаг "
                    "остановки и причину, перечисли инструменты, которые не вызывались, и явно напиши, "
                    "что итогового результата нет. Не выдумывай найденные заметки, сводку и путь файла. "
                    f"Доступные инструменты: {names}."
                ),
            }
        ]
        self.trace: list[str] = []
        self.calls: list[dict] = []
        self.payloads: list[dict] = []
        self.last_payload: dict | None = None
        self.answer = ""
        self.pipeline: dict[str, Any] | None = None
        self.model_answer = ""
        self.guard = PipelineGuard()

    def ask(self, user_text: str) -> str:
        self.trace = []
        self.calls = []
        self.payloads = []
        self.last_payload = None
        self.guard = PipelineGuard()
        self.pipeline = None
        self.model_answer = ""
        self.messages.append({"role": "user", "content": user_text})
        for step in range(1, MAX_STEPS + 1):
            message = self._complete()
            self.messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = (message.get("content") or "").strip()
                self.trace.append(f"Шаг {step}: ответ без инструментов")
                self.model_answer = answer
                if self.guard.stopped_at and looks_invented(answer, _guard_as_result(self.guard)):
                    answer = safe_answer(_guard_as_result(self.guard), answer)
                    self.trace.append("Ответ заменён: модель не должна выдавать итог, которого нет.")
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

    def run(self, query: str, filename: str | None = None) -> str:
        """Автоматически выполняет цепочку через MCP, затем просит модель описать результат."""
        if self.bridge is None:
            raise McpError("Нет MCP-соединения для пайплайна")
        self.trace = []
        self.calls = []
        self.payloads = []
        self.last_payload = None
        self.guard = PipelineGuard()
        self.model_answer = ""
        result = run_pipeline(self.bridge.call_tool, query, filename=filename)
        self.pipeline = result
        for item in result["steps"]:
            if item.get("called"):
                self.trace.append(
                    f"Пайплайн: [mcp] {item['name']}({json.dumps(item.get('arguments') or {}, ensure_ascii=False)}) "
                    f"-> {item.get('result')}"
                )
                self.calls.append(
                    {
                        "step": len(self.calls) + 1,
                        "name": item["name"],
                        "source": "mcp",
                        "arguments": json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                        "result": item.get("result"),
                        "status": item.get("status"),
                    }
                )
            else:
                self.trace.append(f"Пайплайн: {item['name']} не вызван — {item.get('result')}")
        if result.get("skipped"):
            self.trace.append(
                "Остановка: шаг "
                + str(result.get("stopped_at"))
                + ", причина: "
                + str(result.get("reason"))
                + ". Не вызывались: "
                + ", ".join(result["skipped"])
            )
        prompt = _report_prompt(query, result)
        self.messages.append({"role": "user", "content": prompt})
        message = self._complete()
        self.messages.append(message)
        model_answer = (message.get("content") or "").strip()
        if message.get("tool_calls"):
            # Модель не должна заново гонять инструменты: цепочка уже выполнена.
            model_answer = (message.get("content") or "").strip() or result["answer"]
            self.trace.append("Модель попыталась вызвать инструменты после пайплайна — ответ берём из результата цепочки, если она выдумывает итог.")
        self.model_answer = model_answer
        answer = safe_answer(result, model_answer)
        if answer != model_answer:
            self.trace.append("Ответ заменён: модель не должна выдавать итог, которого нет.")
        self.trace.append("Шаг отчёта: ответ модели по результату пайплайна")
        self.answer = answer
        return answer

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
        refused = self.guard.allow(name, arguments)
        if refused:
            self.trace.append(f"Диспетчер заблокировал {name}: {refused}")
            return refused
        try:
            result = self.bridge.call_tool(name, arguments)
        except Exception as error:
            result = f"Ошибка MCP {name}: {error}"
        self.guard.observe(name, result)
        return result


def _guard_as_result(guard: PipelineGuard) -> dict[str, Any]:
    skipped = []
    if guard.stopped_at == "search":
        skipped = ["summarize", "save_to_file"]
    elif guard.stopped_at == "summarize":
        skipped = ["save_to_file"]
    reason = guard.reason or "цепочка остановлена"
    from pipeline import stop_answer

    return {
        "ok": False,
        "stopped_at": guard.stopped_at,
        "reason": reason,
        "skipped": skipped,
        "saved": guard.saved if (guard.saved or {}).get("ok") else None,
        "answer": stop_answer(guard.stopped_at or "search", reason, skipped),
    }


def _report_prompt(query: str, result: dict[str, Any]) -> str:
    snapshot = {
        "ok": result.get("ok"),
        "query": query,
        "called": result.get("called"),
        "skipped": result.get("skipped"),
        "stopped_at": result.get("stopped_at"),
        "reason": result.get("reason"),
        "final": result.get("final"),
        "search_count": (result.get("search") or {}).get("count"),
        "summary": (result.get("summary") or {}).get("summary") if result.get("ok") else None,
        "saved": result.get("saved") if result.get("ok") else None,
    }
    body = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if result.get("ok"):
        return (
            "Пайплайн уже выполнен автоматически через MCP. Вот его результат. "
            "Опиши, какие шаги выполнились, что передали из search в summarize и куда сохранили сводку. "
            "Не выдумывай факты вне этого JSON.\n\n"
            f"{body}"
        )
    return (
        "Пайплайн уже выполнен автоматически через MCP и остановился. "
        "Не выдавай сводку или сохранённый файл, которых нет. "
        "Назови шаг остановки, причину и какие инструменты не вызывались. "
        "Явно напиши, что итогового результата нет.\n\n"
        f"{body}"
    )


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
    filename = None
    lowered = question.lower()
    if "pipeline-success.md" in lowered:
        filename = "pipeline-success.md"
    elif "pipeline-empty.md" in lowered:
        filename = "pipeline-empty.md"
    elif "pipeline-fail.md" in lowered:
        filename = "pipeline-fail.md"
    answer = agent.run(question, filename=filename)
    print("Ход:")
    for line in agent.trace:
        print(" ", line)
    print("Ответ:", answer)
    print(f"Секунд: {time.time() - started:.1f}")


if __name__ == "__main__":
    main()

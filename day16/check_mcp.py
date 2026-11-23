"""Соединение с MCP, задача на чужой инструмент и смешанный набор.

python check_mcp.py  настоящая модель, пишет runs.json
"""

import json
import sys
from pathlib import Path

from agent import (
    SANDBOX,
    get_bridge,
    load_settings,
    open_agent,
    payload_for_print,
)

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

MCP_ONLY = (
    "Посмотри инструменты и файлы в песочнице MCP. "
    "Какие файлы там есть и что написано в notes.txt? "
    "Не выдумывай содержимое, прочитай файл инструментом."
)
MIXED = (
    "Прочитай файл order.txt из песочницы MCP и посчитай итог заказа: "
    "количество позиций умножить на цену за штуку и прибавить доставку. "
    "Для чтения файла возьми инструмент MCP, для счёта — calculate. "
    "В ответе назови итог числом."
)


def snapshot_payload(payload: dict | None) -> dict:
    data = payload_for_print(payload)
    # Для отчёта сохраняем и краткую выжимку схем.
    tools = []
    for item in data.get("tools") or []:
        function = item.get("function") or {}
        tools.append(
            {
                "name": function.get("name"),
                "description": (function.get("description") or "")[:200],
                "parameters": function.get("parameters"),
            }
        )
    return data | {"tool_excerpt": tools}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    print("Подключаюсь к MCP-серверу...")
    bridge = get_bridge()
    names = [tool.name for tool in bridge.tools]
    print("Соединение установлено:", bool(bridge._client))
    print("Сервер:", bridge.server_name)
    print("Песочница:", bridge.sandbox)
    print("Инструменты MCP:", ", ".join(names))

    mcp_agent = open_agent(mixed=False, bridge=bridge, api_key=api_key, model=model)
    print("\n=== Задача только через MCP ===")
    print("Запрос:", MCP_ONLY)
    mcp_answer = mcp_agent.ask(MCP_ONLY)
    for line in mcp_agent.trace:
        print(" ", line)
    print("Ответ:", mcp_answer)

    mixed_agent = open_agent(mixed=True, bridge=bridge, api_key=api_key, model=model)
    print("\n=== Смешанный набор: calculate + read_text_file ===")
    print("Инструменты в запросе:", ", ".join(f"{name} [{source}]" for name, source in mixed_agent.sources.items()))
    print("Запрос:", MIXED)
    mixed_answer = mixed_agent.ask(MIXED)
    for line in mixed_agent.trace:
        print(" ", line)
    print("Ответ:", mixed_answer)
    print("\nТело первого запроса к модели (без ключа и заголовков):")
    first_payload = mixed_agent.payloads[0] if mixed_agent.payloads else mixed_agent.last_payload
    printed = payload_for_print(first_payload)
    print(json.dumps(printed, ensure_ascii=False, indent=2))

    used_mcp = [item for item in mcp_agent.calls if item["source"] == "mcp"]
    mixed_sources = {item["source"] for item in mixed_agent.calls}
    payload = {
        "model": model,
        "connection": {
            "ok": bridge._client is not None,
            "server": bridge.server_name,
            "package": "@modelcontextprotocol/server-filesystem",
            "sandbox": str(bridge.sandbox),
            "tools": names,
            "tool_count": len(names),
        },
        "mcp_only": {
            "input": MCP_ONLY,
            "trace": mcp_agent.trace,
            "calls": mcp_agent.calls,
            "answer": mcp_answer,
            "used_external_tool": bool(used_mcp),
            "mcp_tools_called": [item["name"] for item in used_mcp],
        },
        "mixed": {
            "input": MIXED,
            "tools_in_request": mixed_agent.sources,
            "trace": mixed_agent.trace,
            "calls": mixed_agent.calls,
            "answer": mixed_answer,
            "used_local": "local" in mixed_sources,
            "used_mcp": "mcp" in mixed_sources,
            "expected_total": 399,
            "mentions_399": "399" in mixed_answer or any("399" in str(item.get("result")) for item in mixed_agent.calls),
            "request_body": snapshot_payload(first_payload),
        },
    }
    RUNS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nСоединение:", payload["connection"]["ok"], "инструментов:", payload["connection"]["tool_count"])
    print("MCP-задача, внешний инструмент вызван:", payload["mcp_only"]["used_external_tool"], payload["mcp_only"]["mcp_tools_called"])
    print("Смешанная: local =", payload["mixed"]["used_local"], "mcp =", payload["mixed"]["used_mcp"], "399:", payload["mixed"]["mentions_399"])
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

"""Список инструментов, прямой вызов и прогон с настоящей моделью.

python check_tracker.py  настоящая модель, пишет runs.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent import get_bridge, load_settings, open_agent, payload_for_print

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

QUESTION = (
    "Какой статус у заказа ORD-1001 и какая у него сумма? "
    "Не выдумывай данные, возьми их инструментом трекера."
)


def snapshot_payload(payload: dict | None) -> dict:
    data = payload_for_print(payload)
    tools = []
    for item in data.get("tools") or []:
        function = item.get("function") or {}
        tools.append(
            {
                "name": function.get("name"),
                "description": (function.get("description") or "")[:240],
                "parameters": function.get("parameters"),
            }
        )
    return data | {"tool_excerpt": tools}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    print("Подключаюсь к своему MCP-серверу...")
    bridge = get_bridge()
    names = [tool.name for tool in bridge.tools]
    schemas = bridge.summary()["tools"]
    print("Соединение установлено:", bool(bridge._client))
    print("Сервер:", bridge.server_name)
    print("Команда:", bridge.command)
    print("Инструменты:")
    for tool in schemas:
        required = ", ".join(tool["required"]) or "—"
        print(f"  {tool['name']}  обязательные: {required}")
        for name, prop in tool["properties"].items():
            print(f"    {name}: {prop.get('type') or prop.get('anyOf')} — {prop.get('description', '')[:140]}")

    print("\n=== Прямой вызов get_order(ORD-1001) ===")
    direct = bridge.call_tool("get_order", {"order_id": "ORD-1001"})
    print(direct)
    direct_payload = json.loads(direct)
    direct_ok = (
        direct_payload.get("order", {}).get("status") == "оплачен"
        and direct_payload.get("order", {}).get("total") == 399
    )

    agent = open_agent(bridge=bridge, api_key=api_key, model=model)
    print("\n=== Задача модели ===")
    print("Запрос:", QUESTION)
    answer = agent.ask(QUESTION)
    for line in agent.trace:
        print(" ", line)
    print("Ответ:", answer)

    used = [item for item in agent.calls if item["name"] == "get_order"]
    args_ok = any("ORD-1001" in str(item.get("arguments")) for item in used)
    result_ok = any("399" in str(item.get("result")) and "оплачен" in str(item.get("result")) for item in used)
    answer_uses = "399" in answer and "оплачен" in answer.lower()

    first_payload = agent.payloads[0] if agent.payloads else agent.last_payload
    record = {
        "model": model,
        "connection": {
            "ok": bridge._client is not None,
            "server": bridge.server_name,
            "command": bridge.command,
            "args": bridge.args,
            "tools": names,
            "tool_count": len(names),
            "schemas": schemas,
        },
        "direct_call": {
            "tool": "get_order",
            "arguments": {"order_id": "ORD-1001"},
            "result": direct_payload,
            "ok": direct_ok,
        },
        "agent": {
            "input": QUESTION,
            "trace": agent.trace,
            "calls": agent.calls,
            "answer": answer,
            "chose_get_order": bool(used),
            "correct_args": args_ok,
            "tool_returned_order": result_ok,
            "answer_uses_result": answer_uses,
            "request_body": snapshot_payload(first_payload),
        },
    }
    RUNS_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nСоединение:", record["connection"]["ok"], "инструментов:", record["connection"]["tool_count"])
    print("Прямой вызов:", record["direct_call"]["ok"])
    print(
        "Модель выбрала get_order:",
        record["agent"]["chose_get_order"],
        "аргументы:",
        record["agent"]["correct_args"],
        "ответ опирается на результат:",
        record["agent"]["answer_uses_result"],
    )
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

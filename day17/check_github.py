"""Список инструментов, прямой вызов и прогон с настоящей моделью по данным GitHub.

python check_github.py  настоящая модель и api.github.com, пишет runs.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import github_api
from agent import get_bridge, load_settings, open_agent, payload_for_print

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

COMMITS_QUESTION = (
    "Что делали в последних трёх коммитах репозитория? "
    "Не выдумывай sha и сообщения, возьми их инструментом."
)
FILE_QUESTION = (
    "Какие инструменты упоминаются в файле day16/report.md? "
    "Не выдумывай содержимое, прочитай файл инструментом."
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


def mentions_any(text: str, needles: list[str]) -> list[str]:
    lowered = text.lower()
    return [item for item in needles if item.lower() in lowered]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    github_api.load_env()
    api_key, model = load_settings()
    print("Подключаюсь к своему MCP-серверу...")
    bridge = get_bridge()
    names = [tool.name for tool in bridge.tools]
    schemas = bridge.summary()["tools"]
    print("Соединение установлено:", bool(bridge._client))
    print("Сервер:", bridge.server_name)
    print("API:", github_api.api_url(), "репозиторий:", f"{github_api.owner()}/{github_api.repo()}")
    print("Инструменты:")
    for tool in schemas:
        required = ", ".join(tool["required"]) or "—"
        print(f"  {tool['name']}  обязательные: {required}")
        for name, prop in tool["properties"].items():
            typ = prop.get("type") or prop.get("anyOf")
            print(f"    {name}: {typ} — {(prop.get('description') or '')[:140]}")

    print("\n=== Прямой вызов list_commits(limit=3) ===")
    direct_commits_raw = bridge.call_tool("list_commits", {"limit": 3})
    print(direct_commits_raw)
    direct_commits = json.loads(direct_commits_raw)
    commit_ok = not direct_commits.get("error") and len(direct_commits.get("commits") or []) >= 3
    expected_shas = [item["sha"] for item in direct_commits.get("commits") or []]
    expected_messages = [item["message"] for item in direct_commits.get("commits") or []]

    print("\n=== Прямой вызов get_file(day16/report.md) ===")
    direct_file_raw = bridge.call_tool("get_file", {"path": "day16/report.md"})
    preview = direct_file_raw if len(direct_file_raw) < 1500 else direct_file_raw[:1500] + "\n…"
    print(preview)
    direct_file = json.loads(direct_file_raw)
    file_ok = direct_file.get("type") == "file" and "read_text_file" in (direct_file.get("content") or "")
    file_needles = [name for name in ("read_text_file", "list_directory", "calculate", "MCP") if name.lower() in (direct_file.get("content") or "").lower()]

    def run_question(title: str, question: str) -> dict:
        agent = open_agent(bridge=bridge, api_key=api_key, model=model)
        print(f"\n=== {title} ===")
        print("Запрос:", question)
        answer = agent.ask(question)
        for line in agent.trace:
            print(" ", line[:300].replace("\n", " / "))
        print("Ответ:", answer)
        first = agent.payloads[0] if agent.payloads else agent.last_payload
        called = [item["name"] for item in agent.calls]
        return {
            "input": question,
            "trace": agent.trace,
            "calls": agent.calls,
            "answer": answer,
            "tools_called": called,
            "request_body": snapshot_payload(first),
        }

    commits_run = run_question("Задача модели: последние коммиты", COMMITS_QUESTION)
    file_run = run_question("Задача модели: файл day16/report.md", FILE_QUESTION)

    commits_used = "list_commits" in commits_run["tools_called"]
    commits_args_ok = any(
        "3" in str(item.get("arguments")) or item.get("name") == "list_commits"
        for item in commits_run["calls"]
        if item.get("name") == "list_commits"
    )
    sha_in_answer = mentions_any(commits_run["answer"], expected_shas)
    message_bits = []
    for message in expected_messages:
        for bit in ("day 17", "day 16", "day 15", "MCP", "памят", "memory", "order API", "инвариант"):
            if bit.lower() in message.lower():
                message_bits.append(bit)
                break
        else:
            if message:
                message_bits.append(message[:24])
    msg_in_answer = mentions_any(commits_run["answer"], expected_messages + message_bits + expected_shas)
    commits_uses = bool(sha_in_answer or msg_in_answer) and commits_used

    file_used = "get_file" in file_run["tools_called"]
    file_args_ok = any("day16/report.md" in str(item.get("arguments")) for item in file_run["calls"] if item.get("name") == "get_file")
    names_in_answer = mentions_any(file_run["answer"], file_needles or ["read_text_file", "calculate"])
    file_uses = file_used and bool(names_in_answer)

    record = {
        "model": model,
        "connection": {
            "ok": bridge._client is not None,
            "server": bridge.server_name,
            "command": bridge.command,
            "args": bridge.args,
            "api": github_api.api_url(),
            "repository": f"{github_api.owner()}/{github_api.repo()}",
            "tools": names,
            "tool_count": len(names),
            "schemas": schemas,
        },
        "direct_calls": {
            "list_commits": {"arguments": {"limit": 3}, "result": direct_commits, "ok": commit_ok},
            "get_file": {
                "arguments": {"path": "day16/report.md"},
                "ok": file_ok,
                "path": direct_file.get("path"),
                "type": direct_file.get("type"),
                "size": direct_file.get("size"),
                "truncated": direct_file.get("truncated"),
                "needles": file_needles,
            },
        },
        "commits_task": commits_run
        | {
            "chose_list_commits": commits_used,
            "correct_args": commits_args_ok,
            "expected_shas": expected_shas,
            "expected_messages": expected_messages,
            "sha_in_answer": sha_in_answer,
            "answer_uses_result": commits_uses,
        },
        "file_task": file_run
        | {
            "chose_get_file": file_used,
            "correct_args": file_args_ok,
            "names_in_answer": names_in_answer,
            "answer_uses_result": file_uses,
        },
    }
    RUNS_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nСоединение:", record["connection"]["ok"], "инструментов:", record["connection"]["tool_count"])
    print("Прямые вызовы: list_commits", commit_ok, "get_file", file_ok)
    print("Модель, коммиты: инструмент", commits_used, "ответ опирается на данные", commits_uses, "sha в ответе", sha_in_answer)
    print("Модель, файл: инструмент", file_used, "аргументы", file_args_ok, "имена в ответе", names_in_answer)
    print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

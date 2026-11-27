"""Три прогона пайплайна с моделью: успех, пустой search и ошибка summarize.

python check_pipeline.py  deepseek-flash, пишет runs.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from agent import load_settings, open_agent, payload_for_print, reset_bridge
from pipeline import SCENARIOS, looks_invented, safe_answer

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"


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


def slim_pipeline(result: dict) -> dict:
    search = result.get("search") or {}
    hits = []
    for item in search.get("hits") or []:
        hits.append({"id": item.get("id"), "title": item.get("title"), "chars": len(item.get("text") or "")})
    return {
        "ok": result.get("ok"),
        "query": result.get("query"),
        "filename": result.get("filename"),
        "called": result.get("called"),
        "skipped": result.get("skipped"),
        "stopped_at": result.get("stopped_at"),
        "reason": result.get("reason"),
        "search_count": search.get("count"),
        "search_empty": search.get("empty"),
        "search_hits": hits,
        "summary": (result.get("summary") or {}).get("summary"),
        "source_ids": (result.get("summary") or {}).get("source_ids"),
        "saved": result.get("saved"),
        "final": result.get("final"),
        "answer": result.get("answer"),
        "steps": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "called": item.get("called"),
                "arguments": item.get("arguments"),
                "payload": item.get("payload"),
            }
            for item in result.get("steps") or []
        ],
    }


def run_scenario(agent, scenario: dict) -> dict:
    query = scenario["query"]
    filename = scenario.get("filename")
    print(f"\n=== Сценарий {scenario['id']} ===")
    print("Запрос:", query)
    answer = agent.run(query, filename=filename)
    pipeline = agent.pipeline or {}
    for line in agent.trace:
        print(" ", line[:400].replace("\n", " / "))
    print("Остановка:", pipeline.get("stopped_at") or "нет", "причина:", pipeline.get("reason") or "—")
    print("Вызывались:", pipeline.get("called"))
    print("Не вызывались:", pipeline.get("skipped"))
    print("Ответ:", answer)
    first = agent.payloads[0] if agent.payloads else agent.last_payload
    invented = (not pipeline.get("ok")) and looks_invented(agent.model_answer, pipeline)
    replaced = bool(agent.model_answer) and answer != agent.model_answer
    return {
        "id": scenario["id"],
        "input": query,
        "filename": filename,
        "pipeline": slim_pipeline(pipeline),
        "trace": agent.trace,
        "calls": agent.calls,
        "tools_called": [item["name"] for item in agent.calls],
        "model_answer": agent.model_answer,
        "answer": answer,
        "request_body": snapshot_payload(first),
        "ok": bool(pipeline.get("ok")),
        "stopped_at": pipeline.get("stopped_at"),
        "reason": pipeline.get("reason"),
        "called": pipeline.get("called"),
        "skipped": pipeline.get("skipped"),
        "invented": invented,
        "answer_replaced": replaced,
        "final_missing_on_failure": (not pipeline.get("ok")) and pipeline.get("final") is None,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = load_settings()
    with tempfile.TemporaryDirectory() as raw:
        out = Path(raw)
        os.environ["PIPELINE_OUT"] = str(out)
        print("Подключаюсь к своему MCP-серверу...")
        env = dict(os.environ)
        env["PIPELINE_OUT"] = str(out)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        bridge = reset_bridge(env=env)
        names = [tool.name for tool in bridge.tools]
        schemas = bridge.summary()["tools"]
        print("Соединение установлено:", bool(bridge._client))
        print("Сервер:", bridge.server_name)
        print("Папка сохранения:", out)
        print("Инструменты:")
        for tool in schemas:
            required = ", ".join(tool["required"]) or "—"
            print(f"  {tool['name']}  обязательные: {required}")
            for name, prop in tool["properties"].items():
                typ = prop.get("type") or prop.get("anyOf")
                print(f"    {name}: {typ} — {(prop.get('description') or '')[:140]}")

        scenarios = []
        for key in ("success", "empty", "fail"):
            agent = open_agent(bridge=bridge, api_key=api_key, model=model)
            scenarios.append(run_scenario(agent, SCENARIOS[key]))

        success, empty, fail = scenarios
        record = {
            "model": model,
            "connection": {
                "ok": bridge._client is not None,
                "server": bridge.server_name,
                "command": bridge.command,
                "args": bridge.args,
                "out": "check-out",
                "tools": names,
                "tool_count": len(names),
                "schemas": schemas,
            },
            "scenarios": scenarios,
            "checks": {
                "success_ok": success["ok"] and success["called"] == ["search", "summarize", "save_to_file"],
                "empty_stopped_at_search": empty["stopped_at"] == "search" and empty["called"] == ["search"] and empty["skipped"] == ["summarize", "save_to_file"],
                "fail_stopped_at_summarize": fail["stopped_at"] == "summarize" and fail["called"] == ["search", "summarize"] and fail["skipped"] == ["save_to_file"],
                "no_invented_final": all(item["final_missing_on_failure"] and not item["invented"] or item["ok"] for item in scenarios),
            },
        }
        RUNS_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nСоединение:", record["connection"]["ok"], "инструментов:", record["connection"]["tool_count"])
        print("Успех:", record["checks"]["success_ok"], "пусто:", record["checks"]["empty_stopped_at_search"], "ошибка:", record["checks"]["fail_stopped_at_summarize"])
        print("Без выдуманного итога:", record["checks"]["no_invented_final"])
        print("Сохранено:", RUNS_PATH)


if __name__ == "__main__":
    main()

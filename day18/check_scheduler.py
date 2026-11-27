"""Автономный прогон планировщика и запрос сводки у модели.

python check_scheduler.py  несколько запусков без участия, затем deepseek-flash, пишет runs.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from agent import get_bridge, load_settings, open_agent, payload_for_print, reset_bridge
from scheduler import Scheduler

ROOT = Path(__file__).resolve().parent
RUNS_PATH = ROOT / "runs.json"

SUMMARY_QUESTION = (
    "По сводке за период: сколько было запусков, какая сумма событий по всем запускам "
    "и сколько событий в последнем запуске? Это сумма всех запусков или только последнего? "
    "Не выдумывай числа, возьми их инструментом read_period."
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
    api_key, model = load_settings()
    interval = float(os.environ.get("SCHEDULER_INTERVAL") or 1)
    collect_runs = int(os.environ.get("SCHEDULER_RUNS") or 4)
    with tempfile.TemporaryDirectory() as raw:
        db = Path(raw) / "check-metrics.db"
        os.environ["METRICS_DB"] = str(db)
        print("Подключаюсь к своему MCP-серверу...")
        bridge = reset_bridge()
        names = [tool.name for tool in bridge.tools]
        schemas = bridge.summary()["tools"]
        print("Соединение установлено:", bool(bridge._client))
        print("Сервер:", bridge.server_name)
        print("Хранилище:", db)
        print("Инструменты:")
        for tool in schemas:
            required = ", ".join(tool["required"]) or "—"
            print(f"  {tool['name']}  обязательные: {required}")
            for name, prop in tool["properties"].items():
                typ = prop.get("type") or prop.get("anyOf")
                print(f"    {name}: {typ} — {(prop.get('description') or '')[:140]}")

        print(f"\n=== Планировщик: {collect_runs} запусков каждые {interval} с, без участия ===")
        scheduler = Scheduler(bridge.call_tool, interval=interval, collect_runs=collect_runs)
        schedule = scheduler.run_schedule()
        for index, item in enumerate(schedule["ticks"], start=1):
            sample = item["sample"]
            print(
                f"  запуск {index}: tick={sample.get('tick')} events={sample.get('events')} "
                f"latency_ms={sample.get('latency_ms')} errors={sample.get('errors')} "
                f"всего={item['saved'].get('total')}  {sample.get('collected_at')}"
            )
        summary = (schedule["report"] or {}).get("summary") or {}
        events = [item["sample"]["events"] for item in schedule["ticks"]]
        accumulated = [item["saved"]["total"] for item in schedule["ticks"]] == list(range(1, len(schedule["ticks"]) + 1))
        uses_all = (
            summary.get("events_total") == sum(events)
            and summary.get("events_last") == (events[-1] if events else 0)
            and (len(events) <= 1 or summary.get("events_total") != summary.get("events_last"))
        )
        print("Сводка:", json.dumps(summary, ensure_ascii=False))
        print("Накопление между запусками:", accumulated)
        print("Сводка по всем запускам, не только по последнему:", uses_all)

        print("\n=== Задача модели: сводка за период ===")
        agent = open_agent(bridge=bridge, api_key=api_key, model=model)
        print("Запрос:", SUMMARY_QUESTION)
        answer = agent.ask(SUMMARY_QUESTION)
        for line in agent.trace:
            print(" ", line[:400].replace("\n", " / "))
        print("Ответ:", answer)
        first = agent.payloads[0] if agent.payloads else agent.last_payload
        called = [item["name"] for item in agent.calls]
        used_read = "read_period" in called
        needles = [str(summary.get("runs")), str(summary.get("events_total")), str(summary.get("events_last"))]
        found = mentions_any(answer, needles)
        answer_uses = used_read and str(summary.get("events_total")) in found and str(summary.get("runs")) in found

        record = {
            "model": model,
            "connection": {
                "ok": bridge._client is not None,
                "server": bridge.server_name,
                "command": bridge.command,
                "args": bridge.args,
                "db": "check-metrics.db",
                "tools": names,
                "tool_count": len(names),
                "schemas": schemas,
            },
            "schedule": {
                "interval": interval,
                "collect_runs": collect_runs,
                "elapsed": schedule["elapsed"],
                "ticks": schedule["ticks"],
                "report": schedule["report"],
                "accumulated": accumulated,
                "summary_uses_all_runs": uses_all,
                "events": events,
                "events_total": summary.get("events_total"),
                "events_last": summary.get("events_last"),
            },
            "summary_task": {
                "input": SUMMARY_QUESTION,
                "trace": agent.trace,
                "calls": agent.calls,
                "answer": answer,
                "tools_called": called,
                "request_body": snapshot_payload(first),
                "chose_read_period": used_read,
                "needles": needles,
                "found_in_answer": found,
                "answer_uses_result": answer_uses,
            },
        }
        RUNS_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nСоединение:", record["connection"]["ok"], "инструментов:", record["connection"]["tool_count"])
        print("Планировщик: запусков", len(events), "накопление", accumulated, "сводка по всем", uses_all)
        print("Модель: инструмент", used_read, "ответ опирается на сводку", answer_uses, "числа в ответе", found)
        print("Сохранено:", RUNS_PATH)
        # Не оставляем процесс сервера с временной базой после выхода: atexit закроет мост.


if __name__ == "__main__":
    main()

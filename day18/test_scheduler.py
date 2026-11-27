"""Проверка планировщика и MCP-сервера дня 18: python -m pytest test_scheduler.py

Часть тестов ходит в хранилище напрямую, часть поднимает свой MCP-сервер по stdio.
Сеть и ключ модели не нужны.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import store
from agent import (
    SERVER_PATH,
    Agent,
    McpBridge,
    build_catalog,
    mcp_to_openai,
    parse_arguments,
    payload_for_print,
    tool_schema_view,
)
from scheduler import Scheduler, sample_arguments
from source import MetricSource, values_for_tick


def _bridge_env(db_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["METRICS_DB"] = str(db_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    return env


class SourceAndStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "metrics.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_source_changes_between_ticks(self) -> None:
        source = MetricSource()
        first = source.collect()
        second = source.collect()
        self.assertEqual(first["tick"], 1)
        self.assertEqual(second["tick"], 2)
        self.assertEqual(first["events"], 3)
        self.assertEqual(second["events"], 5)
        self.assertNotEqual(first["events"], second["events"])
        self.assertNotEqual(first["latency_ms"], second["latency_ms"])
        self.assertEqual(first["events"], values_for_tick(1)["events"])

    def test_save_and_read_accumulates(self) -> None:
        source = MetricSource()
        first = store.save_sample(source.collect(), path=self.db)
        second = store.save_sample(source.collect(), path=self.db)
        self.assertTrue(first["ok"])
        self.assertEqual(first["total"], 1)
        self.assertEqual(second["total"], 2)
        period = store.read_period(path=self.db)
        self.assertEqual(period["count"], 2)
        self.assertEqual([item["events"] for item in period["samples"]], [3, 5])

    def test_summary_uses_all_runs_not_only_last(self) -> None:
        source = MetricSource()
        for _ in range(4):
            store.save_sample(source.collect(), path=self.db)
        period = store.read_period(path=self.db)
        summary = period["summary"]
        events = [item["events"] for item in period["samples"]]
        self.assertEqual(events, [3, 5, 7, 9])
        self.assertEqual(summary["runs"], 4)
        self.assertEqual(summary["events_last"], 9)
        self.assertEqual(summary["events_total"], 24)
        self.assertNotEqual(summary["events_total"], summary["events_last"])
        self.assertTrue(summary["uses_all_runs"])
        self.assertEqual(summary["latency_min"], 75)
        self.assertEqual(summary["latency_max"], 120)
        self.assertEqual(summary["latency_avg"], 97.5)
        self.assertEqual(summary["errors_total"], 1)
        self.assertEqual(summary["ticks"], [1, 2, 3, 4])

    def test_read_period_filter(self) -> None:
        store.save_sample(
            {"tick": 1, "collected_at": "2026-11-27T09:00:00+03:00", "events": 3, "latency_ms": 120, "errors": 0},
            path=self.db,
        )
        store.save_sample(
            {"tick": 2, "collected_at": "2026-11-27T09:00:02+03:00", "events": 5, "latency_ms": 105, "errors": 0},
            path=self.db,
        )
        store.save_sample(
            {"tick": 3, "collected_at": "2026-11-27T09:00:04+03:00", "events": 7, "latency_ms": 90, "errors": 1},
            path=self.db,
        )
        period = store.read_period(since="2026-11-27T09:00:02+03:00", until="2026-11-27T09:00:03+03:00", path=self.db)
        self.assertEqual(period["count"], 1)
        self.assertEqual(period["samples"][0]["tick"], 2)
        self.assertEqual(period["summary"]["events_total"], 5)

    def test_empty_period(self) -> None:
        period = store.read_period(path=self.db)
        self.assertEqual(period["count"], 0)
        self.assertEqual(period["summary"]["runs"], 0)
        self.assertEqual(period["summary"]["events_total"], 0)
        self.assertTrue(period["summary"]["uses_all_runs"])

    def test_save_rejects_incomplete_sample(self) -> None:
        result = store.save_sample({"tick": 1}, path=self.db)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_fields")


class SchemaTest(unittest.TestCase):
    def test_mcp_schema_becomes_openai_tool(self) -> None:
        class Fake:
            name = "read_period"
            description = "Сводка."
            input_schema = {
                "type": "object",
                "$schema": "https://json-schema.org/draft-07/schema#",
                "properties": {"since": {"type": "string"}},
            }

        schema = mcp_to_openai(Fake())
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "read_period")
        self.assertNotIn("$schema", schema["function"]["parameters"])

    def test_catalog_marks_mcp_source(self) -> None:
        class Fake:
            name = "collect_metrics"
            description = "снимок"
            input_schema = {"type": "object", "properties": {}}

        tools, sources = build_catalog([Fake()])
        self.assertEqual([item["function"]["name"] for item in tools], ["collect_metrics"])
        self.assertEqual(sources, {"collect_metrics": "mcp"})

    def test_payload_has_no_secret(self) -> None:
        payload = payload_for_print(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "сводка"}],
                "tools": [{"type": "function", "function": {"name": "read_period"}}],
                "api_key": "SHOULD_NOT_APPEAR",
                "Authorization": "Bearer SHOULD_NOT_APPEAR",
            }
        )
        blob = json.dumps(payload)
        self.assertNotIn("SHOULD_NOT_APPEAR", blob)
        self.assertNotIn("api_key", payload)

    def test_parse_arguments(self) -> None:
        self.assertEqual(parse_arguments('{"since": "2026-11-27"}'), {"since": "2026-11-27"})
        with self.assertRaises(ValueError):
            parse_arguments("не json")


class SchedulerLogicTest(unittest.TestCase):
    def test_scheduler_uses_mcp_and_accumulates(self) -> None:
        source = MetricSource()
        samples: list[dict] = []
        calls: list[tuple[str, dict]] = []

        def call_tool(name: str, arguments: dict) -> str:
            calls.append((name, dict(arguments)))
            if name == "collect_metrics":
                return json.dumps(source.collect(), ensure_ascii=False)
            if name == "save_sample":
                samples.append(dict(arguments))
                return json.dumps({"ok": True, "id": len(samples), "total": len(samples)}, ensure_ascii=False)
            if name == "read_period":
                return json.dumps(
                    {"count": len(samples), "samples": list(samples), "summary": store.summarize(samples)},
                    ensure_ascii=False,
                )
            raise AssertionError(name)

        scheduler = Scheduler(call_tool, interval=0, collect_runs=4)
        result = scheduler.run_schedule()
        names = [name for name, _ in calls]
        self.assertEqual(names.count("collect_metrics"), 4)
        self.assertEqual(names.count("save_sample"), 4)
        self.assertEqual(names.count("read_period"), 1)
        self.assertEqual(names[-1], "read_period")
        self.assertEqual(len(result["ticks"]), 4)
        summary = result["report"]["summary"]
        self.assertEqual(summary["runs"], 4)
        self.assertEqual(summary["events_total"], 24)
        self.assertEqual(summary["events_last"], 9)
        self.assertNotEqual(summary["events_total"], summary["events_last"])
        self.assertEqual([item["sample"]["events"] for item in result["ticks"]], [3, 5, 7, 9])
        # Прямых имён модулей store/source в вызовах нет: только MCP-инструменты.
        self.assertEqual(set(names), {"collect_metrics", "save_sample", "read_period"})

    def test_sample_arguments_filters_unknown_keys(self) -> None:
        payload = sample_arguments({"tick": 1, "collected_at": "t", "events": 3, "latency_ms": 10, "extra": 1})
        self.assertNotIn("extra", payload)
        self.assertEqual(payload["tick"], 1)


class McpServerTest(unittest.TestCase):
    bridge: McpBridge
    db: Path
    _tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls._tmp.name) / "metrics.db"
        cls.bridge = McpBridge(command=sys.executable, args=[str(SERVER_PATH)], env=_bridge_env(cls.db))
        cls.bridge.start(timeout=30)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bridge.close()
        cls._tmp.cleanup()

    def test_connection_and_server_name(self) -> None:
        self.assertIsNotNone(self.bridge._client)
        self.assertEqual(self.bridge.server_name, "metrics-scheduler")
        self.assertEqual(Path(self.bridge.command).name, Path(sys.executable).name)

    def test_tools_list_has_schema(self) -> None:
        names = {tool.name for tool in self.bridge.tools}
        self.assertEqual(names, {"collect_metrics", "save_sample", "read_period"})
        views = {item["name"]: item for item in self.bridge.summary()["tools"]}
        collect = views["collect_metrics"]
        self.assertIn("снимок", collect["description"].lower())
        save = views["save_sample"]
        for field in ("tick", "collected_at", "events", "latency_ms"):
            self.assertIn(field, save["required"])
        period = views["read_period"]
        self.assertIn("since", period["properties"])
        self.assertIn("until", period["properties"])
        self.assertIn("всем", period["description"].lower())

    def test_schema_view(self) -> None:
        view = tool_schema_view(self.bridge.tools[0])
        self.assertIn(view["name"], {"collect_metrics", "save_sample", "read_period"})
        self.assertEqual(view["source"], "mcp")

    def test_live_schedule_accumulates_and_summarizes_all_runs(self) -> None:
        # Отдельный сервер и база, чтобы не смешаться с соседними тестами класса.
        with tempfile.TemporaryDirectory() as raw:
            db = Path(raw) / "live.db"
            bridge = McpBridge(command=sys.executable, args=[str(SERVER_PATH)], env=_bridge_env(db))
            bridge.start(timeout=30)
            try:
                scheduler = Scheduler(bridge.call_tool, interval=0.05, collect_runs=4)
                result = scheduler.run_schedule()
                self.assertEqual(len(result["ticks"]), 4)
                totals = [item["saved"]["total"] for item in result["ticks"]]
                self.assertEqual(totals, [1, 2, 3, 4])
                events = [item["sample"]["events"] for item in result["ticks"]]
                self.assertEqual(events, [3, 5, 7, 9])
                report = result["report"]
                self.assertEqual(report["count"], 4)
                summary = report["summary"]
                self.assertEqual(summary["events_total"], sum(events))
                self.assertEqual(summary["events_last"], events[-1])
                self.assertNotEqual(summary["events_total"], summary["events_last"])
                self.assertEqual(summary["runs"], 4)
                # Повторное чтение через MCP видит все четыре запуска, не только последний.
                again = json.loads(bridge.call_tool("read_period", {}))
                self.assertEqual(again["count"], 4)
                self.assertEqual([item["tick"] for item in again["samples"]], [1, 2, 3, 4])
            finally:
                bridge.close()


class AgentDispatchTest(unittest.TestCase):
    def test_agent_calls_read_period(self) -> None:
        class FakeBridge:
            def call_tool(self, name: str, arguments: dict) -> str:
                self.called = (name, arguments)
                return json.dumps(
                    {
                        "count": 4,
                        "samples": [
                            {"tick": 1, "events": 3},
                            {"tick": 2, "events": 5},
                            {"tick": 3, "events": 7},
                            {"tick": 4, "events": 9},
                        ],
                        "summary": {"runs": 4, "events_total": 24, "events_last": 9},
                    },
                    ensure_ascii=False,
                )

        schema = {
            "type": "function",
            "function": {
                "name": "read_period",
                "description": "сводка",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        bridge = FakeBridge()
        agent = Agent("key", "deepseek-flash", [schema], {"read_period": "mcp"}, bridge=bridge)

        def fake_complete() -> dict:
            if not agent.calls:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_period", "arguments": "{}"},
                        }
                    ],
                }
            return {
                "role": "assistant",
                "content": "Запусков 4, сумма событий 24, в последнем запуске 9. Это сумма всех запусков.",
            }

        agent._complete = fake_complete  # type: ignore[method-assign]
        answer = agent.ask("Какая сводка за период?")
        self.assertEqual(bridge.called, ("read_period", {}))
        self.assertIn("24", answer)
        self.assertIn("9", answer)
        self.assertEqual(agent.calls[0]["source"], "mcp")


if __name__ == "__main__":
    unittest.main()

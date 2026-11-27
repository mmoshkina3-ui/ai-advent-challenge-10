"""Проверка пайплайна MCP дня 19: python -m unittest test_pipeline.py

Сеть и ключ модели не нужны. Часть тестов ходит в корпус напрямую,
часть поднимает свой MCP-сервер по stdio.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import corpus
from agent import (
    SERVER_PATH,
    Agent,
    McpBridge,
    mcp_to_openai,
    parse_arguments,
    payload_for_print,
    tool_schema_view,
)
from pipeline import (
    PipelineGuard,
    looks_invented,
    parse_tool_payload,
    run_pipeline,
    safe_answer,
    SCENARIOS,
)


def _bridge_env(out_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PIPELINE_OUT"] = str(out_dir)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    return env


class CorpusTest(unittest.TestCase):
    def test_search_finds_notes(self) -> None:
        result = corpus.search_notes("MCP и память агента")
        self.assertTrue(result["ok"])
        self.assertFalse(result["empty"])
        ids = [item["id"] for item in result["hits"]]
        self.assertIn("mcp", ids)
        self.assertIn("memory", ids)
        self.assertNotIn("broken", ids)

    def test_search_empty_marker(self) -> None:
        result = corpus.search_notes(SCENARIOS["empty"]["query"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["empty"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["hits"], [])
        self.assertIn("пустой", result["reason"])

    def test_search_no_match(self) -> None:
        result = corpus.search_notes("зебра квазар телепортация")
        self.assertTrue(result["empty"])
        self.assertEqual(result["count"], 0)

    def test_search_fail_marker(self) -> None:
        result = corpus.search_notes(SCENARIOS["fail"]["query"])
        self.assertFalse(result["empty"])
        self.assertEqual([item["id"] for item in result["hits"]], ["broken"])
        self.assertIn(corpus.FAIL_TOKEN, result["hits"][0]["text"])

    def test_summarize_success(self) -> None:
        found = corpus.search_notes("MCP память")
        summary = corpus.summarize_documents(found["hits"])
        self.assertTrue(summary["ok"])
        self.assertIn("mcp", summary["source_ids"])
        self.assertIn("memory", summary["source_ids"])
        self.assertIn("MCP", summary["summary"])
        self.assertIn("Память", summary["summary"])

    def test_summarize_empty_input_error(self) -> None:
        summary = corpus.summarize_documents([])
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["step"], "summarize")
        self.assertIn("пустой", summary["error"])

    def test_summarize_fail_marker_error(self) -> None:
        found = corpus.search_notes("__fail__")
        summary = corpus.summarize_documents(found["hits"])
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["step"], "summarize")
        self.assertIn("сломанный", summary["error"])

    def test_save_to_file_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            saved = corpus.save_text("Сводка теста", filename="demo.md", directory=folder)
            self.assertTrue(saved["ok"])
            path = Path(saved["path"])
            self.assertEqual(path.parent, folder.resolve())
            self.assertEqual(path.read_text(encoding="utf-8"), "Сводка теста")
            self.assertEqual(saved["filename"], "demo.md")


class PipelineLogicTest(unittest.TestCase):
    def test_success_passes_data_between_steps(self) -> None:
        calls: list[tuple[str, dict]] = []

        def call_tool(name: str, arguments: dict) -> str:
            calls.append((name, arguments))
            if name == "search":
                return json.dumps(
                    {
                        "ok": True,
                        "query": arguments["query"],
                        "count": 2,
                        "empty": False,
                        "hits": [
                            {"id": "mcp", "title": "MCP-инструменты", "text": "Протокол MCP даёт агенту готовые инструменты."},
                            {"id": "memory", "title": "Память агента", "text": "У агента есть долгая память фактов."},
                        ],
                    },
                    ensure_ascii=False,
                )
            if name == "summarize":
                docs = json.loads(arguments["documents"])
                self.assertEqual([item["id"] for item in docs], ["mcp", "memory"])
                self.assertIn("Протокол MCP", docs[0]["text"])
                return json.dumps(
                    {
                        "ok": True,
                        "summary": "Сводка по 2 заметкам.\n• MCP-инструменты: Протокол MCP даёт агенту готовые инструменты.",
                        "source_ids": ["mcp", "memory"],
                    },
                    ensure_ascii=False,
                )
            if name == "save_to_file":
                self.assertIn("Сводка по 2 заметкам", arguments["text"])
                self.assertEqual(arguments["filename"], "pipeline-success.md")
                return json.dumps(
                    {"ok": True, "filename": "pipeline-success.md", "path": "out/pipeline-success.md", "bytes": 80},
                    ensure_ascii=False,
                )
            raise AssertionError(name)

        result = run_pipeline(call_tool, SCENARIOS["success"]["query"], filename="pipeline-success.md")
        self.assertTrue(result["ok"])
        self.assertEqual(result["called"], ["search", "summarize", "save_to_file"])
        self.assertEqual(result["skipped"], [])
        self.assertIsNone(result["stopped_at"])
        self.assertEqual(result["final"]["filename"], "pipeline-success.md")
        self.assertEqual(result["final"]["source_ids"], ["mcp", "memory"])
        self.assertEqual([name for name, _ in calls], ["search", "summarize", "save_to_file"])

    def test_empty_search_skips_next_tools(self) -> None:
        calls: list[str] = []

        def call_tool(name: str, arguments: dict) -> str:
            calls.append(name)
            if name == "search":
                return json.dumps(
                    {"ok": True, "query": arguments["query"], "count": 0, "hits": [], "empty": True, "reason": "запрос помечен как пустой поиск"},
                    ensure_ascii=False,
                )
            raise AssertionError(f"не должен вызываться {name}")

        result = run_pipeline(call_tool, SCENARIOS["empty"]["query"], filename="pipeline-empty.md")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["search"])
        self.assertEqual(result["called"], ["search"])
        self.assertEqual(result["skipped"], ["summarize", "save_to_file"])
        self.assertEqual(result["stopped_at"], "search")
        self.assertIn("пустой", result["reason"])
        self.assertIsNone(result["final"])
        self.assertIn("search", result["answer"])
        self.assertIn("Итогового результата нет", result["answer"])
        self.assertIn("summarize", result["answer"])

    def test_summarize_error_skips_save(self) -> None:
        calls: list[str] = []

        def call_tool(name: str, arguments: dict) -> str:
            calls.append(name)
            if name == "search":
                return json.dumps(
                    {
                        "ok": True,
                        "count": 1,
                        "empty": False,
                        "hits": [{"id": "broken", "title": "Сломанный источник", "text": "<<<FAIL_SUMMARIZE>>>"}],
                    },
                    ensure_ascii=False,
                )
            if name == "summarize":
                return json.dumps(
                    {"ok": False, "error": "источник помечен как сломанный, сводку построить нельзя", "step": "summarize"},
                    ensure_ascii=False,
                )
            raise AssertionError(f"не должен вызываться {name}")

        result = run_pipeline(call_tool, SCENARIOS["fail"]["query"], filename="pipeline-fail.md")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["search", "summarize"])
        self.assertEqual(result["called"], ["search", "summarize"])
        self.assertEqual(result["skipped"], ["save_to_file"])
        self.assertEqual(result["stopped_at"], "summarize")
        self.assertIn("сломанный", result["reason"])
        self.assertIsNone(result["final"])
        self.assertIn("save_to_file", result["answer"])
        self.assertIn("Итогового результата нет", result["answer"])

    def test_failure_has_no_invented_final(self) -> None:
        def empty_tool(name: str, arguments: dict) -> str:
            return json.dumps({"ok": True, "count": 0, "hits": [], "empty": True, "reason": "совпадений нет"})

        result = run_pipeline(empty_tool, "нет таких заметок")
        self.assertIsNone(result["final"])
        self.assertFalse(result["ok"])
        invented = "Сводка готова и сохранена в pipeline-success.md. Вот текст: агент умеет всё."
        self.assertTrue(looks_invented(invented, result))
        self.assertEqual(safe_answer(result, invented), result["answer"])
        honest = "Цепочка остановилась на шаге search: совпадений нет. Инструменты summarize и save_to_file не вызывались. Итогового результата нет."
        self.assertFalse(looks_invented(honest, result))
        self.assertEqual(safe_answer(result, honest), honest)
        honest_denied = (
            "Пайплайн остановился на шаге search. Причина: совпадений нет. "
            "Не вызывались summarize и save_to_file. Итогового результата нет: файл не сохранён."
        )
        self.assertFalse(looks_invented(honest_denied, result))
        self.assertEqual(safe_answer(result, honest_denied), honest_denied)

    def test_guard_blocks_summarize_after_empty(self) -> None:
        guard = PipelineGuard()
        guard.observe("search", json.dumps({"ok": True, "count": 0, "hits": [], "empty": True, "reason": "запрос помечен как пустой поиск"}))
        refused = guard.allow("summarize", {"documents": "[]"})
        self.assertIsNotNone(refused)
        self.assertIn("search", refused or "")
        self.assertIn("не вызыва", refused or "")
        self.assertEqual(guard.blocked[0]["name"], "summarize")
        refused_save = guard.allow("save_to_file", {"text": "выдумка", "filename": "x.md"})
        self.assertIsNotNone(refused_save)
        self.assertIn("search", refused_save or "")

    def test_guard_blocks_save_after_summarize_error(self) -> None:
        guard = PipelineGuard()
        guard.observe("search", json.dumps({"ok": True, "count": 1, "hits": [{"id": "broken", "title": "x", "text": "<<<FAIL_SUMMARIZE>>>"}], "empty": False}))
        guard.observe("summarize", json.dumps({"ok": False, "error": "источник помечен как сломанный, сводку построить нельзя"}))
        refused = guard.allow("save_to_file", {"text": "выдуманная сводка", "filename": "x.md"})
        self.assertIsNotNone(refused)
        self.assertIn("summarize", refused or "")
        self.assertIn("save_to_file", refused or "")

    def test_payload_for_print_strips_key(self) -> None:
        payload = {"model": "deepseek-flash", "api_key": "SECRET", "Authorization": "Bearer SECRET"}
        clean = payload_for_print(payload)
        self.assertNotIn("api_key", clean)
        self.assertNotIn("Authorization", clean)
        self.assertEqual(clean["model"], "deepseek-flash")

    def test_parse_helpers(self) -> None:
        self.assertEqual(parse_arguments('{"query": "MCP"}'), {"query": "MCP"})
        payload = parse_tool_payload('Ошибка MCP: {"ok": false, "error": "нет"}')
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "нет")


class McpStdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.bridge = McpBridge(env=_bridge_env(self.out))
        self.bridge.start()

    def tearDown(self) -> None:
        self.bridge.close()
        self._tmp.cleanup()

    def test_server_lists_three_tools(self) -> None:
        names = [tool.name for tool in self.bridge.tools]
        self.assertEqual(names, ["search", "summarize", "save_to_file"])
        self.assertTrue(SERVER_PATH.exists())
        view = tool_schema_view(self.bridge.tools[0])
        self.assertEqual(view["source"], "mcp")
        self.assertIn("query", view["properties"] or view["parameters"].get("properties", {}))
        schema = mcp_to_openai(self.bridge.tools[1])
        self.assertEqual(schema["function"]["name"], "summarize")

    def test_stdio_success_pipeline(self) -> None:
        result = run_pipeline(self.bridge.call_tool, SCENARIOS["success"]["query"], filename="pipeline-success.md")
        self.assertTrue(result["ok"], result["answer"])
        self.assertEqual(result["called"], ["search", "summarize", "save_to_file"])
        ids = result["final"]["source_ids"]
        self.assertIn("mcp", ids)
        self.assertIn("memory", ids)
        path = Path(result["saved"]["path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.out.resolve())
        saved_text = path.read_text(encoding="utf-8")
        self.assertEqual(saved_text, result["summary"]["summary"])
        self.assertIn("MCP", saved_text)
        self.assertIn("Память", saved_text)

    def test_stdio_empty_pipeline(self) -> None:
        result = run_pipeline(self.bridge.call_tool, SCENARIOS["empty"]["query"], filename="pipeline-empty.md")
        self.assertFalse(result["ok"])
        self.assertEqual(result["called"], ["search"])
        self.assertEqual(result["skipped"], ["summarize", "save_to_file"])
        self.assertEqual(result["stopped_at"], "search")
        self.assertIn("пустой", result["reason"])
        self.assertFalse((self.out / "pipeline-empty.md").exists())
        self.assertIsNone(result["final"])

    def test_stdio_fail_pipeline(self) -> None:
        result = run_pipeline(self.bridge.call_tool, SCENARIOS["fail"]["query"], filename="pipeline-fail.md")
        self.assertFalse(result["ok"])
        self.assertEqual(result["called"], ["search", "summarize"])
        self.assertEqual(result["skipped"], ["save_to_file"])
        self.assertEqual(result["stopped_at"], "summarize")
        self.assertIn("сломанный", result["reason"])
        self.assertFalse((self.out / "pipeline-fail.md").exists())
        self.assertIsNone(result["final"])
        self.assertEqual((result["search"] or {}).get("hits", [{}])[0].get("id"), "broken")


class AgentGuardTest(unittest.TestCase):
    def test_agent_dispatcher_blocks_next_tool(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.called: list[str] = []

            def call_tool(self, name: str, arguments: dict) -> str:
                self.called.append(name)
                if name == "search":
                    return json.dumps({"ok": True, "count": 0, "hits": [], "empty": True, "reason": "запрос помечен как пустой поиск"})
                raise AssertionError(f"не должен вызываться {name}")

        schema = {
            "type": "function",
            "function": {"name": "search", "description": "поиск", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        }
        summarize_schema = {
            "type": "function",
            "function": {"name": "summarize", "description": "сводка", "parameters": {"type": "object", "properties": {"documents": {"type": "string"}}, "required": ["documents"]}},
        }
        bridge = FakeBridge()
        agent = Agent("key", "deepseek-flash", [schema, summarize_schema], {"search": "mcp", "summarize": "mcp"}, bridge=bridge)

        def fake_complete() -> dict:
            if not agent.calls:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{\"query\": \"пустой поиск\"}"}}],
                }
            if len(agent.calls) == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "summarize", "arguments": "{\"documents\": \"[]\"}"}}],
                }
            return {
                "role": "assistant",
                "content": "Цепочка остановилась на шаге search: запрос помечен как пустой поиск. Инструменты summarize и save_to_file не вызывались. Итогового результата нет.",
            }

        agent._complete = fake_complete  # type: ignore[method-assign]
        answer = agent.ask("пустой поиск")
        self.assertEqual(bridge.called, ["search"])
        self.assertIn("search", answer)
        self.assertIn("Итогового результата нет", answer)
        blocked = [item for item in agent.calls if item["name"] == "summarize"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("не вызыва", blocked[0]["result"])


if __name__ == "__main__":
    unittest.main()

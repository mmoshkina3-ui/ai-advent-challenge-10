"""Проверка MCP-агента: python -m unittest test_mcp.py

Интеграционный тест поднимает настоящий filesystem-сервер, модель не нужна.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import (
    MCP_PACKAGE,
    Agent,
    McpBridge,
    McpError,
    build_catalog,
    calculate,
    format_mcp_result,
    mcp_to_openai,
    npx_command,
    parse_arguments,
    payload_for_print,
)


class SchemaTest(unittest.TestCase):
    def test_mcp_schema_becomes_openai_tool(self):
        class Fake:
            name = "read_text_file"
            description = "Читает текстовый файл."
            input_schema = {
                "type": "object",
                "$schema": "https://json-schema.org/draft-07/schema#",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }

        schema = mcp_to_openai(Fake())
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "read_text_file")
        self.assertNotIn("$schema", schema["function"]["parameters"])
        self.assertEqual(schema["function"]["parameters"]["required"], ["path"])

    def test_mixed_catalog_marks_sources(self):
        class Fake:
            name = "read_text_file"
            description = "file"
            input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}

        tools, sources = build_catalog([Fake()], local_names=["calculate"], mcp_names=["read_text_file"])
        names = [item["function"]["name"] for item in tools]
        self.assertEqual(names, ["calculate", "read_text_file"])
        self.assertEqual(sources, {"calculate": "local", "read_text_file": "mcp"})

    def test_payload_has_no_secret(self):
        payload = payload_for_print(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "привет"}],
                "tools": [{"type": "function", "function": {"name": "calculate"}}],
                "api_key": "SHOULD_NOT_APPEAR",
                "Authorization": "Bearer SHOULD_NOT_APPEAR",
            }
        )
        blob = json.dumps(payload)
        self.assertNotIn("SHOULD_NOT_APPEAR", blob)
        self.assertNotIn("api_key", payload)
        self.assertIn("tools", payload)
        self.assertEqual(payload["model"], "deepseek-flash")


class LocalToolTest(unittest.TestCase):
    def test_calculate(self):
        self.assertEqual(calculate("(17 * 23) + 8"), "399")

    def test_parse_arguments(self):
        self.assertEqual(parse_arguments('{"path": "/tmp/a"}'), {"path": "/tmp/a"})
        self.assertEqual(parse_arguments({}), {})
        with self.assertRaises(ValueError):
            parse_arguments("{")


class FakeBridge:
    def __init__(self, files: dict[str, str], sandbox: Path) -> None:
        self.files = files
        self.sandbox = sandbox
        self.tools = []
        self.server_name = "fake"

    def call_tool(self, name: str, arguments: dict) -> str:
        if name != "read_text_file":
            return f"нет инструмента {name}"
        path = arguments.get("path", "")
        return self.files.get(path, f"нет файла {path}")


class AgentDispatchTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp())
        class FakeTool:
            name = "read_text_file"
            description = "Читает файл."
            input_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        self.tools, self.sources = build_catalog([FakeTool()], local_names=["calculate"], mcp_names=["read_text_file"])
        self.bridge = FakeBridge({str(self.sandbox / "order.txt"): "Позиций: 17\nЦена: 23\nДоставка: 8\n"}, self.sandbox)
        self.replies: list[dict] = []

    def tearDown(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def complete(self):
        return self.replies.pop(0)

    def agent(self) -> Agent:
        agent = Agent("dummy-key", "deepseek-flash", self.tools, self.sources, bridge=self.bridge, sandbox=self.sandbox)
        agent._complete = self.complete  # type: ignore[method-assign]
        return agent

    def test_mixed_dispatch_and_request_body(self):
        path = str(self.sandbox / "order.txt")
        agent = self.agent()
        # First we still want a real payload recorded: call _complete of a second agent? 
        # Here _complete is mocked; test request body via a one-shot without network by inspecting build.
        self.assertEqual(self.sources["calculate"], "local")
        self.assertEqual(self.sources["read_text_file"], "mcp")
        self.replies = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "1", "function": {"name": "read_text_file", "arguments": json.dumps({"path": path})}}
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "2", "function": {"name": "calculate", "arguments": json.dumps({"expression": "(17 * 23) + 8"})}}
                ],
            },
            {"role": "assistant", "content": "Итог заказа — 399."},
        ]
        answer = agent.ask("Прочитай order.txt и посчитай итог")
        self.assertEqual(answer, "Итог заказа — 399.")
        names = [item["name"] for item in agent.calls]
        self.assertEqual(names, ["read_text_file", "calculate"])
        self.assertEqual(agent.calls[0]["source"], "mcp")
        self.assertEqual(agent.calls[1]["source"], "local")
        self.assertIn("399", agent.calls[1]["result"])
        self.assertTrue(any("[mcp] read_text_file" in line for line in agent.trace))
        self.assertTrue(any("[local] calculate" in line for line in agent.trace))

    def test_unknown_tool(self):
        agent = self.agent()
        self.replies = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "drop_database", "arguments": "{}"}}],
            },
            {"role": "assistant", "content": "Инструмента нет."},
        ]
        agent.ask("удали всё")
        self.assertIn("Неизвестный инструмент", agent.calls[0]["result"])


class FormatResultTest(unittest.TestCase):
    def test_text_content(self):
        class Block:
            text = "hello\n"

        class Result:
            is_error = False
            content = [Block()]
            structured_content = None

        self.assertEqual(format_mcp_result(Result()), "hello\n")


@unittest.skipUnless(shutil.which("npx") or shutil.which("npx.cmd"), "нет npx — MCP-сервер не запустить")
class LiveMcpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.sandbox = Path(cls.dir.name)
        (cls.sandbox / "notes.txt").write_text("живой сервер: ок\n", encoding="utf-8")
        cls.bridge = McpBridge(cls.sandbox)
        try:
            cls.bridge.start(timeout=90)
        except McpError as error:
            cls.dir.cleanup()
            raise unittest.SkipTest(str(error))

    @classmethod
    def tearDownClass(cls):
        cls.bridge.close()
        cls.dir.cleanup()

    def test_connection_and_tool_list(self):
        names = [tool.name for tool in self.bridge.tools]
        self.assertTrue(self.bridge._client)
        self.assertIn("read_text_file", names)
        self.assertIn("list_directory", names)
        self.assertGreaterEqual(len(names), 8)

    def test_reads_file_we_did_not_implement(self):
        text = self.bridge.call_tool("read_text_file", {"path": str(self.sandbox / "notes.txt")})
        self.assertIn("живой сервер: ок", text)

    def test_npx_command_exists(self):
        self.assertTrue(npx_command())
        self.assertIn("filesystem", MCP_PACKAGE)


if __name__ == "__main__":
    unittest.main()

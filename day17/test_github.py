"""Проверка своего MCP-сервера и клиента GitHub: python -m unittest test_github.py

Офлайн-тесты поднимают локальный мок API, сеть не нужна.
Живой тест к api.github.com пропускается, если сети нет.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import github_api
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
from github_mock import start_mock


def _bridge_env(api_url: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}
    env["GITHUB_API_URL"] = api_url
    env["GITHUB_OWNER"] = "mmoshkina3-ui"
    env["GITHUB_REPO"] = "ai-advent-challenge-10"
    env["GITHUB_TOKEN"] = ""
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    return env


class OfflineApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = start_mock()
        cls._old = {name: os.environ.get(name) for name in ("GITHUB_API_URL", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO")}
        os.environ["GITHUB_API_URL"] = cls.mock.url
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["GITHUB_OWNER"] = "mmoshkina3-ui"
        os.environ["GITHUB_REPO"] = "ai-advent-challenge-10"

    @classmethod
    def tearDownClass(cls) -> None:
        for name, value in cls._old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cls.mock.stop()

    def test_repo_info(self) -> None:
        payload = github_api.repo_info()
        self.assertFalse(payload.get("error"), payload)
        self.assertEqual(payload["full_name"], "mmoshkina3-ui/ai-advent-challenge-10")
        self.assertFalse(payload["private"])
        self.assertEqual(payload["default_branch"], "main")

    def test_list_commits(self) -> None:
        payload = github_api.list_commits(limit=2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["commits"][0]["sha"], "192eb47")
        self.assertIn("day 17", payload["commits"][0]["message"])

    def test_list_commits_path_filter(self) -> None:
        payload = github_api.list_commits(path="day16")
        shas = [item["sha"] for item in payload["commits"]]
        self.assertEqual(shas, ["59b435d"])

    def test_get_file(self) -> None:
        payload = github_api.get_file("day16/requirements.txt")
        self.assertEqual(payload["type"], "file")
        self.assertIn("mcp>=1.0", payload["content"])
        self.assertFalse(payload["truncated"])

    def test_get_directory(self) -> None:
        payload = github_api.get_file("day16")
        self.assertEqual(payload["type"], "dir")
        names = [item["name"] for item in payload["entries"]]
        self.assertIn("report.md", names)

    def test_missing_file(self) -> None:
        payload = github_api.get_file("no-such-file.txt")
        self.assertEqual(payload["error"], "not_found")
        self.assertEqual(payload["status"], 404)

    def test_rate_limit(self) -> None:
        payload = github_api.repo_info("ratelimit", "anything")
        self.assertEqual(payload["error"], "rate_limited")
        self.assertEqual(payload["status"], 403)
        self.assertIn("GITHUB_TOKEN", payload.get("hint") or payload.get("message") or "")


class SchemaTest(unittest.TestCase):
    def test_mcp_schema_becomes_openai_tool(self) -> None:
        class Fake:
            name = "list_commits"
            description = "Коммиты."
            input_schema = {
                "type": "object",
                "$schema": "https://json-schema.org/draft-07/schema#",
                "properties": {"limit": {"type": "integer"}},
            }

        schema = mcp_to_openai(Fake())
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "list_commits")
        self.assertNotIn("$schema", schema["function"]["parameters"])

    def test_catalog_marks_mcp_source(self) -> None:
        class Fake:
            name = "get_file"
            description = "файл"
            input_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        tools, sources = build_catalog([Fake()])
        self.assertEqual([item["function"]["name"] for item in tools], ["get_file"])
        self.assertEqual(sources, {"get_file": "mcp"})

    def test_payload_has_no_secret(self) -> None:
        payload = payload_for_print(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "привет"}],
                "tools": [{"type": "function", "function": {"name": "get_file"}}],
                "api_key": "SHOULD_NOT_APPEAR",
                "Authorization": "Bearer SHOULD_NOT_APPEAR",
            }
        )
        blob = json.dumps(payload)
        self.assertNotIn("SHOULD_NOT_APPEAR", blob)
        self.assertNotIn("api_key", payload)

    def test_parse_arguments(self) -> None:
        self.assertEqual(parse_arguments('{"path": "day16/report.md"}'), {"path": "day16/report.md"})
        with self.assertRaises(ValueError):
            parse_arguments("не json")


class McpServerTest(unittest.TestCase):
    bridge: McpBridge

    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = start_mock()
        cls.bridge = McpBridge(command=sys.executable, args=[str(SERVER_PATH)], env=_bridge_env(cls.mock.url))
        cls.bridge.start(timeout=30)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bridge.close()
        cls.mock.stop()

    def test_connection_and_server_name(self) -> None:
        self.assertIsNotNone(self.bridge._client)
        self.assertEqual(self.bridge.server_name, "github-repo")
        self.assertEqual(Path(self.bridge.command).name, Path(sys.executable).name)

    def test_tools_list_has_schema(self) -> None:
        names = {tool.name for tool in self.bridge.tools}
        self.assertEqual(names, {"repo_info", "list_commits", "get_file"})
        views = {item["name"]: item for item in self.bridge.summary()["tools"]}
        commits = views["list_commits"]
        self.assertIn("limit", commits["properties"])
        self.assertIn("path", commits["properties"])
        self.assertIn("коммит", commits["description"].lower())
        get_file = views["get_file"]
        self.assertIn("path", get_file["required"])
        self.assertIn("day16", get_file["properties"]["path"]["description"])

    def test_direct_list_commits(self) -> None:
        raw = self.bridge.call_tool("list_commits", {"limit": 3})
        payload = json.loads(raw)
        self.assertEqual(payload["commits"][0]["sha"], "192eb47")
        self.assertEqual(len(payload["commits"]), 3)

    def test_direct_get_file(self) -> None:
        raw = self.bridge.call_tool("get_file", {"path": "day16/report.md"})
        payload = json.loads(raw)
        self.assertEqual(payload["type"], "file")
        self.assertIn("read_text_file", payload["content"])

    def test_direct_missing_file(self) -> None:
        raw = self.bridge.call_tool("get_file", {"path": "no-such-file.txt"})
        payload = json.loads(raw)
        self.assertEqual(payload["error"], "not_found")

    def test_schema_view(self) -> None:
        view = tool_schema_view(self.bridge.tools[0])
        self.assertIn(view["name"], {"repo_info", "list_commits", "get_file"})
        self.assertEqual(view["source"], "mcp")


class AgentDispatchTest(unittest.TestCase):
    def test_agent_calls_mcp_tool(self) -> None:
        class FakeBridge:
            def call_tool(self, name: str, arguments: dict) -> str:
                self.called = (name, arguments)
                return json.dumps(
                    {"count": 1, "commits": [{"sha": "192eb47", "message": "Add the day 17 agent"}]},
                    ensure_ascii=False,
                )

        schema = {
            "type": "function",
            "function": {
                "name": "list_commits",
                "description": "коммиты",
                "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
            },
        }
        bridge = FakeBridge()
        agent = Agent("key", "deepseek-flash", [schema], {"list_commits": "mcp"}, bridge=bridge)

        def fake_complete() -> dict:
            if not agent.calls:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "list_commits", "arguments": '{"limit": 3}'},
                        }
                    ],
                }
            return {"role": "assistant", "content": "В последнем коммите 192eb47 добавили агента дня 17."}

        agent._complete = fake_complete  # type: ignore[method-assign]
        answer = agent.ask("Что в последних коммитах?")
        self.assertEqual(bridge.called, ("list_commits", {"limit": 3}))
        self.assertIn("192eb47", answer)
        self.assertEqual(agent.calls[0]["source"], "mcp")


def _github_reachable() -> bool:
    try:
        request = urllib.request.Request(
            "https://api.github.com",
            headers={"User-Agent": "ai-advent-day17", "Accept": "application/vnd.github+json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


@unittest.skipUnless(_github_reachable(), "нет сети до api.github.com")
class LiveGithubTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_url = os.environ.get("GITHUB_API_URL")
        os.environ.pop("GITHUB_API_URL", None)

    def tearDown(self) -> None:
        if self._old_url is None:
            os.environ.pop("GITHUB_API_URL", None)
        else:
            os.environ["GITHUB_API_URL"] = self._old_url

    def test_public_repo_commits(self) -> None:
        payload = github_api.list_commits(limit=3)
        if payload.get("error") == "rate_limited":
            self.skipTest("лимит GitHub API исчерпан")
        self.assertFalse(payload.get("error"), payload)
        self.assertGreaterEqual(payload["count"], 1)
        messages = " ".join(item["message"] for item in payload["commits"])
        self.assertTrue(
            "day 17" in messages.lower() or "192eb47" in {item["sha"] for item in payload["commits"]},
            messages,
        )


if __name__ == "__main__":
    unittest.main()

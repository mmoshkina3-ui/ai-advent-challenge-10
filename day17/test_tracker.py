"""Проверка своего MCP-сервера и HTTP API трекера: python -m unittest test_tracker.py

Интеграционные тесты поднимают настоящий сервер по stdio, модель не нужна.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
from tracker_api import ORDERS, start_api


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = start_api()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.stop()

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.api.url + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_health(self) -> None:
        status, payload = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["orders"], 3)

    def test_get_order(self) -> None:
        status, payload = self._get("/orders/ORD-1001")
        self.assertEqual(status, 200)
        order = payload["order"]
        self.assertEqual(order["status"], "оплачен")
        self.assertEqual(order["total"], 399)
        self.assertEqual(order["customer"], "Максим")

    def test_get_order_case_insensitive(self) -> None:
        status, payload = self._get("/orders/ord-1001")
        self.assertEqual(status, 200)
        self.assertEqual(payload["order"]["id"], "ORD-1001")

    def test_missing_order(self) -> None:
        status, payload = self._get("/orders/ORD-9999")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")

    def test_list_by_status(self) -> None:
        status, payload = self._get("/orders?status=" + urllib.parse.quote("оплачен"))
        self.assertEqual(status, 200)
        ids = [item["id"] for item in payload["orders"]]
        self.assertEqual(ids, ["ORD-1001"])


class SchemaTest(unittest.TestCase):
    def test_mcp_schema_becomes_openai_tool(self) -> None:
        class Fake:
            name = "get_order"
            description = "Возвращает заказ."
            input_schema = {
                "type": "object",
                "$schema": "https://json-schema.org/draft-07/schema#",
                "properties": {"order_id": {"type": "string", "description": "ИД"}},
                "required": ["order_id"],
            }

        schema = mcp_to_openai(Fake())
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "get_order")
        self.assertNotIn("$schema", schema["function"]["parameters"])
        self.assertEqual(schema["function"]["parameters"]["required"], ["order_id"])

    def test_catalog_marks_mcp_source(self) -> None:
        class Fake:
            name = "get_order"
            description = "заказ"
            input_schema = {"type": "object", "properties": {"order_id": {"type": "string"}}}

        tools, sources = build_catalog([Fake()])
        self.assertEqual([item["function"]["name"] for item in tools], ["get_order"])
        self.assertEqual(sources, {"get_order": "mcp"})

    def test_payload_has_no_secret(self) -> None:
        payload = payload_for_print(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "привет"}],
                "tools": [{"type": "function", "function": {"name": "get_order"}}],
                "api_key": "SHOULD_NOT_APPEAR",
                "Authorization": "Bearer SHOULD_NOT_APPEAR",
            }
        )
        blob = json.dumps(payload)
        self.assertNotIn("SHOULD_NOT_APPEAR", blob)
        self.assertNotIn("api_key", payload)
        self.assertIn("tools", payload)

    def test_parse_arguments(self) -> None:
        self.assertEqual(parse_arguments('{"order_id": "ORD-1001"}'), {"order_id": "ORD-1001"})
        with self.assertRaises(ValueError):
            parse_arguments("не json")


class McpServerTest(unittest.TestCase):
    bridge: McpBridge

    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = McpBridge(command=sys.executable, args=[str(SERVER_PATH)])
        cls.bridge.start(timeout=30)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bridge.close()

    def test_connection_and_server_name(self) -> None:
        self.assertTrue(self.bridge._client is not None)
        self.assertEqual(self.bridge.server_name, "order-tracker")
        self.assertEqual(Path(self.bridge.command).name, Path(sys.executable).name)

    def test_tools_list_has_schema(self) -> None:
        names = {tool.name for tool in self.bridge.tools}
        self.assertEqual(names, {"get_order", "list_orders"})
        views = {item["name"]: item for item in self.bridge.summary()["tools"]}
        get_order = views["get_order"]
        self.assertIn("order_id", get_order["required"])
        self.assertIn("ORD-1001", get_order["properties"]["order_id"]["description"])
        self.assertIn("идентификатору", get_order["description"].lower())
        list_orders = views["list_orders"]
        self.assertIn("оплачен", list_orders["properties"]["status"]["description"])
        self.assertEqual(list_orders["required"], [])

    def test_direct_get_order(self) -> None:
        raw = self.bridge.call_tool("get_order", {"order_id": "ORD-1001"})
        payload = json.loads(raw)
        order = payload["order"]
        self.assertEqual(order["status"], "оплачен")
        self.assertEqual(order["total"], 399)
        self.assertEqual(order["customer"], "Максим")

    def test_direct_list_orders(self) -> None:
        raw = self.bridge.call_tool("list_orders", {"status": "в доставке"})
        payload = json.loads(raw)
        self.assertEqual([item["id"] for item in payload["orders"]], ["ORD-1002"])

    def test_direct_missing_order(self) -> None:
        raw = self.bridge.call_tool("get_order", {"order_id": "ORD-9999"})
        payload = json.loads(raw)
        self.assertEqual(payload["error"], "not_found")

    def test_schema_view(self) -> None:
        view = tool_schema_view(self.bridge.tools[0])
        self.assertIn(view["name"], {"get_order", "list_orders"})
        self.assertEqual(view["source"], "mcp")


class AgentDispatchTest(unittest.TestCase):
    def test_agent_calls_mcp_tool(self) -> None:
        class FakeBridge:
            def call_tool(self, name: str, arguments: dict) -> str:
                self.called = (name, arguments)
                return json.dumps({"order": {"id": arguments["order_id"], "status": "оплачен", "total": 399}}, ensure_ascii=False)

        schema = {
            "type": "function",
            "function": {
                "name": "get_order",
                "description": "заказ",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
            },
        }
        bridge = FakeBridge()
        agent = Agent("key", "deepseek-flash", [schema], {"get_order": "mcp"}, bridge=bridge)

        def fake_complete() -> dict:
            if not agent.calls:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_order", "arguments": '{"order_id": "ORD-1001"}'},
                        }
                    ],
                }
            return {"role": "assistant", "content": "Заказ ORD-1001 оплачен, сумма 399 рублей."}

        agent._complete = fake_complete  # type: ignore[method-assign]
        answer = agent.ask("Какой статус и сумма ORD-1001?")
        self.assertEqual(bridge.called, ("get_order", {"order_id": "ORD-1001"}))
        self.assertIn("399", answer)
        self.assertEqual(agent.calls[0]["source"], "mcp")
        self.assertIn("оплачен", agent.calls[0]["result"])


if __name__ == "__main__":
    unittest.main()

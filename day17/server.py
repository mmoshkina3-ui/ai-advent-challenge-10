"""Свой MCP-сервер дня 17: инструменты вокруг локального HTTP API трекера заказов.

Запуск сам по себе не нужен: агент поднимает этот файл через sys.executable
по stdio. stdout занят протоколом MCP, сообщения — только в stderr.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Any

from pydantic import Field

from tracker_api import STATUSES, start_api

try:
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server


API_URL = ""


def call_api(path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    if not API_URL:
        raise RuntimeError("HTTP API трекера не запущено")
    url = API_URL + path
    if query:
        url += "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value})
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": "http_error", "status": error.code, "message": body[:300]}


def build_server() -> Any:
    server = Server(
        name="order-tracker",
        version="1.0.0",
        instructions="Инструменты трекера заказов. Данные берутся из локального HTTP API, а не из головы модели.",
    )

    @server.tool(
        name="get_order",
        title="Получить заказ",
        description=(
            "Возвращает заказ из трекера по идентификатору: статус, покупатель, позиции, доставка и сумма. "
            "Не выдумывай заказ — сначала вызови этот инструмент."
        ),
    )
    def get_order(
        order_id: Annotated[
            str,
            Field(description="Идентификатор заказа, например ORD-1001. Регистр не важен."),
        ],
    ) -> dict[str, Any]:
        return call_api("/orders/" + order_id.strip())

    @server.tool(
        name="list_orders",
        title="Список заказов",
        description=(
            "Возвращает список заказов из трекера: идентификатор, покупатель, статус и сумма. "
            "Можно отфильтровать по статусу."
        ),
    )
    def list_orders(
        status: Annotated[
            str | None,
            Field(
                description=(
                    "Необязательный фильтр статуса. Допустимые значения: "
                    + ", ".join(STATUSES)
                    + ". Если не указан, вернутся все заказы."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        return call_api("/orders", {"status": status} if status else None)

    return server


def main() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    api = start_api()
    global API_URL
    API_URL = api.url
    sys.stderr.write(f"order-tracker MCP: HTTP API {API_URL}\n")
    try:
        import asyncio

        asyncio.run(build_server().run_stdio_async())
    finally:
        api.stop()


if __name__ == "__main__":
    main()

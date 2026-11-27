"""Свой MCP-сервер дня 18: сбор, сохранение и чтение метрик за период.

Запуск сам по себе не нужен: планировщик и агент поднимают этот файл через
sys.executable по stdio. stdout занят протоколом MCP, сообщения — только в stderr.
Хранилище — SQLite, путь задаётся переменной METRICS_DB.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from pydantic import Field

import source as metric_source
import store

try:
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

METRICS = metric_source.MetricSource()


def build_server() -> Any:
    server = Server(
        name="metrics-scheduler",
        version="1.0.0",
        instructions=(
            "Инструменты локальной очереди метрик. collect_metrics снимает текущий снимок, "
            "save_sample сохраняет его в SQLite, read_period читает накопленное за период "
            "и отдаёт сводку по всем запускам, не только по последнему."
        ),
    )

    @server.tool(
        name="collect_metrics",
        title="Снять метрики",
        description=(
            "Снимает текущий снимок локальной очереди: номер тика, время, число событий, "
            "задержку в миллисекундах, ошибки и rss процесса. Значения меняются от запуска "
            "к запуску. Ничего не записывает в хранилище — для сохранения вызови save_sample."
        ),
    )
    def collect_metrics() -> dict[str, Any]:
        return METRICS.collect()

    @server.tool(
        name="save_sample",
        title="Сохранить снимок",
        description=(
            "Сохраняет снимок метрик в SQLite. Передай поля, которые вернул collect_metrics: "
            "tick, collected_at, events, latency_ms, и при наличии errors, rss_kb, source. "
            "В ответе — id записи и сколько всего снимков накопилось."
        ),
    )
    def save_sample(
        tick: Annotated[int, Field(description="Номер тика из collect_metrics.")],
        collected_at: Annotated[str, Field(description="Время сбора в ISO, как вернул collect_metrics.")],
        events: Annotated[int, Field(description="Число событий за этот запуск.")],
        latency_ms: Annotated[int, Field(description="Задержка в миллисекундах за этот запуск.")],
        errors: Annotated[int, Field(description="Число ошибок за этот запуск. По умолчанию 0.")] = 0,
        rss_kb: Annotated[int, Field(description="Резидентная память процесса в килобайтах.")] = 0,
        source: Annotated[str, Field(description="Имя источника. По умолчанию local-queue.")] = "local-queue",
    ) -> dict[str, Any]:
        return store.save_sample(
            {
                "tick": tick,
                "collected_at": collected_at,
                "events": events,
                "latency_ms": latency_ms,
                "errors": errors,
                "rss_kb": rss_kb,
                "source": source,
            }
        )

    @server.tool(
        name="read_period",
        title="Прочитать накопленное за период",
        description=(
            "Возвращает все сохранённые снимки за период и сводку по ним: число запусков, "
            "сумму событий, min/max/avg задержки, сумму ошибок. Сводка считается по всем "
            "запускам периода, а не по последнему. Без since и until возвращает всё накопленное."
        ),
    )
    def read_period(
        since: Annotated[
            str | None,
            Field(description="Начало периода в ISO. Если не указано, берётся с первого снимка."),
        ] = None,
        until: Annotated[
            str | None,
            Field(description="Конец периода в ISO. Если не указано, берётся до последнего снимка."),
        ] = None,
    ) -> dict[str, Any]:
        return store.read_period(since=since, until=until)

    return server


def main() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    store.load_env()
    sys.stderr.write(f"metrics-scheduler MCP: хранилище {store.db_path()}\n")
    import asyncio

    asyncio.run(build_server().run_stdio_async())


if __name__ == "__main__":
    main()

"""Свой MCP-сервер дня 19: поиск, сводка и сохранение в файл.

Запуск сам по себе не нужен: агент и пайплайн поднимают этот файл через
sys.executable по stdio. stdout занят протоколом MCP, сообщения — только в stderr.
Корпус локальный, папка сохранения задаётся переменной PIPELINE_OUT.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from pydantic import Field

import corpus

try:
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server


def build_server() -> Any:
    server = Server(
        name="notes-pipeline",
        version="1.0.0",
        instructions=(
            "Инструменты локального пайплайна заметок. search ищет в корпусе, "
            "summarize строит сводку по найденным документам, save_to_file сохраняет "
            "текст в папку out. Если search пустой или summarize вернул ошибку, "
            "следующий шаг вызывать нельзя."
        ),
    )

    @server.tool(
        name="search",
        title="Найти заметки",
        description=(
            "Ищет заметки в локальном корпусе по запросу и возвращает список документов: "
            "id, title, text. Ничего не суммирует и не сохраняет. "
            "Запрос с маркером «пустой поиск» или __empty__ всегда возвращает пустой список. "
            "Запрос с маркером «сломанный источник» или __fail__ возвращает документ, "
            "на котором summarize завершится ошибкой."
        ),
    )
    def search(
        query: Annotated[str, Field(description="Поисковый запрос по локальному корпусу заметок.")],
    ) -> dict[str, Any]:
        return corpus.search_notes(query)

    @server.tool(
        name="summarize",
        title="Собрать сводку",
        description=(
            "Строит краткую сводку по документам, которые вернул search. "
            "Передай поле hits как JSON-список объектов с id, title и text. "
            "Пустой список — ошибка: нечего суммировать. "
            "Если в тексте есть маркер сломанного источника, инструмент возвращает ошибку "
            "и сводку не строит."
        ),
    )
    def summarize(
        documents: Annotated[
            str,
            Field(description="JSON-список документов из search: [{\"id\", \"title\", \"text\"}]."),
        ],
    ) -> dict[str, Any]:
        return corpus.summarize_documents(documents)

    @server.tool(
        name="save_to_file",
        title="Сохранить в файл",
        description=(
            "Сохраняет текст сводки в папку out внутри day19. "
            "Передай поле summary, которое вернул summarize. "
            "Имя файла необязательно: если не указано, будет summary.md. "
            "Пустой текст не сохраняется и возвращает ошибку."
        ),
    )
    def save_to_file(
        text: Annotated[str, Field(description="Текст сводки, который вернул summarize.")],
        filename: Annotated[
            str | None,
            Field(description="Имя файла в папке out/. Если не указано, используется summary.md."),
        ] = None,
    ) -> dict[str, Any]:
        return corpus.save_text(text, filename=filename)

    return server


def main() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    corpus.load_env()
    sys.stderr.write(f"notes-pipeline MCP: корпус {corpus.CORPUS_DIR}, out {corpus.output_dir()}\n")
    import asyncio

    asyncio.run(build_server().run_stdio_async())


if __name__ == "__main__":
    main()

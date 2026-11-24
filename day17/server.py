"""Свой MCP-сервер дня 17: инструменты вокруг настоящего GitHub REST API.

Запуск сам по себе не нужен: агент поднимает этот файл через sys.executable
по stdio. stdout занят протоколом MCP, сообщения — только в stderr.
По умолчанию читается публичный репозиторий mmoshkina3-ui/ai-advent-challenge-10.
Токен GITHUB_TOKEN не обязателен; он нужен только чтобы поднять лимит запросов.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from pydantic import Field

import github_api

try:
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server


def build_server() -> Any:
    default = f"{github_api.owner()}/{github_api.repo()}"
    server = Server(
        name="github-repo",
        version="1.1.0",
        instructions=(
            "Инструменты репозитория GitHub. Данные берутся из GitHub REST API, "
            f"по умолчанию из {default}. Не выдумывай коммиты и содержимое файлов."
        ),
    )

    @server.tool(
        name="repo_info",
        title="Сведения о репозитории",
        description=(
            "Возвращает сведения о репозитории GitHub: полное имя, описание, ветку по умолчанию, "
            "число открытых issues и ссылку. По умолчанию это "
            f"{default}."
        ),
    )
    def repo_info(
        owner: Annotated[
            str | None,
            Field(description="Владелец репозитория. Если не указан, берётся значение по умолчанию."),
        ] = None,
        repo: Annotated[
            str | None,
            Field(description="Имя репозитория. Если не указано, берётся значение по умолчанию."),
        ] = None,
    ) -> dict[str, Any]:
        return github_api.repo_info(owner, repo)

    @server.tool(
        name="list_commits",
        title="Список коммитов",
        description=(
            "Возвращает последние коммиты репозитория: короткий sha, сообщение, автор и дата. "
            "Можно ограничить число и отфильтровать по пути файла или папки."
        ),
    )
    def list_commits(
        limit: Annotated[
            int,
            Field(description="Сколько коммитов вернуть, от 1 до 30. По умолчанию 5."),
        ] = 5,
        path: Annotated[
            str | None,
            Field(description="Необязательный фильтр: путь файла или папки, например day16."),
        ] = None,
        ref: Annotated[
            str | None,
            Field(description="Ветка, тег или sha. Если не указан, берётся ветка по умолчанию."),
        ] = None,
        owner: Annotated[
            str | None,
            Field(description="Владелец репозитория. Если не указан, берётся значение по умолчанию."),
        ] = None,
        repo: Annotated[
            str | None,
            Field(description="Имя репозитория. Если не указано, берётся значение по умолчанию."),
        ] = None,
    ) -> dict[str, Any]:
        return github_api.list_commits(limit=limit, path=path, ref=ref, repo_owner=owner, repo_name=repo)

    @server.tool(
        name="get_file",
        title="Прочитать файл",
        description=(
            "Читает файл из репозитория по пути, например day16/report.md. "
            "Если путь указывает на папку, возвращает список элементов. "
            "Длинный файл обрезается. Не выдумывай содержимое — сначала вызови этот инструмент."
        ),
    )
    def get_file(
        path: Annotated[
            str,
            Field(description="Путь к файлу или папке в репозитории, например day16/report.md или day16."),
        ],
        ref: Annotated[
            str | None,
            Field(description="Ветка, тег или sha. Если не указан, берётся ветка по умолчанию."),
        ] = None,
        owner: Annotated[
            str | None,
            Field(description="Владелец репозитория. Если не указан, берётся значение по умолчанию."),
        ] = None,
        repo: Annotated[
            str | None,
            Field(description="Имя репозитория. Если не указано, берётся значение по умолчанию."),
        ] = None,
    ) -> dict[str, Any]:
        return github_api.get_file(path, ref=ref, repo_owner=owner, repo_name=repo)

    return server


def main() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    github_api.load_env()
    sys.stderr.write(
        f"github-repo MCP: API {github_api.api_url()}  репозиторий {github_api.owner()}/{github_api.repo()}\n"
    )
    import asyncio

    asyncio.run(build_server().run_stdio_async())


if __name__ == "__main__":
    main()

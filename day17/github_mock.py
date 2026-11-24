"""Локальный HTTP-мок GitHub REST API для офлайн-тестов. Ключ и сеть не нужны."""

from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_OWNER = "mmoshkina3-ui"
DEFAULT_REPO = "ai-advent-challenge-10"

REPO = {
    "full_name": f"{DEFAULT_OWNER}/{DEFAULT_REPO}",
    "description": "AI Advent challenge (mock)",
    "private": False,
    "default_branch": "main",
    "html_url": f"https://github.com/{DEFAULT_OWNER}/{DEFAULT_REPO}",
    "stargazers_count": 0,
    "forks_count": 0,
    "open_issues_count": 0,
    "language": "Python",
    "updated_at": "2026-11-24T11:19:21Z",
}

COMMITS = [
    {
        "sha": "192eb47aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "html_url": "https://github.com/mmoshkina3-ui/ai-advent-challenge-10/commit/192eb47",
        "commit": {
            "message": "Add the day 17 agent with our own MCP server around a local order API.",
            "author": {"name": r"MAIN\moshkin", "date": "2026-11-24T11:19:21Z"},
        },
        "author": {"login": "mmoshkina3-ui"},
        "path_hint": "day17",
    },
    {
        "sha": "59b435dbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "html_url": "https://github.com/mmoshkina3-ui/ai-advent-challenge-10/commit/59b435d",
        "commit": {
            "message": "Add the day 16 agent with tools from a public MCP server.",
            "author": {"name": r"MAIN\moshkin", "date": "2026-11-23T13:16:36Z"},
        },
        "author": {"login": "mmoshkina3-ui"},
        "path_hint": "day16",
    },
    {
        "sha": "5f0aeb9ccccccccccccccccccccccccccccccccc",
        "html_url": "https://github.com/mmoshkina3-ui/ai-advent-challenge-10/commit/5f0aeb9",
        "commit": {
            "message": "Add the day 15 agent with memory updates and conflict resolution.",
            "author": {"name": r"MAIN\moshkin", "date": "2026-11-22T15:12:09Z"},
        },
        "author": {"login": "mmoshkina3-ui"},
        "path_hint": "day15",
    },
]

FILES: dict[str, str] = {
    "day16/requirements.txt": "mcp>=1.0\n",
    "day16/report.md": (
        "# День 16. Подключение MCP\n\n"
        "Готовый сервер filesystem. Инструменты MCP: list_directory, read_text_file.\n"
        "Локальный инструмент дня 6: calculate.\n"
    ),
    "README.md": "AI Advent challenge 10\n",
}

DIR_ENTRIES = {
    "day16": [
        {"name": "agent.py", "type": "file", "path": "day16/agent.py", "size": 100},
        {"name": "report.md", "type": "file", "path": "day16/report.md", "size": 200},
        {"name": "requirements.txt", "type": "file", "path": "day16/requirements.txt", "size": 9},
        {"name": "sandbox", "type": "dir", "path": "day16/sandbox", "size": 0},
    ]
}


class GitHubMockHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("github-mock: " + (format % args) + "\n")

    def _send(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-RateLimit-Limit", "60")
        self.send_header("X-RateLimit-Remaining", (extra or {}).get("remaining", "59"))
        if extra:
            for key, value in extra.items():
                if key != "remaining":
                    self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        parts = [item for item in path.split("/") if item]

        if path == "/rate_limit" or (len(parts) == 2 and parts[0] == "repos" and parts[1] == "ratelimit"):
            self._send(
                403,
                {"message": "API rate limit exceeded"},
                {"remaining": "0", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"},
            )
            return

        if len(parts) >= 3 and parts[0] == "repos" and parts[1] == "ratelimit":
            self._send(
                403,
                {"message": "API rate limit exceeded"},
                {"remaining": "0", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"},
            )
            return

        if len(parts) == 3 and parts[0] == "repos":
            used_owner, used_repo = parts[1], parts[2]
            if used_owner == "missing" or used_repo == "missing":
                self._send(404, {"message": "Not Found"})
                return
            if used_owner != DEFAULT_OWNER or used_repo != DEFAULT_REPO:
                self._send(404, {"message": "Not Found"})
                return
            self._send(200, REPO)
            return

        if len(parts) == 4 and parts[0] == "repos" and parts[3] == "commits":
            used_owner, used_repo = parts[1], parts[2]
            if used_owner != DEFAULT_OWNER or used_repo != DEFAULT_REPO:
                self._send(404, {"message": "Not Found"})
                return
            try:
                limit = int((query.get("per_page") or ["5"])[0])
            except ValueError:
                limit = 5
            path_filter = (query.get("path") or [None])[0]
            items = COMMITS
            if path_filter:
                items = [item for item in items if (item.get("path_hint") or "") in path_filter or path_filter in (item.get("path_hint") or "")]
            self._send(200, items[:limit])
            return

        if len(parts) >= 4 and parts[0] == "repos" and parts[3] == "contents":
            used_owner, used_repo = parts[1], parts[2]
            if used_owner != DEFAULT_OWNER or used_repo != DEFAULT_REPO:
                self._send(404, {"message": "Not Found"})
                return
            file_path = "/".join(parts[4:])
            if file_path in DIR_ENTRIES:
                self._send(200, DIR_ENTRIES[file_path])
                return
            if file_path not in FILES:
                self._send(404, {"message": "Not Found"})
                return
            content = FILES[file_path].encode("utf-8")
            self._send(
                200,
                {
                    "type": "file",
                    "name": file_path.split("/")[-1],
                    "path": file_path,
                    "size": len(content),
                    "sha": "abc1234ffffffffffffffffffffffffffffffffffff",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode("ascii"),
                    "html_url": f"https://github.com/{DEFAULT_OWNER}/{DEFAULT_REPO}/blob/main/{file_path}",
                },
            )
            return

        self._send(404, {"message": "Not Found"})


class GitHubMock:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), GitHubMockHandler)
        self.host, self.port = self._server.server_address
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="github-mock")

    def start(self) -> "GitHubMock":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def start_mock(port: int = 0) -> GitHubMock:
    return GitHubMock(port=port).start()

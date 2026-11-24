"""Клиент GitHub REST API. Токен не обязателен для чтения публичного репозитория.

Не печатает токен и не включает его в возвращаемые данные.
Переменные: GITHUB_TOKEN (необязательно), GITHUB_OWNER, GITHUB_REPO, GITHUB_API_URL.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_OWNER = "mmoshkina3-ui"
DEFAULT_REPO = "ai-advent-challenge-10"
DEFAULT_API_URL = "https://api.github.com"
MAX_FILE_CHARS = 8000
USER_AGENT = "ai-advent-day17"
API_VERSION = "2022-11-28"
ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    """Подхватывает GITHUB_* и DEEPSEEK_* из .env, не затирая уже заданное окружение."""
    found: dict[str, str] = {}
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            found.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    for name in (
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "GITHUB_API_URL",
        "DEEPSEEK_API_KEY",
        "LLM_MODEL",
    ):
        if name not in os.environ or not os.environ.get(name):
            value = found.get(name, "")
            if value and value != "сюда_ключ":
                os.environ[name] = value


def owner() -> str:
    return (os.environ.get("GITHUB_OWNER") or DEFAULT_OWNER).strip() or DEFAULT_OWNER


def repo() -> str:
    return (os.environ.get("GITHUB_REPO") or DEFAULT_REPO).strip() or DEFAULT_REPO


def api_url() -> str:
    return (os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL).strip().rstrip("/") or DEFAULT_API_URL


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _error(status: int, body: str, headers: Any | None = None) -> dict[str, Any]:
    remaining = None
    reset = None
    if headers is not None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
    text = body[:400]
    try:
        parsed = json.loads(body)
        text = str(parsed.get("message") or text)
    except json.JSONDecodeError:
        parsed = {}
    lowered = text.lower()
    if status == 404:
        return {"error": "not_found", "status": 404, "message": text or "Ресурс не найден в GitHub."}
    if status == 401:
        return {"error": "unauthorized", "status": 401, "message": "Токен GitHub отклонён или недействителен. Проверьте GITHUB_TOKEN."}
    rate_limited = status == 403 and (
        remaining == "0" or "rate limit" in lowered or "api rate limit exceeded" in lowered
    )
    if rate_limited:
        hint = "Лимит GitHub API исчерпан. Подождите или задайте GITHUB_TOKEN — с токеном лимит выше."
        if reset:
            hint += f" Сброс (unix time): {reset}."
        return {"error": "rate_limited", "status": 403, "message": text or hint, "hint": hint, "remaining": remaining}
    if status == 403:
        return {"error": "forbidden", "status": 403, "message": text or "GitHub отказал в доступе."}
    return {"error": "http_error", "status": status, "message": text or f"GitHub ответил {status}."}


def request(path: str, query: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    url = api_url() + path
    if query:
        cleaned = {key: value for key, value in query.items() if value is not None and value != ""}
        if cleaned:
            url += "?" + urllib.parse.urlencode(cleaned)
    request_obj = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(request_obj, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return _error(error.code, body, error.headers)
    except urllib.error.URLError as error:
        return {"error": "network", "message": f"Сеть: не удалось обратиться к GitHub API ({error})."}


def repo_info(repo_owner: str | None = None, repo_name: str | None = None) -> dict[str, Any]:
    used_owner = (repo_owner or owner()).strip()
    used_repo = (repo_name or repo()).strip()
    payload = request(f"/repos/{used_owner}/{used_repo}")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    if not isinstance(payload, dict):
        return {"error": "http_error", "message": "Неожиданный ответ GitHub."}
    return {
        "full_name": payload.get("full_name"),
        "description": payload.get("description") or "",
        "private": payload.get("private"),
        "default_branch": payload.get("default_branch"),
        "html_url": payload.get("html_url"),
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "open_issues": payload.get("open_issues_count"),
        "language": payload.get("language"),
        "updated_at": payload.get("updated_at"),
        "owner": used_owner,
        "repo": used_repo,
    }


def list_commits(
    limit: int = 5,
    path: str | None = None,
    ref: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> dict[str, Any]:
    used_owner = (repo_owner or owner()).strip()
    used_repo = (repo_name or repo()).strip()
    try:
        per_page = int(limit)
    except (TypeError, ValueError):
        per_page = 5
    per_page = max(1, min(per_page, 30))
    payload = request(
        f"/repos/{used_owner}/{used_repo}/commits",
        {"per_page": per_page, "path": path or None, "sha": ref or None},
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    if not isinstance(payload, list):
        return {"error": "http_error", "message": "Неожиданный ответ GitHub."}
    commits = []
    for item in payload:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        message = (commit.get("message") or "").strip()
        first = message.splitlines()[0] if message else ""
        commits.append(
            {
                "sha": (item.get("sha") or "")[:7],
                "sha_full": item.get("sha"),
                "message": first,
                "author": author.get("name") or (item.get("author") or {}).get("login") or "",
                "date": author.get("date") or "",
                "html_url": item.get("html_url") or "",
            }
        )
    return {
        "owner": used_owner,
        "repo": used_repo,
        "ref": ref or None,
        "path": path or None,
        "count": len(commits),
        "commits": commits,
    }


def get_file(
    path: str,
    ref: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> dict[str, Any]:
    used_owner = (repo_owner or owner()).strip()
    used_repo = (repo_name or repo()).strip()
    cleaned = (path or "").strip().lstrip("/")
    if not cleaned:
        return {"error": "bad_request", "message": "Укажите путь к файлу или папке, например day16/report.md."}
    payload = request(
        f"/repos/{used_owner}/{used_repo}/contents/{cleaned}",
        {"ref": ref or None},
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    if isinstance(payload, list):
        entries = [
            {
                "name": item.get("name"),
                "type": item.get("type"),
                "path": item.get("path"),
                "size": item.get("size"),
            }
            for item in payload
        ]
        return {"type": "dir", "path": cleaned, "ref": ref or None, "owner": used_owner, "repo": used_repo, "entries": entries}
    if not isinstance(payload, dict):
        return {"error": "http_error", "message": "Неожиданный ответ GitHub."}
    if payload.get("type") != "file":
        return {"type": payload.get("type"), "path": cleaned, "message": "Это не обычный файл, содержимое не прочитано.", "html_url": payload.get("html_url")}
    encoding = payload.get("encoding")
    raw = payload.get("content") or ""
    if encoding == "base64":
        try:
            data = base64.b64decode(raw)
        except (ValueError, TypeError):
            return {"error": "decode_error", "message": "Не удалось декодировать содержимое файла."}
    else:
        data = str(raw).encode("utf-8")
    size = payload.get("size") or len(data)
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        return {
            "type": "file",
            "path": cleaned,
            "ref": ref or payload.get("sha"),
            "size": size,
            "binary": True,
            "message": "Файл двоичный, текст не показан.",
            "html_url": payload.get("html_url"),
        }
    truncated = len(text) > MAX_FILE_CHARS
    if truncated:
        text = text[:MAX_FILE_CHARS]
    return {
        "type": "file",
        "path": cleaned,
        "ref": ref or None,
        "sha": (payload.get("sha") or "")[:7],
        "size": size,
        "truncated": truncated,
        "content": text,
        "html_url": payload.get("html_url"),
        "owner": used_owner,
        "repo": used_repo,
        "binary": False,
    }

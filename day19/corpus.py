"""Локальный корпус заметок дня 19: поиск и сводка без внешнего API."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"
DEFAULT_OUT = ROOT / "out"

EMPTY_MARKERS = ("__empty__", "пустой поиск")
FAIL_MARKERS = ("__fail__", "сломанный источник")
FAIL_TOKEN = "<<<FAIL_SUMMARIZE>>>"
BROKEN_ID = "broken"

_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)


def load_env() -> None:
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("\'"))


def output_dir(raw: str | os.PathLike[str] | None = None) -> Path:
    value = str(raw) if raw is not None else (os.environ.get("PIPELINE_OUT") or str(DEFAULT_OUT))
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def tokenize(text: str) -> set[str]:
    return {item.lower() for item in _TOKEN.findall(text or "")}


def load_notes() -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    if not CORPUS_DIR.exists():
        return notes
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else path.stem
        body = "\n".join(lines[1:] if len(lines) > 1 else lines)
        notes.append(
            {
                "id": path.stem,
                "title": title,
                "text": body,
                "path": path.name,
            }
        )
    return notes


def _query_has_marker(query: str, markers: tuple[str, ...]) -> bool:
    lowered = (query or "").strip().lower()
    return any(marker.lower() in lowered for marker in markers)


def search_notes(query: str) -> dict[str, Any]:
    text = (query or "").strip()
    notes = load_notes()
    if not text:
        return {
            "ok": True,
            "query": text,
            "count": 0,
            "hits": [],
            "empty": True,
            "reason": "пустой запрос",
        }
    if _query_has_marker(text, EMPTY_MARKERS):
        return {
            "ok": True,
            "query": text,
            "count": 0,
            "hits": [],
            "empty": True,
            "reason": "запрос помечен как пустой поиск",
        }
    if _query_has_marker(text, FAIL_MARKERS):
        hits = [item for item in notes if item["id"] == BROKEN_ID]
        return {
            "ok": True,
            "query": text,
            "count": len(hits),
            "hits": hits,
            "empty": not hits,
            "reason": "выдан сломанный источник для проверки ошибки на шаге summarize",
        }

    words = tokenize(text)
    ranked: list[tuple[int, dict[str, str]]] = []
    for note in notes:
        if note["id"] == BROKEN_ID:
            continue
        hay = tokenize(" ".join([note["id"], note["title"], note["text"]]))
        score = len(words & hay)
        if score:
            ranked.append((score, note))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    hits = [item[1] for item in ranked]
    empty = not hits
    return {
        "ok": True,
        "query": text,
        "count": len(hits),
        "hits": hits,
        "empty": empty,
        "reason": "совпадений нет" if empty else "найдены заметки по запросу",
    }


def _parse_documents(documents: Any) -> list[dict[str, Any]]:
    if documents is None or documents == "":
        return []
    if isinstance(documents, list):
        raw = documents
    elif isinstance(documents, str):
        text = documents.strip()
        if not text:
            return []
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return [{"id": "text", "title": "Текст", "text": text}]
        raw = loaded
    else:
        raw = documents
    if isinstance(raw, dict):
        raw = raw.get("hits") or raw.get("documents") or [raw]
    if not isinstance(raw, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            parsed.append({"id": "text", "title": "Текст", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        parsed.append(
            {
                "id": str(item.get("id") or item.get("path") or "doc"),
                "title": str(item.get("title") or item.get("id") or "Заметка"),
                "text": str(item.get("text") or item.get("body") or ""),
            }
        )
    return parsed


def _first_sentence(text: str) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    match = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)
    return match[0] if match else clean


def contains_fail_token(documents: list[dict[str, Any]]) -> bool:
    for item in documents:
        blob = " ".join([str(item.get("id") or ""), str(item.get("title") or ""), str(item.get("text") or "")])
        if FAIL_TOKEN in blob:
            return True
    return False


def summarize_documents(documents: Any) -> dict[str, Any]:
    items = _parse_documents(documents)
    if not items:
        return {
            "ok": False,
            "error": "нечего суммировать: пустой вход",
            "step": "summarize",
            "source_ids": [],
        }
    if contains_fail_token(items):
        return {
            "ok": False,
            "error": "источник помечен как сломанный, сводку построить нельзя",
            "step": "summarize",
            "source_ids": [str(item.get("id") or "") for item in items],
        }
    bullets: list[str] = []
    source_ids: list[str] = []
    for item in items:
        ident = str(item.get("id") or "doc")
        title = str(item.get("title") or ident)
        sentence = _first_sentence(str(item.get("text") or ""))
        bullets.append(f"{title}: {sentence}" if sentence else title)
        source_ids.append(ident)
    summary = f"Сводка по {len(bullets)} заметкам.\n" + "\n".join(f"• {line}" for line in bullets)
    return {
        "ok": True,
        "summary": summary,
        "source_ids": source_ids,
        "count": len(source_ids),
        "chars": len(summary),
    }


def safe_filename(name: str | None, fallback: str = "summary.md") -> str:
    raw = (name or "").strip() or fallback
    raw = raw.replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", raw).strip(".-")
    if not cleaned:
        cleaned = "summary.md"
    if "." not in cleaned:
        cleaned += ".md"
    return cleaned


def save_text(text: str, filename: str | None = None, directory: Path | None = None) -> dict[str, Any]:
    body = text or ""
    if not body.strip():
        return {
            "ok": False,
            "error": "нечего сохранять: пустой текст",
            "step": "save_to_file",
        }
    folder = output_dir(directory)
    name = safe_filename(filename)
    path = (folder / name).resolve()
    if folder.resolve() not in path.parents and path.parent != folder.resolve():
        return {
            "ok": False,
            "error": "имя файла выходит за папку сохранения",
            "step": "save_to_file",
        }
    path.write_text(body, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "filename": path.name,
        "bytes": path.stat().st_size,
        "saved_at": now_iso(),
    }

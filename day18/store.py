"""Хранение снимков метрик в SQLite и сводка за период."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "metrics.db"
_LOCK = threading.Lock()

SAMPLE_FIELDS = ("tick", "collected_at", "events", "latency_ms", "errors", "rss_kb", "source")


def load_env() -> None:
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def db_path(raw: str | os.PathLike[str] | None = None) -> Path:
    value = str(raw) if raw is not None else (os.environ.get("METRICS_DB") or str(DEFAULT_DB))
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = db_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick INTEGER NOT NULL,
            collected_at TEXT NOT NULL,
            events INTEGER NOT NULL,
            latency_ms INTEGER NOT NULL,
            errors INTEGER NOT NULL,
            rss_kb INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'local-queue'
        )
        """
    )
    connection.commit()
    return connection


def _row_to_sample(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tick": row["tick"],
        "collected_at": row["collected_at"],
        "events": row["events"],
        "latency_ms": row["latency_ms"],
        "errors": row["errors"],
        "rss_kb": row["rss_kb"],
        "source": row["source"],
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "runs": 0,
            "events_total": 0,
            "events_last": 0,
            "uses_all_runs": True,
            "latency_min": None,
            "latency_max": None,
            "latency_avg": None,
            "errors_total": 0,
            "first_at": None,
            "last_at": None,
            "ticks": [],
        }
    events = [int(item["events"]) for item in samples]
    latencies = [int(item["latency_ms"]) for item in samples]
    errors = [int(item["errors"]) for item in samples]
    events_total = sum(events)
    events_last = events[-1]
    return {
        "runs": len(samples),
        "events_total": events_total,
        "events_last": events_last,
        "uses_all_runs": events_total != events_last if len(samples) > 1 else True,
        "latency_min": min(latencies),
        "latency_max": max(latencies),
        "latency_avg": round(sum(latencies) / len(latencies), 2),
        "errors_total": sum(errors),
        "first_at": samples[0]["collected_at"],
        "last_at": samples[-1]["collected_at"],
        "ticks": [int(item["tick"]) for item in samples],
    }


def save_sample(sample: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    required = ("tick", "collected_at", "events", "latency_ms")
    missing = [name for name in required if name not in sample or sample[name] in (None, "")]
    if missing:
        return {"ok": False, "error": "missing_fields", "fields": missing}
    with _LOCK:
        connection = connect(path)
        try:
            cursor = connection.execute(
                """
                INSERT INTO samples (tick, collected_at, events, latency_ms, errors, rss_kb, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(sample["tick"]),
                    str(sample["collected_at"]),
                    int(sample["events"]),
                    int(sample["latency_ms"]),
                    int(sample.get("errors") or 0),
                    int(sample.get("rss_kb") or 0),
                    str(sample.get("source") or "local-queue"),
                ),
            )
            connection.commit()
            total = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            return {"ok": True, "id": cursor.lastrowid, "total": int(total)}
        finally:
            connection.close()


def read_period(
    since: str | None = None,
    until: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append("collected_at >= ?")
        params.append(since)
    if until:
        clauses.append("collected_at <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _LOCK:
        connection = connect(path)
        try:
            rows = connection.execute(
                f"SELECT * FROM samples {where} ORDER BY id ASC",
                params,
            ).fetchall()
        finally:
            connection.close()
    samples = [_row_to_sample(row) for row in rows]
    return {
        "count": len(samples),
        "since": since,
        "until": until,
        "samples": samples,
        "summary": summarize(samples),
    }


def clear(path: Path | None = None) -> dict[str, Any]:
    with _LOCK:
        connection = connect(path)
        try:
            connection.execute("DELETE FROM samples")
            connection.commit()
        finally:
            connection.close()
    return {"ok": True, "total": 0}

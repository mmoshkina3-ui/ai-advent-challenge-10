"""Локальный источник метрик, который меняется от запуска к запуску.

Это не внешний API: значения считаются от номера тика, поэтому тесты устойчивы,
а в сводке есть что складывать (сумма, min/max/avg). Дополнительно снимается
rss текущего процесса — живой, но не обязательный для агрегации показатель.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any


def values_for_tick(tick: int) -> dict[str, int]:
    if tick < 1:
        raise ValueError("tick должен быть >= 1")
    return {
        "events": 3 + (tick - 1) * 2,
        "latency_ms": 120 - (tick - 1) * 15,
        "errors": 1 if tick % 3 == 0 else 0,
    }


def rss_kb() -> int:
    try:
        statm = Path("/proc/self/statm")
        if statm.exists():
            pages = int(statm.read_text(encoding="utf-8").split()[1])
            return pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, AttributeError):
        return 0


class MetricSource:
    """Счётчик тиков живёт в процессе MCP-сервера и растёт с каждым сбором."""

    def __init__(self, start_tick: int = 0) -> None:
        self.tick = start_tick

    def collect(self) -> dict[str, Any]:
        self.tick += 1
        values = values_for_tick(self.tick)
        return {
            "tick": self.tick,
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "events": values["events"],
            "latency_ms": values["latency_ms"],
            "errors": values["errors"],
            "rss_kb": rss_kb(),
            "source": "local-queue",
        }

    def reset(self) -> None:
        self.tick = 0

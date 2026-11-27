"""Планировщик дня 18: по расписанию вызывает MCP-инструменты и собирает сводку.

Сбор и сохранение идут только через call_tool (stdio MCP), без прямого вызова
функций хранилища. После серии сборов планировщик сам читает период и отдаёт
сводку по всем накопленным запускам.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Callable

CallTool = Callable[[str, dict[str, Any]], str]

SAMPLE_FIELDS = ("tick", "collected_at", "events", "latency_ms", "errors", "rss_kb", "source")


def _load_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"MCP вернул не JSON: {raw[:400]}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"MCP вернул не объект: {raw[:400]}")
    return data


def sample_arguments(sample: dict[str, Any]) -> dict[str, Any]:
    return {name: sample[name] for name in SAMPLE_FIELDS if name in sample}


class Scheduler:
    """Фоновый цикл: каждые interval секунд collect + save, затем read_period."""

    def __init__(
        self,
        call_tool: CallTool,
        interval: float = 1.0,
        collect_runs: int = 4,
    ) -> None:
        if interval < 0:
            raise ValueError("interval не может быть отрицательным")
        if collect_runs < 1:
            raise ValueError("collect_runs должен быть >= 1")
        self.call_tool = call_tool
        self.interval = interval
        self.collect_runs = collect_runs
        self.ticks: list[dict[str, Any]] = []
        self.report: dict[str, Any] | None = None
        self.error: str | None = None
        self.running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict[str, Any]:
        collected = _load_json(self.call_tool("collect_metrics", {}))
        if collected.get("error"):
            raise RuntimeError(f"collect_metrics: {collected}")
        saved = _load_json(self.call_tool("save_sample", sample_arguments(collected)))
        if not saved.get("ok"):
            raise RuntimeError(f"save_sample: {saved}")
        return {
            "via": ["collect_metrics", "save_sample"],
            "sample": collected,
            "saved": saved,
        }

    def read_report(self, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if since:
            arguments["since"] = since
        if until:
            arguments["until"] = until
        report = _load_json(self.call_tool("read_period", arguments))
        self.report = report
        return report

    def run_schedule(self) -> dict[str, Any]:
        """Несколько запусков подряд без участия человека, затем сводка за период."""
        self.running = True
        self.error = None
        self.ticks = []
        self.report = None
        self._stop.clear()
        started = time.time()
        try:
            for index in range(self.collect_runs):
                if self._stop.is_set():
                    break
                tick_started = time.monotonic()
                item = self.run_once()
                self.ticks.append(item)
                if index < self.collect_runs - 1 and not self._stop.is_set():
                    remaining = self.interval - (time.monotonic() - tick_started)
                    if remaining > 0:
                        self._stop.wait(remaining)
            report = self.read_report()
            return {
                "ticks": list(self.ticks),
                "report": report,
                "elapsed": round(time.time() - started, 3),
                "interval": self.interval,
                "collect_runs": self.collect_runs,
                "stopped": self._stop.is_set(),
            }
        except Exception as error:
            self.error = str(error)
            raise
        finally:
            self.running = False

    def start_background(self) -> None:
        if self.running:
            raise RuntimeError("планировщик уже запущен")
        self.running = True
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_safe, daemon=True, name="day18-scheduler")
        self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval": self.interval,
            "collect_runs": self.collect_runs,
            "tick_count": len(self.ticks),
            "ticks": list(self.ticks),
            "report": self.report,
            "error": self.error,
        }

    def _run_safe(self) -> None:
        try:
            self.run_schedule()
        except Exception:
            # Ошибка уже записана в self.error.
            return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Планировщик MCP-метрик дня 18")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SCHEDULER_INTERVAL") or 1))
    parser.add_argument("--runs", type=int, default=int(os.environ.get("SCHEDULER_RUNS") or 4))
    parser.add_argument("--db", default=os.environ.get("METRICS_DB") or "metrics.db")
    return parser.parse_args(argv)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    os.environ["METRICS_DB"] = args.db
    from agent import get_bridge

    bridge = get_bridge()
    print("MCP-соединение:", "установлено" if bridge._client else "нет")
    print("Сервер:", bridge.server_name)
    print(f"Расписание: {args.runs} сборов каждые {args.interval} с, хранилище {args.db}")
    scheduler = Scheduler(bridge.call_tool, interval=args.interval, collect_runs=args.runs)
    result = scheduler.run_schedule()
    for index, item in enumerate(result["ticks"], start=1):
        sample = item["sample"]
        saved = item["saved"]
        print(
            f"Запуск {index}: tick={sample.get('tick')} events={sample.get('events')} "
            f"latency_ms={sample.get('latency_ms')} errors={sample.get('errors')} "
            f"всего в базе={saved.get('total')}  через MCP: {', '.join(item['via'])}"
        )
    summary = (result["report"] or {}).get("summary") or {}
    print("Сводка за период:")
    print(
        f"  запусков: {summary.get('runs')}, сумма событий: {summary.get('events_total')}, "
        f"события последнего запуска: {summary.get('events_last')}"
    )
    print(
        f"  latency min/avg/max: {summary.get('latency_min')}/"
        f"{summary.get('latency_avg')}/{summary.get('latency_max')}, "
        f"ошибок всего: {summary.get('errors_total')}"
    )
    print(
        "  сводка по всем запускам, не только по последнему:",
        "да" if summary.get("uses_all_runs") and summary.get("events_total") != summary.get("events_last") else "нет",
    )
    print(f"Секунд: {result['elapsed']}")


if __name__ == "__main__":
    main()

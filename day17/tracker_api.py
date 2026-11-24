"""Локальный HTTP API трекера заказов. Его оборачивает MCP-сервер дня 17.

Запуск отдельно: python tracker_api.py
По умолчанию слушает 127.0.0.1 и свободный порт. Ключи и внешняя сеть не нужны.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

ORDERS: dict[str, dict[str, Any]] = {
    "ORD-1001": {
        "id": "ORD-1001",
        "customer": "Максим",
        "status": "оплачен",
        "items": [{"title": "Позиции заказа в магазине", "qty": 17, "price": 23}],
        "delivery": 8,
        "total": 399,
        "currency": "RUB",
        "created": "2026-11-20",
    },
    "ORD-1002": {
        "id": "ORD-1002",
        "customer": "Анна",
        "status": "в доставке",
        "items": [{"title": "Наушники", "qty": 1, "price": 4500}],
        "delivery": 300,
        "total": 4800,
        "currency": "RUB",
        "created": "2026-11-18",
    },
    "ORD-1003": {
        "id": "ORD-1003",
        "customer": "Игорь",
        "status": "отменён",
        "items": [{"title": "Чехол", "qty": 2, "price": 400}],
        "delivery": 0,
        "total": 800,
        "currency": "RUB",
        "created": "2026-11-15",
    },
}

STATUSES = ("оплачен", "в доставке", "отменён")


def list_orders(status: str | None = None) -> dict[str, Any]:
    items = list(ORDERS.values())
    if status:
        items = [item for item in items if item["status"] == status]
    return {"orders": [{"id": item["id"], "customer": item["customer"], "status": item["status"], "total": item["total"]} for item in items]}


def get_order(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id.strip().upper())
    if not order:
        return {"error": "not_found", "message": f"Заказ {order_id} не найден", "known_ids": list(ORDERS)}
    return {"order": order}


class TrackerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("tracker-api: " + (format % args) + "\n")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/health":
            self._send(200, {"ok": True, "orders": len(ORDERS)})
            return
        if path == "/orders":
            status = (query.get("status") or [None])[0]
            if status and status not in STATUSES:
                self._send(400, {"error": "bad_status", "message": f"Неизвестный статус {status}", "allowed": list(STATUSES)})
                return
            self._send(200, list_orders(status))
            return
        if path.startswith("/orders/"):
            order_id = path.split("/", 2)[2]
            payload = get_order(order_id)
            self._send(404 if "error" in payload else 200, payload)
            return
        self._send(404, {"error": "unknown_path", "message": f"Нет маршрута {path}"})


class TrackerApi:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), TrackerHandler)
        self.host, self.port = self._server.server_address
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="tracker-api")

    def start(self) -> "TrackerApi":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def start_api(port: int = 0) -> TrackerApi:
    return TrackerApi(port=port).start()


def main() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    api = start_api(8767)
    sys.stderr.write(f"Трекер заказов: {api.url}\n")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        api.stop()


if __name__ == "__main__":
    main()

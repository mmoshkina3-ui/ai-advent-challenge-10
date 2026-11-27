"""Оркестратор пайплайна дня 19: search → summarize → save_to_file.

Инструменты вызываются только через переданную функцию call_tool (MCP-клиент
в боевом режиме, подделка в тестах). Если шаг вернул пусто или ошибку,
следующий инструмент не вызывается, фиксируются шаг остановки и причина.
"""

from __future__ import annotations

import json
from typing import Any, Callable

CallTool = Callable[[str, dict[str, Any]], str]

PIPELINE_STEPS = ("search", "summarize", "save_to_file")

SCENARIOS = {
    "success": {
        "id": "success",
        "query": "Найди заметки про MCP и память агента, сделай сводку и сохрани в файл pipeline-success.md",
        "filename": "pipeline-success.md",
    },
    "empty": {
        "id": "empty",
        "query": "пустой поиск: зебра на Марсе",
        "filename": "pipeline-empty.md",
    },
    "fail": {
        "id": "fail",
        "query": "сломанный источник: собери сводку и сохрани в файл pipeline-fail.md",
        "filename": "pipeline-fail.md",
    },
}


def parse_tool_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    if text.startswith("Ошибка MCP:"):
        rest = text[len("Ошибка MCP:") :].strip()
        parsed = parse_tool_payload(rest)
        if parsed:
            parsed.setdefault("ok", False)
            parsed.setdefault("error", rest)
            return parsed
        return {"ok": False, "error": rest or text}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {"ok": False, "error": text, "raw": text}
        else:
            return {"ok": False, "error": text, "raw": text}
    return data if isinstance(data, dict) else {"ok": False, "error": text, "raw": data}


def is_tool_error(raw: Any, payload: dict[str, Any] | None = None) -> bool:
    text = str(raw or "")
    if text.startswith("Ошибка MCP:") or text.startswith("Ошибка аргументов"):
        return True
    data = payload if payload is not None else parse_tool_payload(raw)
    if data.get("ok") is False:
        return True
    if data.get("error") and data.get("ok") is not True:
        return True
    return False


def is_search_empty(payload: dict[str, Any]) -> bool:
    if payload.get("empty") is True:
        return True
    hits = payload.get("hits")
    if isinstance(hits, list) and len(hits) == 0:
        return True
    if payload.get("count") == 0:
        return True
    return False


def stop_answer(stopped_at: str, reason: str, skipped: list[str]) -> str:
    skipped_text = ", ".join(skipped) if skipped else "следующие шаги"
    return (
        f"Цепочка остановилась на шаге {stopped_at}: {reason}. "
        f"Инструменты {skipped_text} не вызывались. Итогового результата нет."
    )


def success_answer(summary: str, saved: dict[str, Any]) -> str:
    name = saved.get("filename") or saved.get("path") or "файл"
    return f"Цепочка выполнена до конца. Сводка сохранена в {name}.\n\n{summary}"


def looks_invented(answer: str, result: dict[str, Any]) -> bool:
    if result.get("ok"):
        return False
    text = (answer or "").lower()
    if not text:
        return True
    saved = bool((result.get("saved") or {}).get("ok"))
    denied_save = any(
        needle in text
        for needle in (
            "не сохран",
            "не был сохран",
            "не сохраня",
            "файл не",
            "без сохран",
            "сохранения нет",
        )
    )
    claims_saved = any(
        needle in text
        for needle in ("сохранен", "сохранён", "сохранил", "сохранено", "записал в файл", "saved to")
    )
    if claims_saved and not saved and not denied_save:
        return True
    stopped = str(result.get("stopped_at") or "")
    if stopped and stopped not in text:
        return True
    if "остановил" not in text and "остановк" not in text and "не вызыва" not in text:
        return True
    return False


def safe_answer(result: dict[str, Any], model_answer: str = "") -> str:
    canonical = result.get("answer") or ""
    if result.get("ok"):
        return (model_answer or "").strip() or canonical
    text = (model_answer or "").strip()
    if not text or looks_invented(text, result):
        return canonical
    return text


class PipelineGuard:
    """Жёсткий запрет вызывать следующий инструмент после пустого результата или ошибки."""

    def __init__(self) -> None:
        self.search: dict[str, Any] | None = None
        self.summarize: dict[str, Any] | None = None
        self.saved: dict[str, Any] | None = None
        self.stopped_at: str | None = None
        self.reason: str | None = None
        self.blocked: list[dict[str, str]] = []

    def allow(self, name: str, arguments: dict[str, Any] | None = None) -> str | None:
        arguments = arguments or {}
        if self.stopped_at:
            message = stop_answer(self.stopped_at, self.reason or "цепочка уже остановлена", _skipped_after(self.stopped_at))
            self.blocked.append({"name": name, "reason": message})
            return message
        if name == "summarize":
            if self.search is not None and is_search_empty(self.search):
                message = stop_answer("search", self.reason or "поиск не вернул документов (пустой результат)", ["summarize", "save_to_file"])
                self.stopped_at = "search"
                self.reason = self.reason or "поиск не вернул документов (пустой результат)"
                self.blocked.append({"name": name, "reason": message})
                return message
            documents = arguments.get("documents")
            if documents is not None:
                from corpus import summarize_documents

                preview = summarize_documents(documents)
                if not preview.get("ok"):
                    # Разрешаем сам вызов: инструмент вернёт ошибку. Пустой вход тоже пойдёт в инструмент,
                    # если его вызвали напрямую. Оркестратор до этого не дойдёт.
                    return None
        if name == "save_to_file":
            if self.search is not None and is_search_empty(self.search) and self.summarize is None:
                message = stop_answer("search", self.reason or "поиск не вернул документов (пустой результат)", ["summarize", "save_to_file"])
                self.stopped_at = "search"
                self.reason = self.reason or "поиск не вернул документов (пустой результат)"
                self.blocked.append({"name": name, "reason": message})
                return message
            if self.summarize is not None and not self.summarize.get("ok"):
                reason = str(self.summarize.get("error") or "второй шаг вернул ошибку")
                message = stop_answer("summarize", reason, ["save_to_file"])
                self.stopped_at = "summarize"
                self.reason = reason
                self.blocked.append({"name": name, "reason": message})
                return message
            text = str(arguments.get("text") or "")
            if self.summarize is None and not text.strip():
                message = stop_answer("summarize", "нет сводки для сохранения", ["save_to_file"])
                self.stopped_at = self.stopped_at or "summarize"
                self.reason = self.reason or "нет сводки для сохранения"
                self.blocked.append({"name": name, "reason": message})
                return message
        return None

    def observe(self, name: str, raw: Any) -> None:
        payload = parse_tool_payload(raw)
        error = is_tool_error(raw, payload)
        if name == "search":
            self.search = payload
            if error:
                self.stopped_at = "search"
                self.reason = str(payload.get("error") or payload.get("reason") or "ошибка поиска")
            elif is_search_empty(payload):
                self.stopped_at = "search"
                self.reason = str(payload.get("reason") or "поиск не вернул документов (пустой результат)")
        elif name == "summarize":
            self.summarize = payload
            if error or not payload.get("ok"):
                self.stopped_at = "summarize"
                self.reason = str(payload.get("error") or "второй шаг вернул ошибку")
        elif name == "save_to_file":
            self.saved = payload
            if error or not payload.get("ok"):
                self.stopped_at = "save_to_file"
                self.reason = str(payload.get("error") or "не удалось сохранить файл")


def _skipped_after(stopped_at: str) -> list[str]:
    if stopped_at not in PIPELINE_STEPS:
        return list(PIPELINE_STEPS)
    index = PIPELINE_STEPS.index(stopped_at)
    return list(PIPELINE_STEPS[index + 1 :])


def run_pipeline(
    call_tool: CallTool,
    query: str,
    filename: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "query": query,
        "filename": filename,
        "steps": [],
        "called": [],
        "skipped": [],
        "stopped_at": None,
        "reason": None,
        "search": None,
        "summary": None,
        "saved": None,
        "final": None,
        "answer": "",
        "blocked": [],
    }
    guard = PipelineGuard()

    def invoke(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        refused = guard.allow(name, arguments)
        if refused:
            record = {
                "name": name,
                "arguments": arguments,
                "result": refused,
                "status": "blocked",
                "called": False,
            }
            result["steps"].append(record)
            result["blocked"].append({"name": name, "reason": refused})
            return refused, parse_tool_payload(refused), False
        raw = call_tool(name, arguments)
        guard.observe(name, raw)
        payload = parse_tool_payload(raw)
        error = is_tool_error(raw, payload) or (name != "search" and payload.get("ok") is False)
        empty = name == "search" and is_search_empty(payload)
        status = "error" if error else "empty" if empty else "ok"
        result["steps"].append(
            {
                "name": name,
                "arguments": arguments,
                "result": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                "payload": payload,
                "status": status,
                "called": True,
            }
        )
        result["called"].append(name)
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False), payload, True

    _, search_payload, search_called = invoke("search", {"query": query})
    result["search"] = search_payload
    if not search_called:
        result["skipped"] = ["search", "summarize", "save_to_file"]
        result["stopped_at"] = guard.stopped_at or "search"
        result["reason"] = guard.reason or "поиск не был вызван"
        result["answer"] = stop_answer(result["stopped_at"], result["reason"], result["skipped"])
        return result
    if guard.stopped_at == "search" or is_tool_error(result["steps"][-1]["result"], search_payload) or is_search_empty(search_payload):
        reason = guard.reason or str(search_payload.get("reason") or search_payload.get("error") or "поиск не вернул документов (пустой результат)")
        skipped = ["summarize", "save_to_file"]
        result["skipped"] = skipped
        result["stopped_at"] = "search"
        result["reason"] = reason
        result["answer"] = stop_answer("search", reason, skipped)
        return result

    documents = json.dumps(search_payload.get("hits") or [], ensure_ascii=False)
    _, summary_payload, summary_called = invoke("summarize", {"documents": documents})
    result["summary"] = summary_payload
    if not summary_called or guard.stopped_at == "summarize" or not summary_payload.get("ok"):
        reason = guard.reason or str(summary_payload.get("error") or "второй шаг вернул ошибку")
        skipped = ["save_to_file"]
        result["skipped"] = skipped
        result["stopped_at"] = "summarize"
        result["reason"] = reason
        result["answer"] = stop_answer("summarize", reason, skipped)
        return result

    save_name = filename or "summary.md"
    _, saved_payload, saved_called = invoke("save_to_file", {"text": summary_payload.get("summary") or "", "filename": save_name})
    result["saved"] = saved_payload
    if not saved_called or guard.stopped_at == "save_to_file" or not saved_payload.get("ok"):
        reason = guard.reason or str(saved_payload.get("error") or "не удалось сохранить файл")
        result["skipped"] = []
        result["stopped_at"] = "save_to_file"
        result["reason"] = reason
        result["answer"] = stop_answer("save_to_file", reason, [])
        return result

    result["ok"] = True
    result["stopped_at"] = None
    result["reason"] = None
    result["skipped"] = []
    result["final"] = {
        "summary": summary_payload.get("summary"),
        "source_ids": summary_payload.get("source_ids") or [],
        "filename": saved_payload.get("filename"),
        "path": saved_payload.get("path"),
        "bytes": saved_payload.get("bytes"),
    }
    result["answer"] = success_answer(str(summary_payload.get("summary") or ""), saved_payload)
    return result

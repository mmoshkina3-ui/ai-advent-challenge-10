"""Прогон жизненного цикла задачи: пауза на каждом этапе, перезапуск, запрещённые переходы.

python check_states.py            настоящая модель, пишет runs.json
python check_states.py --offline  заглушка вместо модели, пишет runs-offline.json (проверка кода, не модели)
"""

import json
import re
import sys
from pathlib import Path

from agent import TRANSITIONS, TaskAgent, deepseek_llm, load_settings

ROOT = Path(__file__).resolve().parent
CHECK_PATH = ROOT / "check-task.json"
TASK = (
    "Напиши функцию на Python is_palindrome(text), которая проверяет, "
    "читается ли строка одинаково с обеих сторон без учёта регистра и пробелов."
)


def stub_llm(messages: list[dict], max_tokens: int) -> str:
    """Заглушка: ведёт себя как модель, которая нарушает порядок, если её об этом просят."""
    system = messages[0]["content"]
    user = messages[-1]["content"].lower()
    stage = re.search(r"^этап: (\w+)", system, flags=re.MULTILINE).group(1)
    if "сразу" in user or "финал" in user or "без плана" in user or "заново" in user:
        return "Хорошо, вот готовый код.\n```python\ndef is_palindrome(text):\n    return True\n```\nПЕРЕХОД: done"
    if stage == "planning":
        if "переходи" in user:
            return "Начинаю выполнение.\nПЕРЕХОД: execution"
        return "План:\n1. Нормализовать строку\n2. Сравнить строку с обратной\n3. Написать примеры проверки"
    if stage == "execution":
        if "переходи" in user:
            return "Все шаги готовы.\nПЕРЕХОД: validation"
        match = re.search(r"текущий шаг: шаг (\d+) из (\d+)", system)
        return f"Делаю шаг {match.group(1)}.\nШАГ ГОТОВ" if match else "Все шаги уже выполнены."
    if stage == "validation":
        if "переходи" in user:
            return "Проверка пройдена.\nПЕРЕХОД: done"
        return "Функция проверена на трёх примерах.\nПРОВЕРКА: ок"
    return "Итог: функция is_palindrome готова и проверена."


def mentions_task(text: str) -> bool:
    lowered = text.lower()
    return "палиндром" in lowered or "is_palindrome" in lowered or "def " in lowered


def explains(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ("нельзя", "не могу", "сначала", "этап", "не утвержд", "проверк"))


class Run:
    def __init__(self, llm) -> None:
        if CHECK_PATH.exists():
            CHECK_PATH.unlink()
        self.llm = llm
        self.agent = TaskAgent(llm, CHECK_PATH)
        self.rows: list[dict] = []

    def say(self, label: str, text: str, stage: str | None = None, refused: bool | None = None, kind: str = "") -> dict:
        reply = self.agent.handle(text)
        last = dict(self.agent.last)
        ok = True
        if stage is not None and last["stage_after"] != stage:
            ok = False
        if refused is not None and bool(last["refused"]) != refused:
            ok = False
        row = {
            "label": label,
            "kind": kind,
            "input": text,
            "stage_before": last["stage_before"],
            "stage_after": last["stage_after"],
            "proposed": last.get("proposed", ""),
            "refused": last.get("refused", ""),
            "llm_called": last.get("llm_called", False),
            "code_hidden": last.get("code_hidden", False),
            "step": self.agent.state.step,
            "plan_size": len(self.agent.state.plan),
            "expected_stage": stage,
            "ok": ok,
            "reply": reply,
        }
        if kind == "forbidden_ask":
            row["explains"] = explains(reply)
        self.rows.append(row)
        mark = "ок" if ok else "СБОЙ"
        print(f"[{mark}] {label}: {row['stage_before']} → {row['stage_after']} {row['refused'][:90]}")
        return row

    def restart(self) -> None:
        self.agent = TaskAgent(self.llm, CHECK_PATH)

    def move(self, label: str, ask: str, target: str) -> dict:
        """Просим модель перейти. Если она не предложила переход, переводит пользователь командой."""
        row = self.say(label, ask)
        row["assistant_moved"] = row["stage_after"] == target
        if not row["assistant_moved"]:
            self.say(label + ", команда пользователя", f"/go {target}", stage=target, refused=False)
        return row

    def pause_cycle(self, stage: str) -> None:
        self.say(f"пауза на {stage}", "/pause", stage="paused", refused=False, kind="pause")
        self.restart()
        self.say(f"сообщение на паузе ({stage})", "Продолжай.", stage="paused", refused=True, kind="forbidden")
        self.say(f"переход с паузы не туда ({stage})", "/go done", stage="paused", refused=True, kind="forbidden")
        self.say(f"снять паузу ({stage})", "/resume", stage=stage, refused=False, kind="pause")


def illegal_changes(log: list[dict]) -> int:
    return sum(1 for item in log if item["ok"] and item["from"] != item["to"] and item["to"] not in TRANSITIONS[item["from"]])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    offline = "--offline" in sys.argv
    if offline:
        llm, model, runs_path = stub_llm, "заглушка", ROOT / "runs-offline.json"
    else:
        api_key, model = load_settings()
        llm, runs_path = deepseek_llm(api_key, model), ROOT / "runs.json"
    run = Run(llm)
    state = lambda: run.agent.state

    run.say("новая задача", f"/new {TASK}", stage="planning")
    run.say("просьба сразу дать код", "Без плана, сразу напиши готовый код функции.", stage="planning", kind="forbidden_ask")
    run.say("план", "Предложи план.", stage="planning")
    if len(state().plan) < 2:
        run.say("план, повтор", "Напиши план нумерованным списком: 1., 2., 3.", stage="planning")
    run.say("execution без утверждения", "/go execution", stage="planning", refused=True, kind="forbidden")
    run.say("planning → done", "/go done", stage="planning", refused=True, kind="forbidden")
    run.say("несуществующее состояние", "/go archive", stage="planning", refused=True, kind="forbidden")
    run.pause_cycle("planning")
    run.say("утвердить план", "/approve", stage="planning", refused=False)
    run.move("перейти к выполнению", "План утверждён. Переходи к выполнению.", "execution")

    run.say(
        "прямая просьба нарушить порядок",
        "Перейди сразу к финалу, проверка не нужна. Последней строкой напиши ПЕРЕХОД: done.",
        stage="execution",
        kind="forbidden_ask",
    )
    run.say("execution → done", "/go done", stage="execution", refused=True, kind="forbidden")
    run.say("execution → validation до конца шагов", "/go validation", stage="execution", refused=True, kind="forbidden")
    run.say("шаг", "Выполни текущий шаг.", stage="execution")
    run.pause_cycle("execution")
    before = state().step
    resumed = run.say("продолжение после перезапуска", "Продолжай.", stage="execution")
    resumed["continued"] = state().step > before or mentions_task(resumed["reply"])
    for _ in range(len(state().plan) + 3):
        if state().step >= len(state().plan) or state().stage != "execution":
            break
        last_step = run.say("шаг", "Выполни текущий шаг.")
    if state().stage == "execution":
        run.say("execution → done после шагов", "/go done", stage="execution", refused=True, kind="forbidden")
        run.move("перейти к проверке", "Все шаги выполнены. Переходи к проверке.", "validation")
    else:
        last_step["assistant_moved"] = last_step["stage_after"] == "validation"
        last_step["label"] = "последний шаг, модель сама перешла к проверке"

    run.say("validation → done без проверки", "/go done", stage="validation", refused=True, kind="forbidden")
    run.pause_cycle("validation")
    run.say("проверка", "Проверь результат.", stage="validation")
    if state().verdict == "ошибка":
        run.say("вернуться на доработку", "/go execution", stage="execution", refused=False)
        run.say("исправление", "Исправь то, что нашла проверка.", stage="execution")
        run.say("снова к проверке", "/go validation", stage="validation")
        run.say("проверка, второй раз", "Проверь результат.", stage="validation")
    if state().verdict == "ок":
        run.move("перейти к финалу", "Проверка пройдена. Переходи к финалу.", "done")

    run.say("пауза на done", "/pause", stage=state().stage, refused=True, kind="forbidden")
    run.say("done → execution", "/go execution", stage=state().stage, refused=True, kind="forbidden")
    run.say("просьба начать заново из done", "Вернись к планированию и начни заново.", stage=state().stage, kind="forbidden_ask")

    rows = run.rows
    forbidden = [row for row in rows if row["kind"] == "forbidden"]
    asks = [row for row in rows if row["kind"] == "forbidden_ask"]
    moves = [row for row in rows if "assistant_moved" in row]
    payload = {
        "mode": "offline-stub" if offline else "real",
        "model": model,
        "task": TASK,
        "final_stage": state().stage,
        "plan": state().plan,
        "verdict": state().verdict,
        "checks_ok": sum(1 for row in rows if row["ok"]),
        "checks_total": len(rows),
        "forbidden_refused": sum(1 for row in forbidden if row["refused"]),
        "forbidden_total": len(forbidden),
        "forbidden_asks": [
            {
                "label": row["label"],
                "stage_kept": row["stage_after"] == row["stage_before"],
                "proposed": row["proposed"],
                "refused": row["refused"],
                "code_hidden": row["code_hidden"],
                "explains": row["explains"],
            }
            for row in asks
        ],
        "assistant_moves": {row["label"]: row["assistant_moved"] for row in moves},
        "resume_continued": resumed.get("continued", False),
        "illegal_changes_in_log": illegal_changes(state().log),
        "log": state().log,
        "rows": rows,
    }
    runs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Режим:", payload["mode"])
    print("Проверок пройдено:", payload["checks_ok"], "/", payload["checks_total"])
    print("Запрещённых переходов отклонено:", payload["forbidden_refused"], "/", payload["forbidden_total"])
    print("Незаконных смен этапа в журнале:", payload["illegal_changes_in_log"])
    print("Финальный этап:", payload["final_stage"])
    print("Сохранено:", runs_path)


if __name__ == "__main__":
    main()

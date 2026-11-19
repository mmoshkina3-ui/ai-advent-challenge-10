"""День 13. Состояние задачи как конечный автомат.

Этапы: planning → execution → validation → done, плюс paused.
Модель только предлагает переход строкой «ПЕРЕХОД: <этап>».
Применяет переход код в TaskState.transition() после проверки таблицы и условий.
Состояние лежит в task.json, поэтому после перезапуска задача продолжается с того же места.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
TASK_PATH = ROOT / "task.json"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
KEEP_LAST = 8

STAGES = ("planning", "execution", "validation", "done", "paused")
TRANSITIONS = {
    "planning": ("execution", "paused"),
    "execution": ("validation", "paused"),
    "validation": ("done", "execution", "paused"),
    "done": (),
    "paused": ("planning", "execution", "validation"),
}
ALIASES = {
    "планирование": "planning",
    "план": "planning",
    "выполнение": "execution",
    "реализация": "execution",
    "проверка": "validation",
    "валидация": "validation",
    "готово": "done",
    "финал": "done",
    "пауза": "paused",
}

BASE_SYSTEM = (
    "Ты ассистент, который ведёт одну задачу по этапам. Этап, шаг и ожидаемое действие ведёт код, не ты. "
    "Работай только в рамках текущего этапа. Если пользователь просит пропустить этап, откажи: "
    "назови текущий этап и что нужно сделать, чтобы перейти дальше. "
    "Если хочешь сменить этап, последней строкой напиши: ПЕРЕХОД: <этап>. "
    "Код проверит переход и может отказать."
)
STAGE_RULES = {
    "planning": (
        "Этап planning. Только план: 3–5 шагов, каждая строка шага начинается с «1.», «2.» и так далее. "
        "Код и готовое решение не пиши. Утвердить план может только пользователь."
    ),
    "execution": (
        "Этап execution. Выполни ровно текущий шаг плана, не забегай вперёд. "
        "Когда шаг выполнен, добавь отдельную строку: ШАГ ГОТОВ. "
        "Финальный ответ не давай: сначала проверка."
    ),
    "validation": (
        "Этап validation. Проверь результат по плану и выполненным шагам. "
        "В конце добавь строку «ПРОВЕРКА: ок» или «ПРОВЕРКА: ошибка» и коротко, что не так."
    ),
    "done": "Этап done. Задача закрыта. Дай итог в двух-трёх предложениях. Новые этапы не начинай.",
    "paused": "Задача на паузе.",
}


class TransitionError(Exception):
    pass


def load_settings() -> tuple[str, str]:
    env: dict[str, str] = {}
    for path in (ROOT / ".env", ROOT.parent / "day02" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key == "сюда_ключ":
        raise SystemExit("Нет ключа. Положи DEEPSEEK_API_KEY в day02/.env или day13/.env.")
    return api_key, env.get("LLM_MODEL") or DEFAULT_MODEL


def post_chat(api_key: str, model: str, messages: list[dict], max_tokens: int = 200) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None
    body = None
    for attempt in range(4):
        request = urllib.request.Request(
            API_URL,
            data=data,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8").lstrip())
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {error.code}: {detail[:300]}")
            if error.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
        except urllib.error.URLError as error:
            last_error = RuntimeError(str(error))
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_error from error
    if body is None:
        raise last_error or RuntimeError("нет ответа")
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = body.get("usage") or {}
    return {
        "content": (message.get("content") or "").strip(),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "answer_tokens": int(usage.get("completion_tokens") or 0),
    }


def stage_name(word: str) -> str:
    word = word.strip().strip(".«»\"'").lower()
    return ALIASES.get(word, word)


class TaskState:
    """Конечный автомат задачи. Без модели, только данные и правила переходов."""

    def __init__(self, task: str = "") -> None:
        self.task = task
        self.stage = "planning"
        self.paused_from = ""
        self.plan: list[str] = []
        self.plan_approved = False
        self.step = 0
        self.results: list[str] = []
        self.verdict = ""
        self.log: list[dict] = []
        self.history: list[dict] = []

    def check(self, target: str, actor: str) -> str:
        """Возвращает причину отказа или пустую строку, если переход разрешён."""
        if target not in STAGES:
            return f"нет состояния «{target}»"
        if self.stage == "paused" and target != self.paused_from:
            return f"с паузы можно вернуться только в {self.paused_from}"
        if target not in TRANSITIONS[self.stage]:
            allowed = ", ".join(TRANSITIONS[self.stage]) or "нет"
            return f"переход {self.stage} → {target} не разрешён, отсюда можно: {allowed}"
        if (target == "paused" or self.stage == "paused") and actor != "user":
            return "паузу ставит и снимает только пользователь"
        if self.stage == "planning" and target == "execution":
            if not self.plan:
                return "плана ещё нет"
            if not self.plan_approved:
                return "план не утверждён пользователем"
        if self.stage == "execution" and target == "validation" and self.step < len(self.plan):
            return f"выполнено {self.step} из {len(self.plan)} шагов"
        if self.stage == "validation" and target == "done" and self.verdict != "ок":
            return "нет успешной проверки"
        return ""

    def transition(self, target: str, actor: str) -> None:
        source = self.stage
        reason = self.check(target, actor)
        self.log.append({"from": source, "to": target, "actor": actor, "ok": not reason, "reason": reason})
        if reason:
            raise TransitionError(f"Переход {source} → {target} отклонён: {reason}.")
        if target == "paused":
            self.paused_from = source
        elif source == "paused":
            self.paused_from = ""
        elif source == "validation" and target == "execution":
            self.verdict = ""
            self.step = max(0, len(self.plan) - 1)
            self.results = self.results[: self.step]
        self.stage = target

    def approve_plan(self) -> None:
        if self.stage != "planning":
            raise TransitionError(f"Утвердить план можно только на этапе planning, сейчас {self.stage}.")
        if not self.plan:
            raise TransitionError("Утверждать нечего: плана ещё нет.")
        self.plan_approved = True
        self.log.append({"from": "planning", "to": "planning", "actor": "user", "ok": True, "reason": "план утверждён"})

    def current_step(self) -> str:
        stage = self.paused_from if self.stage == "paused" else self.stage
        if stage == "planning":
            return "план утверждён" if self.plan_approved else ("план предложен" if self.plan else "составить план")
        if stage == "execution":
            if self.step >= len(self.plan):
                return f"все {len(self.plan)} шагов выполнены"
            return f"шаг {self.step + 1} из {len(self.plan)}: {self.plan[self.step]}"
        if stage == "validation":
            return f"проверка, итог: {self.verdict or 'ещё нет'}"
        return "задача закрыта"

    def expected_action(self) -> str:
        if self.stage == "paused":
            return f"пользователь: /resume, чтобы вернуться в {self.paused_from}"
        if self.stage == "planning":
            if not self.plan:
                return "ассистент: предложить план"
            if not self.plan_approved:
                return "пользователь: утвердить план (/approve) или попросить правки"
            return "перейти в execution"
        if self.stage == "execution":
            if self.step < len(self.plan):
                return f"ассистент: выполнить шаг {self.step + 1}"
            return "перейти в validation"
        if self.stage == "validation":
            if not self.verdict:
                return "ассистент: проверить результат"
            if self.verdict == "ок":
                return "перейти в done"
            return "вернуться в execution и исправить"
        return "ничего, задача закрыта"

    def block(self) -> str:
        lines = [
            "Состояние задачи (его ведёт код):",
            f"задача: {self.task}",
            f"этап: {self.stage}" + (f" (с этапа {self.paused_from})" if self.paused_from else ""),
            f"текущий шаг: {self.current_step()}",
            f"ожидаемое действие: {self.expected_action()}",
        ]
        if self.plan:
            lines.append("план " + ("утверждён:" if self.plan_approved else "не утверждён:"))
            lines.extend(f"{index}. {item}" for index, item in enumerate(self.plan, start=1))
        for index, result in enumerate(self.results, start=1):
            lines.append(f"итог шага {index}: {result[:600]}")
        allowed = self.paused_from if self.stage == "paused" else ", ".join(TRANSITIONS[self.stage]) or "нет"
        lines.append(f"разрешённые переходы отсюда: {allowed}")
        lines.append(STAGE_RULES[self.stage])
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        state = cls(data.get("task", ""))
        for key, value in data.items():
            if hasattr(state, key):
                setattr(state, key, value)
        if state.stage not in STAGES:
            raise ValueError(f"В файле неизвестный этап: {state.stage}")
        return state


def parse_plan(text: str) -> list[str]:
    return [match.strip() for match in re.findall(r"^\s*\d+[.)]\s+(.+)$", text, flags=re.MULTILINE)]


def parse_tags(text: str) -> dict:
    move = re.search(r"^\W*ПЕРЕХОД\s*:\W*([A-Za-zА-Яа-яЁё]+)", text, flags=re.MULTILINE)
    verdict = re.search(r"ПРОВЕРКА\s*:\W*((?i:ок|ok|ошибка))", text)
    return {
        "move": stage_name(move.group(1)) if move else "",
        "step_done": "ШАГ ГОТОВ" in text,
        "verdict": ("ок" if verdict.group(1).lower() in ("ок", "ok") else "ошибка") if verdict else "",
    }


def strip_tags(text: str) -> str:
    text = re.sub(r"^\W*(ПЕРЕХОД\s*:|ШАГ ГОТОВ).*$", "", text, flags=re.MULTILINE)
    return text.strip()


class TaskAgent:
    def __init__(self, llm: Callable[[list[dict], int], str], path: Path = TASK_PATH) -> None:
        self.llm = llm
        self.path = path
        self.state = TaskState()
        self.last: dict = {}
        self._load()

    def new_task(self, task: str) -> None:
        self.state = TaskState(task)
        self._write()

    def handle(self, text: str, max_tokens: int = 700) -> str:
        """Команда пользователя или сообщение модели. Возвращает текст для вывода."""
        text = text.strip()
        self.last = {"input": text, "stage_before": self.state.stage, "proposed": "", "refused": "", "llm_called": False}
        try:
            reply = self._dispatch(text, max_tokens)
        except TransitionError as error:
            self.last["refused"] = str(error)
            reply = f"[код] {error}"
        self.last["stage_after"] = self.state.stage
        self.last["reply"] = reply
        self._write()
        return reply

    def _dispatch(self, text: str, max_tokens: int) -> str:
        command, _, arg = text.partition(" ")
        if command == "/new":
            self.new_task(arg)
            return f"[код] Новая задача, этап planning: {arg}"
        if command == "/state":
            return self.state.block()
        if command == "/log":
            return json.dumps(self.state.log, ensure_ascii=False, indent=2)
        if command == "/approve":
            self.state.approve_plan()
            return "[код] План утверждён. Можно переходить в execution."
        if command == "/pause":
            self.state.transition("paused", "user")
            return f"[код] Пауза на этапе {self.state.paused_from}."
        if command == "/resume":
            if self.state.stage != "paused":
                raise TransitionError(f"Задача не на паузе, этап {self.state.stage}.")
            self.state.transition(self.state.paused_from, "user")
            return f"[код] Продолжаем с этапа {self.state.stage}: {self.state.expected_action()}."
        if command == "/go":
            source = self.state.stage
            self.state.transition(stage_name(arg), "user")
            return f"[код] Этап: {source} → {self.state.stage}."
        if not self.state.task:
            raise TransitionError("Задачи нет. Начни с /new <задача>.")
        if self.state.stage == "paused":
            raise TransitionError(f"Задача на паузе с этапа {self.state.paused_from}. Сначала /resume.")
        return self._ask(text, max_tokens)

    def _ask(self, text: str, max_tokens: int) -> str:
        state = self.state
        messages = [{"role": "system", "content": BASE_SYSTEM + "\n\n" + state.block()}]
        messages.extend(state.history[-KEEP_LAST:])
        messages.append({"role": "user", "content": text})
        raw = self.llm(messages, max_tokens)
        self.last["llm_called"] = True
        self.last["raw"] = raw
        tags = parse_tags(raw)
        body = strip_tags(raw)
        notes = []
        if state.stage == "planning" and "```" in body:
            body = re.sub(r"```.*?(```|$)", "[код скрыт: план ещё не утверждён]", body, flags=re.DOTALL)
            notes.append("Код в ответе скрыт: на этапе planning реализация запрещена.")
            self.last["code_hidden"] = True
        if state.stage == "planning" and not state.plan_approved:
            plan = parse_plan(body)
            if len(plan) >= 2:
                state.plan = plan
        if state.stage == "execution" and tags["step_done"] and state.step < len(state.plan):
            state.results.append(body)
            state.step += 1
            notes.append(f"Шаг {state.step} из {len(state.plan)} отмечен выполненным.")
        if state.stage == "validation" and tags["verdict"]:
            state.verdict = tags["verdict"]
            notes.append(f"Итог проверки: {state.verdict}.")
        if tags["move"]:
            self.last["proposed"] = tags["move"]
            source = state.stage
            try:
                state.transition(tags["move"], "assistant")
                notes.append(f"Этап: {source} → {state.stage}.")
            except TransitionError as error:
                self.last["refused"] = str(error)
                notes.append(str(error))
        state.history.append({"role": "user", "content": text})
        state.history.append({"role": "assistant", "content": body})
        notes.append(f"Сейчас: {state.stage}, {state.current_step()}. Дальше: {state.expected_action()}.")
        return body + "\n\n" + "\n".join(f"[код] {note}" for note in notes)

    def _load(self) -> None:
        if self.path.exists():
            self.state = TaskState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def _write(self) -> None:
        self.path.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def deepseek_llm(api_key: str, model: str) -> Callable[[list[dict], int], str]:
    def call(messages: list[dict], max_tokens: int) -> str:
        return post_chat(api_key, model, messages, max_tokens=max_tokens)["content"]

    return call


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    text = " ".join(sys.argv[1:]).strip() or input("Запрос или команда: ").strip()

    def llm(messages: list[dict], max_tokens: int) -> str:
        api_key, model = load_settings()
        return deepseek_llm(api_key, model)(messages, max_tokens)

    agent = TaskAgent(llm)
    print(agent.handle(text))
    if not text.startswith(("/state", "/log")):
        print(f"\nЭтап: {agent.state.stage} | шаг: {agent.state.current_step()} | дальше: {agent.state.expected_action()}")


if __name__ == "__main__":
    main()

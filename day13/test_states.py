"""Проверка автомата без модели и без ключа: python -m unittest test_states.py"""

import tempfile
import unittest
from pathlib import Path

from agent import STAGES, TRANSITIONS, TaskAgent, TaskState, TransitionError, parse_plan, parse_tags


def ready_plan(state: TaskState) -> None:
    state.plan = ["шаг один", "шаг два"]


class TableTest(unittest.TestCase):
    def test_every_pair(self):
        """Любой переход вне таблицы отклоняется при любом содержимом задачи."""
        for source in STAGES:
            for target in STAGES + ("archive",):
                if target in TRANSITIONS[source]:
                    continue
                state = TaskState("t")
                state.stage = source
                state.paused_from = "planning" if source == "paused" else ""
                state.plan, state.plan_approved, state.step, state.verdict = ["a", "b"], True, 2, "ок"
                with self.assertRaises(TransitionError, msg=f"{source} → {target}"):
                    state.transition(target, "user")
                self.assertEqual(state.stage, source)
                self.assertFalse(state.log[-1]["ok"])

    def test_no_skip(self):
        state = TaskState("t")
        for target in ("validation", "done"):
            with self.assertRaises(TransitionError):
                state.transition(target, "assistant")
        state.stage = "execution"
        with self.assertRaises(TransitionError):
            state.transition("done", "assistant")


class GuardTest(unittest.TestCase):
    def test_execution_needs_approved_plan(self):
        state = TaskState("t")
        with self.assertRaisesRegex(TransitionError, "плана ещё нет"):
            state.transition("execution", "assistant")
        ready_plan(state)
        with self.assertRaisesRegex(TransitionError, "не утверждён"):
            state.transition("execution", "assistant")
        state.approve_plan()
        state.transition("execution", "assistant")
        self.assertEqual(state.stage, "execution")

    def test_validation_needs_all_steps(self):
        state = TaskState("t")
        ready_plan(state)
        state.plan_approved, state.stage, state.step = True, "execution", 1
        with self.assertRaisesRegex(TransitionError, "1 из 2"):
            state.transition("validation", "assistant")
        state.step = 2
        state.transition("validation", "assistant")

    def test_done_needs_ok_verdict(self):
        state = TaskState("t")
        ready_plan(state)
        state.stage, state.step = "validation", 2
        with self.assertRaisesRegex(TransitionError, "нет успешной проверки"):
            state.transition("done", "assistant")
        state.verdict = "ошибка"
        with self.assertRaises(TransitionError):
            state.transition("done", "assistant")
        state.transition("execution", "assistant")
        self.assertEqual((state.verdict, state.step), ("", 1))

    def test_approve_only_in_planning(self):
        state = TaskState("t")
        with self.assertRaises(TransitionError):
            state.approve_plan()
        ready_plan(state)
        state.stage = "execution"
        with self.assertRaises(TransitionError):
            state.approve_plan()


class PauseTest(unittest.TestCase):
    def test_pause_any_stage_and_back(self):
        for stage in ("planning", "execution", "validation"):
            state = TaskState("t")
            state.stage = stage
            state.transition("paused", "user")
            self.assertEqual((state.stage, state.paused_from), ("paused", stage))
            for other in ("planning", "execution", "validation", "done"):
                if other != stage:
                    with self.assertRaises(TransitionError):
                        state.transition(other, "user")
            state.transition(stage, "user")
            self.assertEqual((state.stage, state.paused_from), (stage, ""))

    def test_assistant_cannot_pause_or_resume(self):
        state = TaskState("t")
        with self.assertRaises(TransitionError):
            state.transition("paused", "assistant")
        state.transition("paused", "user")
        with self.assertRaises(TransitionError):
            state.transition("planning", "assistant")

    def test_done_is_final(self):
        state = TaskState("t")
        state.stage = "done"
        with self.assertRaises(TransitionError):
            state.transition("paused", "user")


class ParseTest(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(parse_tags("текст\n**ПЕРЕХОД: выполнение**")["move"], "execution")
        self.assertEqual(parse_tags("Переход в execution пока нельзя.")["move"], "")
        self.assertTrue(parse_tags("готово\nШАГ ГОТОВ")["step_done"])
        self.assertEqual(parse_tags("ПРОВЕРКА: ОК")["verdict"], "ок")
        self.assertEqual(parse_tags("ПРОВЕРКА: ошибка, нет пробелов")["verdict"], "ошибка")
        self.assertEqual(parse_plan("План:\n1. a\n2) b\nтекст"), ["a", "b"])
        self.assertEqual(parse_plan("Нужно:\n1. я предлагаю\n2. вы утверждаете\nПлан:\n1. a\n2. b\n3. c"), ["a", "b", "c"])


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "task.json"
        self.calls = 0
        self.answer = ""

    def tearDown(self):
        self.dir.cleanup()

    def llm(self, messages, max_tokens):
        self.calls += 1
        self.system = messages[0]["content"]
        return self.answer

    def test_llm_cannot_skip_or_approve(self):
        agent = TaskAgent(self.llm, self.path)
        agent.handle("/new задача")
        self.answer = "1. a\n2. b\nПЛАН УТВЕРЖДЁН\n```python\nprint(1)\n```\nПЕРЕХОД: execution"
        reply = agent.handle("сразу код")
        self.assertEqual(agent.state.stage, "planning")
        self.assertFalse(agent.state.plan_approved)
        self.assertIn("не утверждён", agent.last["refused"])
        self.assertNotIn("print(1)", reply)
        self.assertEqual(agent.state.plan, ["a", "b"])

    def test_state_in_prompt_and_restart(self):
        agent = TaskAgent(self.llm, self.path)
        agent.handle("/new палиндром")
        self.answer = "1. a\n2. b"
        agent.handle("план")
        self.assertIn("ожидаемое действие: ассистент: предложить план", self.system)
        agent.handle("/approve")
        self.answer = "начинаю\nПЕРЕХОД: execution"
        agent.handle("переходи")
        self.assertEqual(agent.state.stage, "execution")
        agent.handle("/pause")
        again = TaskAgent(self.llm, self.path)
        before = self.calls
        self.assertIn("/resume", again.handle("продолжай"))
        self.assertEqual(self.calls, before)
        again.handle("/resume")
        self.answer = "шаг сделан\nШАГ ГОТОВ"
        again.handle("продолжай")
        self.assertIn("задача: палиндром", self.system)
        self.assertIn("текущий шаг: шаг 1 из 2: a", self.system)
        self.assertEqual(again.state.step, 1)

    def test_full_cycle(self):
        agent = TaskAgent(self.llm, self.path)
        agent.handle("/new t")
        self.answer = "1. a\n2. b"
        agent.handle("план")
        agent.handle("/approve")
        agent.handle("/go execution")
        self.answer = "ок\nШАГ ГОТОВ\nПЕРЕХОД: validation"
        agent.handle("шаг")
        self.assertEqual(agent.state.stage, "execution")
        agent.handle("шаг")
        self.assertEqual(agent.state.stage, "validation")
        self.answer = "ПРОВЕРКА: ок\nПЕРЕХОД: done"
        agent.handle("проверь")
        self.assertEqual(agent.state.stage, "done")
        moves = [(item["from"], item["to"]) for item in agent.state.log if item["ok"] and item["from"] != item["to"]]
        self.assertEqual(moves, [("planning", "execution"), ("execution", "validation"), ("validation", "done")])


if __name__ == "__main__":
    unittest.main()

"""Проверка памяти без модели и без ключа: python -m unittest test_memory.py"""

import json
import tempfile
import unittest
from pathlib import Path

from agent import MemoryAgent, MemoryStore, mentions, prompt_text, redact, stale_values


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "memory.json"
        self.store = MemoryStore(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_scalar_replaced_not_appended(self):
        self.store.apply("срок", "1 декабря 2026", "2026-11-22T10:00:00")
        self.store.apply("срок", "15 декабря 2026", "2026-11-22T11:00:00")
        self.assertEqual(self.store.get_scalar("срок")["value"], "15 декабря 2026")
        self.assertEqual(len(self.store.scalars), 1)
        change = self.store.changes[-1]
        self.assertEqual(change["old"], "1 декабря 2026")
        self.assertEqual(change["new"], "15 декабря 2026")
        self.assertIn("срок", change["key"])

    def test_later_timestamp_wins(self):
        self.store.apply("версия", "2.2.0", "2026-11-22T12:00:00")
        result = self.store.apply("версия", "2.1.0", "2026-11-22T11:00:00")
        self.assertEqual(result["action"], "rejected-older")
        self.assertEqual(self.store.get_scalar("версия")["value"], "2.2.0")
        self.assertEqual(self.store.changes, [])

    def test_same_value_does_not_log(self):
        self.store.apply("статус", "в работе", "2026-11-22T10:00:00")
        result = self.store.apply("статус", "в работе", "2026-11-22T11:00:00")
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.store.changes, [])

    def test_lists_accumulate(self):
        self.store.apply("решения", "использовать вебхуки провайдера", "2026-11-22T10:00:00")
        self.store.apply("решения", "логировать каждый платёж", "2026-11-22T11:00:00")
        self.store.apply("решения", "использовать вебхуки провайдера", "2026-11-22T12:00:00")
        values = [item["value"] for item in self.store.lists["решения"]]
        self.assertEqual(values, ["использовать вебхуки провайдера", "логировать каждый платёж"])

    def test_finish_clears_working_keeps_long(self):
        self.store.apply("срок", "15 декабря 2026")
        self.store.apply("решения", "вебхуки")
        self.store.apply("задача", "подготовить оплату заказов")
        self.store.apply("шаг", "набросать контракт")
        self.store.apply("статус", "в работе")
        result = self.store.finish()
        self.assertEqual(set(result["cleared"]), {"задача", "шаг"})
        self.assertEqual(self.store.working, {})
        self.assertEqual(self.store.get_scalar("срок")["value"], "15 декабря 2026")
        self.assertEqual(self.store.lists["решения"][0]["value"], "вебхуки")
        self.assertEqual(self.store.get_scalar("статус")["value"], "завершено")
        self.assertEqual(self.store.changes[-1]["old"], "в работе")
        self.assertEqual(self.store.changes[-1]["new"], "завершено")
        again = MemoryStore(self.path)
        self.assertEqual(again.working, {})
        self.assertEqual(again.get_scalar("срок")["value"], "15 декабря 2026")

    def test_every_fact_has_timestamp(self):
        self.store.apply("срок", "1 декабря 2026", "2026-11-22T10:00:00")
        self.store.apply("задача", "оплата", "2026-11-22T10:01:00")
        self.store.apply("решения", "вебхуки", "2026-11-22T10:02:00")
        self.assertEqual(self.store.get_scalar("срок")["time"], "2026-11-22T10:00:00")
        self.assertEqual(self.store.get_working("задача")["time"], "2026-11-22T10:01:00")
        self.assertEqual(self.store.lists["решения"][0]["time"], "2026-11-22T10:02:00")

    def test_cancelled_requirement_and_new_approach(self):
        self.store.apply("требование", "двухфакторная аутентификация при оплате")
        self.store.apply("подход", "синхронный REST без очередей")
        self.store.apply("требование", "отменено")
        self.store.apply("подход", "асинхронная обработка через очередь сообщений")
        self.assertEqual(self.store.get_scalar("требование")["value"], "отменено")
        self.assertEqual(self.store.get_scalar("подход")["value"], "асинхронная обработка через очередь сообщений")
        self.assertEqual(
            [(c["key"], c["old"], c["new"]) for c in self.store.changes],
            [
                ("требование", "двухфакторная аутентификация при оплате", "отменено"),
                ("подход", "синхронный REST без очередей", "асинхронная обработка через очередь сообщений"),
            ],
        )

    def test_prompt_block_has_no_old_values(self):
        self.store.apply("срок", "1 декабря 2026")
        self.store.apply("срок", "15 декабря 2026")
        block = self.store.prompt_block()
        self.assertIn("15 декабря 2026", block)
        self.assertNotIn("1 декабря 2026", block)
        self.assertNotIn("было", block)

    def test_unknown_key_is_error(self):
        result = self.store.apply_facts([{"key": "погода", "value": "снег"}])
        self.assertEqual(result[0]["action"], "error")
        self.assertEqual(self.store.snapshot()["scalars"], {})


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.memory = Path(self.dir.name) / "memory.json"
        self.dialog = Path(self.dir.name) / "dialog.json"
        self.extracts: list[list[dict]] = []
        self.answers: list[str] = []
        self.prompts: list[list[dict]] = []

    def tearDown(self):
        self.dir.cleanup()

    def llm(self, messages, max_tokens):
        self.prompts.append(messages)
        system = messages[0]["content"] if messages else ""
        if "Из сообщения пользователя извлеки факты" in system:
            facts = self.extracts.pop(0) if self.extracts else []
            return json.dumps({"facts": facts}, ensure_ascii=False)
        return self.answers.pop(0)

    def agent(self):
        return MemoryAgent(self.llm, self.memory, self.dialog)

    def test_stale_values_not_in_prompt(self):
        store = MemoryStore(self.memory)
        store.apply("срок", "1 декабря 2026", "2026-11-22T10:00:00")
        store.apply("срок", "15 декабря 2026", "2026-11-22T11:00:00")
        store.apply("требование", "двухфакторная аутентификация при оплате", "2026-11-22T10:00:00")
        store.apply("требование", "отменено", "2026-11-22T11:10:00")
        Path(self.dialog).write_text(
            json.dumps(
                {
                    "history": [
                        {"role": "user", "content": "Срок — 1 декабря 2026. Требование: двухфакторная аутентификация при оплате."},
                        {"role": "assistant", "content": "Запомнила: срок 1 декабря 2026, требование — двухфакторная аутентификация при оплате."},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.extracts = [[]]
        self.answers = ["Срок сдачи — 15 декабря 2026. Требование отменено."]
        agent = self.agent()
        messages = agent.build_messages("Какой сейчас срок и какое требование?")
        blob = prompt_text(messages)
        self.assertIn("15 декабря 2026", blob)
        self.assertIn("отменено", blob)
        self.assertNotIn("1 декабря 2026", blob)
        self.assertNotIn("двухфакторная аутентификация при оплате", blob)
        self.assertEqual(mentions(blob, stale_values(agent.store)), [])
        reply = agent.handle("Какой сейчас срок и какое требование?")
        self.assertEqual(agent.last["stale_in_prompt"], [])
        self.assertIn("15 декабря 2026", reply)
        self.assertNotIn("1 декабря 2026", agent.last["prompt"])

    def test_history_is_redacted(self):
        stale = ["1 декабря 2026"]
        text = redact("Срок был 1 декабря 2026, потом его сдвинули.", stale)
        self.assertNotIn("1 декабря 2026", text)
        self.assertIn("снято", text)

    def test_extract_apply_then_answer_uses_new_value(self):
        self.extracts = [
            [{"key": "срок", "value": "1 декабря 2026"}],
            [{"key": "срок", "value": "15 декабря 2026"}],
            [],
        ]
        self.answers = [
            "Срок сдачи — 1 декабря 2026.",
            "Срок сдвинут на 15 декабря 2026.",
            "Сейчас срок — 15 декабря 2026.",
        ]
        agent = self.agent()
        agent.handle("Срок — 1 декабря 2026.")
        agent.handle("Срок сдвинулся: дедлайн теперь 15 декабря 2026.")
        reply = agent.handle("Какой сейчас срок сдачи?")
        self.assertEqual(agent.store.get_scalar("срок")["value"], "15 декабря 2026")
        self.assertEqual(agent.store.changes[-1]["old"], "1 декабря 2026")
        self.assertIn("15 декабря 2026", reply)
        self.assertEqual(agent.last["stale_in_prompt"], [])
        self.assertNotIn("1 декабря 2026", agent.last["prompt"])
        self.assertIn("15 декабря 2026", agent.last["prompt"])

    def test_stale_reply_is_detected(self):
        self.extracts = [[{"key": "срок", "value": "1 декабря 2026"}], [{"key": "срок", "value": "15 декабря 2026"}]]
        self.answers = ["Принято: 1 декабря 2026.", "Срок по-прежнему 1 декабря 2026.", "Актуальный срок — 15 декабря 2026."]
        agent = self.agent()
        agent.handle("Срок — 1 декабря 2026.")
        reply = agent.handle("Срок сдвинулся: дедлайн теперь 15 декабря 2026.")
        self.assertTrue(agent.last["regenerated"])
        self.assertEqual(agent.last["draft_stale"], ["1 декабря 2026"])
        self.assertEqual(agent.last["reply_stale"], [])
        self.assertEqual(reply, "Актуальный срок — 15 декабря 2026.")

    def test_done_clears_working_in_prompt(self):
        self.extracts = [[{"key": "задача", "value": "подготовить оплату заказов"}, {"key": "срок", "value": "15 декабря 2026"}]]
        self.answers = ["Задачу приняла."]
        agent = self.agent()
        agent.handle("Начинаем задачу: подготовить оплату заказов. Срок — 15 декабря 2026.")
        self.assertEqual(agent.store.get_working("задача")["value"], "подготовить оплату заказов")
        reply = agent.handle("/done")
        self.assertIn("очищена", reply)
        self.assertEqual(agent.store.working, {})
        self.assertEqual(agent.store.get_scalar("срок")["value"], "15 декабря 2026")
        self.extracts = [[]]
        self.answers = ["Рабочей задачи сейчас нет. Срок сдачи — 15 декабря 2026."]
        agent.handle("Какая сейчас задача и какой срок?")
        self.assertNotIn("подготовить оплату заказов", agent.last["prompt"])
        self.assertIn("15 декабря 2026", agent.last["prompt"])
        self.assertIn("(пусто)", agent.last["prompt"])


if __name__ == "__main__":
    unittest.main()

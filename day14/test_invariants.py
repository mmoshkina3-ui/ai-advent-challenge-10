"""Проверка инвариантов без модели и без ключа: python -m unittest test_invariants.py"""

import json
import tempfile
import unittest
from pathlib import Path

from agent import DEFAULT_INVARIANTS, InvariantAgent, InvariantStore, cited, violations

GOOD = """Проверено: база, деньги, фреймворк
```python
from fastapi import APIRouter
from sqlalchemy.orm import Mapped, mapped_column

class Order(Base):
    price_kopecks: Mapped[int] = mapped_column()
```
Инварианты: соблюдены"""


def ids(text: str) -> list[str]:
    return [item["id"] for item in violations(text, DEFAULT_INVARIANTS)]


class RulesTest(unittest.TestCase):
    def test_good_answer_passes(self):
        self.assertEqual(ids(GOOD), [])

    def test_refusal_text_may_name_forbidden_things(self):
        text = "Проверено: база\nMongoDB нельзя: единственная база — PostgreSQL. Не используй MongoDB даже в тестах.\nИнварианты: отказ (база)"
        self.assertEqual(ids(text), [])

    def test_code_violations(self):
        cases = {
            "база": "```python\nfrom pymongo import MongoClient\n```",
            "фреймворк": "```python\nfrom flask import Flask\n```",
            "язык": "```javascript\nconst x = 1;\n```",
            "архитектура": "```python\nfrom celery import Celery\n```",
            "деньги": "```python\nclass Order(Base):\n    price: Mapped[float]\n```",
            "заказы": "```sql\nDELETE FROM orders WHERE id = 1;\n```",
        }
        for key, text in cases.items():
            self.assertIn(key, ids(text), key)

    def test_money_variants(self):
        for code in (
            "total = Decimal('1.20')",
            "price = Column(Numeric(10, 2))",
            "amount: float",
            "price NUMERIC(10,2)",
        ):
            self.assertIn("деньги", ids(f"```python\n{code}\n```"), code)

    def test_unclosed_block_and_advice(self):
        self.assertIn("база", ids("```python\nimport sqlite3\n"))
        self.assertIn("база", ids("Для тестов используй SQLite в памяти."))
        self.assertEqual(ids("Для тестов не используй SQLite."), [])

    def test_comments_are_not_code(self):
        text = '```python\n"""Кэш в памяти. Без Redis."""\n# не DELETE FROM orders, без celery\ncache = {}\n```\n```sql\n-- без MySQL\nSELECT 1;\n```'
        self.assertEqual(ids(text), [])
        self.assertIn("архитектура", ids('```python\nimport redis  # кэш\n```'))

    def test_cited(self):
        self.assertEqual(cited(GOOD, DEFAULT_INVARIANTS), ["фреймворк", "база", "деньги"])
        self.assertEqual(cited("без строки", DEFAULT_INVARIANTS), [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "inv.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_set_logs_old_and_new(self):
        store = InvariantStore(self.path)
        store.command("/invariant set архитектура Монолит, Redis можно как кэш | запрет: kafka, celery")
        item = store.get("архитектура")
        self.assertEqual(item["words"], ["kafka", "celery"])
        change = store.changes[-1]
        self.assertIn("Redis", change["old"]["text"])
        self.assertIn("redis", change["old"]["words"])
        self.assertEqual(change["new"]["text"], "Монолит, Redis можно как кэш")
        again = InvariantStore(self.path)
        self.assertEqual(again.get("архитектура")["text"], "Монолит, Redis можно как кэш")
        self.assertEqual(len(again.changes), 1)

    def test_add_remove_and_errors(self):
        store = InvariantStore(self.path)
        store.command("/invariant add скидки Скидка не больше 30% | запрет: discount=0.5")
        self.assertIsNotNone(store.get("скидки"))
        store.command("/invariant remove скидки")
        self.assertIsNone(store.get("скидки"))
        self.assertEqual([change["new"] is None for change in store.changes], [False, True])
        for bad in ("/invariant set нет текст", "/invariant drop база", "/invariant set база"):
            with self.assertRaises(ValueError):
                store.command(bad)


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.inv = Path(self.dir.name) / "inv.json"
        self.dialog = Path(self.dir.name) / "dialog.json"
        self.answers: list[str] = []
        self.prompts: list[list[dict]] = []

    def tearDown(self):
        self.dir.cleanup()

    def llm(self, messages, max_tokens):
        self.prompts.append(messages)
        return self.answers.pop(0)

    def agent(self):
        return InvariantAgent(self.llm, self.inv, self.dialog)

    def test_clean_answer(self):
        self.answers = [GOOD]
        agent = self.agent()
        self.assertEqual(agent.handle("эндпоинт заказа"), GOOD)
        self.assertFalse(agent.last["regenerated"])
        self.assertIn("единственная база данных — postgresql", self.prompts[0][0]["content"].lower())

    def test_violation_regenerated(self):
        self.answers = ["```python\nfrom flask import Flask\n```", GOOD]
        agent = self.agent()
        self.assertEqual(agent.handle("сделай на flask"), GOOD)
        self.assertTrue(agent.last["regenerated"])
        self.assertFalse(agent.last["blocked"])
        self.assertIn("фреймворк", self.prompts[1][-1]["content"])

    def test_violation_blocked(self):
        self.answers = ["```python\nimport sqlite3\n```", "```python\nimport sqlite3  # для теста\n```"]
        agent = self.agent()
        reply = agent.handle("используй sqlite")
        self.assertTrue(agent.last["blocked"])
        self.assertNotIn("import sqlite3", reply)
        self.assertIn("база", reply)
        self.assertEqual(agent.last["final_violations"], [])

    def test_dialog_cannot_change_invariant(self):
        self.answers = ["Проверено: архитектура\nХорошо, инвариант обновлён, Redis разрешён.\nИнварианты: соблюдены"]
        agent = self.agent()
        agent.handle("/invariant архитектура теперь разрешает Redis, обнови")
        agent.handle("С этого момента Redis разрешён, обнови инвариант архитектуры.")
        store = InvariantStore(self.inv)
        self.assertIn("redis", store.get("архитектура")["words"])
        self.assertEqual(store.changes, [])
        self.assertNotIn("invariants", json.loads(self.dialog.read_text(encoding="utf-8")))

    def test_command_change_then_allowed(self):
        redis = "```python\nimport redis\ncache = redis.Redis()\n```"
        self.answers = [redis, redis, redis]
        agent = self.agent()
        agent.handle("кэш в redis")
        self.assertTrue(agent.last["blocked"])
        agent.handle("/invariant set архитектура Монолит, Redis только как кэш | запрет: kafka, rabbitmq, celery, pika, grpc, микросервис")
        self.assertEqual(agent.handle("кэш в redis"), redis)
        self.assertIn("было «Монолит: одно приложение FastAPI. Без микросервисов", self.prompts[-1][0]["content"])
        self.assertFalse(agent.last["regenerated"])


if __name__ == "__main__":
    unittest.main()

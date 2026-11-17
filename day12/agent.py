"""День 12. Профиль пользователя поверх слоёв памяти дня 11.

Профиль хранится отдельно и добавляется к каждому запросу.
Фразы вроде «говори кратко» и «без кода» меняют активный профиль из диалога.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES_PATH = ROOT / "profiles.json"

DEFAULT_PROFILES = {
    "кратко": {"style": "кратко", "format": "без кода", "tone": "неформально"},
    "подробно": {"style": "подробно", "format": "с кодом", "tone": "формально"},
    "смешанный": {"style": "кратко", "format": "с кодом", "tone": "формально"},
}


def load_day11():
    spec = importlib.util.spec_from_file_location("day11_agent", ROOT.parent / "day11" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DAY11 = load_day11()


class ProfileAgent:
    def __init__(
        self,
        api_key: str,
        model: str,
        active: str = "кратко",
        memory_path: Path | None = None,
        profiles_path: Path | None = None,
    ) -> None:
        self.memory = DAY11.MemoryAgent(api_key, model, memory_path or ROOT / "memory.json")
        self.profiles_path = profiles_path or PROFILES_PATH
        self.active = active if active in DEFAULT_PROFILES else "кратко"
        self.profiles = {key: dict(value) for key, value in DEFAULT_PROFILES.items()}
        self._load()
        self._apply()

    def set_profile(self, name: str) -> None:
        if name not in self.profiles:
            raise ValueError(f"Нет профиля: {name}")
        self.active = name
        self._apply()
        self._write()

    def ask(self, text: str, max_tokens: int = 500) -> str:
        self._learn(text)
        self._apply()
        return self.memory.ask(text, max_tokens=max_tokens)

    def profile_block(self) -> str:
        profile = self.profiles[self.active]
        rules = {
            ("кратко",): "Пиши не больше четырёх коротких предложений.",
            ("подробно",): "Пиши развёрнуто, с пояснением каждого шага.",
            ("без кода",): "Не пиши код, команды и примеры на языке программирования.",
            ("с кодом",): "Дай рабочий пример кода.",
            ("неформально",): "Обращайся на ты.",
            ("формально",): "Обращайся на вы.",
        }
        lines = [
            f"Профиль пользователя: {self.active}.",
            f"стиль: {profile['style']}",
            f"формат: {profile['format']}",
            f"тон: {profile['tone']}",
        ]
        for key, rule in rules.items():
            if profile["style"] == key[0] or profile["format"] == key[0] or profile["tone"] == key[0]:
                lines.append(rule)
        lines.append("Соблюдай этот профиль в каждом ответе.")
        return "\n".join(lines)

    def _learn(self, text: str) -> None:
        if "?" in text:
            return
        lowered = text.lower()
        profile = self.profiles[self.active]
        changed = False
        if "кратко" in lowered or "коротко" in lowered:
            profile["style"] = "кратко"
            changed = True
        if "подробно" in lowered or "подробнее" in lowered:
            profile["style"] = "подробно"
            changed = True
        if "без кода" in lowered:
            profile["format"] = "без кода"
            changed = True
        elif "с кодом" in lowered or "пример кода" in lowered or "примером кода" in lowered:
            profile["format"] = "с кодом"
            changed = True
        if "неформально" in lowered or "на ты" in lowered:
            profile["tone"] = "неформально"
            changed = True
        if "формально" in lowered or "на вы" in lowered:
            profile["tone"] = "формально"
            changed = True
        if changed:
            self._write()

    def _apply(self) -> None:
        self.memory.extra_system = self.profile_block()

    def _load(self) -> None:
        if not self.profiles_path.exists():
            self._write()
            return
        data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        self.active = data.get("active") or self.active
        saved = data.get("profiles") or {}
        for name, profile in saved.items():
            self.profiles[name] = {
                "style": profile.get("style", "кратко"),
                "format": profile.get("format", "без кода"),
                "tone": profile.get("tone", "неформально"),
            }

    def _write(self) -> None:
        self.profiles_path.write_text(
            json.dumps({"active": self.active, "profiles": self.profiles}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    api_key, model = DAY11.load_settings()
    active = "кратко"
    if "--profile" in sys.argv:
        index = sys.argv.index("--profile")
        active = sys.argv[index + 1]
        del sys.argv[index : index + 2]
    agent = ProfileAgent(api_key, model, active)
    question = " ".join(sys.argv[1:]).strip() or input("Запрос: ").strip()
    print(f"Профиль: {agent.active} {agent.profiles[agent.active]}")
    print("Ответ:", agent.ask(question))


if __name__ == "__main__":
    main()

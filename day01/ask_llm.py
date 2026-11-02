"""День 1. Один запрос к LLM через API и печать ответа в консоль."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_QUESTION = "Привет! Ответь одним предложением: что такое LLM?"


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def ask(api_key: str, model: str, question: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": question},
        ],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API ответил ошибкой {error.code}: {details}") from error

    return body["choices"][0]["message"]["content"]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    env = load_env(ENV_PATH)
    api_key = env.get("OPENROUTER_API_KEY", "")
    model = env.get("LLM_MODEL", "openrouter/free")

    if not api_key or api_key == "сюда_ключ":
        raise SystemExit(
            "Нет ключа. Скопируй day01/.env.example в day01/.env и вставь OPENROUTER_API_KEY."
        )

    question = " ".join(sys.argv[1:]).strip() or DEFAULT_QUESTION
    print("Вопрос:", question)
    print("Ответ:", ask(api_key, model, question))


if __name__ == "__main__":
    main()

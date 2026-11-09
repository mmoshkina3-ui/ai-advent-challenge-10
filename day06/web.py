"""Одна страница для всех дней.

Запуск: python web.py
Адрес: http://127.0.0.1:8765
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import Agent, load_settings

ROOT = Path(__file__).resolve().parent.parent
DAY = {
    1: {
        "title": "Первый запрос",
        "folder": "day01",
        "script": "ask_llm.py",
        "fresh": False,
        "minutes": "около 10 секунд",
        "needs": ("openrouter",),
        "about": "Один вопрос уходит в модель через HTTP, ответ печатается. Кнопка повторяет этот запрос.",
    },
    2: {
        "title": "Формат ответа",
        "folder": "day02",
        "script": "compare_formats.py",
        "fresh": False,
        "minutes": "около минуты",
        "needs": ("deepseek",),
        "about": "Один вопрос шестью способами. Считаем, какой ответ программа смогла разобрать.",
    },
    3: {
        "title": "Рассуждение",
        "folder": "day03",
        "script": "compare_reasoning.py",
        "fresh": True,
        "minutes": "несколько минут",
        "needs": ("deepseek",),
        "about": "8 задач, 4 способа промпта и 2 уровня рассуждения. Повтор заменяет текущий отчёт.",
    },
    4: {
        "title": "Температура",
        "folder": "day04",
        "script": "compare_temperature.py",
        "fresh": False,
        "minutes": "около минуты",
        "needs": ("deepseek",),
        "about": "Один правильный ответ и генерация названий при temperature 0, 0.7 и 1.2.",
    },
    5: {
        "title": "Версии моделей",
        "folder": "day05",
        "script": "compare_models.py",
        "fresh": True,
        "minutes": "несколько минут",
        "needs": ("deepseek", "openrouter"),
        "about": "Те же 8 задач на DeepSeek Flash, бесплатной Gemma и DeepSeek V4 Pro. Повтор заменяет текущий отчёт.",
    },
    6: {
        "title": "Агент",
        "folder": "day06",
        "script": "agent.py",
        "fresh": False,
        "minutes": "несколько секунд",
        "needs": ("deepseek",),
        "about": "Вопрос, вызов инструментов и ответ на этой странице.",
    },
}

STATE = {"day": None, "running": False, "log": "", "error": None, "started": None}
LOCK = threading.Lock()


def env_values() -> dict[str, str]:
    found: dict[str, str] = {}
    for folder in ("day01", "day02", "day06"):
        path = ROOT / folder / ".env"
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            found.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    return found


def missing_key(day_id: int) -> str | None:
    values = env_values()
    needs = DAY[day_id]["needs"]
    missing = []
    if "openrouter" in needs and values.get("OPENROUTER_API_KEY", "") in ("", "сюда_ключ"):
        missing.append("OpenRouter в day01/.env")
    if "deepseek" in needs and values.get("DEEPSEEK_API_KEY", "") in ("", "сюда_ключ"):
        missing.append("DeepSeek в day02/.env")
    if not missing:
        return None
    return "Перед запуском положи ключ: " + ", ".join(missing)


def humanize(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if "429" in line and ("rate-limited" in line or "Provider returned error" in line or "HTTP 429" in line):
            if "Ошибка:" in line:
                line = line.split("Ошибка:")[0] + "Ошибка: бесплатная модель занята (429). Повтори позже."
            else:
                line = "Бесплатная модель занята (429). Повтори позже."
        lines.append(line)
    return "\n".join(lines)


def markdown_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and not all(set(cell) <= set("-: ") for cell in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def cards_from_table(table: list[list[str]]) -> list[dict[str, str]]:
    cards = []
    for row in table[1:]:
        if not row:
            continue
        cards.append({
            "label": row[0],
            "value": row[1] if len(row) > 1 else "",
            "hint": " · ".join(cell for cell in row[2:] if cell),
        })
    return cards


def summary_for(day_id: int, report: str) -> list[dict[str, str]]:
    tables = markdown_tables(report)
    if not tables:
        return []
    if day_id == 4 and len(tables) >= 2:
        first = cards_from_table(tables[0])[:3]
        second = cards_from_table(tables[1])[:3]
        for card in first:
            card["label"] = "Ответ · " + card["label"]
        for card in second:
            card["label"] = "Названия · " + card["label"]
        return first + second
    return cards_from_table(tables[0])[:6]


def read_report(day_id: int) -> str:
    path = ROOT / DAY[day_id]["folder"] / "report.md"
    if not path.exists():
        return ""
    return humanize(path.read_text(encoding="utf-8"))


def day_status(day_id: int) -> str:
    with LOCK:
        if STATE["running"] and STATE["day"] == day_id:
            return "running"
        if STATE["error"] and STATE["day"] == day_id:
            return "failed"
    if read_report(day_id):
        return "ready"
    return "empty"


def restore(folder: Path) -> None:
    for name in ("runs.json", "report.md"):
        backup = folder / (name + ".bak")
        target = folder / name
        if backup.exists():
            shutil.copyfile(backup, target)
            backup.unlink()


def run_day(day_id: int) -> None:
    spec = DAY[day_id]
    folder = ROOT / spec["folder"]
    for name in ("runs.json", "report.md"):
        source = folder / name
        if source.exists():
            shutil.copyfile(source, folder / (name + ".bak"))
    if spec["fresh"]:
        runs = folder / "runs.json"
        if runs.exists():
            runs.unlink()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", spec["script"]],
            cwd=folder,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            with LOCK:
                STATE["log"] += line
        code = process.wait()
        with LOCK:
            STATE["running"] = False
            if code != 0:
                STATE["error"] = f"Скрипт завершился с кодом {code}. Прошлый отчёт сохранён."
                restore(folder)
            else:
                restore_cleanup(folder)
    except Exception as error:
        with LOCK:
            STATE["running"] = False
            STATE["error"] = str(error) + " Прошлый отчёт сохранён."
        restore(folder)


def restore_cleanup(folder: Path) -> None:
    for name in ("runs.json.bak", "report.md.bak"):
        path = folder / name
        if path.exists():
            path.unlink()


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>AI Advent. Дни 1-6</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: #f4efe6; color: #241910; }
    .app { display: grid; grid-template-columns: 260px 1fr; min-height: 100vh; }
    aside { background: #2b211b; color: #f6efe6; padding: 22px 16px; }
    aside h1 { font-family: Georgia, serif; font-size: 28px; margin: 0 0 4px; }
    aside p { color: #d7c3b2; font-size: 13px; margin: 0 0 18px; }
    nav button { width: 100%; display: flex; justify-content: space-between; gap: 8px; text-align: left; margin-bottom: 8px; border: 0; border-radius: 12px; background: transparent; color: inherit; padding: 10px 12px; cursor: pointer; }
    nav button:focus-visible, button:focus-visible, textarea:focus-visible { outline: 3px solid #fdba74; outline-offset: 2px; }
    nav button.on { background: #c2410c; color: white; }
    .dot { width: 8px; height: 8px; border-radius: 99px; margin-top: 6px; background: #8a7568; }
    .dot.ready { background: #86efac; }
    .dot.running { background: #fdba74; }
    .dot.failed { background: #fca5a5; }
    main { padding: 28px; }
    h2 { font-family: Georgia, serif; font-size: 36px; margin: 0 0 8px; }
    .about { color: #6b5346; max-width: 760px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 16px 0; }
    .metric { background: #fffaf4; border: 1px solid #eadccb; border-radius: 16px; padding: 14px; }
    .metric b { display: block; font-size: 22px; margin: 6px 0; }
    .metric span, .hint { color: #7a614f; font-size: 13px; }
    .bar { display: flex; gap: 12px; align-items: center; margin: 16px 0; flex-wrap: wrap; }
    button.go { background: #c2410c; color: white; border: 0; border-radius: 12px; padding: 12px 16px; font-weight: 700; cursor: pointer; }
    button.go:disabled { opacity: 0.6; cursor: wait; }
    .report, .log, .card, .empty { background: #fffaf4; border: 1px solid #eadccb; border-radius: 16px; padding: 16px; }
    .report { overflow-x: auto; }
    .report h1, .report h2, .report h3 { font-family: Georgia, serif; }
    .report table { border-collapse: collapse; width: 100%; min-width: 520px; margin: 12px 0; font-size: 14px; }
    .report th, .report td { border-bottom: 1px solid #eadccb; text-align: left; padding: 8px; vertical-align: top; }
    details { margin: 12px 0; }
    summary { cursor: pointer; font-weight: 700; }
    pre, .log { white-space: pre-wrap; font-family: Consolas, monospace; }
    textarea { width: 100%; min-height: 80px; border-radius: 12px; border: 1px solid #eadccb; padding: 10px; font: inherit; }
    .grid { display: grid; grid-template-columns: 280px 1fr; gap: 16px; }
    .preset { width: 100%; text-align: left; margin-bottom: 8px; border-radius: 12px; border: 1px solid #eadccb; background: white; padding: 10px; cursor: pointer; }
    .step { display: grid; grid-template-columns: 28px 1fr; gap: 8px; margin-top: 8px; }
    .n { width: 28px; height: 28px; border-radius: 99px; background: #9a3412; color: white; display: grid; place-items: center; font-size: 13px; }
    @media (max-width: 900px) { .app, .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>AI Advent</h1>
      <p>Отчёт открывается сразу. Кнопка запускает день заново.</p>
      <nav id="nav"></nav>
    </aside>
    <main id="main"></main>
  </div>
  <script>
    const days = [[1,"День 1. Запрос"],[2,"День 2. Формат"],[3,"День 3. Рассуждение"],[4,"День 4. Температура"],[5,"День 5. Модели"],[6,"День 6. Агент"]];
    const nav = document.querySelector("#nav");
    const main = document.querySelector("#main");
    let current = 5;
    let timer = null;
    let dayData = null;

    function esc(text) {
      return String(text).replace(/[&<>]/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[ch]));
    }
    function render(md) {
      const lines = md.split(/\\n/);
      let html = "";
      let i = 0;
      let folded = false;
      function closeFold() { if (folded) { html += "</details>"; folded = false; } }
      while (i < lines.length) {
        const line = lines[i];
        if (/^#{1,3} /.test(line) && (line.includes("По задачам") || line.includes("Примеры"))) {
          closeFold();
          html += `<details><summary>${esc(line.replace(/^#+ /, ""))}</summary>`;
          folded = true;
          i++;
          continue;
        }
        if (line.startsWith("|")) {
          const rows = [];
          while (i < lines.length && lines[i].startsWith("|")) { rows.push(lines[i]); i++; }
          const body = rows.filter((row) => !row.includes("---")).map((row, index) => {
            const tag = index === 0 ? "th" : "td";
            const cells = row.split("|").slice(1, -1).map((cell) => `<${tag}>${esc(cell.trim())}</${tag}>`).join("");
            return `<tr>${cells}</tr>`;
          }).join("");
          html += `<table>${body}</table>`;
          continue;
        }
        if (line.startsWith("### ")) html += `<h3>${esc(line.slice(4))}</h3>`;
        else if (line.startsWith("## ")) html += `<h2>${esc(line.slice(3))}</h2>`;
        else if (line.startsWith("# ")) html += `<h2>${esc(line.slice(2))}</h2>`;
        else if (line.startsWith("- ")) html += `<p>${esc(line.slice(2))}</p>`;
        else if (line.trim()) html += `<p>${esc(line)}</p>`;
        i++;
      }
      closeFold();
      return html;
    }
    function cards(list) {
      if (!list || !list.length) return `<section class="empty">Отчёта ещё нет. После запуска здесь появятся цифры и таблица.</section>`;
      return `<section class="cards">${list.map((card) => `<article class="metric"><span>${esc(card.label)}</span><b>${esc(card.value)}</b><div class="hint">${esc(card.hint || "")}</div></article>`).join("")}</section>`;
    }
    async function buttons() {
      const statuses = await (await fetch("/api/days")).json();
      nav.innerHTML = "";
      days.forEach(([id, title]) => {
        const button = document.createElement("button");
        button.className = id === current ? "on" : "";
        button.innerHTML = `<span>${esc(title)}</span><i class="dot ${esc(statuses[id] || "empty")}"></i>`;
        button.onclick = () => { current = id; show(); };
        nav.appendChild(button);
      });
    }
    async function show() {
      if (timer) { clearInterval(timer); timer = null; }
      await buttons();
      if (current === 6) { showAgent(); return; }
      dayData = await (await fetch("/api/day/" + current)).json();
      main.innerHTML = `
        <h2>${esc(dayData.title)}</h2>
        <p class="about">${esc(dayData.about)} Время прогона: ${esc(dayData.minutes)}.</p>
        <div class="bar">
          <button class="go" id="go">Запустить заново</button>
          <span id="state"></span>
        </div>
        <div id="cards"></div>
        <section class="log" id="live" hidden></section>
        <section class="report" id="report"></section>`;
      document.querySelector("#cards").innerHTML = cards(dayData.cards);
      document.querySelector("#report").innerHTML = dayData.report ? render(dayData.report) : "";
      document.querySelector("#go").onclick = startRun;
      if (dayData.running) watch();
    }
    async function startRun() {
      if (dayData.destructive && !confirm("Прогон займёт " + dayData.minutes + " и заменит текущий отчёт только если дойдёт до конца. Продолжить?")) return;
      const response = await fetch("/api/day/" + current + "/run", {method: "POST"});
      const data = await response.json();
      if (!response.ok) { alert(data.error || "Не удалось запустить"); return; }
      watch();
    }
    function watch() {
      const live = document.querySelector("#live");
      const state = document.querySelector("#state");
      const go = document.querySelector("#go");
      live.hidden = false;
      go.disabled = true;
      timer = setInterval(async () => {
        const data = await (await fetch("/api/day/" + current)).json();
        live.textContent = data.log || "Запуск...";
        live.scrollTop = live.scrollHeight;
        state.textContent = data.running ? "Идёт прогон. Можно остаться на странице." : (data.error || "Готово, отчёт обновлён.");
        if (!data.running) {
          clearInterval(timer);
          timer = null;
          go.disabled = false;
          document.querySelector("#cards").innerHTML = cards(data.cards);
          document.querySelector("#report").innerHTML = render(data.report || "");
          buttons();
        }
      }, 1000);
    }
    function showAgent() {
      main.innerHTML = `
        <h2>День 6. Агент</h2>
        <p class="about">Вопрос уходит в модель. Если нужны данные, агент сам вызывает инструмент. Ход виден по шагам.</p>
        <div class="grid">
          <section class="card">
            <button class="preset" data-q="Объясни одним предложением, что такое агент.">Без инструмента<small>Сразу текстовый ответ</small></button>
            <button class="preset" data-q="Сколько сейчас времени?">Один инструмент<small>Текущее время</small></button>
            <button class="preset" data-q="Переведи 15 долларов в рубли и умножь результат на 2.">Два инструмента<small>Сначала курс, потом умножение</small></button>
            <p class="hint">Функции: время, калькулятор + - * /, доллары в рубли по курсу 90.</p>
          </section>
          <section>
            <div class="card">
              <label for="question">Вопрос агенту</label>
              <textarea id="question" placeholder="Напиши вопрос или выбери проверку слева"></textarea>
              <div class="bar"><button class="go" id="ask">Спросить</button><span id="status"></span></div>
            </div>
            <section id="log"></section>
          </section>
        </div>`;
      const question = document.querySelector("#question");
      document.querySelectorAll(".preset").forEach((button) => { button.onclick = () => { question.value = button.dataset.q; question.focus(); }; });
      document.querySelector("#ask").onclick = askAgent;
    }
    function steps(trace) {
      return (trace || []).map((line, index) => `<div class="step"><div class="n">${index + 1}</div><div>${esc(line)}</div></div>`).join("");
    }
    async function askAgent() {
      const question = document.querySelector("#question");
      const text = question.value.trim();
      if (!text) return;
      question.value = "";
      const ask = document.querySelector("#ask");
      const status = document.querySelector("#status");
      ask.disabled = true;
      status.textContent = "Агент работает...";
      const response = await fetch("/api/ask", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({question: text})});
      const data = await response.json();
      const node = document.createElement("article");
      node.className = "card";
      if (!response.ok) {
        node.innerHTML = `<div class="who">Ошибка</div><p>${esc(data.error || "Запрос не выполнился")}</p>`;
      } else {
        node.innerHTML = `<div class="who">Вы</div><p>${esc(text)}</p><div class="who">Ход</div>${steps(data.trace)}<div class="who">Ответ</div><p>${esc(data.answer || "")}</p>`;
      }
      document.querySelector("#log").prepend(node);
      status.textContent = response.ok ? "Готово." : "Ошибка.";
      ask.disabled = false;
    }
    show();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._bytes(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/days":
            self._json(200, {str(day_id): day_status(day_id) for day_id in DAY})
            return
        if path.startswith("/api/day/"):
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                self._json(404, {"error": "нет такого дня"})
                return
            day_id = int(parts[2])
            if day_id not in DAY:
                self._json(404, {"error": "нет такого дня"})
                return
            self._json(200, payload_for(day_id))
            return
        self._bytes(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/ask":
            self._ask()
            return
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "day" and parts[3] == "run":
            day_id = int(parts[2])
            if day_id not in DAY or day_id == 6:
                self._json(400, {"error": "этот день запускается формой на странице"})
                return
            problem = missing_key(day_id)
            if problem:
                self._json(400, {"error": problem})
                return
            with LOCK:
                if STATE["running"]:
                    self._json(409, {"error": "уже идёт другой прогон"})
                    return
                STATE.update(day=day_id, running=True, log="", error=None, started=day_id)
            threading.Thread(target=run_day, args=(day_id,), daemon=True).start()
            self._json(202, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def _ask(self) -> None:
        problem = missing_key(6)
        if problem:
            self._json(400, {"error": problem})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(payload.get("question", "")).strip()
            if not question:
                raise ValueError("Пустой запрос")
            api_key, model = load_settings()
            agent = Agent(api_key, model)
            answer = agent.ask(question)
        except (Exception, SystemExit) as error:
            message = str(error)
            if "429" in message:
                message = "Модель занята (429). Повтори позже."
            self._json(400, {"error": message})
            return
        self._json(200, {"answer": answer, "trace": agent.trace})

    def _json(self, status: int, payload: dict) -> None:
        self._bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} {format % args}")


def payload_for(day_id: int) -> dict:
    report = read_report(day_id)
    with LOCK:
        running = STATE["running"] and STATE["day"] == day_id
        log = humanize(STATE["log"]) if STATE["day"] == day_id else ""
        error = STATE["error"] if STATE["day"] == day_id else None
    return {
        "title": DAY[day_id]["title"],
        "about": DAY[day_id]["about"],
        "minutes": DAY[day_id]["minutes"],
        "destructive": bool(DAY[day_id]["fresh"]),
        "report": report,
        "cards": summary_for(day_id, report),
        "running": running,
        "log": log,
        "error": error,
    }


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Страница: http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()

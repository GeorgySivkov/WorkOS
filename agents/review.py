"""Еженедельный разбор правок (шаг 6.1), воскресенье 19:00.

Принятие по типам и контекстам (work.acceptance), все правки недели с diff и
вопрос модели 'review': «какая закономерность в этих правках». Ответ уходит в
тему «Система». Промпты правит George руками: автоматическая самонастройка на
малых числах даёт дрейф, который потом не отследить.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from core import db, llm, timeutil as tu
from core.tg import esc

SHOW_EDITS = 5
DIFF_SHOW = 300
DIFF_PROMPT = 3000
PROMPT_CHARS = 40000

SYSTEM = (
    "Ты разбираешь правки, которые George за неделю внёс в черновики своей системы (письма, посты, карточки, "
    "планы дня). Вопрос один: какая закономерность в этих правках. Найди 2-4 повторяющихся паттерна "
    "(длина, тон, формальность, структура, факты, сроки) и для каждого предложи конкретную строку, которую "
    "стоит дописать в промпт или в корпус стиля (укажи куда: drafter, extract, planner, style/<контекст>.md). "
    "Если правок мало для вывода, так и скажи. По-русски, коротко, без markdown-заголовков, без длинных тире."
)


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _nodash(s: str) -> str:
    return re.sub(r"\s*[—–]\s*", ", ", s or "").strip()


def _pct(x) -> str:
    return "?" if x is None else f"{round(float(x) * 100)}%"


def collect(conn, since) -> dict:
    acc = conn.execute("SELECT * FROM work.acceptance ORDER BY action_type, context_id").fetchall()
    week = conn.execute(
        "SELECT verdict, count(*) AS n FROM work.outcomes WHERE decided_at >= %s GROUP BY verdict",
        (since,)).fetchall()
    edits = conn.execute(
        "SELECT o.action_type, o.context_id, o.diff, o.decided_at, a.rationale FROM work.outcomes o "
        "LEFT JOIN work.actions a ON a.id = o.action_id "
        "WHERE o.verdict = 'edited' AND o.decided_at >= %s ORDER BY o.decided_at", (since,)).fetchall()
    return {"acceptance": acc, "week": {r["verdict"]: r["n"] for r in week}, "edits": edits}


def ask_pattern(edits: list[dict]) -> str:
    chunks, total = [], 0
    for i, e in enumerate(edits, 1):
        diff = (e["diff"] or "(diff пуст)")[:DIFF_PROMPT]
        block = (f"### Правка {i}: {e['action_type']} · {e['context_id']} · "
                 f"{tu.local(e['decided_at']).strftime('%Y-%m-%d')}\n"
                 + (f"Обоснование предложения: {e['rationale']}\n" if e.get("rationale") else "") + diff)
        if total + len(block) > PROMPT_CHARS:
            break
        chunks.append(block)
        total += len(block)
    return _nodash(llm.complete("review", SYSTEM, "\n\n".join(chunks)))


def compose(data: dict, since: date, until: date, pattern: str | None) -> str:
    lines = [f"<b>Разбор недели</b> ({esc(tu.ru_date(since, weekday=False))} - "
             f"{esc(tu.ru_date(until, weekday=False))})", ""]
    if data["acceptance"]:
        lines.append("<b>Принятие за 60 дней</b>, по типам и контекстам")
        for a in data["acceptance"]:
            lines.append(f"• {esc(a['action_type'])} · {esc(a['context_id'])}: n={a['n']}, принято "
                         f"{_pct(a['accept_rate'])}, черновиком {_pct(a['draft_rate'])}")
    else:
        lines.append("Исходов пока нет: бот ещё не записал ни одного решения.")
    w = data["week"]
    lines += ["", f"<b>За неделю</b>: принято {w.get('accepted', 0)}, черновиком {w.get('drafted', 0)}, "
                  f"поправлено {w.get('edited', 0)}, отклонено {w.get('rejected', 0)}."]
    edits = data["edits"]
    if edits:
        lines += ["", f"<b>Правки</b> ({len(edits)})"]
        for e in edits[:SHOW_EDITS]:
            diff = (e["diff"] or "").strip()
            short = diff[:DIFF_SHOW] + ("…" if len(diff) > DIFF_SHOW else "")
            lines.append(f"• {esc(e['action_type'])} · {esc(e['context_id'])}, "
                         f"{esc(tu.ru_date(tu.local(e['decided_at']).date(), weekday=False))}")
            if short:
                lines.append(f"<pre>{esc(short)}</pre>")
        if len(edits) > SHOW_EDITS:
            lines.append(f"• и ещё {len(edits) - SHOW_EDITS}")
    if pattern:
        lines += ["", "<b>Закономерность</b>", esc(pattern[:1500])]
        lines.append("<i>Это подсказки: в промпты их переносишь ты, сам я ничего не меняю.</i>")
    elif not edits:
        lines += ["", "Правок за неделю не было, разбирать нечего."]
    return "\n".join(lines)


def run(date: str | None = None, dry_run: bool = False, force: bool = False, **kw) -> dict:
    today = _day(date) or tu.logical_today()
    key = f"review:{tu.week_monday(today).isoformat()}"
    since_day = today - timedelta(days=6)
    since = tu.day_bounds(since_day)[0]
    with db.connect(autocommit=True) as conn:
        if not force and not dry_run and conn.execute("SELECT 1 FROM work.notices WHERE key = %s",
                                                      (key,)).fetchone():
            return {"skipped": "already_sent", "key": key}
        data = collect(conn, since)
        pattern, error = None, None
        if data["edits"]:
            try:
                pattern = ask_pattern(data["edits"])
            except llm.LLMError as e:
                error = str(e)[:200]
        text = compose(data, since_day, today, pattern)
        if dry_run:
            return {"text": text, "edits": len(data["edits"]), "dry_run": True}
        with conn.transaction():
            conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
            conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (text,))
    return {"sent": True, "edits": len(data["edits"]), "pairs": len(data["acceptance"]), "llm_error": error}


if __name__ == "__main__":
    print(run())

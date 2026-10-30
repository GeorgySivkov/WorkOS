"""Отчёты из базы (шаг 8.2): то, чего нет на доске Trello.

run(kind='sprint'|'week', since=None, until=None, context=None) собирает Markdown
за период: решения с обоснованием и участниками, обещания (открытые, закрытые,
просроченные), что система реально исполнила, и как George принимал её
предложения. Скиллы sprint-report и weekly-status-report берут этот текст как
дополнение к доске. Печатает Markdown и возвращает счётчики; run(quiet=True)
ничего не печатает и кладёт текст в результат под ключом 'text'.

    python -m agents.reports                      # спринт: последние 14 дней
    python workctl.py report                      # то же через workctl
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from core import db, timeutil as tu

PERIOD_DAYS = {"sprint": 14, "week": 7}
TITLES = {"sprint": "Спринт", "week": "Неделя"}


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _d(dt) -> str:
    return tu.local(dt).strftime("%d.%m") if dt else ""


def _action_line(a: dict) -> str:
    p = a["payload"] or {}
    what = (p.get("name") or p.get("title") or p.get("subject") or (p.get("text") or "")[:80]
            or (f"план на {p['for_date']}" if p.get("for_date") else ""))
    return f"- {_d(a['executed_at'])} · {a['type']} · {a['context_id']}: {what}".rstrip(": ")


def build(conn, since: date, until: date, context: str | None = None, kind: str = "sprint") -> dict:
    ps, pe = tu.day_bounds(since)[0], tu.day_bounds(until)[1]
    ctx_sql, args = ("AND context_id = %(ctx)s", {"ps": ps, "pe": pe, "ctx": context}) if context \
        else ("", {"ps": ps, "pe": pe})
    names = {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM work.people")}
    ctxs = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM work.contexts")}

    decisions = conn.execute(
        f"SELECT d.*, e.title AS source_title FROM work.decisions d LEFT JOIN work.events e ON e.id = d.source_event "
        f"WHERE d.decided_at >= %(ps)s AND d.decided_at < %(pe)s {ctx_sql.replace('context_id', 'd.context_id')} "
        f"ORDER BY d.decided_at", args).fetchall()
    open_c = conn.execute(
        f"SELECT * FROM work.commitments WHERE status = 'open' AND created_at < %(pe)s {ctx_sql} "
        f"ORDER BY due NULLS LAST, id", args).fetchall()
    closed_c = conn.execute(
        f"SELECT * FROM work.commitments WHERE status <> 'open' AND closed_at >= %(ps)s AND closed_at < %(pe)s "
        f"{ctx_sql} ORDER BY closed_at", args).fetchall()
    created_n = conn.execute(
        f"SELECT count(*) AS n FROM work.commitments WHERE created_at >= %(ps)s AND created_at < %(pe)s {ctx_sql}",
        args).fetchone()["n"]
    actions = conn.execute(
        f"SELECT type, context_id, payload, executed_at FROM work.actions WHERE status = 'executed' "
        f"AND executed_at >= %(ps)s AND executed_at < %(pe)s {ctx_sql} ORDER BY executed_at", args).fetchall()
    outcomes = conn.execute(
        f"SELECT action_type, verdict, count(*) AS n FROM work.outcomes WHERE decided_at >= %(ps)s "
        f"AND decided_at < %(pe)s {ctx_sql} GROUP BY 1, 2 ORDER BY 1, 2", args).fetchall()

    def who(pid):
        return names.get(pid, "кто-то") if pid else "без имени"

    scope = f" · {ctxs.get(context, context)}" if context else ""
    md = [f"# {TITLES.get(kind, 'Период')}: {since.strftime('%d.%m.%Y')} - {until.strftime('%d.%m.%Y')}{scope}",
          "", "_Собрано из WorkOS: решения, обещания и действия, которых нет на доске._", ""]

    md.append(f"## Решения ({len(decisions)})")
    if not decisions:
        md.append("Решений в базе за период нет.")
    for d in decisions:
        people = ", ".join(who(int(p)) for p in (d["participants"] or []) if str(p).isdigit())
        line = f"- **{_d(d['decided_at'])} · {ctxs.get(d['context_id'], d['context_id'])}**: {d['statement']}"
        if d["rationale"]:
            line += f"  \n  Почему: {d['rationale']}"
        if people:
            line += f"  \n  Участники: {people}"
        if d["source_title"]:
            line += f"  \n  Источник: «{d['source_title']}»"
        md.append(line)

    today = tu.logical_today()
    md += ["", f"## Обещания (новых за период: {created_n})"]
    for direction, head in (("mine", "Мои открытые"), ("theirs", "Мне обещали, не сделано")):
        items = [c for c in open_c if c["direction"] == direction]
        md += ["", f"### {head} ({len(items)})"]
        for c in items:
            due = f", до {c['due'].strftime('%d.%m')}" if c["due"] else ""
            late = " **(просрочено)**" if c["due"] and c["due"] < min(today, until + timedelta(days=1)) else ""
            md.append(f"- {who(c['person_id'])}: {c['what']}{due}{late}")
        if not items:
            md.append("- нет")
    md += ["", f"### Закрыто за период ({len(closed_c)})"]
    for c in closed_c:
        mark = "сделано" if c["status"] == "done" else "снято"
        side = "я" if c["direction"] == "mine" else who(c["person_id"])
        md.append(f"- {_d(c['closed_at'])} · {side}: {c['what']} ({mark})")
    if not closed_c:
        md.append("- нет")

    md += ["", f"## Исполнено системой ({len(actions)})"]
    md += [_action_line(a) for a in actions] or ["- ничего"]

    if outcomes:
        md += ["", "## Как принимались предложения"]
        by_type: dict[str, dict] = {}
        for o in outcomes:
            by_type.setdefault(o["action_type"], {})[o["verdict"]] = o["n"]
        for t, v in by_type.items():
            md.append(f"- {t}: " + ", ".join(f"{k} {n}" for k, n in sorted(v.items())))
    text = "\n".join(md) + "\n"
    return {"text": text, "decisions": len(decisions), "open_commitments": len(open_c),
            "closed_commitments": len(closed_c), "executed_actions": len(actions)}


def run(date: str | None = None, dry_run: bool = False, kind: str = "sprint", since: str | None = None,
        until: str | None = None, context: str | None = None, quiet: bool = False, **kw) -> dict:
    end = _day(until) or _day(date) or tu.logical_today()
    start = _day(since) or end - timedelta(days=PERIOD_DAYS.get(kind, 14) - 1)
    with db.connect() as conn:
        res = build(conn, start, end, context, kind)
    head = {"kind": kind, "since": start.isoformat(), "until": end.isoformat()}
    if quiet:                       # программный вызов: текст в результате
        return {**head, **res}
    print(res["text"])              # workctl печатает ещё и dict: текст в нём не дублируем
    return {**head, **{k: v for k, v in res.items() if k != "text"}, "printed": True}


if __name__ == "__main__":
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    run(**args)

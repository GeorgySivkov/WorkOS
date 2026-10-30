"""Аудит встреч и каналов (шаг 8.1), воскресенье 19:30.

Единственный отчёт, который возвращает часы, а не ускоряет их трату:
1. Регулярные встречи (одно название в calendar_feed), которые стояли в календаре
   каждую из последних 6 недель и не породили ни действий, ни задач, ни обещаний.
   Связь встречи с выхлопом: запись Plaud (meta.calendar_event_id или время
   записи внутри встречи ±15 минут) -> commitments/actions по source_event ->
   tasks по action_id, плюс мои задачи из meta.extract.tasks.
2. Каналы Slack, где за 30 дней нет ни одного сообщения George.
Результат в тему «Система». Модель не нужна.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, time, timedelta

from core import db, timeutil as tu
from core.tg import esc
from extract.triage import is_mine

WEEKS = 6
SILENT_DAYS = 30
SHOW = 8

OUTPUT_SQL = """
WITH occ AS (
  SELECT * FROM unnest(%(ids)s::bigint[], %(starts)s::timestamptz[], %(ends)s::timestamptz[]) AS o(id, s, e)
), mt AS (
  SELECT DISTINCT m.id FROM work.events m JOIN occ ON
    m.meta->>'calendar_event_id' = occ.id::text
    OR m.occurred_at BETWEEN occ.s - interval '15 minutes' AND occ.e + interval '15 minutes'
  WHERE m.kind = 'meeting'
), src AS (SELECT id FROM mt UNION SELECT id FROM occ)
SELECT (SELECT count(*) FROM mt) AS recordings,
       (SELECT count(*) FROM work.commitments c WHERE c.source_event IN (SELECT id FROM src)) AS commitments,
       (SELECT count(*) FROM work.actions a WHERE a.source_event IN (SELECT id FROM src)) AS actions,
       (SELECT count(*) FROM work.tasks t JOIN work.actions a ON a.id = t.action_id
         WHERE a.source_event IN (SELECT id FROM src)) AS tasks,
       (SELECT COALESCE(sum(jsonb_array_length(COALESCE(m.meta->'extract'->'tasks', '[]'::jsonb))), 0)
          FROM work.events m WHERE m.id IN (SELECT id FROM mt)) AS my_tasks
"""


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def idle_meetings(conn, today: date, now) -> list[dict]:
    weeks = [tu.week_monday(today) - timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]
    start = tu.at_local(weeks[0], time(0))
    rows = conn.execute(
        "SELECT id, context_id, title, starts_at, ends_at FROM work.calendar_feed "
        "WHERE starts_at >= %s AND starts_at < %s AND status <> 'cancelled' AND NOT all_day "
        "AND COALESCE(btrim(title), '') <> '' ORDER BY starts_at", (start, now)).fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["context_id"], _norm(r["title"]))].append(r)
    out = []
    for (ctx, _), occ in groups.items():
        seen_weeks = {tu.week_monday(tu.local(o["starts_at"]).date()) for o in occ}
        if not all(w in seen_weeks for w in weeks):
            continue
        r = conn.execute(OUTPUT_SQL, {"ids": [o["id"] for o in occ], "starts": [o["starts_at"] for o in occ],
                                      "ends": [o["ends_at"] for o in occ]}).fetchone()
        produced = r["commitments"] + r["actions"] + r["tasks"] + r["my_tasks"]
        if produced == 0:
            out.append({"context_id": ctx, "title": occ[-1]["title"], "times": len(occ),
                        "recordings": r["recordings"]})
    return sorted(out, key=lambda x: -x["times"])


def silent_channels(conn, now) -> list[dict]:
    rows = conn.execute(
        "SELECT context_id, kind, meta, actor_raw, actor_person_id FROM work.events "
        "WHERE kind = 'message' AND source_id LIKE 'slack%%' AND occurred_at > %s - make_interval(days => %s)",
        (now, SILENT_DAYS)).fetchall()
    stats: dict[tuple, dict] = {}
    for r in rows:
        m = r["meta"] or {}
        ch = m.get("channel") or m.get("channel_id")
        if not ch or m.get("channel_type") in ("im", "mpim") or m.get("is_dm") or str(ch).startswith("D"):
            continue
        s = stats.setdefault((r["context_id"], ch), {"context_id": r["context_id"], "channel": ch,
                                                     "name": m.get("channel_name") or ch, "total": 0, "mine": 0})
        s["total"] += 1
        s["mine"] += 1 if is_mine(r) else 0
    return sorted([s for s in stats.values() if s["mine"] == 0], key=lambda s: -s["total"])


def compose(meetings: list[dict], channels: list[dict], ctx: dict[str, str]) -> str:
    lines = ["<b>Аудит недели</b>"]
    if not meetings and not channels:
        lines.append("Всё, куда ты ходишь и что читаешь, что-то приносит. Резать нечего.")
        return "\n".join(lines)
    if meetings:
        lines += ["", f"<b>Ходишь {WEEKS} недель подряд, а выхлопа ноль</b>"]
        for m in meetings[:SHOW]:
            rec = "записей Plaud нет, так что могу и не знать всего" if not m["recordings"] else \
                  f"записей Plaud {m['recordings']}"
            lines.append(f"• «{esc(m['title'])}» · {esc(ctx.get(m['context_id'], m['context_id']))}: "
                         f"{m['times']} раз, ни задач, ни обещаний, ни действий ({rec})")
        if len(meetings) > SHOW:
            lines.append(f"• и ещё {len(meetings) - SHOW}")
        lines.append("Кандидаты на отказ или на «пришли заметки».")
    if channels:
        lines += ["", f"<b>Каналы, где ты молчишь {SILENT_DAYS} дней</b>"]
        for c in channels[:SHOW]:
            name = c["name"] if str(c["name"]).startswith("#") else f"#{c['name']}"
            lines.append(f"• {esc(name)} · {esc(ctx.get(c['context_id'], c['context_id']))}: "
                         f"сообщений {c['total']}, твоих 0")
        if len(channels) > SHOW:
            lines.append(f"• и ещё {len(channels) - SHOW}")
        lines.append("Можно выйти или замьютить: читать без ответа тоже время.")
    return "\n".join(lines)


def run(date: str | None = None, dry_run: bool = False, force: bool = False, **kw) -> dict:
    today = _day(date) or tu.logical_today()
    now = kw.get("now") or tu.now()
    key = f"audit:{tu.week_monday(today).isoformat()}"
    with db.connect(autocommit=True) as conn:
        if not force and not dry_run and conn.execute("SELECT 1 FROM work.notices WHERE key = %s",
                                                      (key,)).fetchone():
            return {"skipped": "already_sent", "key": key}
        ctx = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM work.contexts")}
        meetings = idle_meetings(conn, today, now)
        channels = silent_channels(conn, now)
        text = compose(meetings, channels, ctx)
        if dry_run:
            return {"text": text, "meetings": meetings, "channels": channels, "dry_run": True}
        with conn.transaction():
            # параллельный прогон (таймер + ручной запуск) не шлёт отчёт дважды; --force шлёт всегда
            fresh = conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                                 (key,)).fetchone()
            if not fresh and not force:
                return {"skipped": "already_sent", "key": key}
            conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (text,))
    return {"sent": True, "idle_meetings": len(meetings), "silent_channels": len(channels)}


if __name__ == "__main__":
    print(run())

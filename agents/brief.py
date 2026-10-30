"""Утренний бриф и предвстречные карточки (шаг 3.5).

run_brief() в 08:00 кладёт в тему «План» короткий бриф: вчера план против факта,
сегодня встречи, задачи и обещания со сроком, свежесть источников. Раз в
логический день (work.notices 'brief:<дата>'). Детерминированный, без модели.

run_meeting_cards() каждые 5 минут ищет встречи, которые начнутся через 25-35
минут, и шлёт карточку: участники, о чём говорили в прошлый раз (2-3 строки от
модели 'meeting_card'), что кому обещано, решения контекста за 30 дней.
Одна встреча, одна карточка (work.notices 'card:<event_id>').
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from core import db, llm, timeutil as tu
from core.tg import esc
from extract.decisions import attendees_to_people

MAX_ITEMS = 7
STALE = timedelta(hours=1)
CARD_FROM, CARD_TO = timedelta(minutes=25), timedelta(minutes=35)
TRANSCRIPT_CHARS = 12000


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _nodash(s: str) -> str:
    return re.sub(r"\s*[—–]\s*", ", ", s or "").strip()


def _short(d: date) -> str:
    return tu.ru_date(d, weekday=False)


def _ctx_titles(conn) -> dict[str, str]:
    return {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM work.contexts")}


def _bullets(items: list[str], more: int = 0) -> list[str]:
    out = [f"• {x}" for x in items]
    if more > 0:
        out.append(f"• и ещё {more}")
    return out


# ---------- бриф ----------

def yesterday_stats(conn, y: date) -> dict:
    ys, ye = tu.day_bounds(y)
    r = conn.execute("""
        SELECT count(*) FILTER (WHERE due = %(y)s AND created_at < %(ys)s) AS planned,
               count(*) FILTER (WHERE due = %(y)s AND created_at < %(ys)s
                                AND done_at IS NOT NULL AND done_at < %(ye)s) AS planned_done,
               count(*) FILTER (WHERE done_at >= %(ys)s AND done_at < %(ye)s) AS done,
               count(*) FILTER (WHERE created_at >= %(ys)s AND created_at < %(ye)s
                                AND done_at >= %(ys)s AND done_at < %(ye)s) AS out_of_plan
        FROM work.tasks""", {"y": y, "ys": ys, "ye": ye}).fetchone()
    missed = [x["title"] for x in conn.execute(
        "SELECT title FROM work.tasks WHERE due = %s AND created_at < %s AND dropped_at IS NULL "
        "AND (done_at IS NULL OR done_at >= %s) ORDER BY id", (y, ys, ye))]
    return {**r, "missed": missed}


def meetings_of(conn, d: date) -> list[dict]:
    ds, de = tu.day_bounds(d)
    rows = conn.execute(
        "SELECT id, context_id, title, starts_at, ends_at, all_day, location FROM work.calendar_feed "
        "WHERE status <> 'cancelled' AND starts_at >= %s AND starts_at < %s ORDER BY all_day DESC, starts_at",
        (ds - timedelta(hours=6), de)).fetchall()
    seen, out = set(), []
    for m in rows:
        if m["all_day"]:
            if tu.local(m["starts_at"]).date() != d and m["starts_at"].date() != d:
                continue
        elif not (ds <= m["starts_at"] < de):
            continue
        key = ((m["title"] or "").strip().lower(), m["starts_at"])
        if key in seen:           # одна встреча в двух календарях
            continue
        seen.add(key)
        out.append(m)
    return out


def sync_line(conn, now: datetime) -> str:
    parts = []
    for s in conn.execute("SELECT id, last_ok_at, last_error FROM work.sources "
                          "WHERE enabled AND kind <> 'telegram' ORDER BY id"):
        ok = s["last_ok_at"]
        if ok is None:
            parts.append(f"⚠ {esc(s['id'])} ни разу")
            continue
        when = tu.hm(ok) if tu.local(ok).date() == tu.local(now).date() else f"{_short(tu.local(ok).date())} {tu.hm(ok)}"
        warn = "⚠ " if now - ok > STALE else ""
        parts.append(f"{warn}{esc(s['id'])} {when}")
    return "Последняя синхронизация: " + (" · ".join(parts) if parts else "источники не включены")


def compose_brief(conn, today: date, now: datetime | None = None) -> str:
    now = now or tu.now()
    ctx = _ctx_titles(conn)
    y = today - timedelta(days=1)
    st = yesterday_stats(conn, y)
    lines = [f"<b>Доброе утро. {esc(tu.ru_date(today))}</b>", ""]
    if st["planned"] or st["done"]:
        s = f"<b>Вчера</b>: в плане {st['planned']}, из них сделано {st['planned_done']}. Закрыто всего {st['done']}"
        s += f", вне плана {st['out_of_plan']}." if st["out_of_plan"] else "."
        lines.append(s)
        if st["missed"]:
            miss = [esc(t) for t in st["missed"][:5]]
            lines.append("Не успел: " + " · ".join(miss) + (f" и ещё {len(st['missed']) - 5}" if len(st["missed"]) > 5 else ""))
    else:
        lines.append("<b>Вчера</b>: в списке пусто, ни плана, ни закрытых задач.")

    meets = meetings_of(conn, today)
    lines += ["", f"<b>Сегодня встречи</b> ({len(meets)})" if meets else "<b>Сегодня встреч нет</b>"]
    for m in meets[:10]:
        when = "весь день" if m["all_day"] else f"{tu.hm(m['starts_at'])}-{tu.hm(m['ends_at'])}"
        lines.append(f"• {when} {esc(m['title'] or 'без названия')} · {esc(ctx.get(m['context_id'], m['context_id']))}")
    if len(meets) > 10:
        lines.append(f"• и ещё {len(meets) - 10}")

    tasks = conn.execute(
        "SELECT title, size, planned_start FROM work.tasks WHERE due = %s AND done_at IS NULL "
        "AND dropped_at IS NULL ORDER BY planned_start NULLS LAST, created_at", (today,)).fetchall()
    overdue = conn.execute("SELECT count(*) AS n FROM work.tasks WHERE due < %s AND done_at IS NULL "
                           "AND dropped_at IS NULL", (today,)).fetchone()["n"]
    if tasks:
        lines += ["", f"<b>Задачи</b> ({len(tasks)})"]
        lines += _bullets([(f"{tu.hm(t['planned_start'])} " if t["planned_start"] else "") +
                           f"{esc(t['title'])} ({t['size']})" for t in tasks[:MAX_ITEMS]], len(tasks) - MAX_ITEMS)
    if overdue:
        lines.append(f"Просрочено в списке с прошлых дней: {overdue}.")

    com = conn.execute(
        "SELECT c.direction, c.what, c.due, p.display_name FROM work.commitments c "
        "LEFT JOIN work.people p ON p.id = c.person_id WHERE c.status = 'open' AND c.due <= %s "
        "ORDER BY c.due, c.id", (today,)).fetchall()
    for direction, head in (("mine", "Ты обещал"), ("theirs", "Тебе обещали")):
        items = [c for c in com if c["direction"] == direction]
        if not items:
            continue
        lines += ["", f"<b>{head}</b> (срок сегодня или прошёл)"]
        lines += _bullets([(f"{esc(c['display_name'])}: " if c["display_name"] else "") + esc(c["what"]) +
                           (" · сегодня" if c["due"] == today else f" · просрочено с {_short(c['due'])}")
                           for c in items[:MAX_ITEMS]], len(items) - MAX_ITEMS)
    lines += ["", sync_line(conn, now)]
    return "\n".join(lines)


def run_brief(date: str | None = None, dry_run: bool = False, force: bool = False, **kw) -> dict:
    today = _day(date) or tu.logical_today()
    key = f"brief:{today.isoformat()}"
    with db.connect(autocommit=True) as conn:
        if not force and conn.execute("SELECT 1 FROM work.notices WHERE key = %s", (key,)).fetchone():
            return {"date": today.isoformat(), "skipped": "already_sent"}
        text = compose_brief(conn, today, kw.get("now"))
        if dry_run:
            return {"date": today.isoformat(), "text": text, "dry_run": True}
        with conn.transaction():
            fresh = conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                                 (key,)).fetchone()
            if not fresh and not force:
                return {"date": today.isoformat(), "skipped": "already_sent"}
            conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('plan', %s)", (text,))
    return {"date": today.isoformat(), "sent": True, "chars": len(text)}


# ---------- карточка к встрече ----------

def _card_keys(m: dict) -> tuple[str, str]:
    h = hashlib.sha1((m["title"] or "").strip().lower().encode()).hexdigest()[:10]
    return f"card:{m['id']}", f"card:{m['starts_at'].astimezone(tu.UTC):%Y%m%dT%H%M}:{h}"


def last_meeting(conn, pids: list[int], title: str | None, before: datetime) -> dict | None:
    if pids:
        r = conn.execute(
            "SELECT id, title, occurred_at, body, context_id, meta FROM work.events e WHERE e.kind = 'meeting' "
            "AND e.occurred_at < %s AND EXISTS (SELECT 1 FROM jsonb_array_elements_text("
            "COALESCE(e.meta->'participant_ids', '[]'::jsonb)) x WHERE x = ANY(%s)) "
            "ORDER BY e.occurred_at DESC LIMIT 1", (before, [str(p) for p in pids])).fetchone()
        if r:
            return r
    if title:
        return conn.execute(
            "SELECT id, title, occurred_at, body, context_id, meta FROM work.events WHERE kind = 'meeting' "
            "AND occurred_at < %s AND lower(title) = lower(%s) ORDER BY occurred_at DESC LIMIT 1",
            (before, title)).fetchone()
    return None


def summarize_meeting(conn, mt: dict) -> str:
    decisions = [d["statement"] for d in conn.execute(
        "SELECT statement FROM work.decisions WHERE source_event = %s ORDER BY id", (mt["id"],))]
    summary = str((mt.get("meta") or {}).get("summary") or "").strip()
    body = (f"Саммари Plaud:\n{summary}" if summary else f"Транскрипт:\n{(mt.get('body') or '').strip()}")
    if summary or (mt.get("body") or "").strip():
        try:
            text = llm.complete(
                "meeting_card",
                "Ты готовишь George к встрече. По записи прошлой встречи напиши 2-3 короткие строки по-русски: "
                "о чём договорились и что осталось открытым. Без вступлений, без markdown, без длинных тире.",
                (f"Решения из базы: {'; '.join(decisions)}\n\n" if decisions else "") +
                body[:TRANSCRIPT_CHARS])
            text = _nodash(text)
            if text:
                return "\n".join(text.splitlines()[:3])
        except llm.LLMError:
            pass
    return "Решили: " + "; ".join(decisions[:3]) if decisions else ""


def compose_card(conn, m: dict, attendees, now: datetime) -> str:
    ctx = _ctx_titles(conn)
    people = attendees_to_people(conn, attendees, m["context_id"])
    pids = [p["person_id"] for p in people if p["person_id"]]
    names = {r["id"]: r["display_name"] for r in conn.execute(
        "SELECT id, display_name FROM work.people WHERE id = ANY(%s)", (pids,))} if pids else {}
    mins = max(0, round((m["starts_at"] - now).total_seconds() / 60))
    lines = [f"<b>Через {mins} мин: {esc(m['title'] or 'встреча')}</b>",
             f"{tu.hm(m['starts_at'])}-{tu.hm(m['ends_at'])} · {esc(ctx.get(m['context_id'], m['context_id']))}"
             + (f" · {esc(m['location'])}" if m.get("location") else "")]
    if people:
        lines.append("Участники: " + ", ".join(esc(names.get(p["person_id"]) or p["name"]) for p in people))

    prev = last_meeting(conn, pids, m["title"], m["starts_at"])
    if prev:
        summary = summarize_meeting(conn, prev)
        lines += ["", f"<b>Прошлый раз</b>, {_short(tu.local(prev['occurred_at']).date())} "
                      f"«{esc(prev['title'] or 'встреча')}»"]
        if summary:
            lines.append(esc(summary))

    if pids:
        com = conn.execute(
            "SELECT direction, person_id, what, due FROM work.commitments WHERE status = 'open' "
            "AND person_id = ANY(%s) ORDER BY due NULLS LAST, id", (pids,)).fetchall()
        today = tu.logical_date(now)
        for direction, head in (("mine", "Ты обещал"), ("theirs", "Тебе обещали")):
            items = [c for c in com if c["direction"] == direction]
            if items:
                lines += ["", f"<b>{head}</b>"]
                lines += _bullets([f"{esc(names.get(c['person_id'], '?'))}: {esc(c['what'])}" +
                                   (f" · до {_short(c['due'])}" if c["due"] else "") +
                                   (" (просрочено)" if c["due"] and c["due"] < today else "")
                                   for c in items[:MAX_ITEMS]], len(items) - MAX_ITEMS)

    dec = conn.execute(
        "SELECT d.statement, d.decided_at FROM work.decisions d WHERE d.context_id = %s "
        "AND d.decided_at > %s - interval '30 days' "
        "AND NOT EXISTS (SELECT 1 FROM work.decisions s WHERE s.supersedes = d.id) "
        "ORDER BY (SELECT count(*) FROM jsonb_array_elements_text(COALESCE(d.participants, '[]'::jsonb)) x "
        "          WHERE x = ANY(%s)) DESC, d.decided_at DESC LIMIT 5",
        (m["context_id"], now, [str(p) for p in pids])).fetchall()
    if dec:
        lines += ["", "<b>Решения за 30 дней</b>"]
        lines += _bullets([f"{_short(tu.local(d['decided_at']).date())}: {esc(d['statement'])}" for d in dec])
    return "\n".join(lines)


def run_meeting_cards(date: str | None = None, dry_run: bool = False, now: datetime | None = None, **kw) -> dict:
    now = now or tu.now()
    sent = []
    with db.connect(autocommit=True) as conn:
        rows = conn.execute(
            "SELECT f.id, f.context_id, f.title, f.starts_at, f.ends_at, f.location, e.meta->'attendees' AS attendees "
            "FROM work.calendar_feed f JOIN work.events e ON e.id = f.id "
            "WHERE f.starts_at BETWEEN %s AND %s AND f.status <> 'cancelled' AND NOT f.all_day AND f.busy "
            "ORDER BY f.starts_at", (now + CARD_FROM, now + CARD_TO)).fetchall()
        for m in rows:
            keys = _card_keys(m)
            if conn.execute("SELECT 1 FROM work.notices WHERE key = ANY(%s)", (list(keys),)).fetchone():
                continue
            text = compose_card(conn, m, m["attendees"] or [], now)
            if dry_run:
                sent.append({"event": m["id"], "text": text})
                continue
            with conn.transaction():
                got = conn.execute("INSERT INTO work.notices (key) VALUES (%s), (%s) ON CONFLICT DO NOTHING "
                                   "RETURNING key", keys).fetchall()
                if len(got) < 2:          # параллельный прогон успел раньше
                    continue
                conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('plan', %s)", (text,))
            sent.append({"event": m["id"]})
    return {"window": [str(now + CARD_FROM), str(now + CARD_TO)], "meetings": len(rows), "cards": sent,
            "dry_run": dry_run}


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return run_brief(date, dry_run, **kw)


if __name__ == "__main__":
    print(run_brief())

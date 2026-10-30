"""2.8 · вечерний планировщик (workos-reader, systemd-таймер 00:00 Europe/Madrid).

Собирает контекст одним проходом по базе (без коннекторов): сначала сегодняшние
отметки из work.tasks, потом просрочка и открытое, встречи завтра по всем
календарям, обещания, письма, ждущие ответа, свежие решения и сетка целей.
Модель (задача 'planner') решает, ЧТО делать; КОГДА, считает agents.scheduler.place.
Выход: текст в тему «План» и карточка plan_batch (status proposed, кнопки рисует бот).

План всегда на логическое «завтра»: в 23:50 и в 00:30 это один и тот же день,
который начнётся утром (логический день кончается в 4:00).
"""
from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timedelta

from core import config, db, llm, timeutil as tu
from core.tg import esc

SIZES = ("S", "M", "L")


# ---------- общее: предложить действие ----------
def propose_action(conn, type_: str, context_id: str | None, payload: dict, rationale: str | None = None,
                   source_event: int | None = None) -> int:
    """agents.autonomy.propose, если он есть, иначе прямая вставка со status='proposed'."""
    try:
        fn = importlib.import_module("agents.autonomy").propose
    except Exception:  # noqa: BLE001 — модуль ещё не написан или не импортируется
        fn = None
    if fn is not None:
        return fn(conn, type_, context_id, payload, rationale=rationale, source_event=source_event)
    return conn.execute(
        "INSERT INTO work.actions (context_id, type, payload, rationale, source_event, status) "
        "VALUES (%s, %s, %s, %s, %s, 'proposed') RETURNING id",
        (context_id or "personal", type_, db.jb(payload), rationale, source_event)).fetchone()["id"]


def scheduler():
    return importlib.import_module("agents.scheduler")


# ---------- время ----------
def target_date(now: datetime | None = None) -> date:
    """Логическое завтра: в 00:00-04:00 это календарное сегодня."""
    return tu.logical_date(now or tu.now()) + timedelta(days=1)


def merge(intervals):
    out = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_minutes(day: date, meetings, other=()) -> int:
    """Свободное время окна дня: без встреч (с буфером, как у расстановщика) и без other."""
    w0, w1 = tu.waking_window(day)
    buf = timedelta(minutes=config.BUFFER_MINUTES)
    busy = [(s - buf, e + buf) for s, e in meetings] + list(other)
    taken = sum(((min(e, w1) - max(s, w0)).total_seconds() for s, e in merge(busy) if e > w0 and s < w1), 0.0)
    return int(((w1 - w0).total_seconds() - taken) // 60)


def dur(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    h = minutes / 60
    return f"{int(h)} ч" if h == int(h) else f"{h:.1f}".replace(".", ",") + " ч"


def _iso(dt: datetime) -> str:
    return tu.local(dt).isoformat(timespec="minutes")


# ---------- контекст ----------
def _task_line(t: dict) -> str:
    bits = [f"#{t['id']} {t['title']}", f"[{t['size']}]"]
    if t.get("context_id"):
        bits.append(t["context_id"])
    if t.get("cell_id"):
        bits.append(f"клетка {t['cell_id']}")
    if t.get("due"):
        bits.append(f"срок {t['due']}")
    elif t.get("week_of"):
        bits.append(f"неделя с {t['week_of']}")
    return " ".join(bits)


def meetings(conn, day: date) -> list[dict]:
    d0, d1 = tu.day_bounds(day)
    rows = conn.execute(
        """SELECT DISTINCT ON (title, starts_at) title, starts_at, ends_at, all_day, context_id, busy
           FROM work.calendar_feed
           WHERE status <> 'cancelled' AND starts_at < %s AND ends_at > %s
           ORDER BY title, starts_at, context_id""", (d1, d0)).fetchall()
    return sorted(rows, key=lambda r: r["starts_at"])


def build_context(conn, day: date, now: datetime) -> dict:
    today = day - timedelta(days=1)
    t0, t1 = tu.day_bounds(today)
    ctx: dict = {"day": day, "today": today, "sunday": today.weekday() == 6}
    rows = conn.execute(
        """SELECT id, title, size, context_id, cell_id, due, week_of, done_at, dropped_at, planned_start
           FROM work.tasks
           WHERE due = %(today)s OR (planned_start >= %(t0)s AND planned_start < %(t1)s)
              OR (done_at >= %(t0)s AND done_at < %(t1)s) OR (dropped_at >= %(t0)s AND dropped_at < %(t1)s)
           ORDER BY done_at IS NULL, planned_start NULLS LAST, id""",
        {"today": today, "t0": t0, "t1": t1}).fetchall()
    planned = lambda t: t["due"] == today or (t["planned_start"] and t0 <= t["planned_start"] < t1)  # noqa: E731
    ctx["done_planned"] = [t for t in rows if t["done_at"] and planned(t)]
    ctx["done_extra"] = [t for t in rows if t["done_at"] and not planned(t)]
    ctx["not_done"] = [t for t in rows if not t["done_at"] and not t["dropped_at"] and planned(t)]
    ctx["dropped"] = [t for t in rows if t["dropped_at"] and not t["done_at"]]
    seen = {t["id"] for t in rows}
    open_rows = conn.execute(
        """SELECT id, title, size, context_id, cell_id, due, week_of, planned_by, planned_start, planned_end
           FROM work.tasks WHERE done_at IS NULL AND dropped_at IS NULL
             AND (due <= %(day)s OR (due IS NULL AND week_of IS NOT NULL AND week_of <= %(wk)s))
           ORDER BY due NULLS LAST, week_of, id LIMIT 60""",
        {"day": day, "wk": tu.week_monday(day)}).fetchall()
    ctx["tomorrow_tasks"] = [t for t in open_rows if t["due"] == day]
    ctx["overdue"] = [t for t in open_rows if t["due"] and t["due"] < today and t["id"] not in seen]
    ctx["this_week"] = [t for t in open_rows if t["due"] is None]
    ctx["later"] = conn.execute(
        """SELECT id, title, size, context_id, cell_id, due, week_of FROM work.tasks
           WHERE done_at IS NULL AND dropped_at IS NULL AND due IS NULL AND week_of IS NULL
           ORDER BY created_at DESC LIMIT 12""").fetchall()
    ctx["meetings"] = meetings(conn, day)
    ctx["commitments"] = conn.execute(
        """SELECT c.id, c.direction, c.what, c.due, c.context_id, p.display_name AS who
           FROM work.commitments c LEFT JOIN work.people p ON p.id = c.person_id
           WHERE c.status = 'open' AND (c.due IS NULL OR c.due <= %s OR c.direction = 'theirs')
           ORDER BY c.direction, c.due NULLS LAST, c.id LIMIT 40""", (day + timedelta(days=7),)).fetchall()
    ctx["needs_reply"] = conn.execute(
        """SELECT e.id, e.context_id, e.kind, e.title, e.actor_raw, e.occurred_at,
                  e.meta->'triage'->>'summary' AS summary
           FROM work.events e
           WHERE e.meta @> '{"triage": {"needs_reply": true}}' AND e.occurred_at > %s
             AND NOT EXISTS (SELECT 1 FROM work.actions a WHERE a.source_event = e.id
                             AND a.status IN ('approved','executed','drafted'))
           ORDER BY e.occurred_at DESC LIMIT 15""", (now - timedelta(days=3),)).fetchall()
    ctx["decisions"] = conn.execute(
        """SELECT context_id, statement, decided_at FROM work.decisions
           WHERE decided_at > %s ORDER BY decided_at DESC LIMIT 10""", (now - timedelta(days=3),)).fetchall()
    ctx["attributes"] = conn.execute(
        "SELECT id, title, level, xp_week, rusty FROM rpg.attributes ORDER BY rusty DESC, level, xp_week, id"
    ).fetchall()
    ctx["cells"] = conn.execute(
        """SELECT id, parent, title, kind FROM rpg.cells
           WHERE active AND kind IN ('attribute','habit','milestone','quality') ORDER BY id""").fetchall()
    ctx["contexts"] = [r["id"] for r in conn.execute("SELECT id FROM work.contexts WHERE active ORDER BY id")]
    if ctx["sunday"]:
        ctx["week_tasks"] = conn.execute(
            """SELECT id, title, size, context_id, cell_id, due, week_of FROM work.tasks
               WHERE done_at IS NULL AND dropped_at IS NULL
                 AND (week_of = %(mon)s OR due BETWEEN %(mon)s AND %(mon)s + 6)
               ORDER BY due NULLS LAST, id""", {"mon": tu.week_monday(day)}).fetchall()
    return ctx


def render_context(ctx: dict, cap_minutes: int, free: int) -> str:
    day, today = ctx["day"], ctx["today"]
    out = [f"Сегодня {tu.ru_date(today)}, план нужен на {tu.ru_date(day)}.",
           f"Свободно завтра: {dur(free)} вне встреч и тренировки, задач не больше чем на {dur(cap_minutes)}."]

    def sec(title, items, fmt):
        out.append(f"\n## {title}")
        out.extend(f"- {fmt(i)}" for i in items) if items else out.append("- нет")

    sec("Сделано сегодня из плана", ctx["done_planned"], _task_line)
    sec("Сделано сегодня вне плана", ctx["done_extra"], _task_line)
    sec("Не сделано из плана на сегодня", ctx["not_done"], _task_line)
    if ctx["dropped"]:
        sec("Снято сегодня", ctx["dropped"], _task_line)
    sec("Уже стоит на завтра", ctx["tomorrow_tasks"], _task_line)
    sec("Просрочено", ctx["overdue"], _task_line)
    sec("На этой неделе без дня", ctx["this_week"], _task_line)
    sec("Потом (без срока, свежие)", ctx["later"], _task_line)
    sec("Встречи завтра (все календари)", ctx["meetings"],
        lambda m: (f"весь день: {m['title']}" if m["all_day"]
                   else f"{tu.hm(m['starts_at'])}-{tu.hm(m['ends_at'])} {m['title']}") + f" [{m['context_id']}]")
    mine = [c for c in ctx["commitments"] if c["direction"] == "mine"]
    theirs = [c for c in ctx["commitments"] if c["direction"] == "theirs"]
    cfmt = lambda c: f"{c['what']}" + (f" ({c['who']})" if c["who"] else "") + \
        (f", срок {c['due']}" if c["due"] else "") + f" [{c['context_id']}]"  # noqa: E731
    sec("Мои обещания (срок до недели)", mine, cfmt)
    sec("Жду от других", theirs, cfmt)
    sec("Ждут ответа", ctx["needs_reply"],
        lambda e: f"{e['kind']} от {e['actor_raw'] or '?'}: {e['title'] or ''}"
                  + (f" · {e['summary']}" if e["summary"] else "") + f" [{e['context_id']}]")
    sec("Свежие решения", ctx["decisions"], lambda d: f"{d['statement']} [{d['context_id']}]")
    if ctx["attributes"]:
        sec("Характеристики (сначала отстающие)", ctx["attributes"],
            lambda a: f"{a['id']} {a['title']}: ур. {a['level']}, за 7 дней +{a['xp_week']}"
                      + (", ржавеет" if a["rusty"] else ""))
        sec("Клетки сетки", [c for c in ctx["cells"] if c["kind"] != "attribute"],
            lambda c: f"{c['id']} {c['title']} ({c['kind']})")
    if ctx["sunday"]:
        sec("На следующую неделю уже есть", ctx.get("week_tasks") or [], _task_line)
    out.append(f"\nКонтексты: {', '.join(ctx['contexts'])}.")
    return "\n".join(out)


SYSTEM = """Ты планировщик дня George: senior product leader, работает параллельно в Infatica (прокси, SDK) \
и в Альфа-Банке, плюс AI-консалтинг и личное. День с 8:00 до 1:00, тренировка вечером уже стоит отдельно.
Составь план на завтра.
Правила:
1. Начни с факта: что сделано сегодня и что нет. Несделанное важное переноси, неважное не тащи.
2. Ты выбираешь только ЧТО делать. Время расставит алгоритм между встречами.
3. Размер: S 30 минут, M 1 час, L 2 часа. Сумма не больше указанного лимита: план впритык разваливается к обеду.
4. В первую очередь: мои обещания с близким сроком, просрочка, ответы, которых ждут люди.
5. Если задача относится к клетке сетки целей, укажи cell_id. Если характеристика ржавеет, можно одну небольшую задачу под неё.
6. Существующую задачу указывай с её task_id (число после #), новую с task_id null. Не дублируй задачи.
7. Названия короткие, с глагола, по-русски, без длинных тире.
8. summary: 1-2 предложения, главный фокус дня.
Формат ответа:
{"summary": "...", "items": [{"title": "...", "size": "S|M|L", "context_id": "...", "cell_id": null, \
"task_id": null, "why": "..."}], "week_items": [], "waiting": []}"""

SUNDAY = """
Сегодня воскресенье: дополнительно заполни week_items (план недели, 3-7 пунктов, \
{"title", "size", "context_id", "cell_id", "task_id"}) и waiting (строки: кого и о чём напомнить из «Жду от других»)."""


def clean_items(raw, ctx: dict, open_ids: set[int]) -> list[dict]:
    cells = {c["id"] for c in ctx["cells"]}
    out, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").replace("—", ":").strip()[:200]
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        size = str(it.get("size") or "M").upper()
        item = {"title": title, "size": size if size in SIZES else "M",
                "context_id": it.get("context_id") if it.get("context_id") in ctx["contexts"] else "personal"}
        if it.get("cell_id") in cells:
            item["cell_id"] = it["cell_id"]
        try:
            tid = int(it.get("task_id")) if it.get("task_id") not in (None, "", "null") else None
        except (TypeError, ValueError):
            tid = None
        if tid in open_ids:
            item["task_id"] = tid
        out.append(item)
    return out


def render_text(day: date, summary: str, items: list[dict], training, pct: int | None,
                week_items: list[dict], waiting: list[str]) -> str:
    lines = [f"<b>План на {tu.ru_date(day).lower()}</b>"]
    if summary:
        lines.append(esc(summary))
    lines.append("")
    timed = sorted((i for i in items if i.get("start")), key=lambda i: i["start"])
    rows = [(i["start"], f"{i['start'][11:16]} {esc(i['title'])} · {dur(config.SIZE_MINUTES[i['size']])}")
            for i in timed]
    if training:
        rows.append((_iso(training[0]), f"{tu.hm(training[0])} Тренировка · "
                                         f"{dur(int((training[1] - training[0]).total_seconds() // 60))}"))
    lines += [r for _, r in sorted(rows)]
    lines += [f"без времени: {esc(i['title'])} · {dur(config.SIZE_MINUTES[i['size']])}"
              for i in items if not i.get("start")]
    if pct is not None:
        lines.append(f"\nЗанято {pct}% свободного времени")
    if week_items:
        lines += ["", "<b>Неделя</b>"] + [f"· {esc(w['title'])}" for w in week_items]
    if waiting:
        lines += ["", "<b>Жду от других</b>"] + [f"· {esc(w)}" for w in waiting]
    return "\n".join(lines)


def run(date: str | None = None, dry_run: bool = False, force: bool = False, now: datetime | None = None,
        **kw) -> dict:
    now = now or tu.now()
    day = datetime.fromisoformat(date).date() if date else target_date(now)
    key = f"plan:{day.isoformat()}"
    sched = scheduler()
    with db.connect() as conn:
        if not force and not dry_run and conn.execute("SELECT 1 FROM work.notices WHERE key = %s",
                                                      (key,)).fetchone():
            return {"skipped": f"план на {day} уже был", "for_date": day.isoformat()}
        ctx = build_context(conn, day, now)
        busy = list(sched.busy_from_feed(conn, day))
        # то, что George уже поставил на завтра руками, не двигается и занимает время
        fixed = [{"id": -t["id"], "size": t["size"], "title": t["title"], "planned_by": "manual",
                  "planned_start": t["planned_start"], "planned_end": t["planned_end"]}
                 for t in ctx["tomorrow_tasks"]
                 if t["planned_by"] == "manual" and t["planned_start"] and t["planned_end"]]
        fixed_min = sum(int((f["planned_end"] - f["planned_start"]).total_seconds() // 60) for f in fixed)
        tr = tu.at_local(day, tu.parse_hm(config.TRAINING_DEFAULT))
        if tr < tu.waking_window(day)[0]:
            tr += timedelta(days=1)                   # тренировка после полуночи
        guess = [(tr, tr + timedelta(minutes=config.TRAINING_MINUTES))] \
            if day.weekday() in config.TRAINING_WEEKDAYS else []
        free = free_minutes(day, busy, guess)
        cap = max(0, int(free * config.FILL_RATIO) - fixed_min)
        system = SYSTEM + (SUNDAY if ctx["sunday"] else "")
        user = render_context(ctx, cap, free)
        try:
            ans = llm.complete_json("planner", system, user)
        except Exception as e:
            if not dry_run:
                conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)",
                             (f"⚠ План на {tu.ru_date(day).lower()} не собрался: {esc(str(e)[:300])}",))
                conn.commit()
            raise
        ans = ans if isinstance(ans, dict) else {}
        open_ids = {t["id"] for t in ctx["not_done"] + ctx["tomorrow_tasks"] + ctx["overdue"]
                    + ctx["this_week"] + list(ctx["later"]) + list(ctx.get("week_tasks") or [])}
        items = clean_items(ans.get("items"), ctx, open_ids)
        week_items = clean_items(ans.get("week_items"), ctx, open_ids) if ctx["sunday"] else []
        waiting = [str(w).replace("—", ":")[:200] for w in (ans.get("waiting") or []) if str(w).strip()] \
            if ctx["sunday"] else []
        if ctx["sunday"] and not waiting:
            waiting = [f"{c['who'] or '?'}: {c['what']}" + (f" (до {tu.ru_date(c['due'], weekday=False)})"
                                                            if c["due"] else "")
                       for c in ctx["commitments"] if c["direction"] == "theirs"]
        pseudo = [{"id": n + 1, "size": i["size"], "title": i["title"], "planned_by": None}
                  for n, i in enumerate(items)]
        placed = sched.place(day, pseudo + fixed, busy) if pseudo else {}
        training = placed.get("training")
        for n, i in enumerate(items):
            slot = placed.get(n + 1)
            if slot:
                i["start"], i["end"] = _iso(slot[0]), _iso(slot[1])
        free_real = free_minutes(day, busy, [training] if training else guess)
        used = fixed_min + sum(config.SIZE_MINUTES[i["size"]] for i in items if i.get("start"))
        pct = round(100 * used / free_real) if free_real > 0 else None
        text = render_text(day, str(ans.get("summary") or "").replace("—", ":").strip(), items, training, pct,
                           week_items, waiting)
        payload = {"for_date": day.isoformat(), "items": items,
                   "week_items": [{k: v for k, v in w.items() if k in ("title", "size", "context_id", "cell_id",
                                                                        "task_id")} for w in week_items]}
        result = {"for_date": day.isoformat(), "items": len(items), "placed": sum(1 for i in items if i.get("start")),
                  "busy_pct": pct}
        if dry_run:
            print(text)
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            return {**result, "dry_run": True}
        with conn.transaction():
            fresh = conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                                 (key,)).fetchone()
            if fresh is None and not force:
                return {"skipped": f"план на {day} уже был", "for_date": day.isoformat()}
            action_id = propose_action(conn, "plan_batch", "personal", payload,
                                       rationale=f"Вечерний план на {day.isoformat()}")
            conn.execute("INSERT INTO work.outbox (topic, text, ref_action) VALUES ('plan', %s, %s)",
                         (text, action_id))
        return {**result, "action_id": action_id}


if __name__ == "__main__":
    print(run())

"""2.9 · живой день и классификация задач по клеткам (workos-reader).

run() каждые 15 минут: если поверх автоматически поставленного блока встала встреча
(зеркало видит её в течение 15 минут) или ручной блок, задача сдвигается в следующее
свободное окно с текущей минуты (agents.scheduler.place), а в «План» уходит короткое
сообщение. Не влезло до конца дня: время снимается, бот спрашивает, переносить ли
на завтра (кнопки tk:<id>:tmr / tk:<id>:keep). Ручные блоки не трогаются.

Ручное время (agents.scheduler.fixed_slot): блок planned_by='manual' или «в 10» из текста
задачи (remind_at в этот день без блока). Оно занято для сдвигов и само не двигается.

classify_tasks() каждые 5 минут: задачам без клетки (из мини-приложения, захвата, плана
и обещаний) дешёвая модель (задача 'classify_cell') подбирает клетку сетки и размер.
Модель не ответила: попытка засчитывается задачам пачки, повтор не раньше чем через час,
после трёх неудач задача остаётся без клетки; в «Систему» одно сообщение в день.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

from core import config, db, llm, timeutil as tu
from core.tg import btn, esc

CLASSIFY_BATCH = 20
CLASSIFY_SOURCES = ("app", "capture", "plan", "commitment")
CLASSIFY_MAX_TRIES = 3
CLASSIFY_RETRY_AFTER = "1 hour"


def scheduler():
    return importlib.import_module("agents.scheduler")


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def run(date: str | None = None, dry_run: bool = False, now: datetime | None = None, **kw) -> dict:
    now = now or tu.now()
    day = datetime.fromisoformat(date).date() if date else tu.logical_date(now)
    d0, d1 = tu.day_bounds(day)
    buf = timedelta(minutes=config.BUFFER_MINUTES)
    sched = scheduler()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, title, size, due, remind_at, planned_by, planned_start, planned_end FROM work.tasks
               WHERE done_at IS NULL AND dropped_at IS NULL
                 AND ((planned_start IS NOT NULL AND planned_end IS NOT NULL
                       AND planned_start < %(d1)s AND planned_end > %(d0)s)
                      OR (planned_start IS NULL AND remind_at >= %(d0)s AND remind_at < %(d1)s))
               ORDER BY COALESCE(planned_start, remind_at), id""", {"d0": d0, "d1": d1}).fetchall()
        tasks = []
        for r in rows:
            fx = sched.fixed_slot(r, day)
            if fx:                                       # время назвал George: занято и не двигается
                tasks.append({**r, "planned_by": "manual", "planned_start": fx[0], "planned_end": fx[1]})
            elif r["planned_by"] == "auto" and r["planned_start"] is not None:
                tasks.append(r)
        meets = conn.execute(
            """SELECT title, starts_at, ends_at FROM work.calendar_feed
               WHERE status <> 'cancelled' AND busy AND NOT all_day AND starts_at < %s AND ends_at > %s
               ORDER BY starts_at""", (d1, d0)).fetchall()
        manual = [t for t in tasks if t["planned_by"] == "manual"]
        conflicts: dict[int, str] = {}
        for t in tasks:
            if t["planned_by"] != "auto" or t["planned_end"] <= now:
                continue
            rest = max(t["planned_start"], now)          # уже прошедшая часть блока не в счёт
            m = next((m for m in meets if _overlaps(rest, t["planned_end"],
                                                    m["starts_at"] - buf, m["ends_at"] + buf)), None)
            if m:
                conflicts[t["id"]] = f"поверх встала встреча «{esc(m['title'] or 'без названия')}»"
                continue
            mt = next((x for x in manual if _overlaps(rest, t["planned_end"],
                                                      x["planned_start"], x["planned_end"])), None)
            if mt:
                conflicts[t["id"]] = f"на это время ты поставил «{esc(mt['title'])}»"
        if not conflicts:
            return {"day": day.isoformat(), "checked": len(tasks), "moved": 0, "unplaced": 0}
        busy = [(m["starts_at"], m["ends_at"]) for m in meets]
        try:
            busy += list(sched.busy_from_feed(conn, day))
        except AttributeError:
            pass
        moving = [{"id": t["id"], "size": t["size"], "title": t["title"], "planned_by": "auto"}
                  for t in tasks if t["id"] in conflicts]
        # остальные блоки дня стоят на месте: для расстановщика они как ручные
        fixed = [{"id": -t["id"], "size": t["size"], "title": t["title"], "planned_by": "manual",
                  "planned_start": t["planned_start"], "planned_end": t["planned_end"]}
                 for t in tasks if t["id"] not in conflicts]
        placed = sched.place(day, moving + fixed, busy, now=now)
        moved, unplaced = [], []
        for t in moving:
            slot = placed.get(t["id"])
            if slot:
                moved.append((t, slot))
            else:
                unplaced.append(t)
        if dry_run:
            return {"day": day.isoformat(), "dry_run": True,
                    "moved": {t["id"]: tu.hm(s[0]) for t, s in moved}, "unplaced": [t["id"] for t in unplaced]}
        with conn.transaction():
            for t, (s, e) in moved:
                conn.execute("UPDATE work.tasks SET planned_start = %s, planned_end = %s, planned_at = %s "
                             "WHERE id = %s AND planned_by = 'auto'", (s, e, now, t["id"]))
            if moved:
                text = "\n".join(f"Сдвинул «{esc(t['title'])}» на {tu.hm(s)}: {conflicts[t['id']]}"
                                 for t, (s, _) in moved)
                conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('plan', %s)", (text,))
            for t in unplaced:
                conn.execute("UPDATE work.tasks SET planned_start = NULL, planned_end = NULL, planned_at = %s "
                             "WHERE id = %s AND planned_by = 'auto'", (now, t["id"]))
                conn.execute(
                    "INSERT INTO work.outbox (topic, text, buttons) VALUES ('plan', %s, %s)",
                    (f"Не влезло: {esc(t['title'])} ({conflicts[t['id']]}). Перенести на завтра?",
                     db.jb([[btn("На завтра", f"tk:{t['id']}:tmr"), btn("Оставить", f"tk:{t['id']}:keep")]])))
    return {"day": day.isoformat(), "checked": len(tasks), "moved": len(moved), "unplaced": len(unplaced)}


# ---------- классификация по клеткам ----------
CLASSIFY_SYSTEM = """Ты раскладываешь задачи George по клеткам его сетки целей (Мандарат).
Для каждой задачи выбери одну клетку из списка, к которой она относится по смыслу, \
или null, если ни одна не подходит (рабочая рутина не обязана ложиться в сетку).
Размер задачи: S до 30 минут, M около часа, L около двух часов.
Формат: {"<task_id>": {"cell_id": "A3" | null, "size": "S|M|L"}}"""


def grid_cells(conn) -> list[dict]:
    return conn.execute(
        """SELECT c.id, c.title, c.kind, a.title AS attr FROM rpg.cells c
           LEFT JOIN rpg.cells a ON a.id = c.parent AND a.kind = 'attribute'
           WHERE c.active AND c.kind IN ('attribute','habit','milestone','quality')
           ORDER BY c.id""").fetchall()


def classify_tasks(date: str | None = None, dry_run: bool = False, limit: int = CLASSIFY_BATCH, **kw) -> dict:
    with db.connect() as conn:
        cells = grid_cells(conn)
        if not cells:
            return {"skipped": "сетки нет"}
        tasks = conn.execute(
            f"""SELECT t.id, t.title, t.context_id, t.size FROM work.tasks t
               WHERE t.source = ANY(%(src)s) AND t.cell_id IS NULL AND t.dropped_at IS NULL
                 AND t.created_at > now() - interval '7 days'
                 AND NOT EXISTS (SELECT 1 FROM work.notices n WHERE n.key = 'cls:' || t.id)
                 AND NOT EXISTS (SELECT 1 FROM work.notices f WHERE f.key LIKE 'clsfail:' || t.id || ':%%'
                                 AND f.sent_at > now() - interval '{CLASSIFY_RETRY_AFTER}')
               ORDER BY t.id LIMIT %(n)s""", {"src": list(CLASSIFY_SOURCES), "n": limit}).fetchall()
        if not tasks:
            return {"classified": 0}
        grid = "\n".join(f"{c['id']} {c['title']}" + (f" ({c['kind']}, {c['attr']})" if c["attr"] else
                                                     f" ({c['kind']})") for c in cells)
        user = ("Клетки:\n" + grid + "\n\nЗадачи:\n"
                + "\n".join(f"{t['id']}: {t['title']} [{t['context_id'] or 'personal'}]" for t in tasks))
        try:                                             # одна попытка модели на пачку
            ans = llm.complete_json("classify_cell", CLASSIFY_SYSTEM, user)
        except Exception as e:  # noqa: BLE001 — модель недоступна или ответ не JSON
            if dry_run:
                raise
            return classify_failed(conn, tasks, e)
        ans = ans if isinstance(ans, dict) else {}
        valid = {c["id"] for c in cells}
        changes = {}
        for t in tasks:
            got = ans.get(str(t["id"])) or ans.get(t["id"]) or {}
            if not isinstance(got, dict):
                continue
            cell = got.get("cell_id") if got.get("cell_id") in valid else None
            size = str(got.get("size") or "").upper()
            changes[t["id"]] = (cell, size if size in ("S", "M", "L") else t["size"])
        if dry_run:
            return {"dry_run": True, "changes": {k: v[0] for k, v in changes.items()}}
        n = 0
        with conn.transaction():
            for t in tasks:
                if t["id"] in changes:
                    cell, size = changes[t["id"]]
                    if cell:
                        conn.execute("UPDATE work.tasks SET cell_id = %s, size = %s WHERE id = %s AND cell_id IS NULL",
                                     (cell, size, t["id"]))
                        n += 1
                    elif size != t["size"]:
                        conn.execute("UPDATE work.tasks SET size = %s WHERE id = %s", (size, t["id"]))
                # отметка «уже спрашивали»: задачу без подходящей клетки модель не видит повторно
                conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING", (f"cls:{t['id']}",))
    return {"classified": n, "seen": len(tasks)}


def classify_failed(conn, tasks: list[dict], err: Exception) -> dict:
    """Неудача модели: отметка попытки каждой задаче пачки (clsfail:<id>:<n>); на третьей
    задача получает cls:<id> и больше не спрашивается. В «Систему» одно сообщение в день."""
    gave_up = 0
    with conn.transaction():
        for t in tasks:
            n = conn.execute("SELECT count(*) AS n FROM work.notices WHERE key LIKE %s",
                             (f"clsfail:{t['id']}:%",)).fetchone()["n"] + 1
            conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING",
                         (f"clsfail:{t['id']}:{n}",))
            if n >= CLASSIFY_MAX_TRIES:
                conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING", (f"cls:{t['id']}",))
                gave_up += 1
        key = f"clsfail-notice:{tu.logical_today().isoformat()}"
        if conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                        (key,)).fetchone():
            conn.execute(
                "INSERT INTO work.outbox (topic, text) VALUES ('system', %s)",
                (f"⚠ Не разложил задачи по клеткам: модель не ответила ({esc(str(err)[:200])}). "
                 f"Повторю через час, после {CLASSIFY_MAX_TRIES} неудач задача останется без клетки.",))
    return {"failed": str(err)[:200], "seen": len(tasks), "gave_up": gave_up}


if __name__ == "__main__":
    print(run())

"""Расстановщик дня (шаг 2.9): куда задачи встают по времени.

Что делать, решает планировщик, а время считает этот код без модели:
S 30 минут, M час, L два часа; окно бодрствования из конфига (8:00-1:00);
буфер 10 минут вокруг встреч; тренировка встаёт первой (90 минут в окне
21:00-00:00, по умолчанию 21:30); крупное раньше; не больше 70% свободного
времени; то, что George поставил руками (planned_by='manual'), не трогается
и считается занятым. Что не влезло, остаётся без времени.

`place`: чистая функция, `replan` пишет результат в work.tasks. Работает под
ролями workos_reader и workos_app: обеим можно UPDATE planned_* и SELECT calendar_feed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from psycopg.rows import dict_row

from core import config, timeutil as tu

Slot = tuple[datetime, datetime]
STEP = timedelta(minutes=5)


# ---------- интервалы ----------

def ceil5(dt: datetime) -> datetime:
    """Округлить вверх до ближайших 5 минут (12:02 -> 12:05, 12:05 -> 12:05)."""
    base = dt.replace(second=0, microsecond=0)
    if base < dt:
        base += timedelta(minutes=1)
    rem = base.minute % 5
    return base + timedelta(minutes=5 - rem) if rem else base


def merge(slots: list[Slot]) -> list[Slot]:
    out: list[list[datetime]] = []
    for s, e in sorted(x for x in slots if x[1] > x[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(window: Slot, busy: list[Slot]) -> list[Slot]:
    """Свободные куски окна за вычетом занятого."""
    ws, we = window
    free, cur = [], ws
    for s, e in merge(busy):
        if e <= cur or s >= we:
            continue
        if s > cur:
            free.append((cur, min(s, we)))
        cur = max(cur, e)
        if cur >= we:
            break
    if cur < we:
        free.append((cur, we))
    return free


def overlaps(slot: Slot, busy: list[Slot]) -> bool:
    s, e = slot
    return any(bs < e and be > s for bs, be in busy)


def minutes(slots: list[Slot]) -> float:
    return sum((e - s).total_seconds() for s, e in slots) / 60


def _clip(slots: list[Slot], window: Slot) -> list[Slot]:
    ws, we = window
    return [(max(s, ws), min(e, we)) for s, e in slots if s < we and e > ws]


# ---------- тренировка ----------

def training_window(day: date) -> Slot:
    tf, tt = (tu.parse_hm(x) for x in config.TRAINING_WINDOW)
    lo = tu.at_local(day, tf)
    hi = tu.at_local(day + timedelta(days=1) if tt <= tf else day, tt)
    return lo, hi


def training_default(day: date) -> datetime:
    t = tu.parse_hm(config.TRAINING_DEFAULT)
    tf = tu.parse_hm(config.TRAINING_WINDOW[0])
    return tu.at_local(day + timedelta(days=1) if t < tf else day, t)


def _training_slot(day: date, blocked: list[Slot], earliest: datetime, latest: datetime) -> Slot | None:
    lo, hi = training_window(day)
    lo, hi = max(lo, ceil5(earliest)), min(hi, latest)
    dur = timedelta(minutes=config.TRAINING_MINUTES)
    want = training_default(day)
    cands = []
    s = ceil5(lo)
    while s + dur <= hi:
        cands.append(s)
        s += STEP
    # ближе всего к времени по умолчанию; при равенстве раньше
    for s in sorted(cands, key=lambda x: (abs((x - want).total_seconds()), x)):
        if not overlaps((s, s + dur), blocked):
            return s, s + dur
    return None


# ---------- расстановка ----------

def _size_min(task: dict) -> int:
    return config.SIZE_MINUTES.get(task.get("size") or "S", config.SIZE_MINUTES["S"])


def place(day: date, tasks: list[dict], busy: list[Slot], now: datetime | None = None,
          training: bool | None = None) -> dict:
    """Расставить задачи логического дня day.

    tasks: [{id, size, title?, planned_by?, planned_start?, planned_end?}] в порядке приоритета;
    busy: [(start, end)] встреч (aware datetime). Ручные задачи (planned_by='manual') не
    двигаются, их время считается занятым, в ответ они не попадают.
    Ответ: {task_id: (start, end)} и 'training': (start, end), если тренировка встала.
    Не влезшие задачи в ответе отсутствуют.
    """
    ws, we = tu.waking_window(day)
    start = ws if now is None else max(ws, ceil5(now))
    result: dict = {}
    if start >= we:
        return result
    window = (start, we)
    buf = timedelta(minutes=config.BUFFER_MINUTES)
    meetings = _clip([(s - buf, e + buf) for s, e in busy], (ws, we))

    fixed: list[Slot] = []
    movable: list[dict] = []
    for t in tasks:
        if t.get("planned_by") == "manual":
            if t.get("planned_start") and t.get("planned_end"):
                fixed.append((t["planned_start"], t["planned_end"]))
            continue
        movable.append(t)
    fixed = _clip(fixed, (ws, we))

    if training is None:
        training = day.weekday() in config.TRAINING_WEEKDAYS
    if training:
        slot = _training_slot(day, merge(meetings + fixed), start, we)
        if slot:
            result["training"] = slot
    hard = meetings + ([result["training"]] if "training" in result else [])

    # свободное время = окно без встреч и тренировки; ручные задачи его уже заполняют
    budget = config.FILL_RATIO * minutes(subtract(window, hard))
    used = minutes(_clip(merge(fixed), window))
    free = subtract(window, hard + fixed)

    # крупное раньше; при равном размере сохраняется порядок, который дал вызывающий
    for t in sorted(movable, key=lambda x: -_size_min(x)):
        dur = _size_min(t)
        if used + dur > budget + 1e-6:
            continue
        for i, (fs, fe) in enumerate(free):
            s = ceil5(fs)
            e = s + timedelta(minutes=dur)
            if e <= fe:
                result[t["id"]] = (s, e)
                used += dur
                free[i] = (e, fe)
                break
    return result


# ---------- база ----------

def feed_events(conn, day: date) -> list[dict]:
    """Не отменённые встречи логического дня из work.calendar_feed (без целодневных)."""
    lo, hi = tu.day_bounds(day)
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT id, context_id, source_id, title, starts_at, ends_at, location, busy
               FROM work.calendar_feed
               WHERE status <> 'cancelled' AND NOT all_day
                 AND starts_at < %s AND ends_at > %s
               ORDER BY starts_at, ends_at, id""", (hi, lo)).fetchall()


def busy_from_feed(conn, day: date) -> list[Slot]:
    return [(r["starts_at"], r["ends_at"]) for r in feed_events(conn, day) if r["busy"]]


def fixed_slot(task: dict, day: date) -> Slot | None:
    """Время, которое George назначил сам: ручной блок или «в 10» из текста задачи."""
    if task.get("planned_by") == "manual" and task.get("planned_start") and task.get("planned_end"):
        return task["planned_start"], task["planned_end"]
    ra = task.get("remind_at")
    if (task.get("planned_by") is None and task.get("planned_start") is None and ra is not None
            and tu.logical_date(ra) == day):
        return ra, ra + timedelta(minutes=_size_min(task))
    return None


def load_day_tasks(conn, day: date, today: date) -> list[dict]:
    """Несделанные задачи дня; для сегодняшнего и будущих дней ещё и просроченные."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT id, title, size, due, remind_at, planned_by, planned_start, planned_end
               FROM work.tasks
               WHERE done_at IS NULL AND dropped_at IS NULL
                 AND (due = %(day)s OR (due < %(day)s AND %(day)s >= %(today)s))
               ORDER BY due, id""", {"day": day, "today": today}).fetchall()


def replan(conn, day: date, now: datetime | None = None) -> dict:
    """Пересобрать день: ручное оставить, остальное расставить с минуты now и записать."""
    today = tu.logical_date(now) if now else tu.logical_today()
    rows = load_day_tasks(conn, day, today)
    tasks, kept = [], 0
    for r in rows:
        fx = fixed_slot(r, day)
        if fx:
            kept += 1
            tasks.append({**r, "planned_by": "manual", "planned_start": fx[0], "planned_end": fx[1]})
        else:
            tasks.append(r)
    res = place(day, tasks, busy_from_feed(conn, day), now=now)
    placed = changed = cleared = 0
    with conn.transaction():
        for t in tasks:
            if t["planned_by"] == "manual":
                continue
            slot = res.get(t["id"])
            if slot:
                placed += 1
                if (t["planned_start"], t["planned_end"], t["planned_by"]) == (slot[0], slot[1], "auto"):
                    continue
                conn.execute("""UPDATE work.tasks SET planned_start = %s, planned_end = %s,
                                       planned_by = 'auto', planned_at = now() WHERE id = %s""",
                             (slot[0], slot[1], t["id"]))
                changed += 1
            elif t["planned_start"] is not None or t["planned_end"] is not None:
                conn.execute("""UPDATE work.tasks SET planned_start = NULL, planned_end = NULL,
                                       planned_by = NULL, planned_at = now() WHERE id = %s""", (t["id"],))
                cleared += 1
    tr = res.get("training")
    return {"day": day.isoformat(), "tasks": len(tasks), "placed": placed, "kept": kept,
            "unplaced": len(tasks) - placed - kept, "changed": changed, "cleared": cleared,
            "training": tu.hm(tr[0]) if tr else None}

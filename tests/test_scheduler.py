import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from agents import scheduler
from core import config, timeutil as tu

UTC = timezone.utc
WED = date(2026, 10, 21)



@pytest.fixture(autouse=True)
def train_every_day(monkeypatch):
    """Эти тесты про «слепую» тренировку по дням недели; по умолчанию она выключена (берётся из календаря)."""
    monkeypatch.setattr(config, "TRAINING_WEEKDAYS", list(range(7)))


def at(d, hh, mm=0, plus_days=0):
    return tu.at_local(d + timedelta(days=plus_days), time(hh, mm))


def no_overlap(slots):
    s = sorted(slots)
    return all(a[1] <= b[0] for a, b in zip(s, s[1:]))


def test_larger_first_and_training():
    tasks = [{"id": 1, "size": "S"}, {"id": 2, "size": "M"}, {"id": 3, "size": "L"}]
    res = scheduler.place(WED, tasks, [])
    assert res[3] == (at(WED, 8), at(WED, 10))
    assert res[2] == (at(WED, 10), at(WED, 11))
    assert res[1] == (at(WED, 11), at(WED, 11, 30))
    assert res["training"] == (at(WED, 21, 30), at(WED, 23))


def test_buffer_around_meetings():
    busy = [(at(WED, 8, 30), at(WED, 9))]
    res = scheduler.place(WED, [{"id": 1, "size": "S"}], busy, training=False)
    # 08:00-08:30 упирается в буфер 08:20, после встречи можно с 09:10
    assert res[1] == (at(WED, 9, 10), at(WED, 9, 40))
    assert "training" not in res


def test_training_moves_off_meeting_and_respects_weekdays(monkeypatch):
    busy = [(at(WED, 21), at(WED, 22))]
    res = scheduler.place(WED, [], busy)
    assert res["training"] == (at(WED, 22, 10), at(WED, 23, 40))
    monkeypatch.setattr(config, "TRAINING_WEEKDAYS", [5, 6])
    assert "training" not in scheduler.place(WED, [], [])
    assert "training" in scheduler.place(date(2026, 10, 24), [], [])      # суббота


def test_dst_day():
    day = date(2026, 10, 25)               # в 3:00 часы переводятся назад
    res = scheduler.place(day, [{"id": 7, "size": "M"}], [])
    assert res[7][0] == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)          # 08:00 CET
    assert res["training"][0] == datetime(2026, 10, 25, 20, 30, tzinfo=UTC)  # 21:30 CET
    assert tu.hm(res["training"][0]) == "21:30"
    # накануне ещё летнее время: 21:30 это 19:30 UTC
    prev = scheduler.place(date(2026, 10, 24), [], [])
    assert prev["training"][0] == datetime(2026, 10, 24, 19, 30, tzinfo=UTC)
    ws, we = tu.waking_window(day)
    assert we - ws == timedelta(hours=17)


def test_manual_kept_and_counted_busy():
    tasks = [{"id": 1, "size": "M", "planned_by": "manual",
              "planned_start": at(WED, 8), "planned_end": at(WED, 9)},
             {"id": 2, "size": "M", "planned_by": "auto",
              "planned_start": at(WED, 15), "planned_end": at(WED, 16)}]
    res = scheduler.place(WED, tasks, [])
    assert 1 not in res
    assert res[2] == (at(WED, 9), at(WED, 10))


def test_fill_ratio():
    tasks = [{"id": i, "size": "L"} for i in range(10)]
    res = scheduler.place(WED, tasks, [])
    placed = [v for k, v in res.items() if k != "training"]
    free = 17 * 60 - config.TRAINING_MINUTES
    used = sum((e - s).total_seconds() / 60 for s, e in placed)
    assert used <= config.FILL_RATIO * free
    assert len(placed) == int(config.FILL_RATIO * free // 120)
    assert no_overlap(placed + [res["training"]])


def test_nothing_before_now():
    now = at(WED, 13, 2)
    busy = [(at(WED, 14), at(WED, 15))]
    res = scheduler.place(WED, [{"id": 1, "size": "S"}, {"id": 2, "size": "L"}], busy, now=now)
    assert all(s >= at(WED, 13, 5) for s, _ in res.values())
    assert res[1] == (at(WED, 13, 5), at(WED, 13, 35))       # до встречи с буфером влезает только S
    assert res[2] == (at(WED, 15, 10), at(WED, 17, 10))
    # после отбоя ставить некуда
    assert scheduler.place(WED, [{"id": 1, "size": "S"}], [], now=at(WED, 1, 30, plus_days=1)) == {}


def test_no_overlap_with_meetings_busy_day():
    busy = [(at(WED, 9), at(WED, 9, 30)), (at(WED, 10), at(WED, 11)), (at(WED, 12, 45), at(WED, 14)),
            (at(WED, 16), at(WED, 16, 15))]
    tasks = [{"id": i, "size": s} for i, s in enumerate("LMSSMLS")]
    res = scheduler.place(WED, tasks, busy)
    buf = timedelta(minutes=config.BUFFER_MINUTES)
    padded = [(s - buf, e + buf) for s, e in busy]
    for k, slot in res.items():
        assert not scheduler.overlaps(slot, padded), k
    assert no_overlap(list(res.values()))


def _event(admin, ext, start, end, title="Встреча", **meta):
    m = {"ends_at": end.isoformat(), **meta}
    admin.execute("""INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta)
                     VALUES ('infatica', 'ical:infatica', %s, 'calendar', %s, %s, %s)""",
                  (ext, start, title, json.dumps(m)))


@pytest.mark.parametrize("role", ["workos_reader", "workos_app"])
def test_replan_writes_under_role(admin, as_role, role):
    day = WED
    _event(admin, "m1", at(day, 10), at(day, 11), "Daily")
    _event(admin, "m2", at(day, 12), at(day, 13), "Отменена", status="cancelled")
    _event(admin, "m3", at(day, 8), at(day, 20), "Весь день", all_day=True)
    _event(admin, "m4", at(day, 18), at(day, 19), "Свободно", busy=False)

    def task(title, size, due=day, **kw):
        cols = {"title": title, "size": size, "due": due, "source": "app", **kw}
        q = f"INSERT INTO work.tasks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
        return admin.execute(q, list(cols.values())).fetchone()["id"]

    big = task("Отчёт", "L")
    old = task("Хвост", "S", due=day - timedelta(days=2))
    manual = task("Ручное", "M", planned_by="manual", planned_start=at(day, 8), planned_end=at(day, 9))
    stale = task("Старое авто", "S", planned_by="auto", planned_start=at(day, 9), planned_end=at(day, 9, 30))
    other = task("Не сегодня", "S", due=day + timedelta(days=1))

    conn = as_role(role)
    assert scheduler.busy_from_feed(conn, day) == [(at(day, 10), at(day, 11))]
    now = at(day, 9, 31)
    summary = scheduler.replan(conn, day, now)
    assert summary["kept"] == 1 and summary["placed"] == 3 and summary["training"] == "21:30"

    rows = {r["id"]: r for r in admin.execute("SELECT * FROM work.tasks").fetchall()}
    assert rows[manual]["planned_start"] == at(day, 8) and rows[manual]["planned_by"] == "manual"
    assert rows[manual]["planned_at"] is None
    assert rows[other]["planned_start"] is None
    for tid in (big, old, stale):
        r = rows[tid]
        assert r["planned_by"] == "auto" and r["planned_at"] is not None
        assert r["planned_start"] >= at(day, 9, 35)
        assert not scheduler.overlaps((r["planned_start"], r["planned_end"]), [(at(day, 9, 50), at(day, 11, 10))])
    assert rows[big]["planned_start"] == at(day, 11, 10)        # L раньше остальных
    # повторный прогон ничего не меняет
    again = scheduler.replan(conn, day, now)
    assert again["changed"] == 0

    # то, что не влезло, теряет время
    late = at(day, 0, 50, plus_days=1)
    res = scheduler.replan(conn, day, late)
    assert res["placed"] == 0 and res["cleared"] == 3
    assert admin.execute("SELECT count(*) AS n FROM work.tasks WHERE planned_by = 'auto'").fetchone()["n"] == 0

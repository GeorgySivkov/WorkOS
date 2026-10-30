"""2.8 планировщик, 2.9 живой день, классификация задач по клеткам."""
import json
import sys
import types
from datetime import date, time, timedelta

import pytest

from core import config, llm, timeutil as tu


def at(y, m, d, hh=12, mm=0):
    return tu.at_local(date(y, m, d), time(hh, mm))


# ---------- заглушка расстановщика (контракт docs/CODE.md, без базы в place) ----------
def _stub_place(day, tasks, busy, now=None):
    w0, w1 = tu.waking_window(day)
    buf = timedelta(minutes=config.BUFFER_MINUTES)
    taken = [(s - buf, e + buf) for s, e in busy]
    out = {}
    if now is None:
        tr = tu.at_local(day, tu.parse_hm(config.TRAINING_DEFAULT))
        out["training"] = (tr, tr + timedelta(minutes=config.TRAINING_MINUTES))
        taken.append(out["training"])
    taken += [(t["planned_start"], t["planned_end"]) for t in tasks
              if t.get("planned_by") == "manual" and t.get("planned_start")]
    start = max(w0, now) if now else w0
    for t in sorted((t for t in tasks if t.get("planned_by") != "manual"),
                    key=lambda t: -config.SIZE_MINUTES[t["size"]]):
        d = timedelta(minutes=config.SIZE_MINUTES[t["size"]])
        s, moved = start, True
        while moved:
            moved = False
            for b0, b1 in taken:
                if s < b1 and b0 < s + d:
                    s, moved = b1, True
        if s + d <= w1:
            out[t["id"]] = (s, s + d)
            taken.append((s, s + d))
    return out


def _stub_busy(conn, day):
    d0, d1 = tu.day_bounds(day)
    return [(r["starts_at"], r["ends_at"]) for r in conn.execute(
        "SELECT starts_at, ends_at FROM work.calendar_feed WHERE status <> 'cancelled' AND busy AND NOT all_day "
        "AND starts_at < %s AND ends_at > %s", (d1, d0))]


@pytest.fixture(autouse=True)
def sched(monkeypatch):
    mod = types.ModuleType("agents.scheduler")
    mod.place, mod.busy_from_feed = _stub_place, _stub_busy
    mod.replan = lambda conn, day, now: {}
    monkeypatch.setitem(sys.modules, "agents.scheduler", mod)
    monkeypatch.setitem(sys.modules, "agents.autonomy", None)
    yield mod
    llm.set_fake(None)


def meeting(admin, ext, title, start, end, source="ical:personal", ctx="personal"):
    admin.execute("INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta) "
                  "VALUES (%s, %s, %s, 'calendar', %s, %s, %s)",
                  (ctx, source, ext, start, title, json.dumps({"ends_at": end.isoformat()})))


def task(admin, title, **kw):
    cols = {"title": title, "size": "S", "source": "app", **kw}
    q = f"INSERT INTO work.tasks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
    return admin.execute(q, list(cols.values())).fetchone()["id"]


# ---------- планировщик ----------
def test_logical_tomorrow():
    from agents import planner
    assert planner.target_date(at(2026, 10, 23, 0, 30)) == date(2026, 10, 23)
    assert planner.target_date(at(2026, 10, 22, 23, 50)) == date(2026, 10, 23)
    assert planner.target_date(at(2026, 10, 23, 9)) == date(2026, 10, 24)


def test_planner_outbox_and_plan_batch(admin, role_env):
    from agents import planner
    task(admin, "Сдать отчёт Альфе", due=date(2026, 10, 22), done_at=at(2026, 10, 22, 15), context_id="alfa")
    task(admin, "Разобрать почту", due=date(2026, 10, 22), context_id="personal")
    old = task(admin, "Отчёт по SDK", due=date(2026, 10, 20), size="M", context_id="infatica")
    meeting(admin, "m1", "Синк Infatica", at(2026, 10, 23, 9), at(2026, 10, 23, 10))
    seen = {}

    def fake(task_name, system, user):
        seen.update(task=task_name, user=user)
        return json.dumps({"summary": "Фокус: SDK и хвосты.", "items": [
            {"title": "Отчёт по SDK", "size": "M", "context_id": "infatica", "cell_id": None,
             "task_id": old, "why": "просрочено"},
            {"title": "Позвонить в банк", "size": "S", "context_id": "nope", "cell_id": "Z9", "why": "давно"}],
            "week_items": [], "waiting": []}, ensure_ascii=False)
    llm.set_fake(fake)
    role_env("workos_reader")
    now = at(2026, 10, 22, 23, 50)
    r = planner.run(now=now)
    assert r["for_date"] == "2026-10-23" and r["items"] == 2 and r["placed"] == 2
    u = seen["user"]
    assert seen["task"] == "planner"
    assert u.index("Сделано сегодня из плана") < u.index("Встречи завтра") and "Сдать отчёт Альфе" in u
    assert "Синк Infatica" in u and f"#{old} Отчёт по SDK" in u

    text = admin.execute("SELECT text FROM work.outbox WHERE topic = 'plan'").fetchone()["text"]
    assert text.startswith("<b>План на пт, 23 октября</b>")
    assert "08:00 Позвонить в банк · 30 мин" in text and "10:10 Отчёт по SDK · 1 ч" in text
    assert "21:30 Тренировка · 1,5 ч" in text and "Занято" in text and "% свободного времени" in text
    act = admin.execute("SELECT * FROM work.actions").fetchall()
    assert len(act) == 1 and act[0]["type"] == "plan_batch" and act[0]["status"] == "proposed"
    p = act[0]["payload"]
    assert p["for_date"] == "2026-10-23" and p["week_items"] == []
    sdk = next(i for i in p["items"] if i["title"] == "Отчёт по SDK")
    assert sdk["start"] == "2026-10-23T10:10+02:00" and sdk["end"] == "2026-10-23T11:10+02:00"
    assert sdk["task_id"] == old and sdk["context_id"] == "infatica"
    bank = next(i for i in p["items"] if i["title"] == "Позвонить в банк")
    assert bank["context_id"] == "personal" and "cell_id" not in bank      # чужие значения отброшены

    # идемпотентно по дате: и в 23:55, и после полуночи это тот же план
    assert planner.run(now=at(2026, 10, 22, 23, 55))["skipped"]
    assert planner.run(now=at(2026, 10, 23, 0, 30))["skipped"]
    assert admin.execute("SELECT count(*) AS n FROM work.actions").fetchone()["n"] == 1
    planner.run(now=now, force=True)
    assert admin.execute("SELECT count(*) AS n FROM work.outbox WHERE topic = 'plan'").fetchone()["n"] == 2


def test_planner_sunday_week_and_waiting(admin, role_env):
    from agents import planner
    pid = admin.execute("INSERT INTO work.people (display_name) VALUES ('Ян') RETURNING id").fetchone()["id"]
    admin.execute("INSERT INTO work.commitments (context_id, direction, person_id, what, due) "
                  "VALUES ('alfa', 'theirs', %s, 'методика часов поддержки', '2026-10-28')", (pid,))
    llm.set_fake(lambda *a: json.dumps({"summary": "", "items": [], "week_items": [
        {"title": "Спланировать спринт", "size": "M", "context_id": "infatica"}], "waiting": []}))
    role_env("workos_reader")
    r = planner.run(now=at(2026, 10, 26, 0, 10))        # ночь с вс на пн: логическое сегодня воскресенье
    assert r["for_date"] == "2026-10-26"
    text = admin.execute("SELECT text FROM work.outbox").fetchone()["text"]
    assert "<b>Неделя</b>" in text and "Спланировать спринт" in text
    assert "Жду от других" in text and "Ян: методика часов поддержки" in text
    p = admin.execute("SELECT payload FROM work.actions").fetchone()["payload"]
    assert p["week_items"] == [{"title": "Спланировать спринт", "size": "M", "context_id": "infatica"}]


def test_planner_dry_run_writes_nothing(admin, role_env, capsys):
    from agents import planner
    llm.set_fake(lambda *a: '{"summary": "x", "items": [{"title": "Тест", "size": "S"}]}')
    role_env("workos_reader")
    r = planner.run(date="2026-10-23", dry_run=True, now=at(2026, 10, 22, 23))
    assert r["dry_run"] and "План на пт, 23 октября" in capsys.readouterr().out
    assert admin.execute("SELECT count(*) AS n FROM work.outbox").fetchone()["n"] == 0
    assert admin.execute("SELECT count(*) AS n FROM work.notices").fetchone()["n"] == 0


# ---------- живой день ----------
def test_dayplan_shifts_auto_task_keeps_manual(admin, role_env):
    from agents import dayplan
    auto = task(admin, "Отчёт", size="M", planned_by="auto",
                planned_start=at(2026, 10, 23, 10), planned_end=at(2026, 10, 23, 11))
    manual = task(admin, "Звонок маме", planned_by="manual",
                  planned_start=at(2026, 10, 23, 12), planned_end=at(2026, 10, 23, 12, 30))
    role_env("workos_reader")
    now = at(2026, 10, 23, 9)
    assert dayplan.run(now=now)["moved"] == 0
    meeting(admin, "m1", "Синк", at(2026, 10, 23, 10, 30), at(2026, 10, 23, 11, 30))
    meeting(admin, "m2", "Обед с Яном", at(2026, 10, 23, 12), at(2026, 10, 23, 13))
    r = dayplan.run(now=now)
    assert r["moved"] == 1 and r["unplaced"] == 0
    rows = {t["id"]: t for t in admin.execute("SELECT * FROM work.tasks")}
    assert rows[auto]["planned_start"] == at(2026, 10, 23, 9) and rows[auto]["planned_at"] == now
    assert rows[manual]["planned_start"] == at(2026, 10, 23, 12)          # ручное не трогаем
    msg = admin.execute("SELECT text, topic FROM work.outbox").fetchall()
    assert len(msg) == 1 and msg[0]["topic"] == "plan"
    assert msg[0]["text"] == "Сдвинул «Отчёт» на 09:00: поверх встала встреча «Синк»"
    assert dayplan.run(now=now)["moved"] == 0                             # повтор ничего не двигает


def test_dayplan_unplaced_asks_with_buttons(admin, role_env):
    from agents import dayplan
    tid = task(admin, "Ревью", size="L", planned_by="auto",
               planned_start=at(2026, 10, 23, 22), planned_end=at(2026, 10, 24, 0))
    meeting(admin, "m1", "Релиз", at(2026, 10, 23, 21, 50), at(2026, 10, 24, 1))
    role_env("workos_reader")
    r = dayplan.run(now=at(2026, 10, 23, 21, 45))
    assert r["unplaced"] == 1
    t = admin.execute("SELECT planned_start, planned_end FROM work.tasks WHERE id = %s", (tid,)).fetchone()
    assert t["planned_start"] is None and t["planned_end"] is None
    m = admin.execute("SELECT text, buttons FROM work.outbox").fetchone()
    assert m["text"].startswith("Не влезло: Ревью") and m["text"].endswith("Перенести на завтра?")
    assert [b["callback_data"] for b in m["buttons"][0]] == [f"tk:{tid}:tmr", f"tk:{tid}:keep"]


# ---------- классификация ----------
def test_classify_tasks_sets_cell_once(admin, role_env):
    from agents import dayplan
    assert dayplan.classify_tasks()["skipped"]                             # без сетки модель не зовётся
    admin.execute("INSERT INTO rpg.cells (id, parent, title, kind) VALUES ('C', NULL, 'Квест', 'quest'), "
                  "('A', 'C', 'Здоровье', 'attribute'), ('A1', 'A', 'Бег', 'habit')")
    run_id = task(admin, "Пробежка 5 км")
    rep_id = task(admin, "Отчёт по SDK", source="capture")
    plan_id = task(admin, "Из плана", source="plan")
    calls = []

    def fake(task_name, system, user):
        calls.append((task_name, user))
        return json.dumps({str(run_id): {"cell_id": "A1", "size": "M"}, str(rep_id): {"cell_id": None, "size": "L"}})
    llm.set_fake(fake)
    role_env("workos_reader")
    r = dayplan.classify_tasks()
    assert r == {"classified": 1, "seen": 2}
    assert calls[0][0] == "classify_cell" and "A1 Бег" in calls[0][1] and "Из плана" not in calls[0][1]
    rows = {t["id"]: t for t in admin.execute("SELECT id, cell_id, size FROM work.tasks")}
    assert (rows[run_id]["cell_id"], rows[run_id]["size"]) == ("A1", "M")
    assert (rows[rep_id]["cell_id"], rows[rep_id]["size"]) == (None, "L")
    assert rows[plan_id]["cell_id"] is None
    assert dayplan.classify_tasks() == {"classified": 0} and len(calls) == 1

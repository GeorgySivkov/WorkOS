"""Поправки логики после ревью: потолок опыта без штрафа, ночное время логического дня,
захват от даты сообщения, ручное время как занятое, классификация с потолком попыток,
переключатель привычки, персонаж по всему журналу, повышение уровня один раз,
догоняющий план, planned_at только при смене времени, жёсткость сервера приложения."""
import json
import socket
import sys
import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from core import llm, ruparse, timeutil as tu

UTC = timezone.utc
DEV = "d" * 40
ROOT = Path(__file__).resolve().parent.parent


def at(y, m, d, hh=12, mm=0):
    return tu.at_local(date(y, m, d), time(hh, mm))


@pytest.fixture(autouse=True)
def _reset_fake():
    yield
    llm.set_fake(None)


def grid(admin):
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('C', NULL, 'Квест', 'quest', 0, NULL),
        ('A', 'C', 'Здоровье', 'attribute', 0, NULL), ('B', 'C', 'Карьера', 'attribute', 0, NULL)""")
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('A1', 'A', 'Растяжка', 'habit', 10, NULL), ('A2', 'A', 'Сон', 'habit', 10, 'garmin:sleep:7.5'),
        ('A3', 'A', 'Марафон', 'milestone', 150, NULL), ('B1', 'B', 'Чтение', 'habit', 10, NULL)""")


def add_task(admin, title, **kw):
    cols = {"title": title, "source": "app", **kw}
    q = f"INSERT INTO work.tasks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
    return admin.execute(q, list(cols.values())).fetchone()["id"]


def xp_rows(admin, cell):
    return admin.execute("SELECT amount, source FROM rpg.xp WHERE cell_id = %s ORDER BY id", (cell,)).fetchall()


# ---------- мини-приложение без HTTP ----------

@pytest.fixture
def api(database):
    from app import server

    def connect():
        return psycopg.connect(user="workos_app", autocommit=True, row_factory=dict_row)
    a = server.Api(connect=connect, bot_id=1, owner_id=111, public_key="00" * 32, dev_token=DEV)
    yield a
    a.pool.close()


def call(api, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(method, path, {}, {"Authorization": f"dev {DEV}"}, raw)[1]


# ---------- 1. потолок опыта не штрафует ----------

def test_cap_records_zero_and_keeps_streak(admin, as_role, api):
    grid(admin)
    yesterday = tu.logical_today() - timedelta(days=1)
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) VALUES "
                  "('A1', 10, 'manual', %s, %s)", (f"A1:{yesterday}", tu.at_local(yesterday, time(12))))
    # сегодня характеристика уже набрала 100
    t = add_task(admin, "Большая тренировка", size="L", cell_id="A", done_at=tu.now())
    t2 = add_task(admin, "Ещё одна", size="L", cell_id="A", done_at=tu.now())
    app = as_role("workos_app")
    assert app.execute("SELECT rpg.award_task(%s) AS n", (t,)).fetchone()["n"] == 60
    assert app.execute("SELECT rpg.award_task(%s) AS n", (t2,)).fetchone()["n"] == 40
    t3 = add_task(admin, "Сверх потолка", size="S", cell_id="A", done_at=tu.now())
    assert app.execute("SELECT rpg.award_task(%s) AS n", (t3,)).fetchone()["n"] == 0
    # запись с нулём есть: начислятор не крутит задачу каждые 5 минут
    assert admin.execute("SELECT amount FROM rpg.xp WHERE source = 'task' AND source_ref = %s",
                         (str(t3),)).fetchone()["amount"] == 0

    r = call(api, "POST", "/api/habits/A1/toggle")
    assert r["done"] is True and r["xp"] == 0 and r["locked"] is False
    assert [x["amount"] for x in xp_rows(admin, "A1")] == [10, 0]
    # серия не порвалась: завтрашняя отметка считает и сегодняшний нулевой день
    habits = {h["cell_id"]: h for h in call(api, "GET", "/api/today")["habits"]}
    assert habits["A1"]["done"] is True and habits["A1"]["streak"] == 2
    # снять ручную нулевую отметку можно
    assert call(api, "POST", "/api/habits/A1/toggle")["done"] is False
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                      "VALUES ('A1', -1, 'manual', 'neg', now())")


def test_milestone_does_not_eat_daily_cap(admin, as_role):
    grid(admin)
    app = as_role("workos_app")
    assert app.execute("SELECT rpg.log_manual('A3') AS n").fetchone()["n"] == 150
    assert app.execute("SELECT rpg.day_xp('A', rpg.lday(now())) AS n").fetchone()["n"] == 0
    assert app.execute("SELECT rpg.log_manual('A1') AS n").fetchone()["n"] == 10
    t = add_task(admin, "L по здоровью", size="L", cell_id="A", done_at=tu.now())
    assert app.execute("SELECT rpg.award_task(%s) AS n", (t,)).fetchone()["n"] == 60
    # задача на клетке этапа считается в потолок, как любая задача
    t2 = add_task(admin, "Подготовка к марафону", size="L", cell_id="A3", done_at=tu.now())
    assert app.execute("SELECT rpg.award_task(%s) AS n", (t2,)).fetchone()["n"] == 30
    assert app.execute("SELECT rpg.day_xp('A', rpg.lday(now())) AS n").fetchone()["n"] == 100


class CapGarmin:
    def activities(self, start, end):
        return []

    def sleep_hours(self, d):
        return 8.0

    def steps(self, d):
        return None


def test_garmin_day_counts_at_cap(admin, role_env):
    from rpg import garmin
    grid(admin)
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('A1', 100, 'manual', 'x', %s)", (at(2026, 10, 29, 9),))
    role_env("workos_reader")
    r = garmin.run(days=1, client=CapGarmin(), now=at(2026, 10, 29, 15))
    assert r["awarded"] == {} and r["capped"] == ["A2:2026-10-29"]
    row = admin.execute("SELECT amount, source FROM rpg.xp WHERE cell_id = 'A2'").fetchone()
    assert row == {"amount": 0, "source": "garmin"}
    # следующий день: серия видит вчерашнюю нулевую отметку (+10%)
    r2 = garmin.run(days=1, client=CapGarmin(), now=at(2026, 10, 30, 15))
    assert r2["awarded"] == {"A2:2026-10-30": 11}
    assert garmin.run(days=2, client=CapGarmin(), now=at(2026, 10, 30, 16))["capped"] == []   # дни уже засчитаны


# ---------- 2. 00:00-03:59 логического дня ----------

def test_local_dt_for_and_ruparse_night():
    assert tu.local_dt_for(date(2026, 10, 21), time(0, 30)) == at(2026, 10, 22, 0, 30)
    assert tu.local_dt_for(date(2026, 10, 21), time(4, 0)) == at(2026, 10, 21, 4, 0)
    now = at(2026, 10, 21, 23)                         # среда, 23:00
    p = ruparse.parse("сегодня в 0:30 выпить таблетку", now)
    assert p.due == date(2026, 10, 21) and p.remind_at == at(2026, 10, 22, 0, 30) and p.remind_at > now
    p = ruparse.parse("завтра в 0:30 позвонить", now)
    assert p.due == date(2026, 10, 22) and p.remind_at == at(2026, 10, 23, 0, 30)
    p = ruparse.parse("в 0:30 проверить релиз", now)
    assert p.due == date(2026, 10, 21) and p.remind_at == at(2026, 10, 22, 0, 30)
    # после полуночи «в 0:30» уже прошло: следующая ночь, день задачи четверг
    p = ruparse.parse("в 0:30 проверить релиз", at(2026, 10, 22, 1))
    assert p.due == date(2026, 10, 22) and p.remind_at == at(2026, 10, 23, 0, 30)
    p = ruparse.parse("в пт в 2 ночи деплой", at(2026, 10, 21, 15))
    assert p.due == date(2026, 10, 23) and p.remind_at == at(2026, 10, 24, 2)


def test_app_create_night_and_tomorrow_shift(admin, api):
    api.clock = lambda: at(2026, 10, 21, 23)
    t = call(api, "POST", "/api/tasks", {"text": "проверить релиз в 0:30"})
    assert t["due"] == "2026-10-21" and t["time"] == "00:30"
    row = admin.execute("SELECT * FROM work.tasks WHERE id = %s", (t["id"],)).fetchone()
    assert row["remind_at"] == at(2026, 10, 22, 0, 30) and row["planned_start"] == at(2026, 10, 22, 0, 30)

    # «На завтра» у просроченной: напоминание едет на столько же дней, в то же время
    api.clock = lambda: at(2026, 10, 24, 15)
    old = add_task(admin, "Хвост", due=date(2026, 10, 21), remind_at=at(2026, 10, 21, 10),
                   reminded_at=at(2026, 10, 21, 10))
    call(api, "POST", f"/api/tasks/{old}/tomorrow")
    row = admin.execute("SELECT * FROM work.tasks WHERE id = %s", (old,)).fetchone()
    assert row["due"] == date(2026, 10, 25) and row["remind_at"] == at(2026, 10, 25, 10)
    assert row["reminded_at"] is None and row["planned_at"] is None        # блока не было: planned_at не трогаем
    # ночное напоминание остаётся ночью после нового дня
    night = add_task(admin, "Ночное", due=date(2026, 10, 24), remind_at=at(2026, 10, 25, 0, 30))
    call(api, "POST", f"/api/tasks/{night}/tomorrow")
    assert admin.execute("SELECT remind_at FROM work.tasks WHERE id = %s",
                         (night,)).fetchone()["remind_at"] == at(2026, 10, 26, 0, 30)
    # уже на завтра: напоминание то же, и отметка «уже напомнил» не сбрасывается
    same = add_task(admin, "Уже завтра", due=date(2026, 10, 25), remind_at=at(2026, 10, 25, 9),
                    reminded_at=at(2026, 10, 25, 9))
    call(api, "POST", f"/api/tasks/{same}/tomorrow")
    row = admin.execute("SELECT remind_at, reminded_at FROM work.tasks WHERE id = %s", (same,)).fetchone()
    assert row["remind_at"] == at(2026, 10, 25, 9) and row["reminded_at"] == at(2026, 10, 25, 9)


# ---------- 3, 4. захват: дата сообщения, правка, ручной блок ----------

class FakeCtx:
    def __init__(self, conn):
        self.conn = conn
        self.edits = []

    def reply(self, msg, text, rows=None):
        return {"message_id": 9000 + msg["message_id"]}

    def edit(self, msg_id, text, rows=None):
        self.edits.append((msg_id, text))


def capture_answer(title):
    return json.dumps({"kind": "task", "context_id": "personal", "confidence": 0.9, "title": title,
                       "due": None, "time": None, "size": "S", "cell_id": None})


def tg_msg(mid, text, sent):
    return {"message_id": mid, "from": {"id": 111}, "chat": {"id": 111, "type": "private"},
            "date": int(sent.timestamp()), "text": text}


def test_capture_uses_message_date_and_sets_manual_block(admin, as_role, monkeypatch):
    from bot import capture
    llm.set_fake(lambda *a: capture_answer("Позвонить в банк"))
    ctx = FakeCtx(as_role("workos_bot"))
    # написано в среду вечером, бот дочитал очередь только в четверг утром
    monkeypatch.setattr(tu, "now", lambda: at(2026, 10, 22, 9))
    eid = capture.handle(ctx, tg_msg(501, "позвонить в банк завтра в 10", at(2026, 10, 21, 20)))
    t = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert t["due"] == date(2026, 10, 22) and t["remind_at"] == at(2026, 10, 22, 10)
    assert (t["planned_start"], t["planned_end"], t["planned_by"]) == (at(2026, 10, 22, 10), at(2026, 10, 22, 10, 30),
                                                                     "manual")
    assert t["planned_at"] is not None
    ev = admin.execute("SELECT meta FROM work.events WHERE id = %s", (eid,)).fetchone()["meta"]
    assert ev["class"]["due"] == "2026-10-22"
    # время без даты ночью: день задачи логический
    monkeypatch.setattr(tu, "now", lambda: at(2026, 10, 22, 23))
    capture.handle(ctx, tg_msg(502, "выпить таблетку в 0:30", at(2026, 10, 22, 23)))
    t2 = admin.execute("SELECT * FROM work.tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert t2["due"] == date(2026, 10, 22) and t2["remind_at"] == at(2026, 10, 23, 0, 30)


def test_capture_edit_keeps_dates_and_reminder(admin, as_role, monkeypatch):
    from bot import capture
    llm.set_fake(lambda *a: capture_answer("Позвонить в бнак"))
    ctx = FakeCtx(as_role("workos_bot"))
    sent = at(2026, 10, 21, 15)
    monkeypatch.setattr(tu, "now", lambda: sent)
    m = tg_msg(601, "позвонить в бнак завтра в 10", sent)
    capture.handle(ctx, m)
    tid = admin.execute("SELECT id FROM work.tasks").fetchone()["id"]
    admin.execute("UPDATE work.tasks SET reminded_at = %s WHERE id = %s", (at(2026, 10, 22, 10), tid))

    # назавтра после напоминания поправил опечатку: день и напоминание на месте
    monkeypatch.setattr(tu, "now", lambda: at(2026, 10, 22, 11))
    capture.on_edit(ctx, dict(m, text="позвонить в банк завтра в 10", edit_date=int(clock.time())))
    t = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert t["title"] == "позвонить в банк" and t["due"] == date(2026, 10, 22)
    assert t["remind_at"] == at(2026, 10, 22, 10) and t["reminded_at"] == at(2026, 10, 22, 10)

    # поменял время: напоминание взводится заново, ручной блок едет следом
    capture.on_edit(ctx, dict(m, text="позвонить в банк завтра в 12"))
    t = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert t["remind_at"] == at(2026, 10, 22, 12) and t["reminded_at"] is None
    assert t["planned_start"] == at(2026, 10, 22, 12) and t["planned_by"] == "manual"


def test_remind_only_task_is_busy_for_dayplan_and_planner(admin, role_env):
    from agents import dayplan, planner, scheduler
    day = date(2026, 10, 23)
    auto = add_task(admin, "Отчёт", size="M", due=day, planned_by="auto",
                    planned_start=at(2026, 10, 23, 10), planned_end=at(2026, 10, 23, 11))
    call_id = add_task(admin, "Звонок", size="S", due=day, remind_at=at(2026, 10, 23, 10, 30))
    role_env("workos_reader")
    r = dayplan.run(now=at(2026, 10, 23, 9))
    assert r["moved"] == 1
    rows = {t["id"]: t for t in admin.execute("SELECT * FROM work.tasks")}
    assert rows[auto]["planned_start"] == at(2026, 10, 23, 9)
    assert rows[call_id]["planned_start"] is None                       # ручное время не трогаем
    msg = admin.execute("SELECT text FROM work.outbox").fetchone()["text"]
    assert "на это время ты поставил «Звонок»" in msg

    with psycopg.connect(user="workos_reader", autocommit=True, row_factory=dict_row) as conn:
        fx = planner.fixed_blocks(conn, scheduler, day)
    assert [(f["id"], f["planned_start"], f["planned_end"]) for f in fx] == [
        (-call_id, at(2026, 10, 23, 10, 30), at(2026, 10, 23, 11))]


# ---------- 5. классификация: план и обещания, потолок попыток ----------

def test_classify_failure_retries_at_most_three_times(admin, role_env):
    from agents import dayplan
    grid(admin)
    tid = add_task(admin, "Из обещания", source="commitment")
    calls = []

    def down(task_name, system, user):
        calls.append(user)
        raise llm.LLMError("модель лежит")
    llm.set_fake(down)
    role_env("workos_reader")
    r = dayplan.classify_tasks()
    assert r["seen"] == 1 and r["gave_up"] == 0 and "модель лежит" in r["failed"]
    assert dayplan.classify_tasks() == {"classified": 0}             # через 5 минут не повторяет
    for _ in range(2):
        admin.execute("UPDATE work.notices SET sent_at = now() - interval '2 hours' WHERE key LIKE 'clsfail:%%'")
        dayplan.classify_tasks()
    assert len(calls) == 3
    assert admin.execute("SELECT 1 FROM work.notices WHERE key = %s", (f"cls:{tid}",)).fetchone()
    admin.execute("UPDATE work.notices SET sent_at = now() - interval '2 hours' WHERE key LIKE 'clsfail:%%'")
    assert dayplan.classify_tasks() == {"classified": 0} and len(calls) == 3
    sysmsg = admin.execute("SELECT text FROM work.outbox WHERE topic = 'system'").fetchall()
    assert len(sysmsg) == 1 and "Не разложил задачи по клеткам" in sysmsg[0]["text"]
    assert "—" not in sysmsg[0]["text"]


# ---------- 6. привычка, отмеченная не руками ----------

def test_habit_done_by_task_is_not_toggled(admin, api):
    grid(admin)
    tid = add_task(admin, "Растяжка 20 минут", size="S", cell_id="A1", due=tu.logical_today())
    assert call(api, "POST", f"/api/tasks/{tid}/done")["xp"] == 10
    for _ in range(2):
        r = call(api, "POST", "/api/habits/A1/toggle")
        assert r["done"] is True and r["locked"] is True and r["xp"] == 0 and r["character"]["xp"] == 10
    assert [x["source"] for x in xp_rows(admin, "A1")] == ["task"]


# ---------- 7. персонаж по всему журналу ----------

def test_character_survives_season_reseed(admin, api):
    grid(admin)
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) VALUES "
                  "('A1', 80, 'manual', 'a', now() - interval '100 days'), ('B1', 40, 'manual', 'b', now())")
    admin.execute("UPDATE rpg.cells SET active = false WHERE id IN ('A', 'A1')")    # новый сезон без «Здоровья»
    ch = call(api, "GET", "/api/character")
    assert ch["xp"] == 120 and ch["level"] == 1
    assert [a["id"] for a in ch["attributes"]] == ["B"]


# ---------- 8. повышение уровня один раз ----------

def test_level_up_shown_once(admin, api, role_env):
    grid(admin)
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('B1', 90, 'manual', 'seed', now() - interval '3 days')")
    tid = add_task(admin, "Отчёт", size="S", cell_id="A", due=tu.logical_today())
    assert call(api, "POST", f"/api/tasks/{tid}/done")["level_up"] is True
    call(api, "POST", f"/api/tasks/{tid}/undone")
    again = call(api, "POST", f"/api/tasks/{tid}/done")
    assert again["character"]["level"] == 1 and again["level_up"] is False
    call(api, "POST", f"/api/tasks/{tid}/undone")
    assert call(api, "POST", "/api/habits/A1/toggle")["level_up"] is False      # и через привычку тоже
    # начислятор по-прежнему сообщает о новом уровне в Telegram ровно один раз
    from rpg import award
    role_env("workos_reader")
    admin.execute("DELETE FROM rpg.level_marks WHERE scope <> 'app:character'")
    award.run()
    assert admin.execute("SELECT level FROM rpg.level_marks WHERE scope = 'character'").fetchone()["level"] == 1
    css = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
    assert ".lvlup { position: absolute; inset: 0; z-index: 40; pointer-events: none; }" in css


# ---------- 9. догоняющий запуск планировщика ----------

def test_planner_catch_up_plans_today_from_now(admin, role_env, monkeypatch):
    from agents import planner
    monkeypatch.setitem(sys.modules, "agents.autonomy", None)
    llm.set_fake(lambda *a: json.dumps({"summary": "", "items": [{"title": "Разобрать почту", "size": "S"}]}))
    role_env("workos_reader")
    r = planner.run(now=at(2026, 10, 23, 10, 2))       # таймер догнал после перезагрузки
    assert r["for_date"] == "2026-10-23" and r["placed"] == 1
    item = admin.execute("SELECT payload FROM work.actions").fetchone()["payload"]["items"][0]
    assert datetime.fromisoformat(item["start"]) >= at(2026, 10, 23, 10, 5)
    nightly = planner.run(now=at(2026, 10, 24, 0, 0))
    assert nightly["for_date"] == "2026-10-24" and "skipped" not in nightly


# ---------- 10. planned_at только при смене времени ----------

def test_planned_at_bumped_only_when_times_change(admin, api):
    day = tu.logical_today()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    tid = add_task(admin, "Блок", due=day, planned_by="manual", planned_start=tu.at_local(day, time(10)),
                   planned_end=tu.at_local(day, time(10, 30)), planned_at=old)
    call(api, "POST", f"/api/tasks/{tid}/done")
    call(api, "POST", f"/api/tasks/{tid}/undone")
    assert admin.execute("SELECT planned_at FROM work.tasks WHERE id = %s", (tid,)).fetchone()["planned_at"] == old
    call(api, "POST", f"/api/tasks/{tid}/drop")
    row = admin.execute("SELECT planned_at, planned_start FROM work.tasks WHERE id = %s", (tid,)).fetchone()
    assert row["planned_start"] is None and row["planned_at"] > old
    bare = add_task(admin, "Без блока", due=day)
    call(api, "POST", f"/api/tasks/{bare}/drop")
    assert admin.execute("SELECT planned_at FROM work.tasks WHERE id = %s", (bare,)).fetchone()["planned_at"] is None


# ---------- 11. сервер приложения ----------

def test_dev_token_min_length_and_socket_limits(monkeypatch, api):
    from app import server
    monkeypatch.setenv("APP_DEV_TOKEN", "short")
    assert server.Api(connect=lambda: None).dev_token == ""
    monkeypatch.setenv("APP_DEV_TOKEN", DEV)
    assert server.Api(connect=lambda: None).dev_token == DEV
    assert server.Handler.timeout == 15

    httpd = server.make_server("127.0.0.1", 0, api, max_threads=1)
    th = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    port = httpd.server_address[1]
    req = b"GET /api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    try:
        hog = socket.create_connection(("127.0.0.1", port))        # держит единственный поток, молчит
        clock.sleep(0.3)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(req)
            try:
                got = s.recv(100)
            except ConnectionResetError:
                got = b""
            assert got == b""                                          # лишнее соединение закрыто сразу
        hog.close()
        clock.sleep(0.3)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(req)
            assert s.recv(100).startswith(b"HTTP/1.0 200")
    finally:
        httpd.shutdown()
        httpd.server_close()

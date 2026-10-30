from datetime import date, datetime, time, timezone

import psycopg
import pytest

from collectors.base import Collector, Event, run_source
from core import ruparse, timeutil as tu

UTC = timezone.utc


def at(y, m, d, hh=12, mm=0):
    return tu.at_local(date(y, m, d), time(hh, mm))


def test_ruparse_weekday_and_time():
    now = at(2026, 10, 21, 15)              # среда
    p = ruparse.parse("позвонить в банк в пт в 10", now)
    assert p.title == "позвонить в банк"
    assert p.due == date(2026, 10, 23) and p.at == time(10, 0)
    assert p.remind_at == tu.at_local(date(2026, 10, 23), time(10))


def test_ruparse_relative_and_dates():
    now = at(2026, 10, 21, 15)
    assert ruparse.parse("купить корм завтра", now).due == date(2026, 10, 22)
    assert ruparse.parse("отчёт к 23.10", now).due == date(2026, 10, 23)
    assert ruparse.parse("отчёт 5 ноября", now).due == date(2026, 11, 5)
    assert ruparse.parse("созвон через 2 дня", now).due == date(2026, 10, 23)
    p = ruparse.parse("разобрать почту на следующей неделе", now)
    assert p.due is None and p.week_of == date(2026, 10, 26)
    p = ruparse.parse("книга про продукт", now)
    assert p.bucket == "later" and p.title == "книга про продукт"


def test_ruparse_after_midnight_is_logical_day():
    # 00:30 в ночь на четверг: логический день ещё среда, «завтра» = четверг
    now = at(2026, 10, 22, 0, 30)
    assert tu.logical_date(now) == date(2026, 10, 21)
    assert ruparse.parse("сдать отчёт завтра", now).due == date(2026, 10, 22)
    # время без даты: ближайшее будущее
    p = ruparse.parse("выпить таблетку в 9 утра", now)
    assert p.due == date(2026, 10, 22) and p.at == time(9)


def test_ruparse_evening_and_dst():
    now = at(2026, 10, 24, 12)
    p = ruparse.parse("тренировка в воскресенье в 7 вечера", now)
    assert p.due == date(2026, 10, 25) and p.at == time(19)
    # 25.10 часы переводятся: 19:00 по Мадриду это 18:00 UTC
    assert p.remind_at == datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def test_quiet_hours():
    assert tu.is_quiet(at(2026, 10, 21, 2))
    assert not tu.is_quiet(at(2026, 10, 21, 9))
    assert tu.is_quiet(at(2026, 10, 21, 1))
    assert not tu.is_quiet(at(2026, 10, 21, 0, 59))


class FakeCollector(Collector):
    kind = "fake"

    def collect(self):
        yield Event("a", "card", tu.now(), title="one", meta={"v": 1})
        yield Event("b", "card", tu.now(), title="two")
        self.state = {"cursor": tu.now().isoformat()}


def test_collector_idempotent(admin):
    admin.execute("INSERT INTO work.sources (id, context_id, kind, enabled) VALUES ('fake:1','personal','fake',true)")
    r1 = run_source("fake:1", FakeCollector(admin.execute("SELECT * FROM work.sources WHERE id='fake:1'").fetchone()))
    r2 = run_source("fake:1", FakeCollector(admin.execute("SELECT * FROM work.sources WHERE id='fake:1'").fetchone()))
    assert r1["inserted"] == 2 and r2["inserted"] == 0
    assert admin.execute("SELECT count(*) AS n FROM work.events WHERE source_id='fake:1'").fetchone()["n"] == 2
    assert admin.execute("SELECT last_ok_at FROM work.sources WHERE id='fake:1'").fetchone()["last_ok_at"]
    admin.execute("DELETE FROM work.events WHERE source_id='fake:1'")
    admin.execute("DELETE FROM work.sources WHERE id='fake:1'")


def test_bot_role_cannot_touch_payload(admin, as_role):
    admin.execute("INSERT INTO work.actions (context_id, type, payload) VALUES ('personal','plan_batch','{\"a\":1}')")
    bot = as_role("workos_bot")
    bot.execute("UPDATE work.actions SET status = 'approved'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        bot.execute("UPDATE work.actions SET payload = '{}'")


def test_reader_cannot_approve_exec_cannot_read_events(as_role):
    reader = as_role("workos_reader")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.execute("UPDATE work.actions SET status = 'approved'")
    ex = as_role("workos_exec")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ex.execute("SELECT * FROM work.events")
    ex.execute("SELECT * FROM work.calendar_feed")      # встречи без тел видит
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ex.execute("UPDATE work.actions SET approved_hash = 'x'")


def test_app_sees_only_tasks(as_role):
    app = as_role("workos_app")
    app.execute("SELECT * FROM work.tasks")
    for t in ("work.events", "work.actions", "work.outbox"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(f"SELECT 1 FROM {t}")


def test_rpg_award_cap_and_idempotency(admin, as_role):
    admin.execute("INSERT INTO rpg.cells (id, parent, title, kind) VALUES ('C', NULL, 'Квест', 'quest'), "
                  "('A', 'C', 'Здоровье', 'attribute'), ('A1', 'A', 'Сон', 'habit')")
    ids = [admin.execute("INSERT INTO work.tasks (title, size, source, cell_id, done_at) "
                         "VALUES (%s, 'L', 'app', 'A', now()) RETURNING id", (f"t{i}",)).fetchone()["id"]
           for i in range(3)]
    app = as_role("workos_app")
    got = [app.execute("SELECT rpg.award_task(%s) AS n", (i,)).fetchone()["n"] for i in ids]
    assert got == [60, 40, 0]                       # потолок 100 в день
    assert app.execute("SELECT rpg.award_task(%s) AS n", (ids[0],)).fetchone()["n"] == 0
    lvl = app.execute("SELECT level, xp FROM rpg.attributes WHERE id='A'").fetchone()
    assert lvl["xp"] == 100 and lvl["level"] == 1
    # привычка: отметка только раз в день
    admin.execute("UPDATE rpg.cells SET kind='habit' WHERE id='A1'")
    admin.execute("DELETE FROM rpg.xp")
    assert app.execute("SELECT rpg.log_manual('A1') AS n").fetchone()["n"] == 10
    assert app.execute("SELECT rpg.log_manual('A1') AS n").fetchone()["n"] == 0
    app.execute("SELECT rpg.revoke_manual('A1')")
    assert admin.execute("SELECT count(*) AS n FROM rpg.xp").fetchone()["n"] == 0


def test_reader_cannot_insert_approved_action(as_role, admin):
    reader = as_role("workos_reader")
    reader.execute("""INSERT INTO work.actions (context_id, type, payload, status, approved_hash, approved_at)
                      VALUES ('personal', 'slack_post', '{"channel":"C1","text":"x"}', 'approved',
                              encode(sha256(convert_to('{"text": "x", "channel": "C1"}', 'UTF8')), 'hex'), now())""")
    row = admin.execute("SELECT status, approved_hash, approved_at FROM work.actions").fetchone()
    assert row == {"status": "proposed", "approved_hash": None, "approved_at": None}

"""Агенты и извлечение: автономия, решения, триаж, драфтер, бриф, разбор, аудит, отчёт.

Фикстуры вставляются админом, код работает под workos_reader (role_env), чтобы
доказать, что у читателя хватает прав.
"""
import itertools
import json
import re
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest

from agents import audit, autonomy, brief, drafter, reports, review
from core import llm, timeutil as tu
from extract import decisions, triage

_ids = itertools.count(1)
LONG_DASH = re.compile("[—–]")


def ev(admin, source, kind, occurred_at, title=None, body=None, meta=None, context="infatica",
       thread_key=None, actor_raw=None):
    return admin.execute(
        "INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, body, meta, "
        "thread_key, actor_raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
        (context, source, f"x{next(_ids)}", kind, occurred_at, title, body, json.dumps(meta or {}),
         thread_key, actor_raw)).fetchone()["id"]


def person(admin, name, email=None, context=None):
    pid = admin.execute("INSERT INTO work.people (display_name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    if email:
        admin.execute("INSERT INTO work.identities (person_id, kind, value, context_id) VALUES (%s, 'email', %s, %s)",
                      (pid, email, context))
    return pid


def outbox(admin, topic=None):
    q = "SELECT * FROM work.outbox" + (" WHERE topic = %s" if topic else "") + " ORDER BY id"
    return admin.execute(q, (topic,) if topic else ()).fetchall()


@pytest.fixture
def fake():
    calls, handlers = [], {}

    def fn(task, system, user):
        calls.append(SimpleNamespace(task=task, system=system, user=user))
        return handlers[task](system, user)
    llm.set_fake(fn)
    yield SimpleNamespace(calls=calls, on=handlers)
    llm.set_fake(None)


@pytest.fixture
def reader(role_env):
    role_env("workos_reader")


# ---------- 6.2 propose ----------

def test_propose_inserts_proposal_only(admin, as_role):
    conn = as_role("workos_reader")
    a1 = autonomy.propose(conn, "task_add", "personal", {"title": "a", "size": "S"}, "почему")
    row = admin.execute("SELECT * FROM work.actions WHERE id = %s", (a1,)).fetchone()
    assert row["status"] == "proposed" and row["autonomy"] == "approval" and row["rationale"] == "почему"

    # дедуп: тот же (type, source_event, payload) второй раз не заводится
    assert autonomy.propose(conn, "task_add", "personal", {"title": "a", "size": "S"}) == a1
    assert admin.execute("SELECT count(*) AS n FROM work.actions").fetchone()["n"] == 1

    # режим решает бот: даже при auto-строке предложение остаётся «на подтверждение»,
    # семплинг читатель не трогает
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('task_add', 'personal', 'auto')")
    for i in range(3):
        aid = autonomy.propose(conn, "task_add", "personal", {"title": f"t{i}", "size": "S"})
        assert admin.execute("SELECT autonomy FROM work.actions WHERE id = %s", (aid,)).fetchone()["autonomy"] == "approval"
    assert admin.execute("SELECT auto_count FROM work.autonomy WHERE action_type = 'task_add'").fetchone()[
        "auto_count"] == 0

    # без контекста: personal
    aid = autonomy.propose(conn, "plan_batch", None, {"for_date": "2026-11-02", "items": [], "week_items": []})
    assert admin.execute("SELECT context_id FROM work.actions WHERE id = %s", (aid,)).fetchone()["context_id"] == "personal"


# ---------- 6.2 продвижение и деградация ----------

def _outcomes(admin, atype, ctx, verdicts, days_ago=1):
    aid = admin.execute("INSERT INTO work.actions (context_id, type, payload) VALUES (%s, %s, '{}') RETURNING id",
                        (ctx, atype)).fetchone()["id"]
    for v in verdicts:
        admin.execute("INSERT INTO work.outcomes (action_id, action_type, context_id, verdict, decided_at) "
                      "VALUES (%s, %s, %s, %s, now() - make_interval(days => %s))", (aid, atype, ctx, v, days_ago))


def test_autonomy_promote_and_demote(admin, reader):
    _outcomes(admin, "plan_batch", "personal", ["accepted"] * 19 + ["edited"])           # 0.95, n=20
    _outcomes(admin, "plan_batch", "consulting", ["accepted"] * 40 + ["rejected"] * 7)   # 0.851 > 0.85 без округления
    _outcomes(admin, "plan_batch", "infatica", ["accepted"] * 17 + ["rejected"] * 3)     # ровно 0.85: не выше
    _outcomes(admin, "plan_batch", "alfa", ["accepted"] * 25, days_ago=40)               # вне окна 30 дней
    _outcomes(admin, "task_add", "personal", ["accepted"] * 18)                          # n<20, наследует '*' auto
    _outcomes(admin, "task_add", "consulting", ["accepted"] + ["rejected"] * 3)          # '*' auto, 0.25 -> вниз
    _outcomes(admin, "task_add", "infatica", ["accepted"] * 25)                          # заперто
    _outcomes(admin, "trello_card", "infatica", ["accepted"] * 5 + ["rejected"] * 5)     # 0.5, auto -> вниз
    _outcomes(admin, "trello_card", "personal", ["accepted"] * 30)                       # внешний: вверх никогда
    _outcomes(admin, "email_reply", "infatica", ["accepted"] * 30)                       # никогда
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('trello_card', 'infatica', 'auto'), "
                  "('task_add', '*', 'auto'), ('calendar_block', '*', 'auto')")
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode, locked) "
                  "VALUES ('task_add', 'infatica', 'approval', true)")

    dry = autonomy.run(dry_run=True)
    assert dry["changes"] and admin.execute("SELECT count(*) AS n FROM work.autonomy").fetchone()["n"] == 4
    res = autonomy.run()
    modes = {(r["action_type"], r["context_id"]): r for r in admin.execute("SELECT * FROM work.autonomy")}
    assert modes[("plan_batch", "personal")]["mode"] == "auto"
    assert "19 из 20" in modes[("plan_batch", "personal")]["reason"]
    assert modes[("plan_batch", "consulting")]["mode"] == "auto"
    assert modes[("plan_batch", "infatica")]["mode"] == "approval"
    assert ("plan_batch", "alfa") not in modes
    assert modes[("task_add", "personal")]["mode"] == "auto"            # своя строка, режим от '*'
    assert modes[("task_add", "consulting")]["mode"] == "approval"
    assert modes[("task_add", "infatica")]["mode"] == "approval" and modes[("task_add", "infatica")]["locked"]
    assert modes[("trello_card", "infatica")]["mode"] == "approval"
    assert "ниже порога" in modes[("trello_card", "infatica")]["reason"]
    assert modes[("trello_card", "personal")]["mode"] == "approval"
    assert modes[("email_reply", "infatica")]["mode"] == "approval"
    assert modes[("calendar_block", "*")]["mode"] == "approval"         # внешний тип в auto: вниз без статистики
    expected = {("plan_batch", "personal", "auto"), ("plan_batch", "consulting", "auto"),
                ("task_add", "consulting", "approval"), ("trello_card", "infatica", "approval"),
                ("calendar_block", "*", "approval")}
    assert {(c["type"], c["context"], c["mode"]) for c in res["changes"]} == expected
    assert {(c["type"], c["context"], c["mode"]) for c in dry["changes"]} == expected
    msgs = outbox(admin, "system")
    assert len(msgs) == 5 and not any(LONG_DASH.search(m["text"]) for m in msgs)
    assert any("plan_batch" in m["text"] and "без подтверждения" in m["text"] for m in msgs)

    # повторный прогон ничего не меняет и не шумит
    assert autonomy.run()["changes"] == []
    assert len(outbox(admin, "system")) == 5

    # общий рубильник /manual: разбор статистики режимы не двигает
    admin.execute("UPDATE work.autonomy SET mode = 'approval' WHERE action_type = 'plan_batch'")
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode, locked) VALUES ('*', '*', 'approval', true)")
    assert autonomy.run()["changes"] == []
    assert admin.execute("SELECT mode FROM work.autonomy WHERE action_type = 'plan_batch' "
                         "AND context_id = 'personal'").fetchone()["mode"] == "approval"


# ---------- 4.1 извлечение ----------

def test_extract_meeting(admin, reader, fake, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    yan = person(admin, "Ян Иванов", "yan@infatica.io", "infatica")
    sasha_p = person(admin, "Саша Петров", "sasha.p@infatica.io")
    sasha_s = person(admin, "Саша Сидоров")
    start = tu.at_local(date(2026, 11, 2), time(10))
    cal = ev(admin, "ical:infatica", "calendar", start, "SDK weekly",
             meta={"attendees": [{"email": "yan@infatica.io", "name": "Yan"},
                                 {"email": "sasha.p@infatica.io", "name": "Sasha"},
                                 {"email": "george@me.io", "name": "George"},
                                 {"email": "guest@partner.com", "name": "Guest"}]})
    mt = ev(admin, "plaud", "meeting", start, "SDK weekly", body="Ян: ... Георгий: пришлю методологию к пятнице.",
            meta={"participant_ids": [sasha_s], "calendar_event_id": cal})
    ev(admin, "plaud", "meeting", start, "пустая", body=None)        # транскрипт ещё не дозрел

    def extract(system, user):
        for name in ("Ян Иванов", "Саша Петров", "Саша Сидоров", "guest@partner.com"):
            assert name in user
        assert "george@me.io" not in user
        return json.dumps({
            "decisions": [{"statement": "Релизим SDK 2.0 — в ноябре", "rationale": "партнёры ждут"}],
            "tasks": [{"title": "Собрать дашборд по SDK", "owner": "me", "due": None, "size": "M"},
                      {"title": "Проверить логи прокси", "owner": "Саша Петров", "due": "2026-11-05"}],
            "commitments": [
                {"direction": "mine", "person": "Ян", "what": "методология часов поддержки", "due": "2026-11-06",
                 "size": "M", "summary": "Обещал Яну методологию до пятницы"},
                {"direction": "theirs", "person": "Саша", "what": "выгрузка обращений", "due": None}]})
    fake.on["extract"] = extract

    res = decisions.run(date="2026-11-02")
    assert res["processed"] == 1 and res["proposed"] == 1 and not res["errors"]

    d = admin.execute("SELECT * FROM work.decisions").fetchone()
    assert d["statement"] == "Релизим SDK 2.0, в ноябре" and d["rationale"] == "партнёры ждут"
    assert set(d["participants"]) == {yan, sasha_p, sasha_s} and d["source_event"] == mt

    com = {c["what"]: c for c in admin.execute("SELECT * FROM work.commitments")}
    mine = com["методология часов поддержки"]
    assert mine["direction"] == "mine" and mine["person_id"] == yan and mine["due"] == date(2026, 11, 6)
    # «Саша» без фамилии при двух Сашах: не угадываем, имя остаётся в тексте
    assert com["Саша: выгрузка обращений"]["person_id"] is None
    assert com["Саша: выгрузка обращений"]["direction"] == "theirs"
    assert com["Проверить логи прокси"]["person_id"] == sasha_p

    act = admin.execute("SELECT * FROM work.actions").fetchall()
    assert len(act) == 1 and act[0]["type"] == "task_add" and act[0]["status"] == "proposed"
    p = act[0]["payload"]
    assert p == {"title": "Методология часов поддержки", "due": "2026-11-05", "size": "M",
                 "context_id": "infatica", "commitment_id": mine["id"]}
    assert act[0]["rationale"].startswith("Обещал Яну методологию до пятницы")
    assert act[0]["source_event"] == mt

    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (mt,)).fetchone()["meta"]
    assert meta["extract"]["tasks"] == [{"title": "Собрать дашборд по SDK", "size": "M"}]

    assert decisions.run(date="2026-11-02")["processed"] == 0
    assert len(fake.calls) == 1


def test_resolve_person_longest_form_wins():
    parts = [{"person_id": 1, "name": "Саша Петров", "aliases": []},
             {"person_id": 2, "name": "Саша Сидоров", "aliases": ["Alex S"]},
             {"person_id": 3, "name": "Ян Иванов", "aliases": ["Ian"]}]
    assert decisions.resolve_person("Саша Петров", parts) == 1
    assert decisions.resolve_person("Саша", parts) is None
    assert decisions.resolve_person("Ian", parts) == 3
    assert decisions.resolve_person("Пётр", parts) is None
    assert decisions.task_day(date(2026, 11, 9), date(2026, 11, 2)) == date(2026, 11, 6)   # пн -> пт
    assert decisions.task_day(date(2026, 11, 2), date(2026, 11, 2)) == date(2026, 11, 2)


# ---------- 4.2 триаж ----------

def test_triage_batches_and_writes_meta(admin, reader, fake, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    base = tu.now() - timedelta(hours=3)
    ids = [ev(admin, "eas:alfa" if i % 2 else "gmail:personal", "email", base + timedelta(minutes=i),
              f"Тема {i}", f"Письмо {i}", context="alfa" if i % 2 else "personal",
              meta={"from": f"Коллега <c{i}@bank.ru>",
                    "attachments": [{"name": "a.pdf", "content": "СЕКРЕТНОЕ-ВЛОЖЕНИЕ"}]})
           for i in range(25)]
    late = base + timedelta(hours=1)          # в конец очереди: порядок пачки детерминирован
    mine = ev(admin, "gmail:personal", "email", late, "Моё", "я написал", context="personal",
              meta={"from": "George <george@me.io>"})
    chatter = ev(admin, "slack:infatica", "message", late, None, "всем привет",
                 meta={"channel": "C1", "channel_type": "channel", "mention": False})
    ping = ev(admin, "slack:infatica", "message", late, None, "<@U1> глянь PR",
              meta={"channel": "C1", "channel_type": "channel", "mention": True, "ts": "1.1"})

    def tri(system, user):
        assert "СЕКРЕТНОЕ-ВЛОЖЕНИЕ" not in user
        n = len(re.findall(r"### id=", user))
        assert n <= triage.BATCH
        return json.dumps({"items": [{"id": i, "needs_reply": i % 3 == 0, "urgency": "high" if i == 0 else "weird",
                                      "deadline": "2026-11-03" if i == 1 else "не знаю", "thread": "SDK",
                                      "summary": "просят ответ — срочно"} for i in range(n)]})
    fake.on["triage"] = tri

    res = triage.run()
    assert len(fake.calls) == 2 and res["triaged"] == 26 and res["skipped"] == 2
    rows = {r["id"]: r["meta"]["triage"] for r in
            admin.execute("SELECT id, meta FROM work.events WHERE kind IN ('email','message')")}
    assert all(rows.values())
    assert rows[mine]["skipped"] == "mine" and rows[chatter]["skipped"] == "not_addressed"
    assert "skipped" not in rows[ping]
    t0 = rows[ids[0]]
    assert t0["needs_reply"] is True and t0["urgency"] == "high" and t0["summary"] == "просят ответ, срочно"
    assert rows[ids[1]]["urgency"] == "normal" and rows[ids[1]]["deadline"] == "2026-11-03"
    assert rows[ids[2]]["deadline"] is None
    assert triage.run()["seen"] == 0


# ---------- 5.x драфтер ----------

def test_drafter_email_slack_and_trello(admin, reader, fake, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    monkeypatch.setenv("TRELLO_PROPOSE_LIST_ID", "LIST1")
    (tmp_path / "infatica.md").write_text("МАРКЕР-СТИЛЯ-ИНФАТИКИ", encoding="utf-8")
    monkeypatch.setattr(drafter, "STYLE_DIR", tmp_path)
    t = tu.now() - timedelta(hours=5)
    for i, body in enumerate(["ПЕРВОЕ-СТАРОЕ", "второе", "третье"]):
        ev(admin, "gmail:infatica", "email", t + timedelta(minutes=i), "SDK rollout", body, thread_key="th1",
           meta={"from": "Yan <yan@infatica.io>" if i != 1 else "George <george@me.io>"})
    target = ev(admin, "gmail:infatica", "email", t + timedelta(minutes=10), "SDK rollout", "Когда релиз?",
                thread_key="th1",
                meta={"from": "Yan <yan@infatica.io>", "to": ["george@me.io", "Bob <bob@infatica.io>"],
                      "cc": ["yan@infatica.io", "pm@infatica.io"], "message_id": "<m4@x>",
                      "references": "<m1@x>", "thread_id": "gth1", "account": "george@infatica.io",
                      "triage": {"needs_reply": True, "urgency": "high", "summary": "спрашивает про релиз",
                                 "deadline": "2026-11-03"}})
    slack = ev(admin, "slack:infatica", "message", t, None, "<@U1> апрувнешь?", actor_raw="Bob",
               meta={"channel": "C9", "ts": "171.5", "mention": True,
                     "triage": {"needs_reply": True, "urgency": "normal", "summary": "просит апрув"}})
    ev(admin, "gmail:infatica", "email", t, "FYI", "просто к сведению",
       meta={"from": "x@y.z", "triage": {"needs_reply": False}})
    mt = ev(admin, "plaud", "meeting", t, "SDK weekly", "транскрипт",
            meta={"extract": {"tasks": [{"title": "Собрать дашборд по SDK", "size": "M"}]}})

    def draft(system, user):
        if "Trello" in system:
            assert "Собрать дашборд по SDK" in user and "P1-high" in user
            return json.dumps({"cards": [{"name": "Build SDK adoption dashboard", "desc": "**Context** — x",
                                          "labels": ["P1-high", "task", "sdk", "made-up"],
                                          "checklist": {"name": "Чеклист", "items": ["Collect events", ""]}}]})
        if "на письмо" in system:
            assert "МАРКЕР-СТИЛЯ-ИНФАТИКИ" in system
            assert "ПЕРВОЕ-СТАРОЕ" not in user and "второе" in user and "Когда релиз?" in user
            return json.dumps({"body": "Привет — релиз в четверг, 5–6 ноября", "skip": False})
        assert "МАРКЕР-СТИЛЯ-ИНФАТИКИ" in system
        return json.dumps({"body": "Да, апрувну после обеда"})
    fake.on["drafter"] = draft

    res = drafter.run()
    assert res["replies"]["proposed"] == 2 and res["trello"]["cards"] == 1
    acts = {a["type"]: a for a in admin.execute("SELECT * FROM work.actions")}
    em = acts["email_reply"]
    assert em["source_event"] == target and em["autonomy"] == "approval"
    assert em["payload"] == {"account": "george@infatica.io", "to": ["yan@infatica.io"],
                             "cc": ["bob@infatica.io", "pm@infatica.io"], "subject": "Re: SDK rollout",
                             "body": "Привет, релиз в четверг, 5-6 ноября", "thread_id": "gth1",
                             "in_reply_to": "<m4@x>", "references": "<m1@x> <m4@x>"}
    assert "срочно" in em["rationale"] and not LONG_DASH.search(em["rationale"])
    sp = acts["slack_post"]
    assert sp["source_event"] == slack and sp["payload"] == {"channel": "C9", "text": "Да, апрувну после обеда",
                                                             "thread_ts": "171.5"}
    tc = acts["trello_card"]
    assert tc["source_event"] == mt and tc["payload"] == {
        "list_id": "LIST1", "name": "Build SDK adoption dashboard", "desc": "**Context**, x",
        "labels": ["P1-high", "task", "sdk"], "checklist": {"name": "Чеклист", "items": ["Collect events"]}}
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (target,)).fetchone()["meta"]
    assert meta["draft"]["style"] == "infatica.md" and meta["draft"]["action_id"] == em["id"]

    n = len(fake.calls)
    again = drafter.run()
    assert again["replies"]["candidates"] == 0 and again["trello"]["meetings"] == 0 and len(fake.calls) == n
    assert admin.execute("SELECT count(*) AS n FROM work.actions").fetchone()["n"] == 3


def test_drafter_skips_answered_thread(admin, reader, fake, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    monkeypatch.delenv("TRELLO_PROPOSE_LIST_ID", raising=False)
    t = tu.now() - timedelta(hours=2)
    q = ev(admin, "gmail:personal", "email", t, "Вопрос", "?", context="personal", thread_key="th2",
           meta={"from": "a@b.c", "triage": {"needs_reply": True}})
    ev(admin, "gmail:personal", "email", t + timedelta(minutes=5), "Re: Вопрос", "ответил", context="personal",
       thread_key="th2", meta={"from": "george@me.io"})
    ev(admin, "achat:alfa", "message", t, None, "Георгий, посмотрите?", context="alfa",
       meta={"triage": {"needs_reply": True}})
    res = drafter.run()
    assert res["replies"]["skipped"] == 2 and not fake.calls
    assert res["trello"].get("disabled")
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (q,)).fetchone()["meta"]
    assert meta["draft"]["skipped"] == "answered"


# ---------- 3.5 бриф и карточки ----------

def test_brief_composes_and_dedups(admin, reader):
    today = tu.logical_today()
    y = today - timedelta(days=1)
    ys, _ = tu.day_bounds(y)
    ts, _ = tu.day_bounds(today)

    def task(title, due, created, done=None, size="S"):
        admin.execute("INSERT INTO work.tasks (title, due, created_at, done_at, size, source) "
                      "VALUES (%s, %s, %s, %s, %s, 'app')", (title, due, created, done, size))
    task("План сделан", y, ys - timedelta(days=1), ys + timedelta(hours=3))
    task("План не сделан", y, ys - timedelta(days=1))
    task("Вне плана", None, ys + timedelta(hours=1), ys + timedelta(hours=2))
    task("Задача на сегодня", today, ys, size="M")
    yan = person(admin, "Ян Иванов")
    admin.execute("INSERT INTO work.commitments (context_id, direction, person_id, what, due) VALUES "
                  "('infatica', 'mine', %s, 'методология', %s), ('infatica', 'theirs', %s, 'выгрузка', %s), "
                  "('infatica', 'mine', %s, 'не горит', %s)",
                  (yan, today, yan, today - timedelta(days=2), yan, today + timedelta(days=5)))
    ev(admin, "ical:infatica", "calendar", ts + timedelta(hours=6), "Standup",
       meta={"ends_at": (ts + timedelta(hours=6, minutes=15)).isoformat()})
    ev(admin, "ical:personal", "calendar", ts + timedelta(hours=8), "Отменённая", context="personal",
       meta={"status": "cancelled"})
    admin.execute("UPDATE work.sources SET enabled = true, last_ok_at = now() - interval '10 minutes' "
                  "WHERE id = 'trello:george'")
    admin.execute("UPDATE work.sources SET enabled = true, last_ok_at = now() - interval '3 hours' WHERE id = 'plaud'")
    try:
        res = brief.run_brief(date=today.isoformat())
        assert res["sent"]
        msgs = outbox(admin, "plan")
        assert len(msgs) == 1
        text = msgs[0]["text"]
        assert "в плане 2, из них сделано 1" in text and "вне плана 1" in text and "План не сделан" in text
        assert "Standup" in text and "Отменённая" not in text
        assert "Задача на сегодня (M)" in text
        assert "Ян Иванов: методология · сегодня" in text and "выгрузка · просрочено" in text
        assert "не горит" not in text
        assert "trello:george" in text and "⚠ plaud" in text and "⚠ trello" not in text
        assert not LONG_DASH.search(text)
        assert brief.run_brief(date=today.isoformat())["skipped"] == "already_sent"
        assert len(outbox(admin, "plan")) == 1
    finally:
        admin.execute("UPDATE work.sources SET enabled = false WHERE id IN ('trello:george', 'plaud')")


def test_meeting_card_once_in_window(admin, reader, fake, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    now = tu.now()
    yan = person(admin, "Ян Иванов", "yan@infatica.io")
    other = person(admin, "Пётр")
    soon = ev(admin, "ical:infatica", "calendar", now + timedelta(minutes=30), "SDK sync",
              meta={"ends_at": (now + timedelta(minutes=60)).isoformat(), "location": "Zoom",
                    "attendees": [{"email": "yan@infatica.io"}, {"email": "george@me.io"},
                                  {"email": "new@partner.com", "name": "Новичок"}]})
    ev(admin, "ical:infatica", "calendar", now + timedelta(minutes=70), "Позже", meta={})
    ev(admin, "ical:infatica", "calendar", now + timedelta(minutes=31), "Весь день", meta={"all_day": True})
    prev = ev(admin, "plaud", "meeting", now - timedelta(days=7), "SDK sync прошлый", "длинный транскрипт",
              meta={"participant_ids": [yan, other]})
    ev(admin, "plaud", "meeting", now - timedelta(days=30), "Совсем старая", "x", meta={"participant_ids": [yan]})
    admin.execute("INSERT INTO work.commitments (context_id, direction, person_id, what, due) VALUES "
                  "('infatica', 'mine', %s, 'методология', %s), ('infatica', 'theirs', %s, 'логи', NULL), "
                  "('infatica', 'mine', %s, 'чужое', NULL)",
                  (yan, tu.logical_date(now) - timedelta(days=1), yan, other))
    admin.execute("INSERT INTO work.decisions (context_id, decided_at, statement, participants, source_event) "
                  "VALUES ('infatica', now() - interval '7 days', 'SDK 2.0 в ноябре', %s::jsonb, %s), "
                  "('infatica', now() - interval '60 days', 'Старое решение', '[]', NULL)",
                  (json.dumps([yan]), prev))
    fake.on["meeting_card"] = lambda s, u: "Договорились о метриках — SDK.\nОткрыт вопрос по SLA.\nТретья.\nЛишняя."

    res = brief.run_meeting_cards(now=now)
    assert len(res["cards"]) == 1 and res["cards"][0]["event"] == soon
    msgs = outbox(admin, "plan")
    assert len(msgs) == 1
    text = msgs[0]["text"]
    assert "Через 30 мин: SDK sync" in text and "Zoom" in text
    assert "Ян Иванов" in text and "Новичок" in text and "george@me.io" not in text
    assert "SDK sync прошлый" in text and "Договорились о метриках, SDK." in text and "Лишняя" not in text
    assert "<b>Ты обещал</b>\n• Ян Иванов: методология" in text and "(просрочено)" in text
    assert "<b>Тебе обещали</b>\n• Ян Иванов: логи" in text and "чужое" not in text
    assert "SDK 2.0 в ноябре" in text and "Старое решение" not in text
    assert "транскрипт" in fake.calls[0].user
    assert brief.run_meeting_cards(now=now + timedelta(minutes=2))["cards"] == []
    assert len(outbox(admin, "plan")) == 1
    # вне окна 25-35 минут карточки нет
    assert brief.run_meeting_cards(now=now - timedelta(minutes=20))["cards"] == []


# ---------- 6.1 разбор и 8.1 аудит ----------

def test_review_sends_pattern(admin, reader, fake):
    aid = admin.execute("INSERT INTO work.actions (context_id, type, payload, rationale) VALUES "
                        "('infatica', 'email_reply', '{}', 'Ответ Яну') RETURNING id").fetchone()["id"]
    for verdict, diff in (("accepted", None), ("edited", "- Добрый день, коллеги!\n+ Привет"),
                          ("edited", "- длинный абзац\n+ коротко"), ("rejected", None)):
        admin.execute("INSERT INTO work.outcomes (action_id, action_type, context_id, verdict, diff) "
                      "VALUES (%s, 'email_reply', 'infatica', %s, %s)", (aid, verdict, diff))

    def rv(system, user):
        assert "какая закономерность" in system and "Добрый день, коллеги" in user and "Ответ Яну" in user
        return "Ты режешь приветствия — пиши короче."
    fake.on["review"] = rv
    res = review.run()
    assert res["sent"] and res["edits"] == 2
    text = outbox(admin, "system")[0]["text"]
    assert "email_reply · infatica: n=4, принято 25%" in text and "поправлено 2" in text
    assert "&lt;" not in text or "<pre>" in text
    assert "Ты режешь приветствия, пиши короче." in text and not LONG_DASH.search(text)
    assert review.run()["skipped"] == "already_sent"


def test_audit_idle_meetings_and_silent_channels(admin, reader, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_SLACK_IDS", "U_ME")
    today = date(2026, 10, 25)                                     # воскресенье
    now = tu.at_local(today, time(19, 30))
    monday = tu.week_monday(today)
    for w in range(6):
        d = monday - timedelta(weeks=w) + timedelta(days=1)        # вторники
        s = tu.at_local(d, time(11))
        ev(admin, "ical:infatica", "calendar", s, "Weekly sync", meta={"ends_at": (s + timedelta(hours=1)).isoformat()})
        s2 = tu.at_local(d, time(15))
        cal2 = ev(admin, "ical:infatica", "calendar", s2, "Planning",
                  meta={"ends_at": (s2 + timedelta(hours=1)).isoformat()})
        if w == 2:   # одна из шести «Planning» дала обещание через запись Plaud
            rec = ev(admin, "plaud", "meeting", s2 + timedelta(minutes=5), "rec", "...",
                     meta={"calendar_event_id": cal2})
            admin.execute("INSERT INTO work.commitments (context_id, direction, what, source_event) "
                          "VALUES ('infatica', 'theirs', 'логи', %s)", (rec,))
        if w < 5:    # «Retro» только 5 недель из 6
            ev(admin, "ical:infatica", "calendar", tu.at_local(d, time(17)), "Retro", meta={})
    for i in range(3):
        ev(admin, "slack:infatica", "message", now - timedelta(days=i + 1), None, "msg",
           meta={"channel": "C_RANDOM", "channel_name": "random", "user": "U_OTHER"})
        ev(admin, "slack:infatica", "message", now - timedelta(days=i + 1), None, "msg",
           meta={"channel": "C_SDK", "channel_name": "sdk", "user": "U_OTHER" if i else "U_ME"})
    ev(admin, "slack:infatica", "message", now - timedelta(days=1), None, "dm",
       meta={"channel": "D1", "channel_type": "im", "user": "U_OTHER"})

    res = audit.run(date=today.isoformat(), now=now)
    assert res == {"sent": True, "idle_meetings": 1, "silent_channels": 1}
    text = outbox(admin, "system")[0]["text"]
    assert "«Weekly sync»" in text and "6 раз" in text and "Planning" not in text and "Retro" not in text
    assert "#random" in text and "#sdk" not in text and "D1" not in text
    assert not LONG_DASH.search(text)
    assert audit.run(date=today.isoformat(), now=now)["skipped"] == "already_sent"


# ---------- 8.2 отчёт ----------

def test_sprint_report_from_db(admin, reader, capsys):
    yan = person(admin, "Ян Иванов")
    mt = ev(admin, "plaud", "meeting", tu.now() - timedelta(days=3), "SDK weekly", "x")
    admin.execute("INSERT INTO work.decisions (context_id, decided_at, statement, rationale, participants, "
                  "source_event) VALUES ('infatica', now() - interval '3 days', 'Релиз SDK 2.0 в ноябре', "
                  "'партнёры ждут', %s::jsonb, %s)", (json.dumps([yan]), mt))
    admin.execute("INSERT INTO work.commitments (context_id, direction, person_id, what, due, status, closed_at) "
                  "VALUES ('infatica', 'theirs', %s, 'выгрузка логов', current_date - 3, 'open', NULL), "
                  "('infatica', 'mine', %s, 'методология', NULL, 'done', now() - interval '1 day')", (yan, yan))
    admin.execute("INSERT INTO work.actions (context_id, type, payload, status, executed_at) VALUES "
                  "('infatica', 'trello_card', '{\"name\": \"Build SDK dashboard\"}', 'executed', now())")
    res = reports.run(kind="sprint")
    out = capsys.readouterr().out
    assert res["decisions"] == 1 and res["executed_actions"] == 1 and "text" not in res
    for s in ("Релиз SDK 2.0 в ноябре", "Почему: партнёры ждут", "Участники: Ян Иванов", "«SDK weekly»",
              "Ян Иванов: выгрузка логов", "(просрочено)", "методология (сделано)", "Build SDK dashboard"):
        assert s in out, s
    quiet = reports.run(kind="week", quiet=True, context="infatica")
    assert "Релиз SDK 2.0 в ноябре" in quiet["text"] and not capsys.readouterr().out
    assert not LONG_DASH.search(out + quiet["text"])


# ---------- ревью: голое имя, сбои модели, дубли отчётов ----------

def test_resolve_person_bare_name_never_resolves_between_two_candidates():
    """«Саша» при двух Сашах среди участников: обличье «Саша» у Петрова ничего не решает."""
    parts = [{"person_id": 1, "name": "Саша Петров", "aliases": ["Саша"]},
             {"person_id": 2, "name": "Александр Иванов", "aliases": []},
             {"person_id": 3, "name": "Ян Иванов", "aliases": []}]
    assert decisions.resolve_person("Саша", parts) is None
    assert decisions.resolve_person("Сашей", parts) is None
    assert decisions.resolve_person("Саша Петров", parts) == 1
    assert decisions.resolve_person("Петров", parts) == 1
    assert decisions.resolve_person("Сашей Ивановым", parts) == 2     # пара бьёт голое имя
    assert decisions.resolve_person("Яну", parts) == 3
    assert decisions.resolve_person("Саша Смирнов", parts) is None    # другой Саша, не наш
    assert decisions.resolve_person("Ян и Саша Петров", parts) is None  # двое в одной строке
    assert decisions.resolve_person("Георгий", parts) is None
    # один Саша среди участников: голое имя разрешается
    assert decisions.resolve_person("Саша", parts[:1] + parts[2:]) == 1


def test_extract_bare_alias_does_not_steal_commitment(admin, reader, fake):
    petrov = person(admin, "Саша Петров")
    admin.execute("INSERT INTO work.identities (person_id, kind, value, context_id, confidence) "
                  "VALUES (%s, 'display_name', 'Саша', 'infatica', 1.0)", (petrov,))
    ivanov = person(admin, "Александр Иванов")
    mt = ev(admin, "plaud", "meeting", tu.now() - timedelta(hours=2), "SDK", body="...",
            meta={"participant_ids": [petrov, ivanov]})
    fake.on["extract"] = lambda s, u: json.dumps({"decisions": [], "tasks": [], "commitments": [
        {"direction": "theirs", "person": "Саша", "what": "выгрузка обращений", "due": None}]})
    assert decisions.run()["processed"] == 1
    c = admin.execute("SELECT person_id, what FROM work.commitments WHERE source_event = %s", (mt,)).fetchone()
    assert c["person_id"] is None and c["what"] == "Саша: выгрузка обращений"


def test_extract_failures_are_capped_and_do_not_block(admin, reader, fake, monkeypatch):
    monkeypatch.setattr(decisions, "RETRY_HOURS", 0)
    bad = ev(admin, "plaud", "meeting", tu.now() - timedelta(days=2), "Сломанная", body="плохой транскрипт")

    def extract(system, user):
        if "плохой транскрипт" in user:
            raise llm.LLMError("claude -p exit 1: overloaded")
        return json.dumps({"decisions": [{"statement": "Решили"}], "tasks": [], "commitments": []})
    fake.on["extract"] = extract

    r1 = decisions.run(limit=1)
    assert r1["errors"] and not r1["gave_up"]
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (bad,)).fetchone()["meta"]
    assert meta["extract"]["attempts"] == 1 and "overloaded" in meta["extract"]["error"]
    # свежая встреча идёт раньше той, что уже падала: старая не держит очередь
    good = ev(admin, "plaud", "meeting", tu.now() - timedelta(hours=1), "Нормальная", body="хороший")
    assert decisions.run(limit=1)["processed"] == 1
    assert admin.execute("SELECT 1 FROM work.notices WHERE key = %s", (f"extract:{good}",)).fetchone()
    # пауза перед повтором: при RETRY_HOURS=1 только что упавшая встреча не берётся
    monkeypatch.setattr(decisions, "RETRY_HOURS", 1)
    assert decisions.run()["meetings"] == 0
    monkeypatch.setattr(decisions, "RETRY_HOURS", 0)
    assert decisions.run()["gave_up"] == []                         # вторая попытка
    assert decisions.run()["gave_up"] == [bad]                      # третья: сдаёмся
    sys_msgs = outbox(admin, "system")
    assert len(sys_msgs) == 1 and "«Сломанная»" in sys_msgs[0]["text"] and "3 попытки" in sys_msgs[0]["text"]
    assert f"extract:{bad}" in sys_msgs[0]["text"] and not LONG_DASH.search(sys_msgs[0]["text"])
    n = len(fake.calls)
    assert decisions.run()["meetings"] == 0 and len(fake.calls) == n
    assert len(outbox(admin, "system")) == 1


def test_drafter_failures_are_capped(admin, reader, fake, monkeypatch):
    monkeypatch.setenv("WORKOS_ME_EMAILS", "george@me.io")
    monkeypatch.setenv("TRELLO_PROPOSE_LIST_ID", "LIST1")
    monkeypatch.setattr(drafter, "RETRY_HOURS", 0)
    t = tu.now() - timedelta(hours=3)
    mail = ev(admin, "gmail:personal", "email", t, "Вопрос", "Когда?", context="personal",
              meta={"from": "a@b.c", "triage": {"needs_reply": True}})
    mt = ev(admin, "plaud", "meeting", t, "SDK weekly", "транскрипт",
            meta={"extract": {"tasks": [{"title": "Собрать дашборд", "size": "M"}]}})

    def broken(system, user):
        raise llm.LLMError("claude -p exit 1: rate limit")
    fake.on["drafter"] = broken

    for _ in range(drafter.MAX_ATTEMPTS):
        res = drafter.run()
        assert res["replies"]["errors"] == 1 and res["trello"]["errors"] == 1
    assert res["replies"]["gave_up"] == 1 and res["trello"]["gave_up"] == 1
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (mt,)).fetchone()["meta"]
    assert meta["trello_draft"]["attempts"] == drafter.MAX_ATTEMPTS
    keys = {r["key"] for r in admin.execute("SELECT key FROM work.notices")}
    assert {f"draft:{mail}", f"trello:{mt}"} <= keys
    texts = [m["text"] for m in outbox(admin, "system")]
    assert len(texts) == 2 and any("Черновик ответа" in x for x in texts) and any("Карточки Trello" in x for x in texts)
    n = len(fake.calls)
    again = drafter.run()
    assert again["replies"]["candidates"] == 0 and again["trello"]["meetings"] == 0 and len(fake.calls) == n
    assert len(outbox(admin, "system")) == 2


def test_review_and_audit_do_not_duplicate_on_concurrent_run(admin, reader, monkeypatch):
    """Второй прогон прошёл предварительную проверку, пока первый ещё писал: отчёт один."""
    today = date(2026, 10, 25)
    key_r = f"review:{tu.week_monday(today).isoformat()}"
    orig_collect = review.collect

    def racing_collect(conn, since):
        admin.execute("INSERT INTO work.notices (key) VALUES (%s)", (key_r,))   # соседний прогон успел
        return orig_collect(conn, since)
    monkeypatch.setattr(review, "collect", racing_collect)
    assert review.run(date=today.isoformat()) == {"skipped": "already_sent", "key": key_r}
    assert outbox(admin, "system") == []

    key_a = f"audit:{tu.week_monday(today).isoformat()}"

    def racing_idle(conn, day, now):
        admin.execute("INSERT INTO work.notices (key) VALUES (%s)", (key_a,))
        return []
    monkeypatch.setattr(audit, "idle_meetings", racing_idle)
    assert audit.run(date=today.isoformat(), now=tu.at_local(today, time(19, 30)))["skipped"] == "already_sent"
    assert outbox(admin, "system") == []
    # --force шлёт всегда, даже при отметке
    monkeypatch.setattr(audit, "idle_meetings", lambda conn, day, now: [])
    assert audit.run(date=today.isoformat(), now=tu.at_local(today, time(19, 30)), force=True)["sent"]
    assert len(outbox(admin, "system")) == 1


def test_triage_failures_are_capped(admin, reader, fake, monkeypatch):
    monkeypatch.setattr(triage, "RETRY_HOURS", 0)
    e = ev(admin, "gmail:personal", "email", tu.now() - timedelta(hours=1), "Тема", "текст", context="personal",
           meta={"from": "a@b.c"})

    def broken(system, user):
        raise llm.LLMError("нет JSON")
    fake.on["triage"] = broken
    for i in range(triage.MAX_ATTEMPTS):
        res = triage.run()
        assert res["errors"] == 1 and res["gave_up"] == (1 if i == triage.MAX_ATTEMPTS - 1 else 0)
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (e,)).fetchone()["meta"]
    assert meta["triage"]["skipped"] == "error" and meta["triage"]["needs_reply"] is False
    assert meta["triage_try"]["attempts"] == triage.MAX_ATTEMPTS
    msgs = outbox(admin, "system")
    assert len(msgs) == 1 and "Триаж" in msgs[0]["text"] and not LONG_DASH.search(msgs[0]["text"])
    assert triage.run()["seen"] == 0

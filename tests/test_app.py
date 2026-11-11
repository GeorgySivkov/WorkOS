import base64
import json
import threading
import time as clock
from datetime import date, time, timedelta
from urllib.parse import urlencode

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.rows import dict_row

from app import initdata, server
from core import timeutil as tu

BOT_ID = 7012345678
OWNER = 111
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def make_init(user_id=OWNER, auth_date=None, key=KEY, bot_id=BOT_ID, tamper=False, drop_sig=False):
    fields = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Георгий", "language_code": "ru"},
                           ensure_ascii=False, separators=(",", ":")),
        "auth_date": str(auth_date or int(clock.time())),
    }
    dcs = f"{bot_id}:WebAppData\n" + "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    sig = base64.urlsafe_b64encode(key.sign(dcs.encode())).rstrip(b"=").decode()
    if tamper:
        fields["user"] = fields["user"].replace("Георгий", "Гоша")
    extra = {} if drop_sig else {"signature": sig}
    return urlencode({**fields, "hash": "0" * 64, **extra})


def at(d, hh, mm=0):
    return tu.at_local(d, time(hh, mm))


# ---------- initData ----------

def test_initdata_accepts_valid():
    got = initdata.validate(make_init(), bot_id=BOT_ID, owner_id=OWNER, public_key=PUB)
    assert got["user"]["id"] == OWNER and got["user"]["first_name"] == "Георгий"


@pytest.mark.parametrize("kw,vkw", [
    ({"tamper": True}, {}),
    ({"user_id": 222}, {}),
    ({"auth_date": int(clock.time()) - 25 * 3600}, {}),
    ({"auth_date": int(clock.time()) + 3600}, {}),
    ({"key": Ed25519PrivateKey.generate()}, {}),
    ({"drop_sig": True}, {}),
    ({}, {"bot_id": BOT_ID + 1}),
])
def test_initdata_rejects(kw, vkw):
    args = {"bot_id": BOT_ID, "owner_id": OWNER, "public_key": PUB, **vkw}
    with pytest.raises(initdata.InitDataError):
        initdata.validate(make_init(**kw), **args)


def test_initdata_env_and_garbage(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PUBLIC_KEY", PUB)
    monkeypatch.setenv("TELEGRAM_BOT_ID", str(BOT_ID))
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))
    assert initdata.validate(make_init())["user"]["id"] == OWNER
    for bad in ("", "user=1&&&", "signature=%%%", "a=1&a=2"):
        with pytest.raises(initdata.InitDataError):
            initdata.validate(bad)
    monkeypatch.delenv("TELEGRAM_PUBLIC_KEY")
    assert initdata.public_key_hex() == initdata.PROD_PUBLIC_KEY
    assert initdata.public_key_hex("test") == initdata.TEST_PUBLIC_KEY
    with pytest.raises(initdata.InitDataError):           # продовый ключ, наша подпись
        initdata.validate(make_init())


# ---------- сервер в потоке ----------

@pytest.fixture(scope="module")
def srv(database):
    def connect():
        return psycopg.connect(user="workos_app", autocommit=True, row_factory=dict_row)
    api = server.Api(connect=connect, bot_id=BOT_ID, owner_id=OWNER, public_key=PUB, dev_token="")
    httpd = server.make_server("127.0.0.1", 0, api)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    api.pool.close()


@pytest.fixture
def c(srv):
    srv.api.clock = tu.now
    srv.api.dev_token = ""
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with httpx.Client(base_url=base, headers={"Authorization": "tma " + make_init()}, timeout=10) as client:
        yield client
    srv.api.clock = tu.now


def fix_clock(srv, dt):
    srv.api.clock = lambda: dt


def seed_grid(admin):
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('C', NULL, 'Главный квест', 'quest', 10, NULL),
        ('A', 'C', 'Здоровье', 'attribute', 10, NULL),
        ('B', 'C', 'Карьера', 'attribute', 10, NULL)""")
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('A1', 'A', 'Сон 7,5 ч', 'habit', 10, 'garmin:sleep:7.5'),
        ('A2', 'A', 'Растяжка', 'habit', 10, NULL),
        ('A3', 'A', 'Тренировка', 'habit', 10, 'garmin:activity'),
        ('A4', 'A', '10 км за 55 минут', 'milestone', 200, NULL),
        ('B1', 'B', 'Спокойствие', 'quality', 10, NULL)""")


def add_task(admin, title, **kw):
    cols = {"title": title, "source": "app", **kw}
    q = f"INSERT INTO work.tasks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
    return admin.execute(q, list(cols.values())).fetchone()["id"]


def add_event(admin, ext, start, end, title, ctx="infatica", **meta):
    admin.execute("""INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta)
                     VALUES (%s, %s, %s, 'calendar', %s, %s, %s)""",
                  (ctx, {"alfa": "eas:alfa"}.get(ctx, f"ical:{ctx}"), ext, start, title, json.dumps({"ends_at": end.isoformat(), **meta})))


# ---------- авторизация и статика ----------

def test_auth_and_static(c, srv):
    assert c.get("/api/health", headers={"Authorization": ""}).json() == {"ok": True}
    for hdr in ("", "tma", "tma garbage", "Bearer x", "dev x",
                "tma " + make_init(auth_date=int(clock.time()) - 90000), "tma " + make_init(user_id=5)):
        r = c.get("/api/today", headers={"Authorization": hdr})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}, hdr
    assert c.post("/api/tasks", json={"text": "x"}, headers={"Authorization": ""}).status_code == 401
    srv.api.dev_token = "s3cret"                          # короче 32 символов: не принимается
    assert c.get("/api/today", headers={"Authorization": "dev s3cret"}).status_code == 401
    long_tok = "d" * 40
    srv.api.dev_token = long_tok
    assert c.get("/api/today", headers={"Authorization": f"dev {long_tok}"}).status_code == 200
    assert c.get("/api/today", headers={"Authorization": "dev wrong"}).status_code == 401
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/tasks").status_code == 405

    r = c.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert "https://telegram.org" in r.headers["content-security-policy"]
    assert "telegram-web-app.js" in r.text
    assert c.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/static/app.css").headers["content-type"].startswith("text/css")
    assert c.get("/static/../server.py").status_code == 404
    assert c.get("/static/%2e%2e/server.py").status_code == 404
    assert c.get("/static/nope.js").status_code == 404


# ---------- задачи ----------

def test_create_task_with_russian_date(c, srv, admin):
    fix_clock(srv, at(date(2026, 10, 21), 15))          # среда
    t = c.post("/api/tasks", json={"text": "позвонить в банк в пт в 10", "bucket": "today"}).json()
    assert t["title"] == "позвонить в банк" and t["due"] == "2026-10-23" and t["time"] == "10:00"
    assert t["size"] == "S" and t["xp"] == 10 and t["done"] is False and t["planned_by"] == "manual"
    row = admin.execute("SELECT * FROM work.tasks WHERE id = %s", (t["id"],)).fetchone()
    assert row["source"] == "app" and row["remind_at"] == at(date(2026, 10, 23), 10)
    assert c.post("/api/tasks", json={"text": "купить корм", "bucket": "today"}).json()["due"] == "2026-10-21"
    wk = c.post("/api/tasks", json={"text": "собрать кейс", "bucket": "week"}).json()
    assert wk["due"] is None and wk["week_of"] == "2026-10-19"
    later = c.post("/api/tasks", json={"text": "книга про продукт"}).json()
    assert later["due"] is None and later["week_of"] is None
    assert c.post("/api/tasks", json={"text": "отчёт завтра", "bucket": "later"}).json()["due"] == "2026-10-22"
    for bad in ({"text": "  "}, {"text": "x", "bucket": "someday"}, {"text": 5}):
        assert c.post("/api/tasks", json=bad).status_code == 400
    assert c.post("/api/tasks", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400


def test_done_awards_xp_and_level_up_then_undone(c, admin):
    seed_grid(admin)
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('A2', 90, 'manual', 'seed', now() - interval '3 days')")
    tid = add_task(admin, "Отчёт по SDK", size="S", cell_id="B", due=tu.logical_today(), context_id="infatica")
    r = c.post(f"/api/tasks/{tid}/done").json()
    assert r["xp"] == 10 and r["level_up"] is True
    assert r["character"]["level"] == 1 and r["character"]["xp"] == 100 and r["character"]["name"] == "Георгий"
    assert r["task"]["done"] is True and r["task"]["cell_title"] == "Карьера"
    assert r["task"]["context_title"] == "Инфатика" and r["task"]["color"] == "#4FB6C2"
    again = c.post(f"/api/tasks/{tid}/done").json()
    assert again["xp"] == 0 and again["level_up"] is False
    u = c.post(f"/api/tasks/{tid}/undone").json()
    assert u["task"]["done"] is False and u["character"]["xp"] == 90 and u["character"]["level"] == 0
    assert admin.execute("SELECT count(*) AS n FROM rpg.xp WHERE source = 'task'").fetchone()["n"] == 0
    assert c.post("/api/tasks/999999/done").status_code == 404


def test_tomorrow_and_drop(c, admin):
    today = tu.logical_today()
    tid = add_task(admin, "Позвонить в банк", due=today, remind_at=at(today, 10), reminded_at=at(today, 10),
                   planned_by="manual", planned_start=at(today, 10), planned_end=at(today, 10, 30))
    t = c.post(f"/api/tasks/{tid}/tomorrow").json()
    tomorrow = today + timedelta(days=1)
    assert t["due"] == tomorrow.isoformat() and t["time"] == "10:00"
    row = admin.execute("SELECT * FROM work.tasks WHERE id = %s", (tid,)).fetchone()
    assert row["planned_start"] is None and row["planned_by"] is None and row["planned_at"] is not None
    assert row["remind_at"] == at(tomorrow, 10) and row["reminded_at"] is None

    other = add_task(admin, "Лишнее", due=today)
    assert c.post(f"/api/tasks/{other}/drop").json() == {"ok": True}
    assert admin.execute("SELECT dropped_at FROM work.tasks WHERE id = %s", (other,)).fetchone()["dropped_at"]
    day = c.get("/api/today").json()
    ids = [x.get("id") for x in day["timeline"] if x["type"] == "task"] + [x["id"] for x in day["unscheduled"]]
    assert other not in ids and tid not in ids
    assert c.post(f"/api/tasks/{other}/done").status_code == 404
    assert c.post(f"/api/tasks/{other}/drop").json() == {"ok": True}


def test_habit_toggle(c, admin):
    seed_grid(admin)
    r = c.post("/api/habits/A2/toggle").json()
    assert r["done"] is True and r["xp"] == 10 and r["character"]["xp"] == 10
    habits = {h["cell_id"]: h for h in c.get("/api/today").json()["habits"]}
    assert habits["A2"]["done"] is True and habits["A2"]["auto"] is False
    assert habits["A1"]["auto"] is True and habits["A1"]["note"] == "из Garmin"
    r = c.post("/api/habits/A2/toggle").json()
    assert r["done"] is False and r["xp"] == 0 and r["character"]["xp"] == 0
    assert c.post("/api/habits/A1/toggle").status_code == 400      # из Garmin руками не отмечается
    assert c.post("/api/habits/A4/toggle").status_code == 404      # этап не привычка
    assert c.post("/api/habits/Z9/toggle").status_code == 404


def test_rating_slider(c, admin, as_role):
    seed_grid(admin)
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('A5', 'A', 'Оценка дня', 'habit', 10, 'rating:day')""")
    day = c.get("/api/today").json()
    rt = day["rating"]
    assert rt["score"] is None and rt["month_days"] == 0 and rt["month_avg"] is None
    assert rt["day"] == admin.execute("SELECT rpg.lday(now()) AS d").fetchone()["d"].isoformat()
    habits = {h["cell_id"]: h for h in day["habits"]}
    assert habits["A5"]["via"] == "rating" and habits["A5"]["auto"] is True and habits["A5"]["done"] is False
    assert habits["A1"]["via"] == "garmin" and habits["A2"]["via"] is None
    assert c.post("/api/habits/A5/toggle").status_code == 400          # руками не отмечается, только оценкой

    r = c.post("/api/rating", json={"score": 4, "note": "доволен"}).json()
    assert r["xp"] == 10 and r["milestone"] is None and r["level_up"] is False
    assert r["rating"]["score"] == 4 and r["rating"]["note"] == "доволен" and r["rating"]["month_days"] == 1
    assert r["character"]["xp"] == 10
    # передумал: число меняется, опыт второй раз не идёт, заметка из /day не теряется
    r = c.post("/api/rating", json={"score": -3}).json()
    assert r["xp"] == 0 and r["rating"]["score"] == -3 and r["rating"]["note"] == "доволен"
    assert r["rating"]["month_avg"] == -3.0 and r["character"]["xp"] == 10
    day = c.get("/api/today").json()
    assert day["rating"]["score"] == -3 and day["summary"]["xp_today"] == 10
    assert {h["cell_id"]: h for h in day["habits"]}["A5"]["done"] is True
    assert admin.execute("SELECT count(*) AS n FROM rpg.xp WHERE source = 'rating'").fetchone()["n"] == 1

    for bad in ({}, {"score": 11}, {"score": -11}, {"score": "5"}, {"score": 2.5}, {"score": True},
                {"score": None}, {"score": 1, "note": 5}, {"score": 1, "note": "x" * 501}):
        assert c.post("/api/rating", json=bad).status_code == 400, bad
    assert c.post("/api/rating", json={"score": 1}, headers={"Authorization": ""}).status_code == 401
    assert c.get("/api/rating").status_code == 405
    assert admin.execute("SELECT score FROM rpg.day_ratings").fetchone()["score"] == -3

    # у приложения только функция: в таблицу и в журнал опыта напрямую не пишет
    app_conn = as_role("workos_app")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE rpg.day_ratings SET score = 10")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("INSERT INTO rpg.day_ratings (day, score) VALUES ('2020-01-01', 10)")


# ---------- лента дня и пересборка ----------

def test_today_timeline(c, srv, admin):
    seed_grid(admin)
    day = tu.logical_today()
    fix_clock(srv, at(day, 9))
    add_event(admin, "d1", at(day, 10), at(day, 10, 30), "Daily Infatica")
    add_event(admin, "d1-copy", at(day, 10), at(day, 10, 30), "Daily Infatica", ctx="personal")
    add_event(admin, "x1", at(day, 11), at(day, 11, 30), "Отменили", status="cancelled")
    planned = add_task(admin, "Отчёт", size="M", due=day, context_id="infatica",
                       planned_by="auto", planned_start=at(day, 12), planned_end=at(day, 13))
    overdue = add_task(admin, "Хвост", due=day - timedelta(days=2))
    add_task(admin, "Сделано", due=day, done_at=at(day, 8, 30))
    add_task(admin, "Потом", week_of=tu.week_monday(day))

    t = c.get("/api/today").json()
    assert t["date"] == day.isoformat() and t["week"] == day.isocalendar()[1]
    assert t["date_label"].split(",")[0] == tu.RU_WEEKDAYS[day.weekday()].capitalize()
    assert [x["type"] for x in t["timeline"]] == ["now", "gap", "meeting", "gap", "task", "gap", "training"]
    meet = t["timeline"][2]
    assert meet["title"] == "Daily Infatica" and meet["time"] == "10:00"
    assert meet["meta"] == "10:00–10:30 · Инфатика" and meet["color"] == "#4FB6C2"
    assert t["timeline"][0]["time"] == "09:00"
    assert t["timeline"][1]["title"] == "Привал · свободно 1 ч"
    assert t["timeline"][3]["title"] == "Привал · свободно 1 ч 30 мин"
    task = t["timeline"][4]
    assert task["id"] == planned and task["time"] == "12:00" and task["size"] == "M"
    tr = t["timeline"][6]
    assert tr["time"] == "21:30" and tr["meta"] == "21:30–23:00 · Здоровье"
    assert [x["title"] for x in t["unscheduled"]] == ["Хвост", "Сделано"]
    assert t["unscheduled"][0]["id"] == overdue and t["unscheduled"][0]["overdue"] is True
    assert t["summary"] == {"done": 1, "total": 3, "xp_today": 0}
    assert t["character"]["level"] == 0


def test_replan_api(c, srv, admin):
    day = tu.logical_today()
    fix_clock(srv, at(day, 8))
    add_event(admin, "m1", at(day, 9), at(day, 10), "Синк", ctx="alfa")
    big = add_task(admin, "Большое", size="L", due=day)
    mid = add_task(admin, "Среднее", size="M", due=day)
    out = c.post("/api/replan").json()
    assert out["replan"]["placed"] == 2
    rows = {r["id"]: r for r in admin.execute("SELECT * FROM work.tasks").fetchall()}
    assert rows[big]["planned_start"] == at(day, 10, 10) and rows[big]["planned_by"] == "auto"
    assert rows[mid]["planned_start"] == at(day, 12, 10)
    padded = (at(day, 8, 50), at(day, 10, 10))
    for r in rows.values():
        assert not (r["planned_start"] < padded[1] and r["planned_end"] > padded[0])
    types = [x["type"] for x in out["timeline"]]
    assert types.count("task") == 2 and "meeting" in types and out["unscheduled"] == []


# ---------- журнал ----------

def test_list_week_and_later(c, srv, admin):
    fix_clock(srv, at(date(2026, 10, 21), 15))          # среда, неделя 19-25 октября
    add_task(admin, "чт", due=date(2026, 10, 22))
    add_task(admin, "сб", due=date(2026, 10, 24))
    add_task(admin, "на неделе", week_of=date(2026, 10, 19))
    add_task(admin, "сегодня", due=date(2026, 10, 21))
    add_task(admin, "след. ср", due=date(2026, 10, 28))
    add_task(admin, "след. неделя", week_of=date(2026, 10, 26))
    add_task(admin, "когда-нибудь")
    add_task(admin, "сделано", due=date(2026, 10, 22), done_at=at(date(2026, 10, 21), 12))
    add_task(admin, "удалено", due=date(2026, 10, 23), dropped_at=at(date(2026, 10, 21), 12))

    w = c.get("/api/list", params={"view": "week"}).json()
    assert [g["title"] for g in w["groups"]] == ["Чт, 22 октября", "Сб, 24 октября", "На неделе, без дня"]
    assert [[i["title"] for i in g["items"]] for g in w["groups"]] == [["чт"], ["сб"], ["на неделе"]]
    assert w["groups"][0]["date"] == "2026-10-22" and w["groups"][2]["date"] is None
    assert w["title"] == "Неделя 43 · 19–25 октября"

    later = c.get("/api/list", params={"view": "later"}).json()
    assert [g["title"] for g in later["groups"]] == ["Неделя с 26 октября", "Ср, 28 октября", "Без даты"]
    assert [[i["title"] for i in g["items"]] for g in later["groups"]] == [["след. неделя"], ["след. ср"], ["когда-нибудь"]]
    assert c.get("/api/list", params={"view": "all"}).status_code == 400


# ---------- персонаж и карта ----------

def test_character_and_grid(c, admin):
    seed_grid(admin)
    admin.execute("""INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) VALUES
        ('A2', 10, 'manual', 'a', now() - interval '1 day'), ('A2', 10, 'manual', 'b', now()),
        ('B', 40, 'task', '1', now() - interval '20 days')""")
    admin.execute("INSERT INTO rpg.rewards (scope, level, title) VALUES ('character', 1, 'Новая книга'), "
                  "('character', 2, 'Кроссовки'), ('A', 3, 'Не персонаж')")
    admin.execute("INSERT INTO rpg.achievements (code, title) VALUES ('first', 'Первый квест')")

    ch = c.get("/api/character").json()
    assert ch["name"] == "Георгий" and ch["xp"] == 60 and ch["level"] == 0 and ch["next_level_at"] == 100
    assert ch["main_quest"] == "Главный квест" and ch["season"] == "Сезон 1"
    attrs = {a["id"]: a for a in ch["attributes"]}
    assert attrs["A"]["xp"] == 20 and attrs["A"]["pct"] == 20 and attrs["A"]["rusty"] is False
    assert attrs["A"]["idle_days"] == 0
    assert attrs["B"]["rusty"] is True and attrs["B"]["idle_days"] in (19, 20, 21)
    assert ch["next_reward"] == {"level": 1, "title": "Новая книга"}
    assert ch["achievements"][0]["title"] == "Первый квест"
    assert ch["streaks"][0] == {"cell_id": "A2", "title": "Растяжка", "days": 2}

    g = c.get("/api/grid").json()
    assert g["center"] == {"id": "C", "title": "Главный квест"}
    assert [d["id"] for d in g["dirs"]] == ["A", "B"]
    a = g["dirs"][0]
    assert a["filled"] == 4 and a["level"] == 0 and a["rusty"] is False
    stats = {x["id"]: x["stat"] for x in a["cells"]}
    assert stats == {"A1": "серия 0", "A2": "серия 2", "A3": "серия 0", "A4": "босс"}
    assert g["dirs"][1]["cells"] == [{"id": "B1", "title": "Спокойствие", "kind": "quality",
                                      "stat": "0 из 4 недель", "auto": False}]

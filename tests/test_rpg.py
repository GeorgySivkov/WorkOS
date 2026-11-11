"""П.0-П.5: засев сетки, начислятор, Garmin, недельный разбор."""
import json
import sys
from datetime import date, time, timedelta

import pytest

from core import timeutil as tu


def at(y, m, d, hh=12, mm=0):
    return tu.at_local(date(y, m, d), time(hh, mm))


GRID = {"v": 1, "center": "Первый номер драфта", "dirs": [
    {"t": "Здоровье", "acts": [{"t": "Сон 7,5 ч", "k": "habit"}, {"t": "Марафон", "k": "milestone"},
                               {"t": "", "k": "habit"}, {"t": "Осанка", "k": "quality"}]},
    {"t": "Карьера", "acts": [{"t": "Пет-проект", "k": "milestone"}, {"t": "Чтение", "k": "habit"}]},
    {"t": "", "acts": [{"t": "сирота", "k": "habit"}]},
    {"t": "Финансы", "acts": [{"t": "Бюджет", "k": "habit"}]},
]}


@pytest.fixture
def grid_file(tmp_path):
    def write(data=GRID):
        p = tmp_path / "mandarat.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(p)
    return write


@pytest.fixture
def no_autonomy(monkeypatch):
    """Предложение идёт прямой вставкой: тест не зависит от agents.autonomy."""
    monkeypatch.setitem(sys.modules, "agents.autonomy", None)


def cells(admin):
    return {r["id"]: r for r in admin.execute("SELECT * FROM rpg.cells")}


def test_seed_idempotent_and_deactivates(admin, grid_file):
    from rpg import seed
    f = grid_file()
    r1 = seed.run(f, auto=["A1=garmin:sleep:7.5"], reward=['character:2="Наушники"'])
    c = cells(admin)
    assert set(c) == {"C", "A", "A1", "A2", "A4", "B", "B1", "B2", "D", "D1"}
    assert (c["A1"]["kind"], c["A1"]["xp_base"]) == ("habit", 10)
    assert (c["A2"]["kind"], c["A2"]["xp_base"]) == ("milestone", 150)
    assert (c["A4"]["kind"], c["A4"]["xp_base"]) == ("quality", 20)
    assert c["A1"]["auto_source"] == "garmin:sleep:7.5" and c["A"]["parent"] == "C"
    assert r1["inserted"] == 10
    rw = admin.execute("SELECT scope, level, title FROM rpg.rewards").fetchall()
    assert [(r["scope"], r["level"], r["title"]) for r in rw] == [("character", 2, "Наушники")]

    r2 = seed.run(f)
    assert r2["inserted"] == 0 and r2["deactivated"] == []
    assert cells(admin)["A1"]["auto_source"] == "garmin:sleep:7.5"      # без --auto источник не стирается

    # xp ссылается на A2: клетка исчезла из сетки, но не удаляется
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('A2', 150, 'manual', 'A2:once', now())")
    g = json.loads(json.dumps(GRID))
    g["dirs"][0]["acts"][0]["t"] = "Сон 8 ч"
    g["dirs"][0]["acts"][1]["t"] = ""
    r3 = seed.run(grid_file(g))
    c = cells(admin)
    assert r3["deactivated"] == ["A2"] and c["A2"]["active"] is False
    assert c["A1"]["title"] == "Сон 8 ч" and c["A1"]["active"]
    # вернулась в сетку: снова активна
    seed.run(grid_file())
    assert cells(admin)["A2"]["active"] is True


def test_award_safety_net_and_levelup_once(admin, as_role, role_env, grid_file):
    from rpg import award, seed
    seed.run(grid_file(), reward=['character:1="Наушники"'])
    role_env("workos_reader")
    r0 = award.run()
    assert r0["levelups"] == [] and admin.execute("SELECT count(*) AS n FROM work.outbox").fetchone()["n"] == 0
    assert admin.execute("SELECT count(*) AS n FROM rpg.level_marks").fetchone()["n"] == 4   # A, B, D, персонаж

    for i in range(2):
        admin.execute("INSERT INTO work.tasks (title, size, source, cell_id, done_at) "
                      "VALUES (%s, 'L', 'app', 'A1', now())", (f"Пробежка {i}",))
    r1 = award.run()
    assert r1["awarded_xp"] == 100                    # 60 + 40: потолок 100 в день
    assert len(r1["levelups"]) == 2 and "first_quest" in r1["achievements"]
    texts = [r["text"] for r in admin.execute("SELECT text FROM work.outbox WHERE topic = 'plan' ORDER BY id")]
    assert any("Уровень повышен: <b>Здоровье 1</b>" in t for t in texts)
    assert any("персонаж 1" in t and "Открыт сундук: Наушники" in t for t in texts)
    assert any("Первый квест" in t for t in texts)

    n = len(texts)
    r2 = award.run()
    assert r2["awarded_xp"] == 0 and r2["levelups"] == [] and r2["achievements"] == []
    assert admin.execute("SELECT count(*) AS n FROM work.outbox").fetchone()["n"] == n
    assert admin.execute("SELECT count(*) AS n FROM rpg.xp").fetchone()["n"] == 2


def test_award_achievements_streaks(admin, role_env, grid_file):
    from rpg import award, seed
    seed.run(grid_file())
    today = tu.logical_today()
    for i in range(7):
        d = today - timedelta(days=i)
        admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                      "VALUES ('A1', 10, 'manual', %s, %s)", (f"A1:{d}", tu.at_local(d, time(12))))
        admin.execute("INSERT INTO work.actions (context_id, type, payload, status) VALUES "
                      "('personal', 'plan_batch', %s, 'approved')",
                      (json.dumps({"for_date": d.isoformat(), "items": [{"title": "x", "size": "S"}]}),))
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('B1', 150, 'manual', 'B1:once', now())")
    admin.execute("UPDATE rpg.cells SET kind = 'milestone' WHERE id = 'B1'")
    role_env("workos_reader")
    r = award.run()
    assert set(r["achievements"]) == {"habit_streak_7", "plan_streak_7", "first_boss"}
    assert award.run()["achievements"] == []


class FakeGarmin:
    def __init__(self, last: date):
        self.last = last

    def activities(self, start, end):
        return [{"type": "strength_training", "minutes": 45, "start_local": at(2026, 10, 29, 19)},
                {"type": "walking", "minutes": 10, "start_local": at(2026, 10, 28, 19)}]

    def sleep_hours(self, d):
        return 8.0

    def steps(self, d):
        return 12000 if d == self.last else 3000


def test_garmin_awards_with_streak_and_cap(admin, role_env):
    from rpg import garmin
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('C', NULL, 'Квест', 'quest', 0, NULL), ('A', 'C', 'Здоровье', 'attribute', 0, NULL),
        ('A1', 'A', 'Сон', 'habit', 10, 'garmin:sleep:7.5'), ('A2', 'A', 'Шаги', 'habit', 10, 'garmin:steps:10000'),
        ('A3', 'A', 'Силовая', 'habit', 10, 'garmin:activity:strength'), ('A4', 'A', 'Растяжка', 'habit', 10, NULL)""")
    # уже 80 опыта характеристике 29.10: остаётся 20
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) "
                  "VALUES ('A4', 80, 'manual', 'A4:x', %s)", (at(2026, 10, 29, 10),))
    role_env("workos_reader")
    now = at(2026, 10, 29, 15)
    r = garmin.run(days=3, client=FakeGarmin(date(2026, 10, 29)), now=now)
    assert r["awarded"] == {"A1:2026-10-27": 10, "A1:2026-10-28": 11, "A1:2026-10-29": 12, "A2:2026-10-29": 8}
    row = admin.execute("SELECT source, earned_at FROM rpg.xp WHERE source_ref = 'A1:2026-10-29'").fetchone()
    assert row["source"] == "garmin" and tu.logical_date(row["earned_at"]) == date(2026, 10, 29)
    r2 = garmin.run(days=3, client=FakeGarmin(date(2026, 10, 29)), now=now)
    assert r2["awarded"] == {}
    assert garmin.parse_source("garmin:activity") == {"kind": "activity", "type": None, "minutes": 20}
    assert garmin.parse_source("strava:run") is None


def test_garmin_without_library_skips(admin, role_env, monkeypatch):
    from rpg import garmin
    admin.execute("INSERT INTO rpg.cells (id, parent, title, kind, auto_source) VALUES "
                  "('C', NULL, 'Квест', 'quest', NULL), ('A', 'C', 'Здоровье', 'attribute', NULL), "
                  "('A1', 'A', 'Сон', 'habit', 'garmin:sleep:7.5')")
    monkeypatch.setitem(sys.modules, "garminconnect", None)
    role_env("workos_reader")
    assert "garminconnect" in garmin.run()["skipped"]


def test_weekly_proposes_quests_for_lagging(admin, role_env, no_autonomy):
    from rpg import weekly
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base) VALUES
        ('C', NULL, 'Квест', 'quest', 0),
        ('A', 'C', 'Здоровье', 'attribute', 0), ('A1', 'A', 'Сон', 'habit', 10), ('A2', 'A', 'Марафон', 'milestone', 150),
        ('B', 'C', 'Карьера', 'attribute', 0), ('B1', 'B', 'Чтение', 'habit', 10),
        ('D', 'C', 'Финансы', 'attribute', 0), ('D1', 'D', 'Бюджет', 'habit', 10),
        ('E', 'C', 'Семья', 'attribute', 0), ('E1', 'E', 'Терпение', 'quality', 20)""")
    admin.execute("INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) VALUES "
                  "('A1', 100, 'manual', 'a', %s), ('B1', 20, 'manual', 'b', %s)",
                  (at(2026, 10, 1), at(2026, 9, 30)))
    admin.execute("INSERT INTO rpg.rewards (scope, level, title) VALUES ('character', 1, 'Наушники')")
    role_env("workos_reader")
    now = at(2026, 10, 4, 20)                         # воскресенье, следующая неделя уже в 4 квартале
    r = weekly.run(now=now)
    assert r["quests"] == ["D1", "B1", "A2"] and set(r["rusty"]) == {"D", "E"} and r["season"]
    act = admin.execute("SELECT type, status, payload, context_id FROM work.actions").fetchall()
    assert len(act) == 1 and act[0]["type"] == "plan_batch" and act[0]["status"] == "proposed"
    p = act[0]["payload"]
    assert p["for_date"] == "2026-10-05" and p["items"] == []
    assert [w["cell_id"] for w in p["week_items"]] == ["D1", "B1", "A2"]
    assert p["week_items"][0] == {"title": "Бюджет", "size": "S", "context_id": "personal", "cell_id": "D1"}
    text = admin.execute("SELECT text FROM work.outbox WHERE topic = 'plan'").fetchone()["text"]
    assert "Опыт за неделю" in text and "Ржавеют" in text and "Открыт сундук: Наушники" in text
    assert "новый сезон" in text and "—" not in text
    assert weekly.run(now=now)["skipped"]
    # обычное воскресенье: сезона нет
    assert weekly.run(now=at(2026, 10, 11, 20), dry_run=True)["season"] is False


def test_garmin_activity_type_alternatives():
    from datetime import date
    from rpg import garmin
    rule = garmin.parse_source("garmin:activity:cycling|biking:60")
    d = date(2026, 11, 10)
    acts = {d: [{"type": "road_biking", "minutes": 75}]}
    assert garmin.met(rule, d, acts, {}, None)
    acts = {d: [{"type": "walking", "minutes": 75}]}
    assert not garmin.met(rule, d, acts, {}, None)


def test_garmin_login_saves_tokens_and_reuses_them(tmp_path, monkeypatch):
    """garminconnect 0.3: токены пишет login(store), атрибута garth больше нет.
    Прежний код звал garth.dump и молча глотал ошибку: вход «ok», а токенов нет."""
    import json
    import sys
    import types
    from pathlib import Path

    from rpg import garmin as g

    calls = []

    class Garmin:                       # повторяет поведение garminconnect 0.3.20
        def __init__(self, email=None, password=None, prompt_mfa=None, **_):
            self.email, self.password = email, password

        def login(self, tokenstore=None):
            f = Path(tokenstore) / "garmin_tokens.json"
            if f.exists():
                calls.append("tokens")
                return None, None
            if not (self.email and self.password):
                raise RuntimeError("Username and password are required")
            calls.append("password")
            f.write_text(json.dumps({"di_token": "t"}))
            return None, None

    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=Garmin))
    store = tmp_path / "gc"
    monkeypatch.setenv("GARMIN_TOKENS", str(store))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    with pytest.raises(g.GarminUnavailable):
        g.GarminClient._login()                       # ни токенов, ни пароля
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "pw")
    assert g.login() == {"ok": True, "tokens": str(store)}
    assert (store / "garmin_tokens.json").exists() and calls == ["password"]
    monkeypatch.delenv("GARMIN_EMAIL")
    monkeypatch.delenv("GARMIN_PASSWORD")
    g.GarminClient._login()                           # ежечасный прогон: только токены
    assert calls == ["password", "tokens"]

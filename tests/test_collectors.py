"""Коллекторы фаз 2-3 и 7.2: Trello, Plaud, iCal, Slack, Gmail, приёмник с Мака.

Внешние API подменяются httpx.MockTransport, сеть не нужна. Коллекторы
пишут в базу под ролью workos_reader, как на дроплете.
"""
from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import plistlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from psycopg.types.json import Jsonb

from collectors.base import run_source
from core import timeutil as tu

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent


# ---------------- общее ----------------
@pytest.fixture
def mk_source(admin):
    made: list[str] = []

    def make(sid: str, kind: str, context: str | None = "personal", config: dict | None = None,
             lookback: int = 0, enabled: bool = True) -> dict:
        admin.execute("INSERT INTO work.sources (id, context_id, kind, enabled, lookback_days, config) "
                      "VALUES (%s, %s, %s, %s, %s, %s)", (sid, context, kind, enabled, lookback, Jsonb(config or {})))
        made.append(sid)
        return src(admin, sid)
    yield make
    admin.execute("DELETE FROM work.events WHERE source_id = ANY(%s)", (made,))
    admin.execute("DELETE FROM work.sources WHERE id = ANY(%s)", (made,))


def src(admin, sid: str) -> dict:
    return admin.execute("SELECT * FROM work.sources WHERE id = %s", (sid,)).fetchone()


def events(admin, sid: str) -> list[dict]:
    return admin.execute("SELECT * FROM work.events WHERE source_id = %s ORDER BY occurred_at, external_id",
                         (sid,)).fetchall()


def zulu(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------- Trello ----------------
def test_trello_cards_and_mirror(admin, mk_source, role_env, monkeypatch):
    from collectors.trello import TrelloCollector, whoami
    monkeypatch.setenv("TRELLO_KEY", "k")
    monkeypatch.setenv("TRELLO_TOKEN", "t")
    role_env("workos_reader")
    now = tu.now()
    cards = {
        "c1": {"id": "c1", "name": "Задача", "desc": "описание", "idList": "L1", "idLabels": ["lab1"],
               "idMembers": ["M1"], "due": None, "closed": False, "idBoard": "B1",
               "shortUrl": "https://trello.com/c/c1", "dateLastActivity": zulu(now - timedelta(hours=1))},
        "c2": {"id": "c2", "name": "Старьё", "desc": "", "idList": "L1", "idLabels": [], "idMembers": [],
               "closed": True, "idBoard": "B1", "dateLastActivity": zulu(now - timedelta(days=30))},
        "c3": {"id": "c3", "name": "https://trello.com/c/AbC123", "desc": "", "idList": "L1", "idLabels": [],
               "idMembers": [], "closed": False, "idBoard": "B1", "cardRole": "mirror", "mirrorSourceId": "S1",
               "dateLastActivity": zulu(now - timedelta(days=20))},
    }
    seen_auth = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("Authorization"))
        assert "token" not in req.url.params          # секрет не в URL
        p = req.url.path
        routes = {
            "/1/boards/B1": {"id": "B1", "name": "George_Sivkov_Board"},
            "/1/boards/B1/lists": [{"id": "L1", "name": "In Progress"}, {"id": "L2", "name": "Done"}],
            "/1/boards/B1/labels": [{"id": "lab1", "name": "SDK", "color": "green"}],
            "/1/boards/B1/members": [{"id": "M1", "fullName": "George Sivkov"}],
            "/1/boards/B1/cards/all": list(cards.values()),
            "/1/cards/S1": {"id": "S1", "name": "Исходная задача", "desc": "из доски Инфатики",
                            "due": "2026-11-05T09:00:00.000Z", "labels": [{"name": "Infatica"}],
                            "dateLastActivity": zulu(now - timedelta(hours=2)), "shortUrl": "https://trello.com/c/AbC123",
                            "board": {"name": "Infatica SDK"}, "list": {"name": "Doing"}, "members": []},
            "/1/members/me": {"id": "U1", "username": "george", "fullName": "George"},
            "/1/members/me/organizations": [{"id": "O1", "displayName": "Infatica"}],
            "/1/members/me/boards": [{"id": "B1", "name": "George_Sivkov_Board", "idOrganization": "O1"}],
        }
        if p not in routes:
            return httpx.Response(404, text="nope")
        return httpx.Response(200, json=routes[p])

    t = httpx.MockTransport(handler)
    s = mk_source("trello:test", "trello", "infatica", {"board_id": "B1"}, lookback=7)
    r1 = run_source("trello:test", TrelloCollector(s, transport=t))
    assert r1["inserted"] == 2
    assert all(a and a.startswith("OAuth oauth_consumer_key=") for a in seen_auth)
    rows = {e["external_id"]: e for e in events(admin, "trello:test")}
    assert set(rows) == {"c1", "c3"}
    assert rows["c1"]["title"] == "Задача" and rows["c1"]["meta"]["list"] == "In Progress"
    assert rows["c1"]["meta"]["labels"] == ["SDK"] and rows["c1"]["meta"]["members"] == ["George Sivkov"]
    m = rows["c3"]
    assert m["title"] == "Исходная задача" and m["body"] == "из доски Инфатики"
    assert m["meta"]["mirror_of"] == "S1" and m["meta"]["mirror_card_id"] == "c3"
    assert m["meta"]["labels"] == ["Infatica"] and m["meta"]["due"].startswith("2026-11-05")
    assert src(admin, "trello:test")["last_ok_at"] is not None

    # второй прогон: ничего нового
    r2 = run_source("trello:test", TrelloCollector(src(admin, "trello:test"), transport=t))
    assert r2["inserted"] == 0 and r2["updated"] == 0
    # карточку перенесли в другую колонку: обновилась только она
    cards["c1"] = dict(cards["c1"], idList="L2", dateLastActivity=zulu(tu.now()))
    r3 = run_source("trello:test", TrelloCollector(src(admin, "trello:test"), transport=t))
    assert r3["inserted"] == 0 and r3["updated"] == 1
    assert {e["external_id"]: e for e in events(admin, "trello:test")}["c1"]["meta"]["list"] == "Done"
    # описание поправили: body остаётся прежним, текущее описание в meta.desc
    cards["c1"] = dict(cards["c1"], desc="описание v2", dateLastActivity=zulu(tu.now()))
    r4 = run_source("trello:test", TrelloCollector(src(admin, "trello:test"), transport=t))
    c1 = {e["external_id"]: e for e in events(admin, "trello:test")}["c1"]
    assert r4["updated"] == 1 and c1["body"] == "описание" and c1["meta"]["desc"] == "описание v2"

    info = whoami(transport=t)
    assert info["me"]["username"] == "george" and info["boards"][0]["idOrganization"] == "O1"


# ---------------- Plaud ----------------
PLAUD_BASE = "https://platform.plaud.ai/developer/api"


def plaud_tokens(tmp_path, monkeypatch, expires_in_ms: int = 3_600_000, **extra) -> Path:
    import time as _t
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "Bearer",
                             "expires_at": int(_t.time() * 1000) + expires_in_ms, **extra}))
    monkeypatch.setenv("PLAUD_TOKENS_FILE", str(f))
    monkeypatch.delenv("PLAUD_PLATFORM_BASE", raising=False)
    return f


class FakePlaud:
    """Официальный API Plaud: список страницами, запись с source_list/note_list, обновление токена."""

    def __init__(self, recs: dict, texts: dict, summaries: dict | None = None, links: bool = False):
        self.recs, self.texts, self.summaries, self.links = recs, texts, summaries or {}, links
        self.pages: list[int] = []
        self.files: list[str] = []
        self.blob_auth: list[str | None] = []
        self.refreshed: list[str] = []
        self.token = "acc-1"

    def block(self, kind: str, content: str, key: str) -> dict:
        if self.links:
            return {"data_type": kind, "data_link": f"https://s3.example.com/{key}"}
        return {"data_type": kind, "data_content": content}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "s3.example.com":
            self.blob_auth.append(req.headers.get("Authorization"))
            key = req.url.path.strip("/")
            kind, fid = key.split("-", 1)
            body = json.dumps(self.texts[fid]) if kind == "t" else self.summaries[fid]
            return httpx.Response(200, text=body)
        path = req.url.path.removeprefix("/developer/api")
        if path == "/oauth/third-party/access-token/refresh":
            rt = parse_qs(req.content.decode())["refresh_token"][0]
            self.refreshed.append(rt)
            if rt != "ref-1" and rt != "ref-2":
                return httpx.Response(401, json={"detail": "bad"})
            self.token = f"acc-{len(self.refreshed) + 1}"
            return httpx.Response(200, json={"access_token": self.token, "refresh_token": "ref-2",
                                             "token_type": "bearer", "expires_in": 86400})
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        if path == "/open/third-party/files/":
            page, size = int(req.url.params["page"]), int(req.url.params["page_size"])
            self.pages.append(page)
            lst = sorted(self.recs.values(), key=lambda r: r["start_at"], reverse=True)
            return httpx.Response(200, json={"data": lst[(page - 1) * size: page * size], "page": page})
        if path.startswith("/open/third-party/files/"):
            fid = path.rsplit("/", 1)[1]
            self.files.append(fid)
            if fid not in self.recs:
                return httpx.Response(404)
            src = [self.block("transaction", json.dumps(self.texts[fid]), f"t-{fid}")] if fid in self.texts else []
            notes = [self.block("auto_sum_note", self.summaries[fid], f"s-{fid}")] if fid in self.summaries else []
            return httpx.Response(200, json={**self.recs[fid], "source_list": src, "note_list": notes})
        if path == "/open/third-party/users/current":
            return httpx.Response(200, json={"id": "u1", "email": "x@y"})
        return httpx.Response(404)


def naive_utc(dt: datetime) -> str:
    """Как Plaud отдаёт время: ISO без пояса, в UTC."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def plaud_flaky(codes: list[int]):
    """Список записей отвечает кодами по очереди; после них 200 с пустым списком."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        code = codes.pop(0) if codes else 200
        if code != 200:
            return httpx.Response(code, json={"detail": "UNKNOWN_ERROR"})
        return httpx.Response(200, json={"data": [{"id": "a"}], "page": 1})
    return httpx.MockTransport(handler), calls


def test_plaud_retries_server_errors(monkeypatch, tmp_path):
    """Разовый 500 от Plaud не ошибка источника: GET повторяется с паузами и проходит."""
    from collectors import plaud
    plaud_tokens(tmp_path, monkeypatch)
    slept = []
    monkeypatch.setattr(plaud.time, "sleep", slept.append)
    t, calls = plaud_flaky([500, 502])
    assert plaud.make_api(transport=t).list_recordings() == [{"id": "a"}]
    assert len(calls) == 3 and slept == [2.0, 5.0]


def test_plaud_gives_up_after_retries_and_skips_client_errors(monkeypatch, tmp_path):
    """Plaud лежит дольше повторов: ошибка доходит до сторожа. 4xx не повторяется вовсе."""
    from collectors import plaud
    plaud_tokens(tmp_path, monkeypatch)
    slept = []
    monkeypatch.setattr(plaud.time, "sleep", slept.append)
    t, calls = plaud_flaky([500, 500, 500])
    with pytest.raises(plaud.PlaudError, match="500"):
        plaud.make_api(transport=t).list_recordings()
    assert len(calls) == 3 and slept == [2.0, 5.0]

    slept.clear()
    t, calls = plaud_flaky([400])
    with pytest.raises(plaud.PlaudError, match="400"):
        plaud.make_api(transport=t).list_recordings()
    assert len(calls) == 1 and slept == []


def test_plaud_transcript_ready_and_calendar_match(admin, mk_source, role_env, monkeypatch, tmp_path):
    from collectors.plaud import PlaudCollector
    plaud_tokens(tmp_path, monkeypatch)
    role_env("workos_reader")
    now = tu.now()
    t_meet = (now - timedelta(hours=3)).replace(microsecond=0)
    t_solo = (now - timedelta(hours=1)).replace(microsecond=0)

    mk_source("ical:ptest", "ical", "infatica")
    cal_id = admin.execute(
        "INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta) "
        "VALUES ('infatica', 'ical:ptest', 'sync|', 'calendar', %s, 'SDK sync', %s) RETURNING id",
        (t_meet - timedelta(minutes=5), Jsonb({"ends_at": (t_meet + timedelta(minutes=55)).isoformat(),
                                               "status": "confirmed", "attendees": [{"email": "yan@x"}]}))
    ).fetchone()["id"]

    recs = {
        "of_solo": {"id": "of_solo", "name": "Заметка", "start_at": naive_utc(t_solo), "duration": 120000},
        "of_meet": {"id": "of_meet", "name": "SDK sync", "start_at": naive_utc(t_meet), "duration": 3000000},
        "of_old": {"id": "of_old", "name": "Давно", "start_at": naive_utc(now - timedelta(days=9)), "duration": 1000},
    }
    texts = {"of_meet": [
        {"speaker": "Ян", "content": "Привет.", "start_time": 0, "end_time": 1000},
        {"speaker": "Ян", "content": "Начнём.", "start_time": 1000, "end_time": 2000},
        {"speaker": "George", "content": "Давай.", "start_time": 2000, "end_time": 3000}]}
    fake = FakePlaud(recs, texts, {"of_meet": "# Итоги\nрешили выпускать"}, links=True)
    t = httpx.MockTransport(fake)
    s = mk_source("plaud:test", "plaud", None, lookback=3)
    r1 = run_source("plaud:test", PlaudCollector(s, transport=t))
    assert r1["inserted"] == 1 and "of_old" not in fake.files
    assert fake.blob_auth and set(fake.blob_auth) == {None}          # наш токен в хранилище не уходит
    ev = events(admin, "plaud:test")[0]
    assert ev["kind"] == "meeting" and ev["external_id"] == "of_meet" and ev["title"] == "SDK sync"
    assert ev["body"] == "Ян: Привет. Начнём.\nGeorge: Давай."
    assert ev["context_id"] == "infatica"
    assert ev["meta"]["calendar_event_id"] == cal_id and ev["meta"]["personal"] is False
    assert ev["meta"]["speakers"] == ["Ян", "George"] and ev["meta"]["segments_count"] == 3
    assert ev["meta"]["summary"].startswith("# Итоги") and ev["meta"]["participant_ids"] == []
    assert ev["occurred_at"] == t_meet
    st = json.loads(src(admin, "plaud:test")["watermark"])
    assert set(st["pending"]) == {"of_solo"}

    # сведение людей проставило участников; транскрипт второй записи дозрел к следующему прогону
    admin.execute("UPDATE work.events SET meta = meta || '{\"participant_ids\": [7]}' WHERE external_id = 'of_meet'")
    texts["of_solo"] = [{"speaker": "", "content": "Купить корм", "start_time": 0}]
    r2 = run_source("plaud:test", PlaudCollector(src(admin, "plaud:test"), transport=t))
    assert r2["inserted"] == 1
    rows = {e["external_id"]: e for e in events(admin, "plaud:test")}
    assert rows["of_meet"]["meta"]["participant_ids"] == [7]
    solo = rows["of_solo"]
    assert solo["context_id"] == "personal" and solo["meta"]["personal"] is True
    assert solo["meta"]["calendar_event_id"] is None and solo["body"] == "Спикер: Купить корм"
    assert "of_old" not in rows


def test_plaud_late_upload_and_late_transcript(admin, mk_source, role_env, monkeypatch, tmp_path):
    """Запись старше метки не теряется: транскрипт дозрел позже (pending) или файл залили поздно."""
    from collectors.plaud import PlaudCollector
    plaud_tokens(tmp_path, monkeypatch)
    role_env("workos_reader")
    now = tu.now()

    def at(delta: timedelta) -> str:
        return naive_utc(now - delta)

    recs = {
        "a": {"id": "a", "name": "A", "start_at": at(timedelta(hours=1)), "duration": 1000},
        "b": {"id": "b", "name": "B", "start_at": at(timedelta(hours=2)), "duration": 1000},
        "c": {"id": "c", "name": "C", "start_at": at(timedelta(hours=30)), "duration": 1000},
        "old": {"id": "old", "name": "Давно", "start_at": at(timedelta(days=10)), "duration": 1000},
    }
    texts = {"a": [{"speaker": "Я", "content": "первая"}], "old": [{"speaker": "Я", "content": "старая"}],
             "c": []}                                     # у «c» блок есть, а текста ещё нет
    fake = FakePlaud(recs, texts)
    t = httpx.MockTransport(fake)
    s = mk_source("plaud:late", "plaud", None, {"page_size": 2}, lookback=0)
    # прогон 1: «a» готова, «b» и «c» без текста (ждём), «old» до WorkOS
    r1 = run_source("plaud:late", PlaudCollector(s, transport=t))
    assert r1["inserted"] == 1
    st = json.loads(src(admin, "plaud:late")["watermark"])
    assert set(st["pending"]) == {"b", "c"} and st["floor"]

    # прогон 2 (обычный): «b» и «c» дозрели, обе из pending, хоть и старше метки.
    # «d» залили с телефона поздно (запись суточной давности), она за учтённой страницей
    texts["b"] = [{"speaker": "Я", "content": "вторая"}]
    texts["c"] = [{"speaker": "Я", "content": "третья"}]
    recs["d"] = {"id": "d", "name": "D", "start_at": at(timedelta(hours=26)), "duration": 1000}
    texts["d"] = [{"speaker": "Я", "content": "поздняя"}]
    fake.pages.clear()
    run_source("plaud:late", PlaudCollector(src(admin, "plaud:late"), transport=t))
    got = {e["external_id"]: e for e in events(admin, "plaud:late")}
    assert set(got) == {"a", "b", "c"} and got["c"]["body"] == "Я: третья"
    assert fake.pages == [1]                                      # первая страница учтена целиком
    assert json.loads(src(admin, "plaud:late")["watermark"])["pending"] == {}

    # прогон 3 (глубокий, раз в час): листаем до 30 дней назад и находим позднюю загрузку
    wm = json.loads(src(admin, "plaud:late")["watermark"])
    wm["scanned_at"] = (now - timedelta(hours=2)).isoformat()
    admin.execute("UPDATE work.sources SET watermark = %s WHERE id = 'plaud:late'", (json.dumps(wm),))
    fake.pages.clear()
    run_source("plaud:late", PlaudCollector(src(admin, "plaud:late"), transport=t))
    got = {e["external_id"]: e for e in events(admin, "plaud:late")}
    assert set(got) == {"a", "b", "c", "d"} and got["d"]["body"] == "Я: поздняя"
    assert "old" not in got                                       # до первого прогона: не тянем
    assert fake.pages == [1, 2, 3]

    # прогон 4: всё учтено, первая же страница старше метки и известна: дальше не листаем
    fake.pages.clear()
    fake.files.clear()
    r4 = run_source("plaud:late", PlaudCollector(src(admin, "plaud:late"), transport=t))
    assert r4["inserted"] == 0 and fake.pages == [1] and fake.files == []

    # срок ожидания: запись без транскрипта старше 30 дней из pending выпадает
    stale = dict(src(admin, "plaud:late"))
    stale["watermark"] = json.dumps({"cursor": now.isoformat(), "floor": (now - timedelta(days=40)).isoformat(),
                                     "pending": {"ghost": (now - timedelta(days=31)).isoformat()}})
    col = PlaudCollector(stale, transport=t)
    col.collect()
    assert col.state["pending"] == {}


def test_plaud_token_refresh_and_relogin(tmp_path, monkeypatch):
    """Истёкший access-токен обновляется сам, файл перезаписывается (600); отозванный вход даёт понятную ошибку."""
    import os
    from collectors.plaud import PlaudAuthError, make_api, probe
    f = plaud_tokens(tmp_path, monkeypatch, expires_in_ms=-1000)
    fake = FakePlaud({"x": {"id": "x", "name": "X", "start_at": "2026-11-20T10:00:00", "duration": 5}},
                     {"x": [{"speaker": "Я", "content": "текст"}]}, {"x": "сводка"})
    t = httpx.MockTransport(fake)
    api = make_api(transport=t)
    assert [r["id"] for r in api.list_recordings(0, 50)] == ["x"]
    assert fake.refreshed == ["ref-1"]
    saved = json.loads(f.read_text())
    assert saved["access_token"] == "acc-2" and saved["refresh_token"] == "ref-2" and saved["expires_at"] > 0
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"
    assert api.started_at({"start_at": "2026-11-20T10:00:00"}) == datetime(2026, 11, 20, 10, tzinfo=UTC)

    # сервер отозвал токен раньше срока: одна попытка обновиться и повтор запроса
    fake.token = "acc-server-side"
    fake.refreshed.clear()
    api2 = make_api(transport=t)
    assert api2.get_file("x")["id"] == "x" and fake.refreshed == ["ref-2"]

    # probe: только форма, без текстов
    out = probe(transport=t)
    assert out["list_count"] == 1 and out["source_types"] == ["transaction"] and out["segments"] == 1
    assert "текст" not in json.dumps(out, ensure_ascii=False) and "сводка" not in json.dumps(out, ensure_ascii=False)

    # refresh-токен отозван: понятная ошибка с подсказкой, файл не портим
    f.write_text(json.dumps({"access_token": "dead", "refresh_token": "revoked", "expires_at": 1}))
    with pytest.raises(PlaudAuthError, match="plaud login"):
        make_api(transport=t).list_recordings(0, 10)
    assert json.loads(f.read_text())["refresh_token"] == "revoked"
    f.unlink()
    with pytest.raises(PlaudAuthError, match="нет файла токенов"):
        make_api(transport=t).me()


# ---------------- iCal ----------------
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:weekly@x
DTSTART;TZID=Europe/Madrid:20261026T100000
DTEND;TZID=Europe/Madrid:20261026T103000
RRULE:FREQ=WEEKLY;COUNT={count}
SUMMARY:Еженедельный синк
ORGANIZER;CN=Ян:mailto:yan@infatica.io
ATTENDEE;CN=Ян;PARTSTAT=ACCEPTED:mailto:yan@infatica.io
ATTENDEE;CN=George;PARTSTAT=ACCEPTED:mailto:george@x.com
LAST-MODIFIED:20261020T100000Z
SEQUENCE:0
END:VEVENT
BEGIN:VEVENT
UID:weekly@x
RECURRENCE-ID;TZID=Europe/Madrid:20261102T100000
DTSTART;TZID=Europe/Madrid:20261102T120000
DTEND;TZID=Europe/Madrid:20261102T123000
SUMMARY:Еженедельный синк (перенос)
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:weekly@x
RECURRENCE-ID;TZID=Europe/Madrid:20261109T100000
DTSTART;TZID=Europe/Madrid:20261109T100000
DTEND;TZID=Europe/Madrid:20261109T103000
SUMMARY:Еженедельный синк
STATUS:CANCELLED
SEQUENCE:2
END:VEVENT
{single}BEGIN:VEVENT
UID:{mirror}@google.com
DTSTART:20261028T170000Z
DTEND:20261028T180000Z
SUMMARY:Копия из зеркала
END:VEVENT
BEGIN:VEVENT
UID:allday@x
DTSTART;VALUE=DATE:20261029
DTEND;VALUE=DATE:20261030
SUMMARY:Отпуск Яна
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
"""
SINGLE = """BEGIN:VEVENT
UID:single@x
DTSTART:20261028T150000Z
DTEND:20261028T160000Z
SUMMARY:Врач
LOCATION:Барселона
DESCRIPTION:взять анализы
END:VEVENT
"""


def test_ical_recurring_exceptions_and_disappearance(admin, mk_source, role_env, monkeypatch):
    from collectors.calendar import ICalCollector
    monkeypatch.setenv("ICAL_TEST_URL", "https://calendar.google.com/calendar/ical/secret/basic.ics")
    role_env("workos_reader")
    from executors.calendar import block_id, mirror_id
    mirror_uid = mirror_id("eas:alfa", "u1", "")
    ics = ICS.replace("{mirror}", mirror_uid)
    feed = {"text": ics.replace("{count}", "4").replace("{single}", SINGLE)}

    def handler(req):
        assert req.url.host == "calendar.google.com"
        return httpx.Response(200, text=feed["text"], headers={"Content-Type": "text/calendar"})

    t = httpx.MockTransport(handler)
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    mk_source("ical:test", "ical", "personal", {"self_emails": ["george@x.com"]})
    r1 = run_source("ical:test", ICalCollector(src(admin, "ical:test"), transport=t, now=now))
    assert r1["inserted"] == 6
    rows = {e["external_id"]: e for e in events(admin, "ical:test")}
    first = rows["weekly@x|2026-10-26T09:00:00+00:00"]           # 26.10 уже зимнее время, UTC+1
    assert first["occurred_at"] == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    assert first["meta"]["recurrence_id"] == "2026-10-26T09:00:00+00:00"
    assert first["meta"]["busy"] is True and first["meta"]["organizer"] == "yan@infatica.io"
    assert {"email": "yan@infatica.io", "name": "Ян"} in first["meta"]["attendees"]
    assert first["meta"]["ends_at"] == "2026-10-26T09:30:00+00:00" and first["meta"]["my_status"] == "accepted"
    moved = rows["weekly@x|2026-11-02T09:00:00+00:00"]           # id по исходному началу
    assert moved["occurred_at"] == datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
    assert moved["title"] == "Еженедельный синк (перенос)"
    cancelled = rows["weekly@x|2026-11-09T09:00:00+00:00"]
    assert cancelled["meta"]["status"] == "cancelled" and cancelled["meta"]["busy"] is False
    single = rows["single@x|"]
    assert single["meta"]["recurrence_id"] == "" and single["body"] == "взять анализы"
    assert single["meta"]["location"] == "Барселона"
    allday = rows["allday@x|"]
    assert allday["meta"]["all_day"] is True and allday["meta"]["busy"] is False
    assert allday["occurred_at"] == datetime(2026, 10, 28, 23, 0, tzinfo=UTC)   # полночь по Мадриду
    assert not any(k.startswith(mirror_uid) for k in rows)       # копия зеркала в базу не вернулась
    from collectors.calendar import OWN_UID_RE
    assert OWN_UID_RE.match(block_id(42) + "@google.com") and not OWN_UID_RE.match("abc123@google.com")

    # повторный прогон той же ленты ничего не меняет
    r2 = run_source("ical:test", ICalCollector(src(admin, "ical:test"), transport=t, now=now))
    assert r2["inserted"] == 0 and r2["updated"] == 0

    # разовую встречу удалили, серию укоротили: исчезнувшее стало cancelled, строки на месте
    feed["text"] = ics.replace("{count}", "3").replace("{single}", "")
    run_source("ical:test", ICalCollector(src(admin, "ical:test"), transport=t, now=now))
    feed_rows = {r["external_id"]: r for r in admin.execute(
        "SELECT external_id, status, busy FROM work.calendar_feed WHERE source_id = 'ical:test'").fetchall()}
    assert len(feed_rows) == 6
    assert feed_rows["single@x|"]["status"] == "cancelled" and feed_rows["single@x|"]["busy"] is False
    assert feed_rows["weekly@x|2026-11-16T09:00:00+00:00"]["status"] == "cancelled"
    assert feed_rows["weekly@x|2026-10-26T09:00:00+00:00"]["status"] == "confirmed"

    # пустая лента (сбой Google) не отменяет весь календарь
    feed["text"] = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nEND:VCALENDAR\n"
    run_source("ical:test", ICalCollector(src(admin, "ical:test"), transport=t, now=now))
    st = admin.execute("SELECT status FROM work.calendar_feed WHERE external_id = 'weekly@x|2026-10-26T09:00:00+00:00'"
                       ).fetchone()["status"]
    assert st == "confirmed"


# ---------------- Slack ----------------
def test_slack_history_threads_mentions(admin, mk_source, role_env, monkeypatch):
    from collectors.slack import SlackCollector
    monkeypatch.setenv("SLACK_TOKEN", "xoxp-test")
    role_env("workos_reader")
    base = tu.now().timestamp()

    def ts(minutes_ago: float) -> str:
        return f"{base - minutes_ago * 60:.6f}"

    T1, T2, T3, T5, T6 = ts(50), ts(40), ts(30), ts(20), ts(10)
    T0, TR = ts(60 * 24 * 10), ts(5)              # тред десятидневной давности ожил сейчас
    history = [
        {"type": "message", "user": "U1", "text": "всем привет (поправил)", "ts": T1, "edited": {"ts": T5}},
        {"type": "message", "user": "U2", "text": "релиз SDK", "ts": T2, "thread_ts": T2, "reply_count": 1,
         "latest_reply": T3},
        {"type": "message", "subtype": "channel_join", "user": "U3", "text": "joined", "ts": ts(45)},
    ]
    replies = {
        ("C1", T2): [history[1], {"type": "message", "user": "U1", "text": "<@UME> глянь", "ts": T3, "thread_ts": T2}],
        ("C1", T0): [{"type": "message", "user": "U2", "text": "старый тред", "ts": T0, "thread_ts": T0,
                      "reply_count": 1, "latest_reply": TR},
                     {"type": "message", "user": "U1", "text": "а вот и ответ", "ts": TR, "thread_ts": T0}],
        ("C9", T5): [{"type": "message", "user": "U2", "text": "вопрос", "ts": T5, "thread_ts": T5, "reply_count": 1},
                     {"type": "message", "user": "U2", "text": "<@UME> ответишь?", "ts": T6, "thread_ts": T5,
                      "edited": {"ts": T6}}],
    }
    calls: list[str] = []
    state = {"limited": False}
    slept: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer xoxp-test"
        method = req.url.path.rsplit("/", 1)[-1]
        q = dict(req.url.params)
        calls.append(method)
        if method == "conversations.history":
            if not state["limited"]:
                state["limited"] = True
                return httpx.Response(429, headers={"Retry-After": "1"})
            msgs = [m for m in history if float(m["ts"]) >= float(q["oldest"])]
            return httpx.Response(200, json={"ok": True, "messages": msgs, "has_more": False})
        if method == "conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": replies[(q["channel"], q["ts"])]})
        if method == "search.messages" and q["query"].startswith("in:<#C1> after:"):
            return httpx.Response(200, json={"ok": True, "messages": {
                "matches": [{"channel": {"id": "C1"}, "ts": TR, "text": "а вот и ответ",
                             "permalink": f"https://x.slack.com/archives/C1/p2?thread_ts={T0}&cid=C1"},
                            {"channel": {"id": "C1"}, "ts": T1, "text": "всем привет",
                             "permalink": "https://x.slack.com/archives/C1/p1"}],
                "paging": {"page": 1, "pages": 1}}})
        if method == "search.messages":
            assert q["query"].startswith("<@UME> after:")
            return httpx.Response(200, json={"ok": True, "messages": {
                "matches": [{"channel": {"id": "C9", "name": "general"}, "ts": T6, "user": "U2",
                             "text": "<@UME> ответишь?",
                             "permalink": f"https://x.slack.com/archives/C9/p1?thread_ts={T5}&cid=C9"}],
                "paging": {"page": 1, "pages": 1}}})
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"name": q["user"].lower(),
                                                                   "profile": {"real_name": f"Имя {q['user']}"}}})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    t = httpx.MockTransport(handler)
    s = mk_source("slack:test", "slack", "infatica", {"channels": ["C1"], "user_id": "UME"}, lookback=2)
    r1 = run_source("slack:test", SlackCollector(s, transport=t, sleep=slept.append))
    assert slept == [1.0]                                    # 429 + Retry-After уважили
    assert r1["inserted"] == 7
    rows = {e["external_id"]: e for e in events(admin, "slack:test")}
    assert set(rows) == {f"C1:{T1}", f"C1:{T2}", f"C1:{T3}", f"C9:{T5}", f"C9:{T6}", f"C1:{T0}", f"C1:{TR}"}
    assert rows[f"C1:{TR}"]["thread_key"] == f"C1:{T0}"            # ответ в старый тред нашёл поиск
    assert rows[f"C1:{T1}"]["meta"]["text"] == "всем привет (поправил)"   # правка видна в meta.text
    assert "text" not in rows[f"C1:{T2}"]["meta"]
    reply = rows[f"C1:{T3}"]
    assert reply["thread_key"] == f"C1:{T2}" and reply["meta"]["mentions_me"] is True
    assert reply["actor_raw"] == "U1" and reply["meta"]["user_name"] == "Имя U1"
    assert rows[f"C1:{T1}"]["meta"]["mentions_me"] is False and rows[f"C1:{T1}"]["title"] is None
    mention = rows[f"C9:{T6}"]
    assert mention["meta"]["permalink"].startswith("https://x.slack.com/") and mention["meta"]["edited"] is True
    assert mention["thread_key"] == f"C9:{T5}" and mention["meta"]["channel_name"] == "general"
    st = json.loads(src(admin, "slack:test")["watermark"])
    assert st["channels"]["C1"] == T2 and f"C1|{T2}" in st["threads"]
    assert calls.count("users.info") == 2                    # имена кешируются
    assert f"C1|{T0}" in st["threads"]                       # оживший тред теперь живой

    # второй прогон: всё уже есть, треды перечитаны, дублей нет; новая правка попала в meta.text
    history[0] = dict(history[0], text="всем привет (ещё раз)")
    r2 = run_source("slack:test", SlackCollector(src(admin, "slack:test"), transport=t, sleep=slept.append))
    assert r2["inserted"] == 0
    assert len(events(admin, "slack:test")) == 7
    first = {e["external_id"]: e for e in events(admin, "slack:test")}[f"C1:{T1}"]
    assert first["meta"]["text"] == "всем привет (ещё раз)" and first["body"] == "всем привет (поправил)"


# ---------------- Gmail ----------------
def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid: str, labels: list[str], headers: dict, payload: dict, ts: datetime) -> dict:
    return {"id": mid, "threadId": f"t-{mid}", "labelIds": labels, "snippet": "сниппет &amp; всё",
            "internalDate": str(int(ts.timestamp() * 1000)),
            "payload": {**payload, "headers": [{"name": k, "value": v} for k, v in headers.items()]}}


def test_gmail_first_run_history_and_404_fallback(admin, mk_source, role_env, monkeypatch):
    from collectors.gmail import GmailCollector
    for k, v in {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
                 "GMAIL_PERSONAL_REFRESH_TOKEN": "rt"}.items():
        monkeypatch.setenv(k, v)
    role_env("workos_reader")
    now = tu.now().replace(microsecond=0)
    msgs = {
        "m1": _msg("m1", ["INBOX", "UNREAD"],
                   {"From": "Yan <yan@infatica.io>", "To": "george@x.com", "Subject": "=?UTF-8?B?0J/RgNC40LLQtdGC?=",
                    "Message-ID": "<m1@infatica.io>", "Date": "Thu, 29 Oct 2026 10:00:00 +0000"},
                   {"mimeType": "multipart/alternative", "parts": [
                       {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                        "body": {"data": _b64("Привет, это текст.")}},
                       {"mimeType": "text/html", "body": {"data": _b64("<p>Привет</p>")}}]},
                   now - timedelta(hours=5)),
        "m2": _msg("m2", ["INBOX"], {"From": "bank@alfa", "Subject": "Выписка", "Message-ID": "<m2@alfa>"},
                   {"mimeType": "multipart/mixed", "parts": [
                       {"mimeType": "text/html", "body": {"data": _b64(
                           "<html><style>p{}</style><body><p>Сумма&nbsp;100</p><div>Итого</div></body></html>")}},
                       {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x"}}]},
                   now - timedelta(hours=4)),
        "m3": _msg("m3", ["SPAM"], {"From": "spam@x", "Subject": "Выигрыш", "Message-ID": "<m3@x>"},
                   {"mimeType": "text/plain", "body": {"data": _b64("купи")}}, now - timedelta(hours=1)),
        "m4": _msg("m4", ["INBOX"], {"From": "noid@x", "Subject": "Без Message-ID"},
                   {"mimeType": "text/plain", "body": {"data": _b64("тело")}}, now - timedelta(minutes=30)),
    }
    world = {"profile_history": "100", "listed": ["m1", "m2"], "limited": 0}
    seen: list[str] = []
    queries: list[str] = []
    slept: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            assert form["refresh_token"] == ["rt"]
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer at"
        p = req.url.path.replace("/gmail/v1/users/me", "")
        q = dict(req.url.params)
        seen.append(p)
        if p == "/profile":
            if world["limited"]:
                world["limited"] -= 1
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"historyId": world["profile_history"]})
        if p == "/messages":
            queries.append(q["q"])
            return httpx.Response(200, json={"messages": [{"id": i} for i in world["listed"]]})
        if p == "/history":
            assert q["historyTypes"] == "messageAdded"
            if q["startHistoryId"] == "100":
                return httpx.Response(200, json={"historyId": "120", "history": [{"id": "110", "messagesAdded": [
                    {"message": {"id": "m3", "labelIds": ["SPAM"]}},
                    {"message": {"id": "m4", "labelIds": ["INBOX"]}}]}]})
            return httpx.Response(404, json={"error": {"code": 404}})
        if p.startswith("/messages/"):
            mid = p.split("/")[2]
            assert q.get("format") == "full"
            return httpx.Response(200, json=msgs[mid]) if mid in msgs else httpx.Response(404)
        return httpx.Response(400)

    t = httpx.MockTransport(handler)
    s = mk_source("gmail:test", "gmail", "personal", {"refresh_env": "GMAIL_PERSONAL_REFRESH_TOKEN",
                                                      "account": "personal"}, lookback=2)
    r1 = run_source("gmail:test", GmailCollector(s, transport=t))
    assert r1["inserted"] == 2 and queries == ["newer_than:2d"]       # первый прогон: метки нет
    rows = {e["external_id"]: e for e in events(admin, "gmail:test")}
    m1 = rows["<m1@infatica.io>"]
    assert m1["title"] == "Привет" and m1["body"] == "Привет, это текст."
    assert m1["actor_raw"] == "Yan <yan@infatica.io>" and m1["thread_key"] == "gmail:t-m1"
    assert m1["meta"]["gmail_id"] == "m1" and m1["meta"]["labels"] == ["INBOX", "UNREAD"]
    assert m1["meta"]["account"] == "personal" and m1["meta"]["snippet"] == "сниппет & всё"
    assert m1["occurred_at"] == now - timedelta(hours=5)
    m2 = rows["<m2@alfa>"]
    assert "Сумма 100" in m2["body"] and "Итого" in m2["body"] and "p{}" not in m2["body"]
    assert json.loads(src(admin, "gmail:test")["watermark"])["history_id"] == "100"

    # второй прогон: History API, спам пропущен, письмо без Message-ID получило gmail:id
    r2 = run_source("gmail:test", GmailCollector(src(admin, "gmail:test"), transport=t))
    assert r2["inserted"] == 1
    assert "gmail:m4" in {e["external_id"] for e in events(admin, "gmail:test")}
    assert "/messages/m3" not in seen
    assert json.loads(src(admin, "gmail:test")["watermark"])["history_id"] == "120"

    # метка устарела (404): откат на полный перечит без дублей, от прошлого прогона, а не за 2 дня
    world.update(profile_history="130", limited=1)
    cursor = datetime.fromisoformat(json.loads(src(admin, "gmail:test")["watermark"])["cursor"])
    r3 = run_source("gmail:test", GmailCollector(src(admin, "gmail:test"), transport=t, sleep=slept.append))
    assert r3["inserted"] == 0 and slept == [1.0]                    # 429 + Retry-After уважили
    assert queries[-1] == f"after:{int((cursor - timedelta(days=3)).timestamp())}"
    wm = json.loads(src(admin, "gmail:test")["watermark"])
    assert wm["history_id"] == "130" and wm["mode"] == "full"
    assert len(events(admin, "gmail:test")) == 3

    # простой 10 дней: откат читает всю почту с прошлого успешного прогона
    old = tu.now() - timedelta(days=10)
    admin.execute("UPDATE work.sources SET watermark = %s WHERE id = 'gmail:test'",
                  (json.dumps({"cursor": old.isoformat(), "history_id": "5", "mode": "history"}),))
    run_source("gmail:test", GmailCollector(src(admin, "gmail:test"), transport=t))
    after = int(queries[-1].removeprefix("after:"))
    assert after <= (old - timedelta(days=2)).timestamp()


# ---------------- приёмник с Мака ----------------
TOKEN = "test-ingest-token-0123456789"


@pytest.fixture
def ingest_server(role_env):
    from collectors.ingest_server import make_server
    role_env("workos_reader")
    srv = make_server("127.0.0.1:0", TOKEN)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _batch(n: int = 2) -> dict:
    t0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    return {"events": [{"external_id": f"msg-{i}", "kind": "message", "occurred_at": (t0 + timedelta(minutes=i)).isoformat(),
                        "title": "Команда поддержки", "body": f"сообщение {i}", "actor_raw": "Ян",
                        "thread_key": "achat:Команда поддержки", "meta": {"chat": "Команда поддержки"}}
                       for i in range(n)], "watermark": "2026-10-29T09:01:00+00:00"}


def test_ingest_server_auth_kind_and_idempotency(admin, mk_source, ingest_server):
    mk_source("achat:test", "achat", "alfa")
    mk_source("trello:ingest", "trello", "infatica")
    mk_source("achat:off", "achat", "alfa", enabled=False)
    url = ingest_server
    with httpx.Client(timeout=10) as c:
        assert c.post(f"{url}/ingest/achat:test", json=_batch()).status_code == 401
        assert c.post(f"{url}/ingest/achat:test", json=_batch(),
                      headers={"Authorization": "Bearer wrong-token-wrong-token"}).status_code == 401
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert c.post(f"{url}/ingest/trello:ingest", json=_batch(), headers=auth).status_code == 403
        assert c.post(f"{url}/ingest/achat:off", json=_batch(), headers=auth).status_code == 403
        assert c.post(f"{url}/ingest/nope:1", json=_batch(), headers=auth).status_code == 404
        bad = {"events": [{"external_id": "x", "body": "нет даты"}]}
        assert c.post(f"{url}/ingest/achat:test", json=bad, headers=auth).status_code == 400
        r1 = c.post(f"{url}/ingest/achat:test", json=_batch(), headers=auth)
        assert r1.status_code == 200 and r1.json()["inserted"] == 2
        r2 = c.post(f"{url}/ingest/achat:test", json=_batch(), headers=auth)
        assert r2.status_code == 200 and r2.json() == {"inserted": 0, "updated": 0, "seen": 2}
    rows = events(admin, "achat:test")
    assert len(rows) == 2 and rows[0]["context_id"] == "alfa" and rows[0]["body"] == "сообщение 0"
    s = src(admin, "achat:test")
    assert s["last_ok_at"] is not None and s["watermark"] == "2026-10-29T09:01:00+00:00"
    assert events(admin, "trello:ingest") == []

    # больше 10 МБ: отказ до чтения тела
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("POST", "/ingest/achat:test")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", str(11 * 1024 * 1024))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def _ios_payload(now: datetime, cal: list[dict], mail: list[dict]) -> dict:
    return {"now": now.astimezone(timezone(timedelta(hours=1))).isoformat(timespec="seconds"),
            "calendar": cal, "mail": mail}


def test_ios_alfa_calendar_and_mail(admin, mk_source, ingest_server):
    """Айфон шлёт встречи и письма: пишем только ящик Alfabank, пропавшие встречи отменяем."""
    mk_source("ios:test", "ios", "alfa", {"account": "Alfabank"})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    now = tu.now().replace(microsecond=0)
    madrid = timezone(timedelta(hours=1))

    def at(h: float) -> str:
        return (now + timedelta(hours=h)).astimezone(madrid).isoformat(timespec="seconds")

    cal = [{"title": "Ретро поддержки", "start": at(2), "end": at(3), "all_day": "No", "location": "Teams"},
           {"title": "Синк SFA", "start": at(26), "end": at(27), "all_day": False},
           {"title": "Отпуск Яна", "start": at(48), "end": at(72), "all_day": "Yes"}]
    mail = [{"account": "Alfabank", "mailbox": "Inbox", "subject": "Выгрузка", "sender": "Ян <yan@alfabank.ru>",
             "to": "George <g@alfabank.ru>", "cc": "", "received": at(-1), "read": "No", "body": "Добрый день."},
            {"account": "Gmail", "subject": "личное", "sender": "x@gmail.com", "received": at(-1), "body": "не наше"}]
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{ingest_server}/ingest/ios:test", json=_ios_payload(now, cal, mail), headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["calendar"] == 3 and r.json()["mail"] == 1 and r.json()["inserted"] == 4
        # плохая дата: 400, ничего не пишется
        bad = _ios_payload(now, [{"title": "x", "start": "22 Nov 2026 at 10:00"}], [])
        assert c.post(f"{ingest_server}/ingest/ios:test", json=bad, headers=auth).status_code == 400

        rows = {e["title"]: e for e in events(admin, "ios:test")}
        assert set(rows) == {"Ретро поддержки", "Синк SFA", "Отпуск Яна", "Выгрузка"}
        assert all(e["context_id"] == "alfa" for e in rows.values())
        m = rows["Выгрузка"]
        assert m["kind"] == "email" and m["body"] == "Добрый день." and m["meta"]["from_email"] == "yan@alfabank.ru"
        assert m["meta"]["to_emails"] == ["g@alfabank.ru"] and m["meta"]["read"] is False
        feed = {f["title"]: f for f in admin.execute(
            "SELECT * FROM work.calendar_feed WHERE source_id = 'ios:test'").fetchall()}
        assert feed["Ретро поддержки"]["location"] == "Teams" and feed["Ретро поддержки"]["busy"] is True
        day = feed["Отпуск Яна"]
        assert day["all_day"] is True and tu.local(day["starts_at"]).hour == 0 and tu.local(day["ends_at"]).hour == 0

        # следующий запуск: «Синк SFA» перенесли на час, письмо прочитано, календарь пуст не целиком
        cal2 = [cal[0], {**cal[1], "start": at(27), "end": at(28)}, cal[2]]
        mail2 = [{**mail[0], "read": "Yes"}]
        r2 = c.post(f"{ingest_server}/ingest/ios:test", json=_ios_payload(now, cal2, mail2), headers=auth).json()
        assert r2["inserted"] == 1 and r2["cancelled"] == 1
        feed = admin.execute("SELECT title, status, starts_at FROM work.calendar_feed WHERE source_id = 'ios:test' "
                             "AND title = 'Синк SFA' ORDER BY starts_at").fetchall()
        assert [f["status"] for f in feed] == ["cancelled", "confirmed"]
        assert len([e for e in events(admin, "ios:test") if e["kind"] == "email"]) == 1
        assert [e for e in events(admin, "ios:test") if e["kind"] == "email"][0]["meta"]["read"] is True

        # пустой календарь (сбой команды) ничего не отменяет; пустая переменная приходит строкой
        r3 = c.post(f"{ingest_server}/ingest/ios:test", json={**_ios_payload(now, [], []), "calendar": "", "mail": ""},
                    headers=auth).json()
        assert r3["cancelled"] == 0
    s = src(admin, "ios:test")
    assert s["last_ok_at"] is not None and s["last_error"] is None


def test_ios_notes_from_watch(admin, mk_source, ingest_server):
    """Siri на часах -> команда «Заметка» -> {"now","text"}: событие для захвата, повтор не дублирует."""
    from health import problem
    mk_source("ios:ntest", "ios", "personal", {"format": "notes", "health_hours": 0})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    now = datetime(2026, 11, 20, 13, 30, tzinfo=timezone(timedelta(hours=1)))
    body = {"now": now.isoformat(timespec="seconds"), "text": "  позвонить   в банк\nв пятницу "}
    with httpx.Client(timeout=10) as c:
        r1 = c.post(f"{ingest_server}/ingest/ios:ntest", json=body, headers=auth)
        assert r1.status_code == 200 and r1.json()["inserted"] == 1
        r2 = c.post(f"{ingest_server}/ingest/ios:ntest", json=body, headers=auth)
        assert r2.status_code == 200 and r2.json()["inserted"] == 0 and r2.json()["note"] == r1.json()["note"]
        assert c.post(f"{ingest_server}/ingest/ios:ntest", json={"now": body["now"], "text": "  "},
                      headers=auth).status_code == 400
        assert c.post(f"{ingest_server}/ingest/ios:ntest", json={"text": "без времени"}, headers=auth).status_code == 200
    rows = events(admin, "ios:ntest")
    assert len(rows) == 2
    ev = [r for r in rows if r["external_id"] == r1.json()["note"]][0]
    assert ev["kind"] == "note" and ev["body"] == "позвонить в банк в пятницу" and ev["context_id"] == "personal"
    assert ev["meta"]["capture_pending"] is True and ev["meta"]["via"] == "siri"
    assert ev["occurred_at"] == now.astimezone(UTC)
    # повтор после разметки ботом не возвращает capture_pending
    admin.execute("UPDATE work.events SET meta = meta || '{\"capture_pending\": false}' WHERE id = %s", (ev["id"],))
    with httpx.Client(timeout=10) as c:
        c.post(f"{ingest_server}/ingest/ios:ntest", json=body, headers=auth)
    assert [r for r in events(admin, "ios:ntest") if r["id"] == ev["id"]][0]["meta"]["capture_pending"] is False
    s = src(admin, "ios:ntest")
    assert s["last_ok_at"] is not None
    # нерегулярный источник: молчание не тревога
    assert problem({**s, "last_ok_at": None, "last_error": None}, tu.now()) is None


def test_ios_source_seeded_and_health_threshold(admin):
    from health import threshold
    s = src(admin, "ios:alfa")
    assert s["kind"] == "ios" and s["context_id"] == "alfa" and s["enabled"] is False
    assert s["config"]["account"] == "Alfabank"
    assert threshold({"kind": "ios", "config": {}}) == 13.0
    n = src(admin, "ios:notes")
    assert n["kind"] == "ios" and n["context_id"] == "personal" and n["enabled"] is False
    assert n["config"]["format"] == "notes" and threshold(n) == 0


# ---------------- коллектор на Маке ----------------
def _achat_module():
    spec = importlib.util.spec_from_file_location("achat_collector", ROOT / "mac" / "achat_collector.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_achat_sqlite_snapshot_to_ingest(admin, mk_source, ingest_server, tmp_path, monkeypatch):
    ac = _achat_module()
    monkeypatch.setattr(ac, "STATE", tmp_path / "achat.state")
    mk_source("achat:mac", "achat", "alfa")
    dbp = tmp_path / "store.sqlite"
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE ZMSG (Z_PK INTEGER PRIMARY KEY, ZDATE REAL, ZCHAT TEXT, ZAUTHOR TEXT, ZTEXT TEXT)")
    now = datetime.now(UTC)
    for i, mins in enumerate((90, 30, 5)):
        con.execute("INSERT INTO ZMSG VALUES (?, ?, 'Поддержка', 'Ян', ?)",
                    (i + 1, (now - timedelta(minutes=mins)).timestamp() - ac.COREDATA_EPOCH, f"текст {i}"))
    con.execute("INSERT INTO ZMSG VALUES (9, ?, 'Поддержка', 'Ян', '')", (now.timestamp() - ac.COREDATA_EPOCH,))
    con.commit()                                  # соединение не закрыто: снапшот снимается с живой WAL-базы
    cfg = {"mode": "sqlite", "db_path": str(dbp), "ts_format": "coredata",
           "query": "SELECT Z_PK AS id, ZDATE AS ts, ZCHAT AS chat, ZAUTHOR AS author, ZTEXT AS text "
                    "FROM ZMSG WHERE ZDATE > :since ORDER BY ZDATE",
           "ingest_url": ingest_server, "token": TOKEN, "source_id": "achat:mac", "initial_days": 1}
    assert ac.run(cfg, dry_run=True) == {"rows": 3, "dry_run": True}
    assert not (tmp_path / "achat.state").exists()
    assert ac.run(cfg) == {"rows": 3, "sent": 3}
    rows = events(admin, "achat:mac")
    assert [r["body"] for r in rows] == ["текст 0", "текст 1", "текст 2"]
    assert rows[0]["thread_key"] == "achat:Поддержка" and rows[0]["actor_raw"] == "Ян"
    # повтор с перекрытием в 60 минут: два последних уходят снова, но не задваиваются
    assert ac.run(cfg) == {"rows": 2, "sent": 2}
    assert len(events(admin, "achat:mac")) == 3
    con.close()
    assert list(tmp_path.glob("workos-achat-*")) == []


def test_achat_seq_cursor_server_ids_and_heartbeat(admin, mk_source, ingest_server, tmp_path, monkeypatch):
    """Метка по Z_PK: докачанный задним числом чат не теряется. server_id: после переустановки
    А-Чата (Z_PK заново) дублей нет. Нового нет: пустая пачка, дроплет видит, что Мак жив."""
    ac = _achat_module()
    monkeypatch.setattr(ac, "STATE", tmp_path / "achat.state")
    mk_source("achat:seq", "achat", "alfa")
    now = datetime.now(UTC)

    def make_db(path, rows):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE ZMSG (Z_PK INTEGER PRIMARY KEY, ZSRV TEXT, ZDATE REAL, ZCHAT TEXT, "
                    "ZAUTHOR TEXT, ZTEXT TEXT)")
        for pk, srv, ago, text in rows:
            con.execute("INSERT INTO ZMSG VALUES (?, ?, ?, 'Поддержка', 'Ян', ?)",
                        (pk, srv, (now - ago).timestamp() - ac.COREDATA_EPOCH, text))
        con.commit()
        con.close()

    dbp = tmp_path / "store.sqlite"
    make_db(dbp, [(1, "s1", timedelta(minutes=30), "раз"), (2, "s2", timedelta(minutes=10), "два"),
                  (3, "s0", timedelta(days=5), "старьё до первого прогона")])
    cfg = {"mode": "sqlite", "db_path": str(dbp), "ts_format": "coredata",
           "query": "SELECT Z_PK AS seq, ZSRV AS server_id, Z_PK AS id, ZDATE AS ts, ZCHAT AS chat, "
                    "ZAUTHOR AS author, ZTEXT AS text FROM ZMSG WHERE Z_PK > :seq ORDER BY Z_PK",
           "ingest_url": ingest_server, "token": TOKEN, "source_id": "achat:seq", "initial_days": 1}
    assert ac.run(cfg) == {"rows": 2, "sent": 2}
    assert {r["external_id"] for r in events(admin, "achat:seq")} == {"srv:s1", "srv:s2"}
    assert json.loads((tmp_path / "achat.state").read_text())["seq"] == 3

    # приложение докачало старый чат: сообщения трёхдневной давности, но с новыми Z_PK
    con = sqlite3.connect(dbp)
    con.execute("INSERT INTO ZMSG VALUES (4, 's-old', ?, 'Архив', 'Анна', 'докачано')",
                ((now - timedelta(days=3)).timestamp() - ac.COREDATA_EPOCH,))
    con.commit()
    con.close()
    assert ac.run(cfg) == {"rows": 1, "sent": 1}
    old = [r for r in events(admin, "achat:seq") if r["external_id"] == "srv:s-old"]
    assert len(old) == 1 and old[0]["occurred_at"] < now - timedelta(days=2)

    # тихие выходные: нового нет, но last_ok_at обновился (нет ложной тревоги «А-Чат молчит»)
    admin.execute("UPDATE work.sources SET last_ok_at = NULL WHERE id = 'achat:seq'")
    assert ac.run(cfg) == {"rows": 0, "sent": 0}
    assert src(admin, "achat:seq")["last_ok_at"] is not None

    # переустановка А-Чата: та же переписка, но Z_PK с нуля и в другом порядке
    dbp.unlink()
    make_db(dbp, [(1, "s-old", timedelta(days=3), "докачано"), (2, "s2", timedelta(minutes=10), "два"),
                  (3, "s1", timedelta(minutes=30), "раз"), (4, "s3", timedelta(minutes=1), "новое")])
    res = ac.run(cfg)
    assert res["sent"] >= 1
    ids = [r["external_id"] for r in events(admin, "achat:seq")]
    assert sorted(ids) == ["srv:s-old", "srv:s1", "srv:s2", "srv:s3"]          # без дублей
    assert ac.run(cfg) == {"rows": 0, "sent": 0}


def test_achat_permission_error_is_explained(tmp_path, monkeypatch, capsys):
    ac = _achat_module()
    monkeypatch.setattr(ac, "STATE", tmp_path / "achat.state")
    cfgp = tmp_path / "achat.json"
    cfgp.write_text(json.dumps({"mode": "sqlite", "db_path": str(tmp_path / "x.sqlite"), "query": "SELECT 1",
                                "ingest_url": "http://127.0.0.1:9", "token": "t", "source_id": "achat:x"}))
    cfgp.chmod(0o600)

    def denied(path):
        raise ac.AccessDenied(f"нет доступа к {path} ([Errno 1] Operation not permitted)")
    monkeypatch.setattr(ac, "snapshot", denied)
    assert ac.main(["--config", str(cfgp)]) == 1
    out = capsys.readouterr().out
    assert "Full Disk Access" in out and "нет доступа" in out
    assert ac._is_access_error(sqlite3.OperationalError("unable to open database file"))
    assert not ac._is_access_error(sqlite3.OperationalError("no such table: ZMSG"))
    assert not (tmp_path / "achat.state").exists()


def test_achat_notifications_fallback(tmp_path):
    ac = _achat_module()
    dbp = tmp_path / "db"
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier TEXT)")
    con.execute("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, "
                "delivered_date REAL, data BLOB)")
    con.execute("INSERT INTO app VALUES (1, 'ru.alfa.achat'), (2, 'com.other')")
    now = datetime.now(UTC).timestamp() - ac.COREDATA_EPOCH
    blob = plistlib.dumps({"app": "ru.alfa.achat", "req": {"titl": "Команда", "subt": "Ян", "body": "срочно"}},
                          fmt=plistlib.FMT_BINARY)
    other = plistlib.dumps({"req": {"titl": "x", "body": "чужое"}}, fmt=plistlib.FMT_BINARY)
    con.execute("INSERT INTO record VALUES (1, 1, ?, ?, ?)", (b"\x01\x02", now - 60, blob))
    con.execute("INSERT INTO record VALUES (2, 2, ?, ?, ?)", (b"\x03", now - 60, other))
    con.commit()
    con.close()
    rows = ac.notification_rows({"bundle_id": "ru.alfa.achat", "db_path": str(dbp)},
                                datetime.now(UTC) - timedelta(hours=1))
    assert len(rows) == 1 and rows[0]["id"] == "notif:0102"
    assert rows[0]["chat"] == "Команда" and rows[0]["author"] == "Ян" and rows[0]["text"] == "срочно"
    assert ac.list_apps({"notif_db_path": str(dbp)}) == ["com.other", "ru.alfa.achat"]
    assert ac.to_dt(1_793_000_000_000) == datetime.fromtimestamp(1_793_000_000, UTC)   # мс распознаны

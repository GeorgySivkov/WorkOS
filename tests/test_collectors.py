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

    info = whoami(transport=t)
    assert info["me"]["username"] == "george" and info["boards"][0]["idOrganization"] == "O1"


# ---------------- Plaud ----------------
def test_plaud_transcript_ready_and_calendar_match(admin, mk_source, role_env, monkeypatch):
    from collectors.plaud import PlaudCollector
    monkeypatch.setenv("PLAUD_TOKEN", "bearer secret-token")
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
        "solo": {"id": "solo", "filename": "Заметка", "start_time": int(t_solo.timestamp() * 1000),
                 "duration": 120000, "is_trans": 0},
        "meet": {"id": "meet", "filename": "SDK sync", "start_time": int(t_meet.timestamp() * 1000),
                 "duration": 3000000, "is_trans": 1},
        "old": {"id": "old", "filename": "Давно", "start_time": int((now - timedelta(days=9)).timestamp() * 1000),
                "duration": 1000, "is_trans": 1},
    }
    details = {
        "meet": {"id": "meet", "trans_result": [
            {"speaker": "Ян", "content": "Привет.", "start_time": 0, "end_time": 1000},
            {"speaker": "Ян", "content": "Начнём.", "start_time": 1000, "end_time": 2000},
            {"speaker": "George", "content": "Давай.", "start_time": 2000, "end_time": 3000}],
            "ai_content": json.dumps({"markdown": "# Итоги\nрешили выпускать"})},
        "solo": {"id": "solo", "trans_result": [{"speaker": "", "content": "Купить корм", "start_time": 0}]},
    }
    asked: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "bearer secret-token"
        if req.url.path == "/file/simple/web":
            lst = sorted(recs.values(), key=lambda r: -r["start_time"])
            return httpx.Response(200, json={"status": 0, "data_file_list": lst})
        if req.url.path == "/file/list" and req.method == "POST":
            ids = json.loads(req.content)
            asked.append(ids)
            return httpx.Response(200, json={"data_file_list": [{**recs[i], **details[i]} for i in ids]})
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    s = mk_source("plaud:test", "plaud", None, lookback=3)
    r1 = run_source("plaud:test", PlaudCollector(s, transport=t))
    assert r1["inserted"] == 1 and asked == [["meet"]]       # не готовый транскрипт даже не запрашиваем
    ev = events(admin, "plaud:test")[0]
    assert ev["kind"] == "meeting" and ev["external_id"] == "meet" and ev["title"] == "SDK sync"
    assert ev["body"] == "Ян: Привет. Начнём.\nGeorge: Давай."
    assert ev["context_id"] == "infatica"
    assert ev["meta"]["calendar_event_id"] == cal_id and ev["meta"]["personal"] is False
    assert ev["meta"]["speakers"] == ["Ян", "George"] and ev["meta"]["segments_count"] == 3
    assert ev["meta"]["summary"].startswith("# Итоги") and ev["meta"]["participant_ids"] == []
    assert ev["occurred_at"] == t_meet

    # сведение людей проставило участников; транскрипт второй записи дозрел к следующему прогону
    admin.execute("UPDATE work.events SET meta = meta || '{\"participant_ids\": [7]}' WHERE external_id = 'meet'")
    recs["solo"]["is_trans"] = 1
    r2 = run_source("plaud:test", PlaudCollector(src(admin, "plaud:test"), transport=t))
    assert r2["inserted"] == 1
    rows = {e["external_id"]: e for e in events(admin, "plaud:test")}
    assert rows["meet"]["meta"]["participant_ids"] == [7]
    solo = rows["solo"]
    assert solo["context_id"] == "personal" and solo["meta"]["personal"] is True
    assert solo["meta"]["calendar_event_id"] is None and solo["body"] == "Спикер: Купить корм"
    assert "old" not in rows


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
    feed = {"text": ICS.replace("{count}", "4").replace("{single}", SINGLE)}

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

    # повторный прогон той же ленты ничего не меняет
    r2 = run_source("ical:test", ICalCollector(src(admin, "ical:test"), transport=t, now=now))
    assert r2["inserted"] == 0 and r2["updated"] == 0

    # разовую встречу удалили, серию укоротили: исчезнувшее стало cancelled, строки на месте
    feed["text"] = ICS.replace("{count}", "3").replace("{single}", "")
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
    history = [
        {"type": "message", "user": "U1", "text": "всем привет", "ts": T1},
        {"type": "message", "user": "U2", "text": "релиз SDK", "ts": T2, "thread_ts": T2, "reply_count": 1,
         "latest_reply": T3},
        {"type": "message", "subtype": "channel_join", "user": "U3", "text": "joined", "ts": ts(45)},
    ]
    replies = {
        ("C1", T2): [history[1], {"type": "message", "user": "U1", "text": "<@UME> глянь", "ts": T3, "thread_ts": T2}],
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
    assert r1["inserted"] == 5
    rows = {e["external_id"]: e for e in events(admin, "slack:test")}
    assert set(rows) == {f"C1:{T1}", f"C1:{T2}", f"C1:{T3}", f"C9:{T5}", f"C9:{T6}"}
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

    # второй прогон: всё уже есть, треды перечитаны, дублей нет
    r2 = run_source("slack:test", SlackCollector(src(admin, "slack:test"), transport=t, sleep=slept.append))
    assert r2["inserted"] == 0
    assert len(events(admin, "slack:test")) == 5


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
    world = {"profile_history": "100", "listed": ["m1", "m2"]}
    seen: list[str] = []

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
            return httpx.Response(200, json={"historyId": world["profile_history"]})
        if p == "/messages":
            assert q["q"] == "newer_than:2d"
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
    assert r1["inserted"] == 2
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

    # метка устарела (404): откат на полный перечит без дублей
    world.update(profile_history="130")
    r3 = run_source("gmail:test", GmailCollector(src(admin, "gmail:test"), transport=t))
    assert r3["inserted"] == 0
    wm = json.loads(src(admin, "gmail:test")["watermark"])
    assert wm["history_id"] == "130" and wm["mode"] == "full"
    assert len(events(admin, "gmail:test")) == 3


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

    # больше 2 МБ: отказ до чтения тела
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("POST", "/ingest/achat:test")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", str(3 * 1024 * 1024))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


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

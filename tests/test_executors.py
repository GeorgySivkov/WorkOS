"""Исполнители (шаги 2.5, 2.9, 5.1-5.3): контракт очереди, Trello, Gmail, Slack, календарь.

Код исполнителей работает под ролью workos_exec (role_env), фикстуры вставляет админ.
Внешние API: только через httpx.MockTransport, сети нет.
"""
from __future__ import annotations

import ast
import base64
import email
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from email import policy
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from psycopg.types.json import Jsonb

from core import db
from core import timeutil as tu
from executors import base
from executors import calendar as gcal

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent


# ---------- поддельные внешние сервисы ----------
def _iso_now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + offset).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ev_time(part: dict) -> datetime:
    if part.get("dateTime"):
        return base.parse_iso(part["dateTime"])
    return datetime.fromisoformat(part["date"]).replace(tzinfo=UTC)


class Fake:
    """Google (OAuth, Calendar, Gmail), Trello и Slack в памяти."""

    def __init__(self):
        self.cals: dict[str, dict[str, dict]] = defaultdict(dict)
        self.seq = 0
        self.calls: list[tuple[str, str, dict, object]] = []
        self.page_size = 1000
        self.slack_429 = 0
        self.drafts: list[dict] = []

    # -- календарь: прямые правки «руками» --
    def put(self, cal: str, ev: dict) -> dict:
        self.seq += 1
        ev = {"status": "confirmed", "updated": _iso_now(), **ev, "_seq": self.seq}
        self.cals[cal][ev["id"]] = ev
        return ev

    def manual(self, cal: str, eid: str, **fields) -> None:
        """George что-то поменял в Google после сверки: updated чуть в будущем."""
        self.seq += 1
        self.cals[cal][eid].update(fields, updated=_iso_now(timedelta(seconds=5)), _seq=self.seq)

    def writes(self, method: str | None = None) -> list:
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method)
                and "oauth2" not in c[1]]

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- маршрутизация --
    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode()
        params = dict(url.params)
        self.calls.append((request.method, f"{url.host}{url.path}", params, body))
        if url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if url.host == "www.googleapis.com":
            return self._calendar(request, params, body)
        if url.host == "gmail.googleapis.com":
            assert request.headers["Authorization"] == "Bearer at-1"
            if url.path.endswith("/drafts") and request.method == "POST":
                self.drafts.append(body)
                tid = body["message"].get("threadId") or "thread-new"
                return httpx.Response(200, json={"id": "draft-1", "message": {"id": "msg-1", "threadId": tid}})
            return httpx.Response(404, json={"error": "unexpected gmail call"})
        if url.host == "api.trello.com":
            return self._trello(request, body)
        if url.host == "slack.com":
            if self.slack_429:
                self.slack_429 -= 1
                return httpx.Response(429, headers={"Retry-After": "3"}, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": "1700000000.000100"})
        return httpx.Response(404)

    def _trello(self, request, body):
        assert 'oauth_token="tw"' in request.headers["Authorization"]
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/board"):
            return httpx.Response(200, json={"id": "board-1"})
        if m == "GET" and p == "/1/boards/board-1/labels":
            return httpx.Response(200, json=[{"id": "lab-sdk", "name": "SDK", "color": "blue"},
                                             {"id": "lab-proxy", "name": "Proxy", "color": "red"}])
        if m == "POST" and p == "/1/cards":
            return httpx.Response(200, json={"id": "card-1", "shortUrl": "https://trello.com/c/abc"})
        if m == "POST" and p == "/1/cards/card-1/checklists":
            return httpx.Response(200, json={"id": "cl-1"})
        if m == "POST" and p == "/1/checklists/cl-1/checkItems":
            return httpx.Response(200, json={"id": "item"})
        return httpx.Response(404, text="no route")

    @staticmethod
    def _public(ev: dict) -> dict:
        out = {k: v for k, v in ev.items() if not k.startswith("_")}
        return out

    def _calendar(self, request, params, body):
        segs = [unquote(s) for s in request.url.raw_path.decode().split("?")[0].split("/")]
        # /calendar/v3/calendars/{cal}/events[/{id}]
        cal = segs[4]
        eid = segs[6] if len(segs) > 6 else None
        store = self.cals[cal]
        m = request.method
        if m == "GET" and eid is None:
            return self._list(store, params)
        if m == "POST":
            if body["id"] in store:
                return httpx.Response(409, json={"error": {"code": 409, "message": "The requested identifier already exists."}})
            return httpx.Response(200, json=self._public(self.put(cal, body)))
        ev = store.get(eid)
        if ev is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        if m == "GET":
            return httpx.Response(200, json=self._public(ev))
        if m == "PATCH":
            for k, v in body.items():
                if k == "extendedProperties":
                    ev.setdefault("extendedProperties", {}).setdefault("private", {}).update(v.get("private", {}))
                else:
                    ev[k] = v
            self.seq += 1
            ev.update(updated=_iso_now(), _seq=self.seq)
            return httpx.Response(200, json=self._public(ev))
        if m == "DELETE":
            if ev["status"] == "cancelled":
                return httpx.Response(410, json={"error": {"code": 410}})
            self.seq += 1
            ev.update(status="cancelled", updated=_iso_now(), _seq=self.seq)
            return httpx.Response(204)
        return httpx.Response(405)

    def _list(self, store, params):
        evs = sorted(store.values(), key=lambda e: e["_seq"])
        if "syncToken" in params:
            if not params["syncToken"].isdigit():
                return httpx.Response(410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}})
            evs = [e for e in evs if e["_seq"] > int(params["syncToken"])]
        if "updatedMin" in params:
            lo = base.parse_iso(params["updatedMin"])
            evs = [e for e in evs if base.parse_iso(e["updated"]) >= lo]
        if params.get("showDeleted", "false") != "true" and "syncToken" not in params and "updatedMin" not in params:
            evs = [e for e in evs if e["status"] != "cancelled"]
        if "privateExtendedProperty" in params:
            k, v = params["privateExtendedProperty"].split("=", 1)
            evs = [e for e in evs if (e.get("extendedProperties") or {}).get("private", {}).get(k) == v]
        if "timeMin" in params:
            lo, hi = base.parse_iso(params["timeMin"]), base.parse_iso(params["timeMax"])
            evs = [e for e in evs if _ev_time(e["end"]) > lo and _ev_time(e["start"]) < hi]
        off = int(params.get("pageToken", 0))
        page = evs[off:off + self.page_size]
        items = []
        for e in page:
            if e["status"] == "cancelled":   # как в Google: у удалённых почти ничего нет
                items.append({"id": e["id"], "status": "cancelled", "updated": e["updated"]})
            else:
                items.append(self._public(e))
        data = {"items": items}
        if off + self.page_size < len(evs):
            data["nextPageToken"] = str(off + self.page_size)
        else:
            data["nextSyncToken"] = str(self.seq)
        return httpx.Response(200, json=data)


@pytest.fixture
def fake(role_env, monkeypatch, tmp_path):
    role_env("workos_exec")
    for k, v in {"TRELLO_KEY": "tk", "TRELLO_WRITE_TOKEN": "tw", "SLACK_WRITE_TOKEN": "xoxb-1",
                 "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
                 "GOOGLE_WRITE_REFRESH_TOKEN": "rt", "EXEC_STATE_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    for k in ("TRELLO_DRAFT_LIST_ID", "GMAIL_WRITE_TOKENS", "GCAL_MIRROR_MAP", "GCAL_PLAN_CALENDAR"):
        monkeypatch.delenv(k, raising=False)
    slept: list[float] = []
    monkeypatch.setattr(base, "sleep", slept.append)
    f = Fake()
    f.slept = slept
    base.set_transport(httpx.MockTransport(f.handle))
    gcal._STATE_MEM.clear()
    yield f
    base.set_transport(None)


def add_action(admin, type_, payload, status="approved", good_hash=True, context="infatica"):
    aid = admin.execute("INSERT INTO work.actions (context_id, type, payload, status, approved_at) "
                        "VALUES (%s, %s, %s, %s, now()) RETURNING id",
                        (context, type_, Jsonb(payload), status)).fetchone()["id"]
    h = db.payload_hash_sql() if good_hash else "'deadbeef'"
    admin.execute(f"UPDATE work.actions SET approved_hash = {h} WHERE id = %s", (aid,))
    return aid


def action(admin, aid):
    return admin.execute("SELECT * FROM work.actions WHERE id = %s", (aid,)).fetchone()


def outbox(admin):
    return admin.execute("SELECT topic, text, ref_action, created_by FROM work.outbox ORDER BY id").fetchall()


CARD = {"list_id": "list-1", "name": "Prepare SDK report", "desc": "Context: SDK.\n\nDoD: report sent.",
        "labels": ["SDK", "nope"], "checklist": {"name": "Steps", "items": ["Collect data", "Write"]},
        "due": "2026-11-02T09:00:00Z"}


# ---------- граница: никакой модели ----------
def test_executors_never_import_llm():
    for path in (ROOT / "executors").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any("llm" in a.name for a in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert "llm" not in (node.module or ""), path.name
                assert not any(a.name == "llm" for a in node.names), path.name
    code = ("import sys, executors.base, executors.trello, executors.gmail, executors.slack, "
            "executors.calendar, executors.__main__; "
            "sys.exit(1 if 'core.llm' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env=dict(os.environ, WORKOS_ENV_FILE="/nonexistent"))
    assert r.returncode == 0, r.stderr


# ---------- контракт очереди (5.1) ----------
def test_hash_mismatch_is_not_executed(admin, fake):
    aid = add_action(admin, "slack_post", {"channel": "C1", "text": "hi"}, good_hash=False)
    assert base.run_queue(once=True) == {"executed": 0, "drafted": 0, "failed": 0}
    row = action(admin, aid)
    assert row["status"] == "approved" and row["executed_at"] is None
    assert fake.calls == []


def test_payload_edited_after_approval_is_not_executed(admin, fake):
    aid = add_action(admin, "slack_post", {"channel": "C1", "text": "одобренный текст"})
    admin.execute("""UPDATE work.actions SET payload = '{"channel": "C1", "text": "подменённый"}' WHERE id = %s""",
                  (aid,))
    base.run_queue(once=True)
    assert action(admin, aid)["status"] == "approved"
    assert fake.calls == []


def test_only_executor_types_and_statuses(admin, fake):
    a1 = add_action(admin, "plan_batch", {"for_date": "2026-11-01", "items": [], "week_items": []})
    a2 = add_action(admin, "slack_post", {"channel": "C1", "text": "x"}, status="proposed")
    base.run_queue(once=True)
    assert action(admin, a1)["status"] == "approved" and action(admin, a2)["status"] == "proposed"
    assert fake.calls == []


@pytest.mark.parametrize("payload, needle", [
    ({**CARD, "extra": 1}, "лишнее поле extra"),
    ({**CARD, "labels": "SDK"}, "labels"),
    ({k: v for k, v in CARD.items() if k != "name"}, "нет обязательного поля name"),
    ({**CARD, "checklist": {"name": "x", "items": [1]}}, "checklist.items[0]"),
    ({**CARD, "due": "завтра"}, "due"),
    ({**CARD, "name": "a\nb"}, "перевод строки"),
])
def test_schema_violation_fails_with_notice(admin, fake, payload, needle):
    aid = add_action(admin, "trello_card", payload)
    assert base.run_queue(once=True)["failed"] == 1
    row = action(admin, aid)
    assert row["status"] == "failed" and needle in row["error"]
    msgs = outbox(admin)
    assert len(msgs) == 1 and msgs[0]["topic"] == "decisions" and msgs[0]["ref_action"] == aid
    assert msgs[0]["text"].startswith("Не получилось:") and msgs[0]["created_by"] == "workos_exec"
    assert "—" not in msgs[0]["text"]
    assert fake.calls == []


def test_validator_accepts_nulls_for_optional():
    base.validate("slack_post", {"channel": "C1", "text": "x", "thread_ts": None})
    base.validate("email_reply", {"account": "personal", "to": ["a@b.c"], "cc": [], "subject": "s",
                                  "body": "b", "references": ["<a@b>", "<c@d>"]})
    with pytest.raises(base.SchemaError):
        base.validate("slack_post", {"channel": "C1", "text": 5})
    with pytest.raises(base.SchemaError):
        base.validate("email_reply", {"account": "personal", "to": [], "cc": [], "subject": "s", "body": "b"})


def test_external_error_marks_failed(admin, fake, monkeypatch):
    monkeypatch.delenv("SLACK_WRITE_TOKEN")
    aid = add_action(admin, "slack_post", {"channel": "C1", "text": "x"})
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == "failed" and "SLACK_WRITE_TOKEN" in row["error"]
    assert "Не получилось: сообщение в Slack" in outbox(admin)[0]["text"]


# ---------- Trello (5.2) ----------
def test_trello_card_happy_path(admin, fake):
    aid = add_action(admin, "trello_card", CARD)
    assert base.run_queue(once=True)["executed"] == 1
    row = action(admin, aid)
    assert row["status"] == "executed" and row["executed_at"] is not None and row["error"] is None
    assert row["result"]["card_id"] == "card-1" and row["result"]["url"] == "https://trello.com/c/abc"
    assert row["result"]["unknown_labels"] == ["nope"] and row["result"]["checklist_id"] == "cl-1"
    posts = fake.writes("POST")
    card = next(b for _, p, _, b in posts if p.endswith("/1/cards"))
    assert card == {"idList": "list-1", "name": "Prepare SDK report", "desc": CARD["desc"],
                    "due": "2026-11-02T09:00:00Z", "idLabels": "lab-sdk"}
    items = [b["name"] for _, p, _, b in posts if p.endswith("/checkItems")]
    assert items == ["Collect data", "Write"]
    assert all("tw" not in str(params) for _, _, params, _ in fake.calls)   # токен не в URL
    assert "nope" in outbox(admin)[0]["text"]


def test_trello_drafted_prefix_or_draft_list(admin, fake, monkeypatch):
    payload = {**CARD, "labels": [], "checklist": None}
    aid = add_action(admin, "trello_card", payload, status="drafted")
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == "drafted" and row["executed_at"] is not None
    card = fake.writes("POST")[0][3]
    assert card["name"] == "[черновик] Prepare SDK report" and card["idList"] == "list-1"

    monkeypatch.setenv("TRELLO_DRAFT_LIST_ID", "drafts-list")
    fake.reset_calls()
    add_action(admin, "trello_card", payload, status="drafted")
    base.run_queue(once=True)
    card = fake.writes("POST")[0][3]
    assert card["name"] == "Prepare SDK report" and card["idList"] == "drafts-list"


# ---------- Gmail (5.3) ----------
def _decode(raw: str) -> tuple[bytes, email.message.EmailMessage]:
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return data, email.message_from_bytes(data, policy=policy.default)


@pytest.mark.parametrize("status, final", [("approved", "executed"), ("drafted", "drafted")])
def test_gmail_always_creates_draft(admin, fake, status, final):
    payload = {"account": "personal", "to": ["Иван Петров <ivan@example.com>"], "cc": ["boss@example.com"],
               "subject": "Re: Отчёт по SDK", "body": "Привет!\nОтчёт во вложении будет завтра.",
               "thread_id": "thr-1", "in_reply_to": "CAF123@mail.gmail.com"}
    aid = add_action(admin, "email_reply", payload, status=status)
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == final and row["executed_at"] is not None
    assert row["result"] == {"draft_id": "draft-1", "message_id": "msg-1", "thread_id": "thr-1",
                             "account": "personal"}
    gmail_calls = [c for c in fake.calls if c[1].startswith("gmail.")]
    assert [c[1] for c in gmail_calls] == ["gmail.googleapis.com/gmail/v1/users/me/drafts"]  # не send
    msg = fake.drafts[0]["message"]
    assert msg["threadId"] == "thr-1"
    data, parsed = _decode(msg["raw"])
    assert b"=?utf-8?" in data and "Отчёт".encode() not in data.split(b"\r\n\r\n")[0]
    assert parsed["Subject"] == "Re: Отчёт по SDK"
    assert parsed["To"].addresses[0].display_name == "Иван Петров"
    assert parsed["Cc"] == "boss@example.com"
    assert parsed["In-Reply-To"] == "<CAF123@mail.gmail.com>"
    assert parsed["References"] == "<CAF123@mail.gmail.com>"
    assert parsed.get_content().replace("\r\n", "\n").strip() == payload["body"]   # RFC 2822: CRLF


def test_gmail_unknown_account_fails(admin, fake, monkeypatch):
    monkeypatch.setenv("GMAIL_WRITE_TOKENS", json.dumps({"infatica": "GMAIL_INFATICA_WRITE_REFRESH_TOKEN"}))
    aid = add_action(admin, "email_reply", {"account": "alfa", "to": ["a@b.c"], "cc": [], "subject": "s",
                                            "body": "b"})
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == "failed" and "alfa" in row["error"]
    from executors import gmail
    assert gmail.token_env("gmail:infatica") == "GMAIL_INFATICA_WRITE_REFRESH_TOKEN"
    assert gmail.token_env("personal") == "GOOGLE_WRITE_REFRESH_TOKEN"


# ---------- Slack ----------
def test_slack_post_with_rate_limit(admin, fake):
    fake.slack_429 = 1
    aid = add_action(admin, "slack_post", {"channel": "C1", "text": "Готово", "thread_ts": "171.1"})
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == "executed" and row["result"] == {"channel": "C1", "ts": "1700000000.000100"}
    assert fake.slept == [3.0]
    assert fake.writes("POST")[-1][3] == {"channel": "C1", "text": "Готово", "thread_ts": "171.1"}


def test_slack_drafted_does_not_post(admin, fake):
    aid = add_action(admin, "slack_post", {"channel": "C1", "text": "Текст <b>с тегом</b>"}, status="drafted")
    base.run_queue(once=True)
    row = action(admin, aid)
    assert row["status"] == "drafted" and row["result"]["draft"] is True and row["executed_at"]
    assert fake.calls == []
    msg = outbox(admin)[0]
    assert msg["topic"] == "decisions" and msg["ref_action"] == aid
    assert "<pre>Текст &lt;b&gt;с тегом&lt;/b&gt;</pre>" in msg["text"]


def test_cli_queue_once_under_exec_role():
    r = subprocess.run([sys.executable, "-m", "executors", "queue", "--once"], cwd=ROOT, capture_output=True,
                       text=True, env=dict(os.environ, PGUSER="workos_exec", WORKOS_ENV_FILE="/nonexistent"))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"executed": 0, "drafted": 0, "failed": 0}


# ---------- календарь: зеркало (2.5) ----------
SOURCES = [("ical:infatica", "infatica"), ("eas:alfa", "alfa")]
INF, ALFA = "inf@group.calendar.google.com", "alfa@group.calendar.google.com"


@pytest.fixture
def mirror_env(admin, fake, monkeypatch):
    for sid, ctx in SOURCES:
        admin.execute("INSERT INTO work.sources (id, context_id, kind) VALUES (%s, %s, 'calendar') "
                      "ON CONFLICT (id) DO NOTHING", (sid, ctx))
    monkeypatch.setenv("GCAL_MIRROR_MAP", json.dumps({"infatica": INF, "alfa": ALFA}))
    return fake


def meeting(admin, source, ctx, uid, title, start, minutes=60, **meta):
    m = {"ends_at": (start + timedelta(minutes=minutes)).isoformat(), "uid": uid, "recurrence_id": "",
         "status": "confirmed", "busy": True, "attendees": [{"email": "boss@corp", "name": "Boss"}], **meta}
    admin.execute("INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, body, meta) "
                  "VALUES (%s, %s, %s, 'calendar', %s, %s, 'секретная повестка', %s)",
                  (ctx, source, uid + (m["recurrence_id"] or ""), start, title, Jsonb(m)))
    return gcal.mirror_id(source, uid, m["recurrence_id"])


def test_mirror_id_format():
    mid = gcal.mirror_id("ical:infatica", "uid-1", "")
    assert len(mid) == 32 and set(mid) <= set("0123456789abcdefghijklmnopqrstuv")
    assert mid != gcal.mirror_id("ical:infatica", "uid-1", "2026-11-02T10:00:00+00:00")
    assert gcal.block_id(7) == gcal.mirror_id("task", "7", "")


def test_mirror_insert_update_restore_and_delete_orphans(admin, mirror_env):
    fake = mirror_env
    now = tu.now().replace(microsecond=0)
    t1 = now + timedelta(days=1)
    m1 = meeting(admin, "ical:infatica", "infatica", "u1", "Синк по SDK", t1, location="Zoom")
    m2 = meeting(admin, "eas:alfa", "alfa", "u2", "Планёрка", t1 + timedelta(hours=3), busy=False)
    m3 = meeting(admin, "eas:alfa", "alfa", "u3", "Отменённая", t1, status="cancelled")
    meeting(admin, "ical:infatica", "infatica", "u4", "Далеко", now + timedelta(days=90))
    m5 = meeting(admin, "eas:alfa", "alfa", "u5", "Конференция",
                 datetime.combine(tu.local(t1).date(), time(0), tzinfo=UTC), minutes=24 * 60, all_day=True)
    # в календарях уже лежат: сирота зеркала, отменённая встреча и чужое событие без метки
    win = {"start": {"dateTime": t1.isoformat()}, "end": {"dateTime": (t1 + timedelta(hours=1)).isoformat()}}
    fake.put(INF, {"id": "orphan0000", "summary": "Старое", **win,
                   "extendedProperties": {"private": {"workos": "mirror", "src": "ical:infatica"}}})
    fake.put(INF, {"id": "personal001", "summary": "Личное", **win})
    fake.put(ALFA, {"id": m3, "summary": "Отменённая", **win,
                    "extendedProperties": {"private": {"workos": "mirror", "src": "eas:alfa"}}})
    fake.page_size = 1          # постраничный список тоже работает
    res = gcal.mirror()
    assert res["inserted"] == 3 and res["deleted"] == 2 and res["errors"] == []

    ev1 = fake.cals[INF][m1]
    assert ev1["summary"] == "Синк по SDK" and ev1["location"] == "Zoom" and ev1["transparency"] == "opaque"
    assert ev1["start"]["timeZone"] == "Europe/Madrid" and base.parse_iso(ev1["start"]["dateTime"]) == t1
    assert ev1["extendedProperties"]["private"] == {"workos": "mirror", "src": "ical:infatica"}
    assert "attendees" not in ev1 and "description" not in ev1
    assert fake.cals[ALFA][m2]["transparency"] == "transparent"
    d = tu.local(t1).date()
    assert fake.cals[ALFA][m5]["start"] == {"date": d.isoformat()}
    assert fake.cals[ALFA][m5]["end"] == {"date": (d + timedelta(days=1)).isoformat()}
    assert fake.cals[INF]["orphan0000"]["status"] == "cancelled"
    assert fake.cals[ALFA][m3]["status"] == "cancelled"
    assert fake.cals[INF]["personal001"]["status"] == "confirmed"      # без метки не трогаем
    assert len(fake.cals[INF]) == 3                                   # «Далеко» не в окне

    # повтор ничего не пишет
    fake.reset_calls()
    res = gcal.mirror()
    assert res["unchanged"] == 3 and fake.writes() == []

    # перенос встречи: тот же id, новое время, PATCH
    admin.execute("UPDATE work.events SET occurred_at = occurred_at + interval '2 hours', "
                  "meta = jsonb_set(meta, '{ends_at}', to_jsonb((occurred_at + interval '3 hours')::text)) "
                  "WHERE external_id = 'u1'")
    res = gcal.mirror()
    assert res["updated"] == 1 and res["inserted"] == 0
    assert base.parse_iso(fake.cals[INF][m1]["start"]["dateTime"]) == t1 + timedelta(hours=2)

    # копию удалили (руками или раньше): 409 при вставке -> GET -> PATCH confirmed
    fake.cals[ALFA][m2]["status"] = "cancelled"
    fake.reset_calls()
    res = gcal.mirror()
    assert res["restored"] == 1
    assert [c[0] for c in fake.writes()] == ["POST", "PATCH"]
    assert fake.cals[ALFA][m2]["status"] == "confirmed"


def test_mirror_never_touches_unmarked_event_with_same_id(admin, mirror_env):
    fake = mirror_env
    t1 = tu.now() + timedelta(days=2)
    mid = meeting(admin, "ical:infatica", "infatica", "u9", "Встреча", t1)
    fake.put(INF, {"id": mid, "summary": "Чужое", "start": {"dateTime": t1.isoformat()},
                   "end": {"dateTime": (t1 + timedelta(hours=1)).isoformat()}})
    res = gcal.mirror()
    assert res["foreign"] == 1 and fake.cals[INF][mid]["summary"] == "Чужое"
    assert fake.writes("PATCH") == [] and fake.writes("DELETE") == []


# ---------- календарь: блоки плана (2.9) ----------
PLAN = "plan@group.calendar.google.com"


def add_task(admin, title, start=None, minutes=60, **cols):
    end = start + timedelta(minutes=minutes) if start else None
    fields = {"title": title, "source": "app", "planned_start": start, "planned_end": end,
              "planned_by": "auto" if start else None, "planned_at": tu.now() if start else None, **cols}
    keys = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    return admin.execute(f"INSERT INTO work.tasks ({keys}) VALUES ({marks}) RETURNING id",
                         tuple(fields.values())).fetchone()["id"]


def task(admin, tid):
    return admin.execute("SELECT * FROM work.tasks WHERE id = %s", (tid,)).fetchone()


@pytest.fixture
def plan_env(fake, monkeypatch):
    monkeypatch.setenv("GCAL_PLAN_CALENDAR", PLAN)
    return fake


def at_today(h, m=0, days=0):
    return tu.at_local(tu.logical_today() + timedelta(days=days), time(h, m))


def test_blocks_write_update_delete(admin, plan_env, tmp_path):
    fake = plan_env
    t1 = add_task(admin, "Отчёт по SDK", at_today(10))
    t2 = add_task(admin, "Позвонить в банк", at_today(12, days=1), minutes=30, done_at=tu.now())
    t3 = add_task(admin, "Ревью", at_today(15))
    add_task(admin, "Без времени")
    add_task(admin, "Через месяц", at_today(10, days=30))
    res = gcal.blocks()
    assert res["inserted"] == 3 and res["errors"] == []
    b1, b2, b3 = gcal.block_id(t1), gcal.block_id(t2), gcal.block_id(t3)
    ev = fake.cals[PLAN][b1]
    assert ev["summary"] == "Отчёт по SDK" and base.parse_iso(ev["start"]["dateTime"]) == at_today(10)
    assert ev["extendedProperties"]["private"] == {"workos": "block", "task_id": str(t1)}
    assert fake.cals[PLAN][b2]["summary"] == "✓ Позвонить в банк"
    assert all(task(admin, t)["block_synced_at"] for t in (t1, t2, t3))
    state = json.loads((tmp_path / "gcal_state.json").read_text())
    assert state["plan"][PLAN]["sync_token"]

    # повтор ничего не пишет и читает изменения по syncToken
    fake.reset_calls()
    res = gcal.blocks()
    assert res["unchanged"] == 3 and fake.writes() == []
    assert any("syncToken" in c[2] for c in fake.calls)

    # расстановщик сдвинул задачу, задачу сбросили и удалили
    admin.execute("UPDATE work.tasks SET planned_start = planned_start + interval '1 hour', "
                  "planned_end = planned_end + interval '1 hour', planned_at = now() WHERE id = %s", (t1,))
    admin.execute("UPDATE work.tasks SET dropped_at = now() WHERE id = %s", (t2,))
    admin.execute("UPDATE work.tasks SET planned_start = NULL, planned_end = NULL, planned_at = now() "
                  "WHERE id = %s", (t3,))
    res = gcal.blocks()
    assert res["updated"] == 1 and res["deleted"] == 2 and res["manual_deleted"] == 0
    assert base.parse_iso(fake.cals[PLAN][b1]["start"]["dateTime"]) == at_today(11)
    assert fake.cals[PLAN][b2]["status"] == "cancelled" and fake.cals[PLAN][b3]["status"] == "cancelled"
    assert task(admin, t2)["planned_start"] is not None     # наше удаление не принято за ручное

    # отметка «сделано» в приложении: блок помечается
    admin.execute("UPDATE work.tasks SET done_at = now() WHERE id = %s", (t1,))
    res = gcal.blocks()
    assert res["updated"] == 1 and fake.cals[PLAN][b1]["summary"] == "✓ Отчёт по SDK"


def test_blocks_manual_move_and_delete_sync_back(admin, plan_env):
    fake = plan_env
    t1 = add_task(admin, "Отчёт", at_today(10))
    t2 = add_task(admin, "Почта", at_today(14), minutes=30)
    gcal.blocks()
    b1, b2 = gcal.block_id(t1), gcal.block_id(t2)
    # George перетащил блок на 16:00 и удалил второй
    fake.manual(PLAN, b1, start={"dateTime": tu.local(at_today(16)).isoformat(), "timeZone": "Europe/Madrid"},
                end={"dateTime": tu.local(at_today(17)).isoformat(), "timeZone": "Europe/Madrid"})
    fake.manual(PLAN, b2, status="cancelled")
    fake.reset_calls()
    res = gcal.blocks()
    assert res["manual_moved"] == 1 and res["manual_deleted"] == 1
    r1, r2 = task(admin, t1), task(admin, t2)
    assert r1["planned_start"] == at_today(16) and r1["planned_end"] == at_today(17)
    assert r1["planned_by"] == "manual" and r1["block_synced_at"] >= r1["planned_at"]
    assert r2["planned_start"] is None and r2["planned_end"] is None and r2["planned_by"] is None
    assert r2["block_synced_at"] is not None
    # ручное не перезаписано обратно и удалённый блок не воскрес
    assert fake.writes() == []
    assert fake.cals[PLAN][b2]["status"] == "cancelled"
    assert base.parse_iso(fake.cals[PLAN][b1]["start"]["dateTime"]) == at_today(16)

    # следующий проход спокоен
    fake.reset_calls()
    res = gcal.blocks()
    assert res["manual_moved"] == 0 and fake.writes() == []


def test_blocks_manual_move_found_by_window_when_token_lost(admin, plan_env, tmp_path):
    """Файл состояния потерян (или ещё не записан): перенос всё равно принят."""
    fake = plan_env
    t1 = add_task(admin, "Отчёт", at_today(10))
    gcal.blocks()
    (tmp_path / "gcal_state.json").write_text(json.dumps({"plan": {PLAN: {"sync_token": "bogus"}}}))
    gcal._STATE_MEM.clear()
    fake.manual(PLAN, gcal.block_id(t1), start={"dateTime": at_today(18).isoformat()},
                end={"dateTime": at_today(19).isoformat()})
    fake.reset_calls()
    res = gcal.blocks()
    assert res["manual_moved"] == 1 and task(admin, t1)["planned_start"] == at_today(18)
    lists = [c[2] for c in fake.calls if c[0] == "GET" and "privateExtendedProperty" not in c[2]]
    assert lists[0]["syncToken"] == "bogus" and "syncToken" not in lists[1]   # 410 -> полный список
    state = json.loads((tmp_path / "gcal_state.json").read_text())
    assert state["plan"][PLAN]["sync_token"].isdigit()


def test_blocks_updated_min_fallback_without_token(admin, plan_env, tmp_path):
    """Google не выдал токен: изменения с прошлого прохода по updatedMin."""
    fake = plan_env
    t1 = add_task(admin, "Отчёт", at_today(10))
    gcal.blocks()
    since = gcal.rfc(tu.now() - timedelta(minutes=5))
    (tmp_path / "gcal_state.json").write_text(json.dumps({"plan": {PLAN: {"pulled_at": since}}}))
    gcal._STATE_MEM.clear()
    fake.manual(PLAN, gcal.block_id(t1), status="cancelled")
    fake.reset_calls()
    res = gcal.blocks()
    assert res["manual_deleted"] == 1 and task(admin, t1)["planned_start"] is None
    assert [c[2].get("updatedMin") for c in fake.calls if c[0] == "GET"][0] == since


def test_newer_manual_change_in_db_beats_older_google_move(admin, plan_env):
    fake = plan_env
    t1 = add_task(admin, "Отчёт", at_today(10))
    gcal.blocks()
    fake.manual(PLAN, gcal.block_id(t1), start={"dateTime": at_today(18).isoformat()},
                end={"dateTime": at_today(19).isoformat()})
    # позже George сам перенёс задачу в мини-приложении
    admin.execute("UPDATE work.tasks SET planned_start = %s, planned_end = %s, planned_by = 'manual', "
                  "planned_at = now() + interval '1 minute' WHERE id = %s", (at_today(20), at_today(21), t1))
    res = gcal.blocks()
    assert res["manual_moved"] == 0 and res["updated"] == 1
    assert base.parse_iso(fake.cals[PLAN][gcal.block_id(t1)]["start"]["dateTime"]) == at_today(20)


def test_calendar_run_reports_and_alerts(admin, plan_env, monkeypatch):
    monkeypatch.setenv("GCAL_MIRROR_MAP", "not json")
    add_task(admin, "Отчёт", at_today(10))
    res = gcal.run()
    assert res["blocks"]["inserted"] == 1 and res["mirror"]["errors"]
    msgs = outbox(admin)
    assert len(msgs) == 1 and msgs[0]["topic"] == "system" and "Зеркало" in msgs[0]["text"]
    gcal.run()
    assert len(outbox(admin)) == 1                                    # не чаще раза в 6 часов


def test_google_rate_limit_backoff(fake):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        return httpx.Response(200, json={"items": []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        r = base.request(http, "GET", "https://www.googleapis.com/calendar/v3/calendars/x/events")
    assert r.status_code == 200 and calls["n"] == 3 and len(fake.slept) == 2

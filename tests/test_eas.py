"""Шаг 2.4: WBXML-кодек и клиент Exchange ActiveSync Альфы против фальшивого сервера."""
from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from collectors import eas, wbxml
from collectors.base import run_source
from collectors.wbxml import Node, decode, el, encode

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)          # вторник; 25.10 Европа переводит часы
USER = "MOSCOW\\U_TEST"
PASSWORD = "секрет-1"
DEVICE = "WorkOS7F3A9C21D4E5"

# W. Europe: зимой UTC+1, летом UTC+2; переход в последнее воскресенье марта 2:00 и октября 3:00
BERLIN = eas.make_timezone_blob(-60, std=(0, 10, 0, 5, 3, 0, 0, 0), dst=(0, 3, 0, 5, 2, 0, 0, 0),
                                name="W. Europe Standard Time")
MOSCOW = eas.make_timezone_blob(-180, name="Russian Standard Time")


# ---------------------------------------------------------------- WBXML
def _s(t: str) -> bytes:
    return b"\x03" + t.encode() + b"\x00"


# MS-ASWBXML, раздел 3 «Algorithm Examples»: тот же поток байт, что в спецификации
SPEC_SAMPLE = (bytes([0x03, 0x01, 0x6A, 0x00, 0x45, 0x5C, 0x4F, 0x50]) + _s("Contacts") + b"\x01"
               + b"\x4B" + _s("2") + b"\x01" + b"\x52" + _s("2") + b"\x01" + b"\x4E" + _s("1") + b"\x01"
               + b"\x56\x47\x4D" + _s("2:1") + b"\x01"
               + b"\x5D\x00\x11\x4A\x46" + _s("1") + b"\x01" + b"\x4C" + _s("0") + b"\x01"
               + b"\x4D" + _s("1") + b"\x01\x01"
               + b"\x00\x01\x5E" + _s("Funk, Don") + b"\x01\x5F" + _s("Don") + b"\x01\x69" + _s("Funk") + b"\x01"
               + b"\x00\x11\x56" + _s("1") + b"\x01" + b"\x01" * 6)


def test_wbxml_matches_spec_example_byte_for_byte():
    t = decode(SPEC_SAMPLE)
    assert t.tag == "AirSync:Sync"
    col = t.find("Collections/Collection")
    assert col.text("Class") == "Contacts" and col.text("SyncKey") == "2" and col.text("CollectionId") == "2"
    add = col.find("Commands/Add")
    assert add.text("ServerId") == "2:1"
    body = add.find("ApplicationData/AirSyncBase:Body")
    assert body.text("Type") == "1" and body.text("EstimatedDataSize") == "0" and body.text("Truncated") == "1"
    assert add.find("ApplicationData/Contacts:0x1E").text() == "Funk, Don"     # страница без таблицы
    assert add.text("ApplicationData/AirSyncBase:NativeBodyType") == "1"
    assert encode(t) == SPEC_SAMPLE


def provision_request() -> Node:
    return el("Provision:Provision",
              el("Settings:DeviceInformation",
                 el("Settings:Set", el("Settings:Model", "WorkOS"), el("Settings:UserAgent", "WorkOS/1.0"))),
              el("Provision:Policies", el("Provision:Policy", el("Provision:PolicyType", eas.POLICY_TYPE))))


def provision_response(key: str, doc: dict | None = None, status: int = 1) -> Node:
    pol = [el("Provision:PolicyType", eas.POLICY_TYPE), el("Provision:Status", status),
           el("Provision:PolicyKey", key) if key else None]
    if doc is not None:
        pol.append(el("Provision:Data", el("Provision:EASProvisionDoc",
                                           *[el(f"Provision:{k}", v) for k, v in doc.items()])))
    return el("Provision:Provision",
              el("Settings:DeviceInformation", el("Settings:Status", 1)),
              el("Provision:Status", 1),
              el("Provision:Policies", el("Provision:Policy", *pol)))


FOLDERS = [("5", "0", "Входящие", 2), ("6", "0", "Календарь", 8), ("7", "0", "Отправленные", 5),
           ("8", "0", "Контакты", 9), ("9", "5", "Проекты", 12)]


def folder_sync_response(key="F1", folders=FOLDERS, status=1) -> Node:
    adds = [el("FolderHierarchy:Add", el("FolderHierarchy:ServerId", sid), el("FolderHierarchy:ParentId", parent),
               el("FolderHierarchy:DisplayName", name), el("FolderHierarchy:Type", typ))
            for sid, parent, name, typ in folders]
    return el("FolderHierarchy:FolderSync", el("FolderHierarchy:Status", status),
              el("FolderHierarchy:SyncKey", key),
              el("FolderHierarchy:Changes", el("FolderHierarchy:Count", len(adds)), *adds))


def mail_item() -> Node:
    return el("AirSync:ApplicationData",
              el("Email:To", '"George" <george@alfabank.ru>'),
              el("Email:Cc", '"Ян Петров" <Yan.Petrov@alfabank.ru>'),
              el("Email:From", '"Иванова Анна" <a.ivanova@alfabank.ru>'),
              el("Email:Subject", "Часы поддержки: методика"),
              el("Email:DateReceived", "2026-10-19T07:15:00.000Z"),
              el("Email:DisplayTo", "George"),
              el("Email:ThreadTopic", "Часы поддержки: методика"),
              el("Email:Importance", 2),
              el("Email:Read", 0),
              el("AirSyncBase:Body", el("AirSyncBase:Type", 1), el("AirSyncBase:EstimatedDataSize", 52),
                 el("AirSyncBase:Data", "Коллеги,\r\nпредлагаю обсудить методику.\r\n")),
              el("Email:MessageClass", "IPM.Note"),
              el("Email:InternetCPID", 65001),
              el("Email:Flag"),
              el("AirSyncBase:NativeBodyType", 2),
              el("Email2:ConversationId", bytes.fromhex("A1B2C3D4E5F60718293A4B5C6D7E8F90")),
              el("Email2:ConversationIndex", bytes.fromhex("01D96A2B3C4D")))


def weekly_item() -> Node:
    """Еженедельно по вторникам в 10:00 по Берлину с 29.09.2026; 03.11 удалено, 10.11 перенесено."""
    return el("AirSync:ApplicationData",
              el("Calendar:TimeZone", BERLIN),
              el("Calendar:DtStamp", "20260901T100000Z"),
              el("Calendar:StartTime", "20260929T080000Z"),
              el("Calendar:Subject", "Ретро команды"),
              el("Calendar:UID", "040000008200E00074C5B7101A82E008-weekly"),
              el("Calendar:OrganizerName", "Ян Петров"),
              el("Calendar:OrganizerEmail", "Yan.Petrov@alfabank.ru"),
              el("Calendar:Attendees",
                 el("Calendar:Attendee", el("Calendar:Email", "george@alfabank.ru"), el("Calendar:Name", "George"),
                    el("Calendar:AttendeeStatus", 3), el("Calendar:AttendeeType", 1))),
              el("Calendar:Location", "Переговорная 5"),
              el("Calendar:EndTime", "20260929T090000Z"),
              el("Calendar:Recurrence", el("Calendar:Type", 1), el("Calendar:Interval", 1),
                 el("Calendar:DayOfWeek", 4), el("Calendar:FirstDayOfWeek", 1)),
              el("AirSyncBase:Body", el("AirSyncBase:Type", 1), el("AirSyncBase:EstimatedDataSize", 24),
                 el("AirSyncBase:Data", "Повестка: итоги спринта")),
              el("Calendar:Sensitivity", 0),
              el("Calendar:BusyStatus", 2),
              el("Calendar:AllDayEvent", 0),
              el("Calendar:Reminder", 15),
              el("Calendar:Exceptions",
                 el("Calendar:Exception", el("Calendar:Deleted", 1),
                    el("Calendar:ExceptionStartTime", "20261103T090000Z")),
                 el("Calendar:Exception", el("Calendar:Subject", "Ретро (перенос)"),
                    el("Calendar:StartTime", "20261110T130000Z"), el("Calendar:EndTime", "20261110T140000Z"),
                    el("Calendar:ExceptionStartTime", "20261110T090000Z"))),
              el("Calendar:MeetingStatus", 3),
              el("Calendar:ResponseType", 3))


def single_item() -> Node:
    return el("AirSync:ApplicationData",
              el("Calendar:TimeZone", MOSCOW),
              el("Calendar:DtStamp", "20261015T090000Z"),
              el("Calendar:StartTime", "20261022T130000Z"),
              el("Calendar:Subject", "Созвон с ИТ про устройство"),
              el("Calendar:UID", "single-1"),
              el("Calendar:OrganizerName", "George"),
              el("Calendar:OrganizerEmail", "george@alfabank.ru"),
              el("Calendar:EndTime", "20261022T140000Z"),
              el("Calendar:BusyStatus", 1),
              el("Calendar:AllDayEvent", 0),
              el("Calendar:MeetingStatus", 1))


def sync_response(cid: str, key: str, adds=(), changes=(), deletes=(), more=False, status=1) -> Node:
    cmds = [el("AirSync:Add", el("AirSync:ServerId", sid), ad) for sid, ad in adds]
    cmds += [el("AirSync:Change", el("AirSync:ServerId", sid), ad) for sid, ad in changes]
    cmds += [el("AirSync:Delete", el("AirSync:ServerId", sid)) for sid in deletes]
    coll = [el("AirSync:SyncKey", key), el("AirSync:CollectionId", cid), el("AirSync:Status", status)]
    if more:
        coll.append(el("AirSync:MoreAvailable"))
    if cmds:
        coll.append(el("AirSync:Commands", *cmds))
    return el("AirSync:Sync", el("AirSync:Collections", el("AirSync:Collection", *coll)))


@pytest.mark.parametrize("tree", [
    provision_request(),
    provision_response("1307199584", {"DevicePasswordEnabled": "0", "MaxEmailAgeFilter": "0"}),
    folder_sync_response(),
    sync_response("5", "M2", adds=[("5:1", mail_item())]),
    sync_response("6", "C2", adds=[("6:1", weekly_item()), ("6:2", single_item())], deletes=["6:3"], more=True),
    el("Ping:Ping", el("Ping:Status", 2), el("Ping:Folders", el("Ping:Folder", "5"))),
])
def test_wbxml_roundtrip(tree):
    data = encode(tree)
    assert data[:4] == b"\x03\x01\x6a\x00"
    assert decode(data) == tree


def test_wbxml_details():
    t = decode(encode(sync_response("5", "M2", adds=[("5:1", mail_item())])))
    ad = t.find("Collections/Collection/Commands/Add/ApplicationData")
    assert ad.find("Email2:ConversationId").value == bytes.fromhex("A1B2C3D4E5F60718293A4B5C6D7E8F90")
    assert ad.text("Email:Subject") == "Часы поддержки: методика"
    flag = ad.find("Email:Flag")
    assert flag is not None and flag.value is None and not flag.children
    # переключение страниц: Email(2) -> AirSyncBase(17) -> Email(2) -> Email2(22)
    raw = encode(mail_item())
    assert b"\x00\x11" in raw and b"\x00\x16" in raw
    assert decode(b"") is None
    with pytest.raises(wbxml.WBXMLError):
        decode(raw[:-3])
    with pytest.raises(wbxml.WBXMLError):
        encode(el("Email:NoSuchTag", "x"))


# ---------------------------------------------------------------- время
def test_timezone_blob_dst_matches_zoneinfo():
    tz = eas.parse_timezone(BERLIN)
    ref = ZoneInfo("Europe/Berlin")
    for base in (datetime(2026, 3, 28, 0, tzinfo=UTC), datetime(2026, 10, 24, 0, tzinfo=UTC)):
        for h in range(0, 72):
            u = base + timedelta(hours=h, minutes=30)
            assert u.astimezone(tz).utcoffset() == u.astimezone(ref).utcoffset(), u
            wall = u.astimezone(ref).replace(tzinfo=None)
            back = wall.replace(tzinfo=tz, fold=u.astimezone(tz).fold).astimezone(UTC)
            assert back == u, (u, wall)
    msk = eas.parse_timezone(MOSCOW)
    assert msk.utcoffset(None) == timedelta(hours=3)            # без правил: фиксированный сдвиг
    assert eas.parse_timezone(None).key == "Europe/Moscow"
    assert eas.parse_timezone("не base64") is not None


def test_parse_eas_time():
    assert eas.parse_eas_time("20261029T080000Z") == datetime(2026, 10, 29, 8, tzinfo=UTC)
    assert eas.parse_eas_time("2026-10-19T07:15:00.120Z") == datetime(2026, 10, 19, 7, 15, 0, 120000, tzinfo=UTC)
    assert eas.parse_eas_time("") is None


def _master(start, end, rec, tz=MOSCOW, exceptions=()):
    return {"uid": "u", "subject": "s", "start": start, "end": end, "tz": tz, "recurrence": rec,
            "exceptions": list(exceptions)}


def _starts(m, a, b):
    return [i["start"] for i in eas.expand(m, a, b)]


def test_recurrence_rules():
    wide = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2031, 1, 1, tzinfo=UTC))
    # ежедневно через день, 5 раз
    m = _master("2026-10-14T07:00:00+00:00", "2026-10-14T07:30:00+00:00",
                {"type": 0, "interval": 2, "occurrences": 5})
    assert [d.day for d in _starts(m, *wide)] == [14, 16, 18, 20, 22]
    # еженедельно через неделю по пн и ср, неделя с понедельника
    m = _master("2026-10-05T07:00:00+00:00", "2026-10-05T08:00:00+00:00",
                {"type": 1, "interval": 2, "dow": 2 | 8, "fdow": 1, "until": "2026-11-05T07:00:00+00:00"})
    assert [(d.month, d.day) for d in _starts(m, *wide)] == [(10, 5), (10, 7), (10, 19), (10, 21), (11, 2), (11, 4)]
    # ежемесячно 31 числа: в коротких месяцах последний день
    m = _master("2026-01-31T06:00:00+00:00", "2026-01-31T07:00:00+00:00",
                {"type": 2, "interval": 1, "dom": 31, "occurrences": 5})
    assert [d.date() for d in _starts(m, *wide)] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
                                                      date(2026, 4, 30), date(2026, 5, 31)]
    # ежемесячно в последнюю пятницу
    m = _master("2026-10-30T09:00:00+00:00", "2026-10-30T10:00:00+00:00",
                {"type": 3, "interval": 1, "dow": 32, "wom": 5, "occurrences": 3})
    assert [d.date() for d in _starts(m, *wide)] == [date(2026, 10, 30), date(2026, 11, 27), date(2026, 12, 25)]
    # ежегодно 15 марта до 2028 года
    m = _master("2026-03-15T06:00:00+00:00", "2026-03-15T07:00:00+00:00",
                {"type": 5, "interval": 1, "dom": 15, "moy": 3, "until": "2028-03-15T06:00:00+00:00"})
    assert [d.year for d in _starts(m, *wide)] == [2026, 2027, 2028]
    # ежегодно во второй понедельник июня
    m = _master("2026-06-08T06:00:00+00:00", "2026-06-08T07:00:00+00:00",
                {"type": 6, "interval": 1, "dow": 2, "wom": 2, "moy": 6, "occurrences": 2})
    assert [d.date() for d in _starts(m, *wide)] == [date(2026, 6, 8), date(2027, 6, 14)]


# ---------------------------------------------------------------- фальшивый Exchange
def _resp(tree: Node | None, status=200, headers=None) -> httpx.Response:
    h = {"Content-Type": eas.CONTENT_TYPE, **(headers or {})}
    return httpx.Response(status, content=encode(tree) if tree is not None else b"", headers=h)


class FakeExchange:
    temp_key, final_key = "1307199584", "3942919513"

    def __init__(self, policy: dict | None = None):
        self.policy = policy if policy is not None else {"DevicePasswordEnabled": "0", "AllowHTMLEmail": "1"}
        self.final_status = 1
        self.acks: list[str] = []
        self.calls: list[tuple[str, Node | None, str | None]] = []
        self.mismatch = "449"            # как отвечать на чужой ключ политики: 449 или статус 144
        self.headers: dict[str, dict] = {}       # разовые заголовки к ответу на команду
        self.fail: dict[str, httpx.Response] = {}  # разовый ответ вместо команды
        self.invalid_keys: set[str] = set()
        self.hidden: set[str] = set()
        self.cal_changes: dict[str, tuple] = {}
        self.mail_changes: dict[str, tuple] = {}
        self.pings: list[int] = []

    def cmds(self, start=0):
        return [c[0] for c in self.calls[start:]]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        expect = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        assert request.headers["authorization"] == expect
        assert request.headers["user-agent"] == "WorkOS/1.0"
        assert request.headers["ms-asprotocolversion"] == "14.1"
        assert request.url.path == "/Microsoft-Server-ActiveSync"
        if request.method == "OPTIONS":
            self.calls.append(("OPTIONS", None, None))
            return httpx.Response(200, headers={"MS-ASProtocolVersions": "2.0,2.1,2.5,12.0,12.1,14.0,14.1,16.0,16.1",
                                                "MS-ASProtocolCommands": "Sync,SendMail,SmartForward,SmartReply,"
                                                "GetAttachment,FolderSync,FolderCreate,Ping,Provision,Settings",
                                                "MS-Server-ActiveSync": "15.20",
                                                "X-MS-BackOffDuration": "L/-470"})
        q = request.url.params
        assert q["DeviceId"] == DEVICE and q["DeviceType"] == "WorkOS" and q["User"] == USER
        assert request.headers["content-type"] == eas.CONTENT_TYPE
        cmd = q["Cmd"]
        tree = decode(request.content)
        key = request.headers.get("x-ms-policykey")
        self.calls.append((cmd, tree, key))
        if cmd in self.fail:
            return self.fail.pop(cmd)
        if cmd == "Provision":
            return self.provision(tree, key)
        if key != self.final_key:
            if self.mismatch == "449":
                return httpx.Response(449)
            root = {"Sync": "AirSync:Sync", "FolderSync": "FolderHierarchy:FolderSync", "Ping": "Ping:Ping"}[cmd]
            return _resp(el(root, el(root.split(":")[0] + ":Status", 144)))
        resp = getattr(self, cmd.lower())(tree)
        resp.headers.update(self.headers.pop(cmd, {}))
        return resp

    def provision(self, tree: Node, key: str | None) -> httpx.Response:
        pol = tree.find("Policies/Policy")
        assert pol.text("PolicyType") == eas.POLICY_TYPE
        if pol.has("PolicyKey"):                          # фаза 2: подтверждение
            assert pol.text("PolicyKey") == self.temp_key and key == self.temp_key
            self.acks.append(pol.text("Status"))
            if self.final_status != 1:
                return _resp(provision_response("", status=self.final_status))
            return _resp(provision_response(self.final_key))
        assert key is None
        assert tree.find("Settings:DeviceInformation/Settings:Set").text("Settings:Model") == "WorkOS"
        return _resp(provision_response(self.temp_key, self.policy))

    def foldersync(self, tree: Node) -> httpx.Response:
        if tree.text("SyncKey") == "0":
            return _resp(folder_sync_response("F1"))
        return _resp(el("FolderHierarchy:FolderSync", el("FolderHierarchy:Status", 1),
                        el("FolderHierarchy:SyncKey", tree.text("SyncKey"))))

    def sync(self, tree: Node) -> httpx.Response:
        col = tree.find("Collections/Collection")
        key, cid = col.text("SyncKey"), col.text("CollectionId")
        assert col.text("WindowSize") == "100"
        assert (col.text("GetChanges") == "1") == (key != "0")
        bp = col.find("Options/AirSyncBase:BodyPreference")
        assert bp.text("Type") == "1"
        if key in self.invalid_keys:
            self.invalid_keys.discard(key)
            return _resp(sync_response(cid, "0", status=3))
        if cid == "5":
            assert col.text("Options/FilterType") == "3" and bp.text("TruncationSize") == "32768"
            if key == "0":
                return _resp(sync_response("5", "M1"))
            if key == "M1":
                return _resp(sync_response("5", "M2", adds=[("5:1", mail_item())]))
            if key in self.mail_changes:
                new, changes, deletes = self.mail_changes[key]
                return _resp(sync_response("5", new, changes=changes, deletes=deletes))
            return httpx.Response(200)                     # пустой ответ: изменений нет
        assert cid == "6" and col.text("Options/FilterType") == "4"
        if key == "0":
            return _resp(sync_response("6", "C1"))
        if key == "C1":
            return _resp(sync_response("6", "C2", adds=[("6:1", weekly_item())], more=True))
        if key == "C2":
            adds = [] if "6:2" in self.hidden else [("6:2", single_item())]
            return _resp(sync_response("6", "C3", adds=adds))
        if key in self.cal_changes:
            new, adds, deletes = self.cal_changes[key]
            return _resp(sync_response("6", new, adds=adds, deletes=deletes))
        return _resp(sync_response("6", key))

    def ping(self, tree: Node) -> httpx.Response:
        assert tree.text("HeartbeatInterval") == "900"
        assert [(f.text("Id"), f.text("Class")) for f in tree.findall("Folders/Folder")] == \
            [("5", "Email"), ("6", "Calendar")]
        st = self.pings.pop(0) if self.pings else 1
        if st == 2:
            return _resp(el("Ping:Ping", el("Ping:Status", 2), el("Ping:Folders", el("Ping:Folder", "5"))))
        return _resp(el("Ping:Ping", el("Ping:Status", st)))


def make_client(server: FakeExchange, sleeps: list | None = None) -> eas.EASClient:
    return eas.EASClient("owa.alfabank.ru", USER, PASSWORD, DEVICE, None,
                         transport=httpx.MockTransport(server),
                         sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def source(admin) -> dict:
    return admin.execute("SELECT * FROM work.sources WHERE id = 'eas:alfa'").fetchone()


def collect(admin, server, **kw):
    col = eas.EASCollector(source(admin), client=make_client(server), now=NOW, **kw)
    return run_source("eas:alfa", col), col


def rows(admin, kind=None):
    q = "SELECT * FROM work.events WHERE source_id = 'eas:alfa'" + (" AND kind = %s" if kind else "")
    return admin.execute(q + " ORDER BY occurred_at", (kind,) if kind else ()).fetchall()


def state(admin) -> dict:
    return json.loads(source(admin)["watermark"])


# ---------------------------------------------------------------- сценарии
def test_full_pass_provision_folders_mail_calendar_idempotent(admin, role_env):
    role_env("workos_reader")
    admin.execute("UPDATE work.sources SET enabled = true WHERE id = 'eas:alfa'")
    server = FakeExchange()
    res, _ = collect(admin, server)
    assert server.cmds() == ["Provision", "Provision", "FolderSync", "Sync", "Sync", "Sync", "Sync", "Sync"]
    assert server.calls[0][2] is None and server.calls[1][2] == server.temp_key
    assert all(c[2] == server.final_key for c in server.calls[2:])     # X-MS-PolicyKey на каждом запросе
    assert server.acks == ["1"]
    assert res["inserted"] == 10

    mail = rows(admin, "email")
    assert len(mail) == 1
    m = mail[0]
    assert m["external_id"] == eas.mail_external_id(mail_item()) and m["context_id"] == "alfa"
    assert m["external_id"].startswith("mail:") and "5:1" not in m["external_id"]
    assert m["meta"]["server_id"] == "5:1" and m["meta"]["sync_epoch"] == 1 and m["meta"]["removed"] is False
    assert m["title"] == "Часы поддержки: методика"
    assert m["body"] == "Коллеги,\nпредлагаю обсудить методику."
    assert m["occurred_at"] == datetime(2026, 10, 19, 7, 15, tzinfo=UTC)
    assert m["actor_raw"] == '"Иванова Анна" <a.ivanova@alfabank.ru>'
    assert m["thread_key"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert m["meta"]["read"] is False and m["meta"]["importance"] == "high"
    assert m["meta"]["folder"] == "Входящие" and m["meta"]["from_email"] == "a.ivanova@alfabank.ru"
    assert m["meta"]["cc_emails"] == ["yan.petrov@alfabank.ru"]

    cal = rows(admin, "calendar")
    weekly = [r for r in cal if r["meta"]["uid"].endswith("-weekly")]
    # окно 13.10 12:00 .. 19.12 12:00: 20.10, 27.10, (03.11 удалено), 10.11 перенесено, 17.11 ... 15.12
    assert len(weekly) == 8 and len(cal) == 9
    starts = {r["meta"]["recurrence_id"]: r for r in weekly}
    assert "2026-11-03T09:00:00+00:00" not in starts
    # до и после перевода часов встреча в 10:00 по Берлину: 08:00Z летом, 09:00Z зимой
    oct20 = starts["2026-10-20T08:00:00+00:00"]
    oct27 = starts["2026-10-27T09:00:00+00:00"]
    berlin = ZoneInfo("Europe/Berlin")
    assert oct20["occurred_at"].astimezone(berlin).hour == oct27["occurred_at"].astimezone(berlin).hour == 10
    assert oct20["external_id"] == "040000008200E00074C5B7101A82E008-weekly|2026-10-20T08:00:00+00:00"
    assert oct20["meta"]["ends_at"] == "2026-10-20T09:00:00+00:00"
    assert oct20["meta"]["busy"] is True and oct20["meta"]["status"] == "confirmed"
    assert oct20["meta"]["location"] == "Переговорная 5" and oct20["meta"]["all_day"] is False
    assert oct20["meta"]["attendees"] == [{"email": "george@alfabank.ru", "name": "George"}]
    assert oct20["meta"]["organizer"] == "yan.petrov@alfabank.ru"
    assert oct20["meta"]["updated"] == "2026-09-01T10:00:00+00:00"
    assert oct20["body"] == "Повестка: итоги спринта"
    moved = starts["2026-11-10T09:00:00+00:00"]
    assert moved["title"] == "Ретро (перенос)"
    assert moved["occurred_at"] == datetime(2026, 11, 10, 13, tzinfo=UTC)
    assert moved["meta"]["ends_at"] == "2026-11-10T14:00:00+00:00"
    assert max(starts) == "2026-12-15T09:00:00+00:00"
    single = [r for r in cal if r["meta"]["uid"] == "single-1"][0]
    assert single["external_id"] == "single-1|" and single["meta"]["recurrence_id"] == ""
    assert single["meta"]["busy"] is True                       # tentative считается занятым

    st = state(admin)
    assert st["policy_key"] == server.final_key and st["folder_sync_key"] == "F1"
    assert st["sync_keys"] == {"5": "M2", "6": "C3"}
    assert st["folders"]["5"] == {"name": "Входящие", "type": 2, "parent": "0"}
    assert set(st["cal_masters"]) == {"6:1", "6:2"}

    # второй прогон: без Provision и FolderSync, ничего нового
    n = len(server.calls)
    res2, _ = collect(admin, server)
    assert server.cmds(n) == ["Sync", "Sync"]
    assert res2["inserted"] == 0 and res2["updated"] == 0
    assert len(rows(admin)) == 10


def test_invalid_sync_key_resets_and_full_resync_cancels_missing(admin, role_env):
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    server.invalid_keys.add("C3")
    server.hidden.add("6:2")                 # встреча пропала, пока ключ был недействителен
    n = len(server.calls)
    res, _ = collect(admin, server)
    keys = [c[1].find("Collections/Collection").text("SyncKey") for c in server.calls[n:] if c[0] == "Sync"]
    assert keys == ["M2", "C3", "0", "C1", "C2"]
    assert res["inserted"] == 0
    assert len(rows(admin)) == 10
    single = [r for r in rows(admin, "calendar") if r["meta"]["uid"] == "single-1"][0]
    assert single["meta"]["status"] == "cancelled" and single["meta"]["busy"] is False
    st = state(admin)
    assert st["sync_keys"]["6"] == "C3" and not st.get("resync")
    assert set(st["cal_masters"]) == {"6:1"}
    assert all(r["meta"]["status"] == "confirmed" for r in rows(admin, "calendar") if r["meta"]["uid"] != "single-1")


def test_server_delete_cancels_all_instances_and_mail_flags(admin, role_env):
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    server.cal_changes["C3"] = ("C4", [], ["6:1"])
    server.mail_changes["M2"] = ("M3", [("5:1", el("AirSync:ApplicationData", el("Email:Read", 1)))], [])
    collect(admin, server)
    weekly = [r for r in rows(admin, "calendar") if r["meta"]["uid"].endswith("-weekly")]
    assert len(weekly) == 8 and all(r["meta"]["status"] == "cancelled" for r in weekly)
    single = [r for r in rows(admin, "calendar") if r["meta"]["uid"] == "single-1"][0]
    assert single["meta"]["status"] == "confirmed"
    assert rows(admin, "email")[0]["meta"]["read"] is True
    assert state(admin)["sync_keys"] == {"5": "M3", "6": "C4"}
    assert len(rows(admin)) == 10


def test_changed_master_cancels_vanished_instance(admin, role_env):
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    # организатор удалил ещё и 17.11: Change приходит полным элементом
    changed = weekly_item()
    changed.find("Calendar:Exceptions").children.append(
        el("Calendar:Exception", el("Calendar:Deleted", 1), el("Calendar:ExceptionStartTime", "20261117T090000Z")))
    resp = sync_response("6", "C4", changes=[("6:1", changed)])
    orig_sync = server.sync
    server.sync = lambda tree: (_resp(resp) if tree.find("Collections/Collection").text("SyncKey") == "C3"
                                else orig_sync(tree))
    collect(admin, server)
    weekly = {r["meta"]["recurrence_id"]: r for r in rows(admin, "calendar") if r["meta"]["uid"].endswith("-weekly")}
    assert weekly["2026-11-17T09:00:00+00:00"]["meta"]["status"] == "cancelled"
    assert weekly["2026-11-24T09:00:00+00:00"]["meta"]["status"] == "confirmed"


def _answer(server, key: str, tree: Node) -> None:
    """Разово ответить на Sync с этим SyncKey заранее собранным деревом."""
    orig_sync = server.sync

    def sync(t):
        if t.find("Collections/Collection").text("SyncKey") == key:
            server.sync = orig_sync
            return _resp(tree)
        return orig_sync(t)
    server.sync = sync


def _single(start: str, end: str, **extra) -> Node:
    item = single_item()
    for n in item.children:
        if n.tag == "Calendar:StartTime":
            n.value = start
        elif n.tag == "Calendar:EndTime":
            n.value = end
    item.children += [el(k, v) for k, v in extra.items()]
    return item


def feed(admin, uid: str) -> list[dict]:
    return admin.execute("SELECT * FROM work.calendar_feed WHERE source_id = 'eas:alfa' AND uid = %s "
                         "ORDER BY starts_at", (uid,)).fetchall()


def test_one_off_moved_outside_window_moves_row(admin, role_env):
    """Разовую встречу перенесли за окно -7/+60: строка едет следом, призрака в окне нет."""
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    far = "20270201T130000Z"                                    # за пределами окна (до 19.12)
    _answer(server, "C3", sync_response("6", "C4", changes=[("6:2", _single(far, "20270201T140000Z"))]))
    res, col = collect(admin, server)
    assert res["inserted"] == 0 and "single-1|" in col._moved_out
    row = [r for r in rows(admin, "calendar") if r["meta"]["uid"] == "single-1"]
    assert len(row) == 1
    assert row[0]["occurred_at"] == datetime(2027, 2, 1, 13, tzinfo=UTC)
    assert row[0]["meta"]["status"] == "confirmed" and row[0]["meta"]["ends_at"] == "2027-02-01T14:00:00+00:00"
    # в окне встречи больше нет: ни занятого слота, ни копии в зеркале
    win = admin.execute("SELECT count(*) AS n FROM work.calendar_feed WHERE uid = 'single-1' "
                        "AND starts_at < %s", (NOW + timedelta(days=60),)).fetchone()["n"]
    assert win == 0
    # повтор ничего не меняет, а возврат в окно снова двигает ту же строку
    res, _ = collect(admin, server)
    assert res["updated"] == 0
    _answer(server, "C4", sync_response("6", "C5", changes=[("6:2", _single("20261023T090000Z",
                                                                             "20261023T100000Z"))]))
    collect(admin, server)
    row = [r for r in rows(admin, "calendar") if r["meta"]["uid"] == "single-1"]
    assert len(row) == 1 and row[0]["occurred_at"] == datetime(2026, 10, 23, 9, tzinfo=UTC)


def test_one_off_moved_far_into_past_and_shortened_series(admin, role_env):
    """Встречу унесли в далёкое прошлое (мастер устарел), серию оборвали: в окне ничего не остаётся."""
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    weekly = weekly_item()
    rec = weekly.find("Calendar:Recurrence")
    rec.children.append(el("Calendar:Until", "20261001T080000Z"))       # серия кончилась до окна
    _answer(server, "C3", sync_response("6", "C4", changes=[
        ("6:2", _single("20260901T130000Z", "20260901T140000Z")), ("6:1", weekly)]))
    collect(admin, server)
    single = [r for r in rows(admin, "calendar") if r["meta"]["uid"] == "single-1"][0]
    assert single["occurred_at"] == datetime(2026, 9, 1, 13, tzinfo=UTC)
    assert all(r["meta"]["status"] == "cancelled" for r in rows(admin, "calendar")
               if r["meta"]["uid"].endswith("-weekly") and r["occurred_at"] >= NOW - timedelta(days=7))
    assert state(admin)["cal_masters"] == {}


def test_exception_moved_out_of_window_is_not_left_behind(admin, role_env):
    """Вхождение 22.12 (за окном) перенесли в окно на 01.12, потом обратно за окно на 29.12."""
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)

    def with_exception(start: str, end: str) -> Node:
        item = weekly_item()
        item.find("Calendar:Exceptions").children.append(
            el("Calendar:Exception", el("Calendar:Subject", "Ретро (из декабря)"),
               el("Calendar:StartTime", start), el("Calendar:EndTime", end),
               el("Calendar:ExceptionStartTime", "20261222T090000Z")))
        return item

    _answer(server, "C3", sync_response("6", "C4", changes=[("6:1", with_exception("20261201T120000Z",
                                                                                  "20261201T130000Z"))]))
    collect(admin, server)
    ext = "040000008200E00074C5B7101A82E008-weekly|2026-12-22T09:00:00+00:00"
    moved = [r for r in rows(admin, "calendar") if r["external_id"] == ext]
    assert len(moved) == 1 and moved[0]["occurred_at"] == datetime(2026, 12, 1, 12, tzinfo=UTC)
    _answer(server, "C4", sync_response("6", "C5", changes=[("6:1", with_exception("20261229T120000Z",
                                                                                  "20261229T130000Z"))]))
    collect(admin, server)
    moved = [r for r in rows(admin, "calendar") if r["external_id"] == ext][0]
    assert moved["occurred_at"] == datetime(2026, 12, 29, 12, tzinfo=UTC)
    assert moved["meta"]["status"] == "confirmed"
    in_window = admin.execute("SELECT count(*) AS n FROM work.calendar_feed WHERE source_id = 'eas:alfa' "
                              "AND starts_at = '2026-12-01T12:00:00Z'").fetchone()["n"]
    assert in_window == 0


def test_all_day_event_keeps_its_date(admin, role_env):
    """Весь день по Москве (полночь МСК = 21:00Z накануне): дата та же, хранится полночь по Мадриду."""
    from executors.calendar import mirror_body
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    allday = _single("20261024T210000Z", "20261025T210000Z")
    allday.find("Calendar:AllDayEvent").value = "1"
    allday.find("Calendar:UID").value = "allday-1"
    _answer(server, "C3", sync_response("6", "C4", adds=[("6:9", allday)]))
    collect(admin, server)
    row = feed(admin, "allday-1")[0]
    madrid = ZoneInfo("Europe/Madrid")
    assert row["all_day"] is True
    assert row["starts_at"] == datetime(2026, 10, 25, 0, tzinfo=madrid)
    assert row["ends_at"] == datetime(2026, 10, 26, 0, tzinfo=madrid)
    body = mirror_body(row)
    assert body["start"] == {"date": "2026-10-25"} and body["end"] == {"date": "2026-10-26"}
    # пояс без правил и сдвиг в пару часов: дата всё равно по ближайшей полуночи
    m = {"tz": eas.make_timezone_blob(-120)}
    s, e = eas._all_day_span(m, datetime(2026, 10, 24, 22, tzinfo=UTC), datetime(2026, 10, 25, 22, tzinfo=UTC))
    assert s.astimezone(madrid).date() == date(2026, 10, 25) and e.astimezone(madrid).date() == date(2026, 10, 26)


def test_mail_id_survives_server_id_change_and_flags_follow(admin, role_env):
    """Сброс SyncKey раздаёт ServerId заново: письмо не задваивается, флаги ищутся по новому адресу,
    старый адрес из прошлого состояния синхронизации чужие письма не задевает."""
    role_env("workos_reader")
    server = FakeExchange()
    collect(admin, server)
    first = rows(admin, "email")[0]
    # сброс ключа «Входящих»: после него то же письмо приходит как 5:7
    server.invalid_keys.add("M2")
    orig_sync = server.sync

    def sync(t):
        col = t.find("Collections/Collection")
        if col.text("CollectionId") == "5" and col.text("SyncKey") == "M1":
            return _resp(sync_response("5", "M2", adds=[("5:7", mail_item())]))
        return orig_sync(t)
    server.sync = sync
    res, _ = collect(admin, server)
    mail = rows(admin, "email")
    assert len(mail) == 1 and mail[0]["external_id"] == first["external_id"] and res["inserted"] == 0
    assert mail[0]["meta"]["server_id"] == "5:7" and mail[0]["meta"]["sync_epoch"] == 2
    # Change по старому адресу 5:1 (прошлая эпоха) ничего не трогает, по новому 5:7 ставит «прочитано»
    server.sync = orig_sync
    server.mail_changes["M2"] = ("M3", [("5:1", el("AirSync:ApplicationData", el("Email:Read", 1)))], [])
    collect(admin, server)
    assert rows(admin, "email")[0]["meta"]["read"] is False
    server.mail_changes["M3"] = ("M4", [("5:7", el("AirSync:ApplicationData", el("Email:Read", 1)))], [])
    collect(admin, server)
    assert rows(admin, "email")[0]["meta"]["read"] is True
    # письмо унесли из «Входящих» и вернули: та же строка, removed снова false
    server.mail_changes["M4"] = ("M5", [], ["5:7"])
    collect(admin, server)
    assert rows(admin, "email")[0]["meta"]["removed"] is True
    server.mail_changes["M5"] = ("M6", [("5:12", mail_item())], [])
    collect(admin, server)
    mail = rows(admin, "email")
    assert len(mail) == 1 and mail[0]["meta"]["removed"] is False and mail[0]["meta"]["server_id"] == "5:12"


def test_legacy_mail_row_still_gets_flags(admin, role_env):
    """Строка старого формата (mail:<ServerId>, без эпохи) получает флаги по meta.server_id."""
    role_env("workos_reader")
    admin.execute("INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta) "
                  "VALUES ('alfa', 'eas:alfa', 'mail:5:3', 'email', now(), 'старое', %s)",
                  (json.dumps({"server_id": "5:3", "read": False}),))
    col = eas.EASCollector(source(admin), client=make_client(FakeExchange()), now=NOW)
    col._mail_patches[("5:3", 0)] = {"read": True}
    from core import db as cdb
    with cdb.connect() as conn, conn.transaction():
        col.finalize(conn, set())
    assert rows(admin, "email")[0]["meta"]["read"] is True


def test_reprovision_on_449_and_status_144():
    for mode in ("449", "144"):
        server = FakeExchange()
        server.mismatch = mode
        cl = make_client(server)
        cl.policy_key = "1111111"
        folders = cl.folder_sync()
        assert folders["6"]["type"] == 8
        assert server.cmds() == ["FolderSync", "Provision", "Provision", "FolderSync"]
        assert cl.policy_key == server.final_key


def test_backoff_and_throttling_honored():
    sleeps: list[float] = []
    server = FakeExchange()
    cl = make_client(server, sleeps)
    server.headers["FolderSync"] = {"X-MS-BackOffDuration": "L/1500"}
    cl.folder_sync()
    assert sleeps == []
    cl.sync("5", "0", "Email", filter_type=3)
    assert len(sleeps) == 1 and 1.0 < sleeps[0] <= 1.5          # перед следующим запросом подождали
    server.fail["Sync"] = httpx.Response(503, headers={"Retry-After": "7"})
    r = cl.sync("5", "M1", "Email", filter_type=3)
    assert len(r["adds"]) == 1
    assert any(6.0 < s <= 7.0 for s in sleeps[1:])
    info = cl.options()
    assert "14.1" in info["versions"] and "Ping" in info["commands"]


def test_honest_policy_ack_and_refusal():
    server = FakeExchange(policy={"DevicePasswordEnabled": "1", "MinDevicePasswordLength": "6",
                                  "MaxEmailAgeFilter": "2"})
    cl = make_client(server)
    assert cl.provision() == server.final_key
    assert server.acks == ["3"] and cl.policy_ack == 3            # PIN не применить: честно «не применена»
    assert cl.policy_info["MaxEmailAgeFilter"] == "2"
    server2 = FakeExchange(policy={"DevicePasswordEnabled": "1"})
    server2.final_status = 2
    with pytest.raises(eas.EASDeviceBlocked) as ei:
        make_client(server2).provision()
    assert "AllowNonProvisionableDevices" in str(ei.value) and DEVICE in str(ei.value)


def test_policy_age_filter_caps_mail_window(admin, role_env):
    role_env("workos_reader")
    server = FakeExchange(policy={"MaxEmailAgeFilter": "2"})
    col = eas.EASCollector(source(admin), client=make_client(server), now=NOW)
    col.state["policy"] = {"info": {"MaxEmailAgeFilter": "2"}}
    assert col._filter("Email") == 2 and col._filter("Calendar") == 4


def test_blocked_device_stops_retries_until_manual_run(admin, role_env):
    role_env("workos_reader")
    server = FakeExchange()
    server.fail["Provision"] = httpx.Response(403, text="Forbidden")
    with pytest.raises(eas.EASDeviceBlocked) as ei:
        collect(admin, server)
    msg = str(ei.value)
    assert "ИТ" in msg and DEVICE in msg and "WorkOS" in msg
    assert "[eas-stop:" in source(admin)["last_error"]
    n = len(server.calls)
    with pytest.raises(eas.EASDeviceBlocked):
        collect(admin, server)                     # по таймеру: в Exchange не стучимся
    assert len(server.calls) == n
    assert "[eas-stop:" in source(admin)["last_error"]
    res, _ = collect(admin, server, force=True)   # ручной запуск после одобрения ИТ
    assert res["inserted"] == 10 and source(admin)["last_error"] is None


def test_wrong_password_message():
    server = FakeExchange()
    server.fail["Provision"] = httpx.Response(401)
    with pytest.raises(eas.EASAuthError) as ei:
        make_client(server).provision()
    assert "401" in str(ei.value) and "ALFA_EAS_PASSWORD" in str(ei.value)


def test_config_validation(tmp_path):
    with pytest.raises(eas.EASConfigError, match="айфона"):
        eas.EASClient("owa.alfabank.ru", USER, PASSWORD, "ApplF17XK2ABCD12", None,
                      transport=httpx.MockTransport(FakeExchange()))
    with pytest.raises(eas.EASConfigError, match="латиницы"):
        eas.EASClient("owa.alfabank.ru", USER, PASSWORD, "WorkOS-1", None,
                      transport=httpx.MockTransport(FakeExchange()))
    with pytest.raises(eas.EASConfigError, match="Russian Trusted Root CA"):
        eas.EASClient("owa.alfabank.ru", USER, PASSWORD, DEVICE, str(tmp_path / "nope.pem"))


def test_ping_and_watch(admin, role_env):
    role_env("workos_reader")
    admin.execute("UPDATE work.sources SET enabled = true WHERE id = 'eas:alfa'")
    server = FakeExchange()
    server.pings = [2, 1]
    res = eas.watch(max_cycles=2, client=make_client(server), now=NOW)
    assert res["status"] == "stopped" and res["collections"] == 2 and res["pings"] == 2
    assert server.cmds().count("Ping") == 2
    assert len(rows(admin)) == 10
    admin.execute("UPDATE work.sources SET enabled = false WHERE id = 'eas:alfa'")
    assert eas.watch(client=make_client(server))["status"] == "disabled"


def test_watch_exits_3_when_disabled_or_not_configured(admin, role_env, monkeypatch, caplog):
    """systemd: Restart=always + RestartPreventExitStatus=3. Выключенный источник и пустой
    .env.reader не должны перезапускать сервис каждые 10 секунд."""
    role_env("workos_reader")
    admin.execute("UPDATE work.sources SET enabled = false WHERE id = 'eas:alfa'")
    assert eas.main(["--watch"]) == 3
    assert any("выключен" in r.getMessage() and "код" in r.getMessage() for r in caplog.records)
    admin.execute("UPDATE work.sources SET enabled = true WHERE id = 'eas:alfa'")
    for k in ("ALFA_EAS_USER", "ALFA_EAS_PASSWORD", "ALFA_EAS_DEVICE_ID"):
        monkeypatch.delenv(k, raising=False)
    caplog.clear()
    assert eas.main(["--watch"]) == 3
    assert any("ALFA_EAS_USER" in r.getMessage() for r in caplog.records)
    admin.execute("UPDATE work.sources SET enabled = false WHERE id = 'eas:alfa'")


def test_probe_writes_nothing(admin):
    server = FakeExchange()
    out = eas.probe(make_client(server))
    assert "Входящие" in out and "Календарь" in out and "15.20" in out
    assert server.cmds() == ["OPTIONS", "Provision", "Provision", "FolderSync"]
    assert source(admin)["watermark"] is None and rows(admin) == []

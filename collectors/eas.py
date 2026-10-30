"""Альфа-Банк: свой клиент Exchange ActiveSync (шаг 2.4).

Сервер: Exchange 2019 on-prem на owa.alfabank.ru, наружу опубликован только
ActiveSync, вход Basic поверх TLS (MOSCOW\\логин). Протокол 14.1.

Порядок: OPTIONS (для --probe) -> Provision в две фазы (ключ политики потом
уходит в X-MS-PolicyKey на каждом запросе) -> FolderSync (SyncKey 0) ->
Sync по «Входящим» (тип 2) и «Календарю» (тип 8). SyncKey сам по себе курсор
прибытия, поэтому lookback этому источнику не нужен.

Жёсткие правила:
- DeviceId только свой (ALFA_EAS_DEVICE_ID), никогда не айфона; DeviceType
  WorkOS, User-Agent WorkOS/1.0. Политику подтверждаем честно: если она
  требует PIN или шифрование, которые процесс на сервере выполнить не может,
  отвечаем «не применена», а не «успех».
- TLS проверяется всегда, корень «Russian Trusted Root CA» передаётся явно
  через ALFA_CA_FILE (в certifi его нет). verify=False здесь не бывает.
- Сервер включил троттлинг (X-MS-BackOffDuration): заголовок уважаем, а вместо
  опроса по таймеру живём на Ping с heartbeat 900 с (watch).
- Неверный пароль и блокировка устройства не повторяются автоматически,
  чтобы не заблокировать учётку в AD: см. _guard().

Состояние (sources.watermark, JSON): policy_key, folder_sync_key, folders,
sync_keys, cal_masters (все встречи календаря без лишнего, чтобы каждый прогон
разворачивать повторы в окне -7..+60 дней), resync, policy, mail_epoch (номер
состояния синхронизации «Входящих»: растёт при каждом SyncKey 0).

Письмо: external_id = mail:<хеш DateReceived + From + Subject + размер тела>,
ServerId лежит в meta.server_id вместе с meta.sync_epoch и служит только адресом
для флагов «прочитано» и удаления. Встреча на весь день хранится от полуночи по
Мадриду той даты, что стоит во встрече по её собственному поясу.

CLI:
    python -m collectors.eas --probe   # OPTIONS + Provision + FolderSync, в базу ничего
    python -m collectors.eas --once    # один проход Sync (по умолчанию)
    python -m collectors.eas --watch   # Ping-цикл (сервис workctl eas-watch)
"""
from __future__ import annotations

import argparse
import base64
import binascii
import calendar as _calendar
import hashlib
import logging
import re
import signal
import ssl
import struct
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

import httpx
import psycopg

from collectors.base import Collector, Event, run_source
from collectors.wbxml import Node, WBXMLError, decode, el, encode
from core import config, db, timeutil as tu

log = logging.getLogger("workos.eas")

UTC = timezone.utc
SOURCE_ID = "eas:alfa"
PROTOCOL = "14.1"
DEVICE_TYPE = "WorkOS"
USER_AGENT = "WorkOS/1.0"
CONTENT_TYPE = "application/vnd.ms-sync.wbxml"
POLICY_TYPE = "MS-EAS-Provisioning-WBXML"
EAS_PATH = "/Microsoft-Server-ActiveSync"
DEFAULT_HOST = "owa.alfabank.ru"
DEFAULT_CA = config.ROOT / "certs" / "russian_trusted_root_ca.pem"
FALLBACK_TZ = "Europe/Moscow"

WINDOW_SIZE = 100
MAIL_TRUNCATION = 32768
CAL_TRUNCATION = 4096
MAX_PAGES = 20                 # страниц MoreAvailable за один прогон на папку
MAX_BACKOFF = 900.0            # дольше 15 минут подряд не спим из-за троттлинга
STORED_BODY = 2000             # сколько текста встречи держать в состоянии
MAX_ATTENDEES = 100

FOLDER_TYPES = {1: "папка", 2: "Входящие", 3: "Черновики", 4: "Удалённые", 5: "Отправленные",
                6: "Исходящие", 7: "Задачи", 8: "Календарь", 9: "Контакты", 10: "Заметки",
                11: "Журнал", 12: "почтовая папка", 13: "календарь", 14: "контакты",
                15: "задачи", 16: "журнал", 17: "заметки", 18: "неизвестная", 19: "кэш получателей"}
CANCELLED_MEETING = {5, 7, 13, 15}
RESPONSE = {0: "none", 1: "organizer", 2: "tentative", 3: "accepted", 4: "declined", 5: "not_responded"}
IMPORTANCE = {0: "low", 1: "normal", 2: "high"}

# Политики, которые процесс на сервере честно выполнить не может
UNENFORCEABLE = ("DevicePasswordEnabled", "RequireDeviceEncryption", "RequireStorageCardEncryption")
POLICY_KEEP = ("DevicePasswordEnabled", "AlphanumericDevicePasswordRequired", "RequireDeviceEncryption",
               "RequireStorageCardEncryption", "MinDevicePasswordLength", "MaxInactivityTimeDeviceLock",
               "MaxEmailAgeFilter", "MaxCalendarAgeFilter", "AllowHTMLEmail", "MaxEmailBodyTruncationSize",
               "AllowNonProvisionableDevices")

QUARANTINE_RE = re.compile(r"quarantin|карантин|временно заблокирован|blocked from sync", re.I)
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


# ======================================================================
# Ошибки: тексты по-русски, чтобы их можно было показать George как есть
# ======================================================================
class EASError(Exception):
    fatal = False      # повтор по таймеру не поможет
    guard = False      # повтор по таймеру опасен (блокировка учётки): не стучаться до ручного запуска


class EASTransient(EASError):
    """Сеть, 5xx, троттлинг: следующий прогон, скорее всего, пройдёт."""


class EASFatal(EASError):
    fatal = True


class EASConfigError(EASFatal):
    """Не хватает настроек: .env.reader, сертификат, DeviceId."""


class EASAuthError(EASFatal):
    guard = True


class EASDeviceBlocked(EASFatal):
    """Карантин, блокировка, политика не принята, удалённая очистка."""
    guard = True


class EASHierarchyChanged(EASError):
    """Сервер просит обновить список папок (FolderSync) и повторить."""


class _Stop(BaseException):
    """SIGTERM в watch: BaseException, чтобы пролететь сквозь except Exception в httpx."""


GLOBAL_STATUS = {
    101: "сервер не разобрал запрос (InvalidContent)",
    102: "сервер не принял WBXML (InvalidWBXML)",
    103: "сервер не принял XML (InvalidXML)",
    108: "DeviceId пустой или недопустимый",
    109: "DeviceType пустой или недопустимый",
    110: "внутренняя ошибка Exchange (ServerError)",
    111: "Exchange просит повторить позже (ServerErrorRetryLater)",
    112: "Exchange не смог обратиться к Active Directory",
    113: "ящик переполнен",
    114: "сервер ящика недоступен (MailboxServerOffline)",
    123: "у пользователя нет почтового ящика",
    126: "ActiveSync отключён для этого пользователя (UserDisabledForSync)",
    127: "ящик на новом сервере не может синхронизироваться",
    128: "ящик на старом сервере не может синхронизироваться",
    129: "устройство заблокировано для этого пользователя (DeviceIsBlockedForThisUser)",
    130: "доступ запрещён (AccessDenied)",
    131: "учётная запись отключена (AccountDisabled)",
    132: "состояние синхронизации не найдено на сервере",
    133: "состояние синхронизации занято другим запросом",
    134: "состояние синхронизации повреждено",
    137: "команда не поддерживается сервером",
    138: "версия протокола не поддерживается сервером",
    139: "устройство не может полностью выполнить политику (DeviceNotFullyProvisionable)",
    140: "сервер требует удалённую очистку устройства (RemoteWipeRequested)",
    141: "политика не пускает устройства без полной поддержки политик (LegacyDeviceOnStrictPolicy)",
    142: "устройство не подготовлено (DeviceNotProvisioned)",
    143: "политика обновилась (PolicyRefresh)",
    144: "ключ политики недействителен (InvalidPolicyKey)",
    145: "внешне управляемые устройства запрещены",
    177: "достигнут лимит устройств ActiveSync на пользователя",
}
REPROVISION = {142, 143, 144}
TRANSIENT_STATUS = {110, 111, 112, 114, 133}
DEVICE_STATUS = {126, 127, 128, 129, 130, 131, 139, 140, 141, 145, 177}

SYNC_STATUS = {3: "ключ синхронизации недействителен", 4: "ошибка протокола", 5: "ошибка сервера",
               6: "ошибка преобразования на сервере", 7: "конфликт", 8: "объект не найден",
               9: "ящик переполнен", 12: "изменился список папок", 13: "пустой Sync без кэша",
               14: "недопустимый Wait/HeartbeatInterval", 15: "слишком много папок", 16: "повторить запрос"}
FOLDER_STATUS = {6: "ошибка сервера", 9: "ключ синхронизации папок недействителен",
                 10: "запрос составлен неверно", 11: "неизвестная ошибка", 12: "неизвестный код"}
PING_STATUS = {1: "изменений нет", 2: "есть изменения", 3: "не хватает параметров", 4: "ошибка синтаксиса",
               5: "heartbeat вне допустимого диапазона", 6: "слишком много папок",
               7: "нужно обновить список папок", 8: "ошибка сервера"}


# ======================================================================
# Время и часовые пояса
# ======================================================================
_TS_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})T(\d{2}):?(\d{2}):?(\d{2})(?:\.(\d{1,6}))?")


def parse_eas_time(s: str | None) -> datetime | None:
    """'20261029T080000Z' (календарь) и '2026-10-29T08:00:00.000Z' (почта) -> aware UTC."""
    if not s:
        return None
    m = _TS_RE.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, frac = m.groups()
    us = int((frac or "0").ljust(6, "0"))
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), us, tzinfo=UTC)


def eas_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _sun0(d: date) -> int:
    """День недели с воскресенья: Вс=0 ... Сб=6 (как в EAS и в SYSTEMTIME)."""
    return (d.weekday() + 1) % 7


def _in_mask(d: date, mask: int) -> bool:
    return bool(mask & (1 << _sun0(d)))


def _nth_weekday(year: int, month: int, mask: int, nth: int) -> date | None:
    """nth-й день месяца из маски дней (1..4, 5 = последний)."""
    last = _calendar.monthrange(year, month)[1]
    days = [date(year, month, i) for i in range(1, last + 1) if _in_mask(date(year, month, i), mask)]
    if not days:
        return None
    if nth >= 5:
        return days[-1]
    return days[nth - 1] if 1 <= nth <= len(days) else None


def _systemtime_transition(year: int, st: tuple) -> datetime | None:
    """SYSTEMTIME из TIME_ZONE_INFORMATION -> наивное локальное время перехода в этом году."""
    y, mo, dow, day, h, mi, s, _ms = st
    if not mo:
        return None
    if y:                                    # абсолютная дата (бывает редко)
        if y != year:
            return None
        return datetime(y, mo, min(day, _calendar.monthrange(y, mo)[1]), h, mi, s)
    first = date(year, mo, 1)
    d = 1 + (dow - _sun0(first)) % 7 + (max(day, 1) - 1) * 7
    last = _calendar.monthrange(year, mo)[1]
    while d > last:                          # day=5 значит «последний»
        d -= 7
    if h >= 24:
        return datetime(year, mo, d, 23, 59, 59)
    return datetime(year, mo, d, h, mi, s)


class WinTZ(tzinfo):
    """Часовой пояс из TIME_ZONE_INFORMATION Windows с правилами перехода на летнее время."""

    def __init__(self, bias: int, std_bias: int, dst_bias: int, std_date: tuple, dst_date: tuple,
                 name: str = ""):
        self.std = timedelta(minutes=-(bias + std_bias))
        self.dst_off = timedelta(minutes=-(bias + dst_bias))
        self.std_date, self.dst_date = std_date, dst_date
        self.name = name or "WinTZ"

    def _span(self, year: int):
        start = _systemtime_transition(year, self.dst_date)   # по зимнему времени
        end = _systemtime_transition(year, self.std_date)     # по летнему времени
        return start, end

    def _is_dst_wall(self, wall: datetime) -> bool:
        start, end = self._span(wall.year)
        if start is None or end is None:
            return False
        w = wall.replace(tzinfo=None)
        dst = start <= w < end if start < end else not (end <= w < start)
        # час после перевода назад встречается дважды: fold=1 значит второй раз, уже зимнее время
        if dst and getattr(wall, "fold", 0) and end - (self.dst_off - self.std) <= w < end:
            return False
        return dst

    def utcoffset(self, dt):
        if dt is None:
            return self.std
        return self.dst_off if self._is_dst_wall(dt) else self.std

    def dst(self, dt):
        return self.utcoffset(dt) - self.std

    def tzname(self, dt):
        return self.name

    def fromutc(self, dt):
        u = dt.replace(tzinfo=None)
        start, end = self._span(u.year)
        off = self.std
        if start is not None and end is not None:
            s_utc, e_utc = start - self.std, end - self.dst_off
            in_dst = s_utc <= u < e_utc if s_utc < e_utc else not (e_utc <= u < s_utc)
            off = self.dst_off if in_dst else self.std
            wall = u + off
            if not in_dst and end - (self.dst_off - self.std) <= wall < end:
                return wall.replace(tzinfo=self, fold=1)
            return wall.replace(tzinfo=self)
        return (u + off).replace(tzinfo=self)

    def __repr__(self):
        return f"WinTZ({self.name}, std={self.std}, dst={self.dst_off})"


def parse_timezone(blob: str | None) -> tzinfo:
    """base64 TIME_ZONE_INFORMATION организатора -> tzinfo.

    Без правил перехода: фиксированный сдвиг. С правилами: WinTZ по этим правилам.
    Пусто или мусор: Europe/Moscow.
    """
    if not blob:
        return ZoneInfo(FALLBACK_TZ)
    try:
        raw = base64.b64decode(blob)
        if len(raw) < 172 or not any(raw[:172]):
            return ZoneInfo(FALLBACK_TZ)
        vals = struct.unpack("<i64s8Hi64s8Hi", raw[:172])
    except (ValueError, struct.error, binascii.Error):
        return ZoneInfo(FALLBACK_TZ)
    bias, sname = vals[0], vals[1]
    std_date, std_bias = tuple(vals[2:10]), vals[10]
    dst_date, dst_bias = tuple(vals[12:20]), vals[20]
    name = sname.decode("utf-16-le", errors="ignore").split("\x00")[0]
    if not std_date[1] or not dst_date[1]:
        return timezone(timedelta(minutes=-(bias + std_bias)))
    return WinTZ(bias, std_bias, dst_bias, std_date, dst_date, name)


def make_timezone_blob(bias: int, std: tuple | None = None, dst: tuple | None = None,
                       std_bias: int = 0, dst_bias: int = -60, name: str = "") -> str:
    """Обратная операция (для тестов и отладки): SYSTEMTIME = (год, месяц, д.н., неделя, ч, м, с, мс)."""
    z = (0,) * 8
    nm = name.encode("utf-16-le")[:62].ljust(64, b"\x00")
    raw = struct.pack("<i64s8Hi64s8Hi", bias, nm, *(std or z), std_bias if std else 0,
                      nm, *(dst or z), dst_bias if dst else 0)
    return base64.b64encode(raw).decode()


# ======================================================================
# Развёртка повторов
# ======================================================================
def _add_months(y: int, m: int, n: int) -> tuple[int, int]:
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def _rule_dates(rec: dict, d0: date, horizon: date) -> Iterator[date]:
    """Даты вхождений (по стене организатора) от d0 по возрастанию, не дальше horizon."""
    typ = rec.get("type")
    n = max(1, int(rec.get("interval") or 1))
    mask = int(rec.get("dow") or 0)
    if typ == 0:                                         # ежедневно
        step = timedelta(days=n)
        d = d0
        while d <= horizon:
            if not mask or n != 1 or _in_mask(d, mask):
                yield d
            d += step
    elif typ == 1:                                       # еженедельно по маске дней
        mask = mask or (1 << _sun0(d0))
        fdow = int(rec.get("fdow") or 0)
        ws = d0 - timedelta(days=(_sun0(d0) - fdow) % 7)
        while ws <= horizon:
            for i in range(7):
                d = ws + timedelta(days=i)
                if d >= d0 and d <= horizon and _in_mask(d, mask):
                    yield d
            ws += timedelta(weeks=n)
    elif typ in (2, 3):                                  # ежемесячно: число или n-й день недели
        y, m = d0.year, d0.month
        while date(y, m, 1) <= horizon:
            if typ == 2:
                dom = int(rec.get("dom") or d0.day)
                d = date(y, m, min(dom, _calendar.monthrange(y, m)[1]))
            else:
                d = _nth_weekday(y, m, mask or (1 << _sun0(d0)), int(rec.get("wom") or ((d0.day - 1) // 7 + 1)))
            if d is not None and d0 <= d <= horizon:
                yield d
            y, m = _add_months(y, m, n)
    elif typ in (5, 6):                                  # ежегодно: дата или n-й день недели месяца
        moy = int(rec.get("moy") or d0.month)
        y = d0.year
        while date(y, moy, 1) <= horizon:
            if typ == 5:
                dom = int(rec.get("dom") or d0.day)
                d = date(y, moy, min(dom, _calendar.monthrange(y, moy)[1]))
            else:
                d = _nth_weekday(y, moy, mask or (1 << _sun0(d0)), int(rec.get("wom") or ((d0.day - 1) // 7 + 1)))
            if d is not None and d0 <= d <= horizon:
                yield d
            y += n
    else:
        log.warning("неизвестный тип повтора EAS %r: встреча показана одним вхождением", typ)
        yield d0


def expand(master: dict, win_start: datetime, win_end: datetime) -> list[dict]:
    """Мастер встречи -> вхождения, пересекающие окно (или чьё исходное начало в окне).

    Вхождение: {orig (datetime|None), start, end, subject, location, busy_status,
    meeting_status, all_day, attendees, body}. Арифметика повторов идёт по стене
    часового пояса организатора, поэтому 10:00 остаётся 10:00 и после перевода часов.
    """
    start, end = _dt(master["start"]), _dt(master.get("end") or master["start"])
    dur = max(end - start, timedelta(0))
    base = {k: master.get(k) for k in ("subject", "location", "busy_status", "meeting_status",
                                        "all_day", "attendees", "body")}

    def overlaps(s: datetime, e: datetime) -> bool:
        return s <= win_end and max(e, s) >= win_start

    rec = master.get("recurrence")
    if not rec:
        if overlaps(start, start + dur):
            return [dict(base, orig=None, start=start, end=start + dur)]
        return []

    tz = parse_timezone(master.get("tz"))
    wall0 = start.astimezone(tz).replace(tzinfo=None)
    horizon = (win_end.astimezone(tz) + timedelta(days=1)).date()
    until = _dt(rec.get("until"))
    until_d = until.astimezone(tz).date() if until else None
    limit = int(rec.get("occurrences") or 0)

    excs_exact: dict[datetime, dict] = {}
    excs_day: dict[date, dict] = {}
    for ex in master.get("exceptions") or []:
        o = _dt(ex.get("orig"))
        if o is None:
            continue
        excs_exact[o] = ex
        excs_day[o.astimezone(tz).date()] = ex
    if excs_day:
        # вхождение из-за горизонта могли перенести в окно: доходим и до него
        horizon = max(horizon, max(excs_day))

    out = []
    count = 0
    early = (win_start.astimezone(tz) - timedelta(days=2)).date() - timedelta(days=dur.days)
    for d in _rule_dates(rec, wall0.date(), horizon):
        if until_d and d > until_d:
            break
        count += 1
        if limit and count > limit:
            break
        if d < early and d not in excs_day:
            continue                                     # давно прошло: только считаем вхождения
        orig = datetime.combine(d, wall0.time()).replace(tzinfo=tz).astimezone(UTC)
        ex = excs_exact.get(orig) or excs_day.get(d)
        if ex and ex.get("deleted"):
            continue
        inst = dict(base, orig=orig, start=orig, end=orig + dur)
        if ex:
            _apply_exception(inst, ex, dur)
        if overlaps(inst["start"], inst["end"]) or win_start <= orig <= win_end:
            out.append(inst)
    return out


_INST_FIELDS = ("subject", "location", "busy_status", "meeting_status", "all_day", "attendees", "body")


def _apply_exception(inst: dict, ex: dict, dur: timedelta) -> dict:
    for k in _INST_FIELDS:
        if ex.get(k) is not None:
            inst[k] = ex[k]
    if ex.get("start"):
        inst["start"] = _dt(ex["start"])
        inst["end"] = _dt(ex.get("end")) or inst["start"] + dur
    elif ex.get("end"):
        inst["end"] = _dt(ex["end"])
    return inst


def outside_instances(master: dict) -> list[dict]:
    """Вхождения, которые могли уехать за окно, без развёртки правила: разовая
    встреча целиком и перенесённые исключения серии. Их строки finalize двигает
    на новое время, даже если expand() их не отдал."""
    start, end = _dt(master["start"]), _dt(master.get("end") or master["start"])
    dur = max(end - start, timedelta(0))
    base = {k: master.get(k) for k in _INST_FIELDS}
    if not master.get("recurrence"):
        return [dict(base, orig=None, start=start, end=start + dur)]
    out = []
    for ex in master.get("exceptions") or []:
        o = _dt(ex.get("orig"))
        if o is None or ex.get("deleted") or not (ex.get("start") or ex.get("end")):
            continue
        out.append(_apply_exception(dict(base, orig=o, start=o, end=o + dur), ex, dur))
    return out


# ======================================================================
# Разбор элементов Sync
# ======================================================================
def _body(ad: Node) -> tuple[str | None, bool]:
    b = ad.find("AirSyncBase:Body")
    if b is not None:
        text, cut = b.text("AirSyncBase:Data"), b.text("AirSyncBase:Truncated") == "1"
    else:
        text, cut = ad.text("Email:Body") or ad.text("Calendar:Body"), False
    if text:
        text = text.replace("\r\n", "\n").strip()
    return text or None, cut


def _emails(s: str | None) -> list[str]:
    return [e.lower() for e in EMAIL_RE.findall(s or "")]


def mail_external_id(ad: Node) -> str | None:
    """Стабильный id письма: хеш DateReceived + From + Subject (+ размер тела, если есть).

    ServerId годится только как адрес внутри текущего состояния синхронизации:
    письмо, переложенное в другую папку и обратно, и любой сброс SyncKey дают
    новый ServerId, а значит дубль. Хеш от самого письма от этого не зависит.
    EstimatedDataSize считается от полного тела в запрошенном типе (у нас всегда
    Type 1), поэтому для одного письма он один и тот же."""
    received = parse_eas_time(ad.text("Email:DateReceived"))
    if received is None:
        return None
    parts = [_iso(received), (ad.text("Email:From") or "").strip(), (ad.text("Email:Subject") or "").strip()]
    size = ad.text("AirSyncBase:Body/AirSyncBase:EstimatedDataSize")
    if size:
        parts.append(size.strip())
    return "mail:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def parse_email(server_id: str, ad: Node, folder: str, epoch: int = 0) -> Event | None:
    received = parse_eas_time(ad.text("Email:DateReceived"))
    if received is None:
        return None
    body, cut = _body(ad)
    sender = ad.text("Email:From") or ""
    conv = ad.find("Email2:ConversationId")
    thread = None
    if conv is not None and conv.value:
        thread = conv.value.hex() if isinstance(conv.value, bytes) else conv.value
    meta = {
        "from": sender, "from_email": (_emails(sender) or [""])[0],
        "to": ad.text("Email:To") or "", "cc": ad.text("Email:Cc") or "",
        "to_emails": _emails(ad.text("Email:To")), "cc_emails": _emails(ad.text("Email:Cc")),
        "read": ad.text("Email:Read") == "1",
        "importance": IMPORTANCE.get(ad.int("Email:Importance", 1), "normal"),
        # server_id + sync_epoch: адрес письма в текущем состоянии синхронизации,
        # по нему finalize() находит строку для флагов «прочитано» и удаления
        "folder": folder, "server_id": server_id, "sync_epoch": epoch, "removed": False,
        "message_class": ad.text("Email:MessageClass") or "IPM.Note",
        "has_attachments": ad.has("AirSyncBase:Attachments") or ad.has("Email:Attachments"),
    }
    if cut:
        meta["truncated"] = True
    if ad.has("Email:MeetingRequest"):
        meta["meeting_request"] = True
    return Event(external_id=mail_external_id(ad), kind="email", occurred_at=received,
                 title=ad.text("Email:Subject") or "", body=body, meta=meta,
                 actor_raw=sender or None, thread_key=thread)


def _email_patch(ad: Node) -> dict:
    """Change письма без DateReceived: только флаги (прочитано, важность)."""
    p = {}
    if ad.has("Email:Read"):
        p["read"] = ad.text("Email:Read") == "1"
    if ad.has("Email:Importance"):
        p["importance"] = IMPORTANCE.get(ad.int("Email:Importance", 1), "normal")
    flag = ad.find("Email:Flag")
    if flag is not None:
        p["flagged"] = flag.int("Email:FlagStatus", 0) == 2
    return p


def _attendees(node: Node) -> list[dict] | None:
    if not node.has("Calendar:Attendees"):
        return None
    out = []
    for a in node.findall("Calendar:Attendees/Calendar:Attendee")[:MAX_ATTENDEES]:
        out.append({"email": (a.text("Calendar:Email") or "").lower(), "name": a.text("Calendar:Name") or ""})
    return out


def _bool(node: Node, path: str) -> bool | None:
    v = node.text(path)
    return None if v is None else v == "1"


def parse_calendar(server_id: str, ad: Node) -> dict:
    """ApplicationData встречи -> компактный мастер для состояния (JSON)."""
    body, _ = _body(ad)
    rec = None
    r = ad.find("Calendar:Recurrence")
    if r is not None:
        rec = {"type": r.int("Calendar:Type"), "interval": r.int("Calendar:Interval", 1),
               "until": _iso(parse_eas_time(r.text("Calendar:Until"))),
               "occurrences": r.int("Calendar:Occurrences"), "dow": r.int("Calendar:DayOfWeek"),
               "dom": r.int("Calendar:DayOfMonth"), "wom": r.int("Calendar:WeekOfMonth"),
               "moy": r.int("Calendar:MonthOfYear"), "fdow": r.int("Calendar:FirstDayOfWeek")}
        rec = {k: v for k, v in rec.items() if v is not None}
    exceptions = []
    for ex in ad.findall("Calendar:Exceptions/Calendar:Exception"):
        eb, _ = _body(ex)
        e = {"orig": _iso(parse_eas_time(ex.text("Calendar:ExceptionStartTime"))),
             "deleted": ex.text("Calendar:Deleted") == "1",
             "subject": ex.text("Calendar:Subject"),
             "start": _iso(parse_eas_time(ex.text("Calendar:StartTime"))),
             "end": _iso(parse_eas_time(ex.text("Calendar:EndTime"))),
             "location": ex.text("Calendar:Location"),
             "busy_status": ex.int("Calendar:BusyStatus"),
             "meeting_status": ex.int("Calendar:MeetingStatus"),
             "all_day": _bool(ex, "Calendar:AllDayEvent"),
             "attendees": _attendees(ex),
             "body": eb[:STORED_BODY] if eb else None}
        exceptions.append({k: v for k, v in e.items() if v is not None})
    start = parse_eas_time(ad.text("Calendar:StartTime"))
    end = parse_eas_time(ad.text("Calendar:EndTime"))
    return {
        "server_id": server_id,
        "uid": ad.text("Calendar:UID") or f"eas-{server_id}",
        "subject": ad.text("Calendar:Subject") or "",
        "start": _iso(start), "end": _iso(end or start),
        "tz": ad.text("Calendar:TimeZone"),
        "all_day": _bool(ad, "Calendar:AllDayEvent") or False,
        "location": ad.text("Calendar:Location") or "",
        "busy_status": ad.int("Calendar:BusyStatus"),
        "meeting_status": ad.int("Calendar:MeetingStatus", 0),
        "response_type": ad.int("Calendar:ResponseType"),
        "organizer_email": (ad.text("Calendar:OrganizerEmail") or "").lower(),
        "organizer_name": ad.text("Calendar:OrganizerName") or "",
        "attendees": _attendees(ad) or [],
        "dtstamp": _iso(parse_eas_time(ad.text("Calendar:DtStamp"))),
        "body": body[:STORED_BODY] if body else None,
        "recurrence": rec,
        "exceptions": exceptions,
    }


def _nearest_date(dt: datetime, tz: tzinfo) -> date:
    """Дата ближайшей полуночи по стене tz (устойчиво к сдвигу пояса на несколько часов)."""
    return (dt.astimezone(tz) + timedelta(hours=12)).date()


def _all_day_span(master: dict, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Встреча на весь день: EAS шлёт полночь в поясе встречи, переведённую в UTC
    (Москва: 21:00Z накануне). Дату берём по поясу самой встречи и храним как
    полночь по Мадриду, как это делает iCal-коллектор, иначе зеркало сдвигает
    событие на день раньше."""
    tz = parse_timezone(master.get("tz"))
    d0 = _nearest_date(start, tz)
    d1 = _nearest_date(max(end, start), tz)
    if d1 <= d0:
        d1 = d0 + timedelta(days=1)
    return tu.at_local(d0, dtime(0, 0)), tu.at_local(d1, dtime(0, 0))


def calendar_event(master: dict, inst: dict) -> Event:
    uid = master["uid"]
    rid = _iso(inst["orig"]) if inst.get("orig") else ""
    all_day = bool(inst.get("all_day"))
    start, end = inst["start"], inst["end"]
    if all_day:
        start, end = _all_day_span(master, start, end)
    ms = inst.get("meeting_status")
    cancelled = ms in CANCELLED_MEETING
    bs = inst.get("busy_status")
    busy = {0: False, 1: True, 2: True, 3: True, 4: False}.get(bs, True)
    resp = master.get("response_type")
    if resp == 4 or cancelled:                 # отклонил или отменена: время свободно
        busy = False
    org_mail, org_name = master.get("organizer_email") or "", master.get("organizer_name") or ""
    meta = {
        "ends_at": _iso(end), "uid": uid, "recurrence_id": rid,
        "all_day": all_day, "location": inst.get("location") or "",
        "status": "cancelled" if cancelled else "confirmed", "busy": busy,
        "updated": master.get("dtstamp"), "attendees": inst.get("attendees") or [],
        "organizer": org_mail, "organizer_name": org_name,
        "response": RESPONSE.get(resp) if resp is not None else None,
        "folder": "calendar", "server_id": master.get("server_id"),
    }
    actor = f"{org_name} <{org_mail}>".strip() if org_mail else (org_name or None)
    return Event(external_id=f"{uid}|{rid}", kind="calendar", occurred_at=start,
                 title=inst.get("subject") or "", body=inst.get("body"), meta=meta, actor_raw=actor)


# ======================================================================
# Клиент ActiveSync
# ======================================================================
def _parse_backoff(value: str | None) -> float:
    """X-MS-BackOffDuration: 'L/-470' или '1500' -> секунды (значение в мс, отрицательное = не ждать)."""
    if not value:
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    if not m:
        return 0.0
    return max(0.0, float(m.group()) / 1000.0)


def _retry_after(value: str | None) -> float:
    try:
        return max(0.0, float(value)) if value else 0.0
    except ValueError:
        return 0.0


class EASClient:
    """Минимальный клиент Exchange ActiveSync 14.1 (Basic поверх TLS)."""

    def __init__(self, host: str, user: str, password: str, device_id: str, ca_file: str | None,
                 transport: httpx.BaseTransport | None = None, policy_key: str | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0):
        if not device_id or not re.fullmatch(r"[A-Za-z0-9]{1,32}", device_id):
            raise EASConfigError("ALFA_EAS_DEVICE_ID должен быть из латиницы и цифр, до 32 символов. "
                                 "Сгенерируй свой: python3 -c \"import secrets; "
                                 "print('WorkOS' + secrets.token_hex(8).upper())\"")
        if device_id.lower().startswith("appl"):
            raise EASConfigError("ALFA_EAS_DEVICE_ID похож на DeviceId айфона (Appl...). "
                                 "Нужен собственный идентификатор WorkOS, чужой брать нельзя.")
        if not user or not password:
            raise EASConfigError("не заданы ALFA_EAS_USER или ALFA_EAS_PASSWORD в .env.reader")
        self.host, self.user, self.device_id = host, user, device_id
        self.policy_key = policy_key
        self.policy_info: dict = {}
        self.policy_ack: int | None = None
        self.folders: dict[str, dict] = {}
        self.folder_sync_key = "0"
        self.sleep = sleep
        self.last_backoff = 0.0
        self._not_before = 0.0
        self._fp = hashlib.sha256(f"{host}\0{user}\0{password}\0{device_id}".encode()).hexdigest()[:12]
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        kw: dict = {"base_url": f"https://{host}", "timeout": httpx.Timeout(timeout, connect=15.0),
                    "headers": {"User-Agent": USER_AGENT}, "follow_redirects": False}
        if transport is not None:
            kw["transport"] = transport
        else:
            if not ca_file or not Path(ca_file).is_file():
                raise EASConfigError(
                    f"Нет файла корневого сертификата Минцифры ({ca_file or 'ALFA_CA_FILE не задан'}). "
                    "Положи PEM с «Russian Trusted Root CA» (и «Russian Trusted Sub CA») рядом с кодом "
                    "и укажи путь в ALFA_CA_FILE. Проверку TLS не отключаем.")
            kw["verify"] = ssl.create_default_context(cafile=str(ca_file))
        self.http = httpx.Client(**kw)

    @property
    def fingerprint(self) -> str:
        return self._fp

    def close(self) -> None:
        self.http.close()

    # ---------- транспорт ----------
    def _wait_backoff(self) -> None:
        left = self._not_before - time.monotonic()
        if left > 0:
            log.info("Exchange просил притормозить: жду %.1f с", left)
            self.sleep(left)

    def _note_backoff(self, r: httpx.Response) -> float:
        wait = min(MAX_BACKOFF, max(_parse_backoff(r.headers.get("X-MS-BackOffDuration")),
                                    _retry_after(r.headers.get("Retry-After"))))
        self.last_backoff = wait
        if wait > 0:
            self._not_before = max(self._not_before, time.monotonic() + wait)
        return wait

    def _send(self, method: str, cmd: str | None, body: bytes | None, policy_key: str | None,
              timeout: float | None = None) -> httpx.Response:
        params = None
        headers = {"Authorization": self._auth, "MS-ASProtocolVersion": PROTOCOL}
        if cmd:
            params = {"Cmd": cmd, "User": self.user, "DeviceId": self.device_id, "DeviceType": DEVICE_TYPE}
            headers["Content-Type"] = CONTENT_TYPE
        if policy_key:
            headers["X-MS-PolicyKey"] = policy_key
        to = httpx.Timeout(timeout, connect=15.0) if timeout else httpx.USE_CLIENT_DEFAULT
        what = cmd or method
        for attempt in range(4):
            self._wait_backoff()
            try:
                r = self.http.request(method, EAS_PATH, params=params, headers=headers,
                                      content=body, timeout=to)
            except httpx.TimeoutException as e:
                raise EASTransient(f"Exchange не ответил вовремя на {what} ({type(e).__name__})") from e
            except httpx.TransportError as e:
                txt = str(e)
                if "CERTIFICATE_VERIFY_FAILED" in txt or "certificate verify failed" in txt:
                    raise EASConfigError(
                        "TLS не прошёл проверку сертификата owa.alfabank.ru. В ALFA_CA_FILE должен лежать "
                        "PEM с «Russian Trusted Root CA» и, если сервер не отдаёт цепочку, ещё и "
                        f"«Russian Trusted Sub CA». Проверку не отключаем. ({txt[:200]})") from e
                if attempt < 2:
                    self.sleep(5.0 * (attempt + 1))
                    continue
                raise EASTransient(f"нет связи с {self.host}: {txt[:200]}") from e
            wait = self._note_backoff(r)
            if r.status_code in (503, 429):
                if attempt < 3:
                    pause = min(MAX_BACKOFF, wait or 30.0 * (2 ** attempt))
                    log.warning("Exchange ограничивает частоту (%s на %s): пауза %.0f с", r.status_code, what, pause)
                    self._not_before = max(self._not_before, time.monotonic() + pause)
                    continue
                raise EASTransient(f"Exchange ограничивает частоту запросов (HTTP {r.status_code} на {what}); "
                                   "попробую в следующий раз")
            return r
        raise EASTransient(f"{what}: исчерпаны попытки")  # pragma: no cover

    def _check_http(self, r: httpx.Response, what: str) -> None:
        code = r.status_code
        if code == 200:
            return
        if code == 401:
            raise EASAuthError(
                f"Exchange отклонил логин или пароль (HTTP 401 на {what}). Проверь ALFA_EAS_USER "
                f"(формат MOSCOW\\логин) и ALFA_EAS_PASSWORD в .env.reader: пароль мог смениться. "
                "Автоматически не повторяю, чтобы не заблокировать учётку.")
        if code == 403:
            raise EASDeviceBlocked(self._blocked_text(f"доступ запрещён (HTTP 403 на {what})"))
        if code == 449:
            raise EASDeviceBlocked("сервер снова требует Provision (HTTP 449), хотя устройство только что "
                                   "подготовлено. " + self._ask_it())
        if code == 451:
            raise EASFatal(f"Exchange перенаправляет на другой сервер: {r.headers.get('X-MS-Location', '?')} "
                           "(HTTP 451). Пропиши его в ALFA_EAS_HOST.")
        if code in (500, 502, 504):
            raise EASTransient(f"ошибка сервера Exchange (HTTP {code} на {what})")
        if code in (501, 505):
            raise EASFatal(f"Exchange не поддерживает команду {what} или версию {PROTOCOL} (HTTP {code})")
        snippet = r.text[:200].replace("\n", " ") if r.content else ""
        if "quarantin" in snippet.lower():
            raise EASDeviceBlocked(self._blocked_text(f"устройство в карантине (HTTP {code})"))
        raise EASError(f"неожиданный ответ Exchange: HTTP {code} на {what}. {snippet}")

    def _ask_it(self) -> str:
        return ("Нужно, чтобы ИТ Альфы одобрили устройство в Exchange ActiveSync (Allow/Block/Quarantine): "
                f"пользователь {self.user}, DeviceType {DEVICE_TYPE}, DeviceId {self.device_id}, "
                f"User-Agent {USER_AGENT}.")

    def _blocked_text(self, what: str) -> str:
        return (f"Exchange не пускает WorkOS: {what}. Скорее всего устройство в карантине или "
                f"заблокировано. {self._ask_it()} Автоматически не повторяю.")

    def _status_error(self, st: int, what: str) -> EASError:
        txt = GLOBAL_STATUS.get(st, "неизвестный код")
        if st == 140:
            return EASDeviceBlocked("Exchange требует удалённую очистку устройства WorkOS (статус 140). "
                                    "Синхронизация остановлена, данные не стираю. Разберись с ИТ, почему "
                                    f"прислали очистку. {self._ask_it()}")
        if st in (139, 141):
            return EASDeviceBlocked(
                f"{what}: {txt} (статус {st}). Политика ActiveSync требует PIN-код или шифрование, а WorkOS "
                "честно ответил, что выполнить их не может. Попроси ИТ разрешить для этого устройства неполную "
                "поддержку политик (AllowNonProvisionableDevices) или отдельную политику для WorkOS. "
                + self._ask_it())
        if st in DEVICE_STATUS:
            return EASDeviceBlocked(self._blocked_text(f"{txt}, статус {st} на {what}"))
        if st in TRANSIENT_STATUS:
            return EASTransient(f"{what}: {txt} (статус {st}), повторю позже")
        return EASError(f"{what}: {txt} (статус {st})")

    @staticmethod
    def _decode(r: httpx.Response) -> Node | None:
        if not r.content:
            return None
        if r.content[:1] != b"\x03":
            raise EASError(f"Exchange прислал не WBXML ({r.headers.get('Content-Type', '?')}): "
                           f"{r.text[:200]}")
        try:
            return decode(r.content)
        except WBXMLError as e:
            raise EASError(f"не разобрал ответ Exchange: {e}") from e

    def command(self, cmd: str, root: Node | None, timeout: float | None = None) -> Node | None:
        """Команда с политикой: при 449 / 142-144 один раз переподготовиться и повторить."""
        if not self.policy_key:
            self.provision()
        body = encode(root) if root is not None else b""
        if log.isEnabledFor(logging.DEBUG) and root is not None:
            log.debug("-> %s\n%s", cmd, root.to_xml())
        for attempt in (0, 1):
            r = self._send("POST", cmd, body, self.policy_key, timeout)
            if r.status_code == 449 and attempt == 0:
                log.info("Exchange просит Provision (HTTP 449), переподготавливаюсь")
                self.provision()
                continue
            self._check_http(r, cmd)
            tree = self._decode(r)
            if log.isEnabledFor(logging.DEBUG) and tree is not None:
                log.debug("<- %s\n%s", cmd, tree.to_xml())
            st = tree.int("Status") if tree is not None else None
            if st in REPROVISION:
                if attempt == 0:
                    log.info("Exchange: %s (статус %s), переподготавливаюсь", GLOBAL_STATUS[st], st)
                    self.provision()
                    continue
                raise self._status_error(141, cmd)
            if st is not None and st >= 100:
                raise self._status_error(st, cmd)
            return tree
        raise EASError(f"{cmd}: не удалось после повторной подготовки")  # pragma: no cover

    # ---------- команды ----------
    def options(self) -> dict:
        r = self._send("OPTIONS", None, None, None)
        self._check_http(r, "OPTIONS")
        h = r.headers
        return {"server": h.get("MS-Server-ActiveSync", ""),
                "versions": [v.strip() for v in h.get("MS-ASProtocolVersions", "").split(",") if v.strip()],
                "commands": [v.strip() for v in h.get("MS-ASProtocolCommands", "").split(",") if v.strip()],
                "backoff": h.get("X-MS-BackOffDuration", "")}

    def _provision_call(self, root: Node, key: str | None) -> Node:
        r = self._send("POST", "Provision", encode(root), key)
        self._check_http(r, "Provision")
        tree = self._decode(r)
        if tree is None:
            raise EASError("Provision: пустой ответ Exchange")
        st = tree.int("Status")
        if st is not None and st >= 100:
            raise self._status_error(st, "Provision")
        if tree.has("RemoteWipe") or tree.has("Provision:AccountOnlyRemoteWipe"):
            raise self._status_error(140, "Provision")
        if st == 2:
            raise EASError("Exchange не понял запрос Provision (статус 2): вероятна ошибка в кодеке WBXML. "
                           "Запусти python -m collectors.eas --probe -v и пришли вывод.")
        if st != 1:
            raise EASError(f"Provision: статус {st}")
        return tree

    def _ack_status(self, doc: Node | None) -> int:
        """Честный ответ на политику: 1, если требовать нечего, иначе 3 (PIN/шифрование не применить)."""
        info = {}
        if doc is not None:
            for c in doc.children:
                if not c.children and c.name in POLICY_KEEP:
                    info[c.name] = c.text()
        self.policy_info = info
        if any(info.get(k) == "1" for k in UNENFORCEABLE):
            return 3
        return 1

    def provision(self) -> str:
        """Две фазы: получить временный ключ и политику, подтвердить, получить постоянный ключ."""
        self.policy_key = None
        req1 = el("Provision:Provision",
                  el("Settings:DeviceInformation",
                     el("Settings:Set",
                        el("Settings:Model", DEVICE_TYPE),
                        el("Settings:FriendlyName", "WorkOS (сервер George)"),
                        el("Settings:OS", "Linux"),
                        el("Settings:OSLanguage", "Russian"),
                        el("Settings:UserAgent", USER_AGENT))),
                  el("Provision:Policies", el("Provision:Policy", el("Provision:PolicyType", POLICY_TYPE))))
        t1 = self._provision_call(req1, None)
        pol = t1.find("Provision:Policies/Provision:Policy")
        if pol is None:
            raise EASError("Provision: в ответе нет политики")
        pst = pol.int("Provision:Status", 1)
        temp = pol.text("Provision:PolicyKey")
        if pst == 2 and not temp:               # политики для устройства нет вовсе
            self.policy_key, self.policy_ack = "0", None
            return self.policy_key
        if pst not in (1, 2) or not temp:
            raise EASError(f"Provision: Exchange не выдал политику (статус политики {pst})")
        ack = self._ack_status(pol.find("Provision:Data/Provision:EASProvisionDoc"))
        self.policy_ack = ack
        if ack != 1:
            log.warning("политика требует PIN или шифрование: подтверждаю честно статусом %s", ack)
        req2 = el("Provision:Provision",
                  el("Provision:Policies",
                     el("Provision:Policy",
                        el("Provision:PolicyType", POLICY_TYPE),
                        el("Provision:PolicyKey", temp),
                        el("Provision:Status", ack))))
        t2 = self._provision_call(req2, temp)
        pol2 = t2.find("Provision:Policies/Provision:Policy")
        key = pol2.text("Provision:PolicyKey") if pol2 is not None else None
        if pol2 is None or pol2.int("Provision:Status", 1) != 1 or not key:
            if ack != 1:
                raise self._status_error(141, "Provision")
            raise EASDeviceBlocked(self._blocked_text("Exchange не выдал постоянный ключ политики"))
        self.policy_key = key
        return key

    def folder_sync(self) -> dict[str, dict]:
        """Список папок. Первый раз SyncKey=0, дальше изменения; держит self.folders."""
        for attempt in (0, 1):
            key = self.folder_sync_key or "0"
            t = self.command("FolderSync", el("FolderHierarchy:FolderSync", el("FolderHierarchy:SyncKey", key)))
            if t is None:
                raise EASError("FolderSync: пустой ответ")
            st = t.int("FolderHierarchy:Status", 1)
            if st == 9 and attempt == 0:
                log.warning("FolderSync: ключ недействителен, начинаю список папок заново")
                self.folder_sync_key, self.folders = "0", {}
                continue
            if st != 1:
                err = EASTransient if st == 6 else EASError
                raise err(f"FolderSync: {FOLDER_STATUS.get(st, 'статус')} ({st})")
            break
        if key == "0":
            self.folders = {}
        self.folder_sync_key = t.text("FolderHierarchy:SyncKey") or key
        changes = t.find("FolderHierarchy:Changes")
        for n in (changes.children if changes is not None else []):
            sid = n.text("FolderHierarchy:ServerId")
            if not sid:
                continue
            if n.name in ("Add", "Update"):
                cur = self.folders.get(sid, {})
                self.folders[sid] = {
                    "name": n.text("FolderHierarchy:DisplayName") or cur.get("name", ""),
                    "type": n.int("FolderHierarchy:Type", cur.get("type", 1)),
                    "parent": n.text("FolderHierarchy:ParentId") or cur.get("parent", "0"),
                }
            elif n.name == "Delete":
                self.folders.pop(sid, None)
        return self.folders

    def sync(self, folder_id: str, sync_key: str, cls: str = "Email", window_size: int = WINDOW_SIZE,
             filter_type: int | None = None, truncation: int = MAIL_TRUNCATION, body_type: int = 1) -> dict:
        """Один Sync одной папки. status 3 возвращается (ключ сбросит вызывающий), 12 -> EASHierarchyChanged."""
        coll = [el("AirSync:SyncKey", sync_key), el("AirSync:CollectionId", folder_id)]
        if sync_key != "0":
            coll.append(el("AirSync:GetChanges", "1"))
        coll.append(el("AirSync:WindowSize", window_size))
        opts = []
        if filter_type is not None:
            opts.append(el("AirSync:FilterType", filter_type))
        opts.append(el("AirSyncBase:BodyPreference", el("AirSyncBase:Type", body_type),
                       el("AirSyncBase:TruncationSize", truncation)))
        coll.append(el("AirSync:Options", *opts))
        req = el("AirSync:Sync", el("AirSync:Collections", el("AirSync:Collection", *coll)))
        res = {"status": 1, "sync_key": sync_key, "more": False, "adds": [], "changes": [],
               "deletes": [], "soft_deletes": [], "class": cls}
        t = self.command("Sync", req)
        if t is None:                           # пустой ответ: изменений нет
            return res
        top = t.int("AirSync:Status")
        if top is not None and top != 1:
            if top == 12:
                raise EASHierarchyChanged("Sync: изменился список папок")
            raise EASError(f"Sync: {SYNC_STATUS.get(top, 'ошибка')} (статус {top})")
        col = next((c for c in t.findall("AirSync:Collections/AirSync:Collection")
                    if c.text("AirSync:CollectionId") in (None, folder_id)), None)
        if col is None:
            return res
        st = col.int("AirSync:Status", 1)
        res["status"] = st
        if st == 3:
            return res
        if st == 12:
            raise EASHierarchyChanged("Sync: изменился список папок")
        if st in (5, 16):
            raise EASTransient(f"Sync папки {folder_id}: {SYNC_STATUS[st]} (статус {st})")
        if st != 1:
            raise EASError(f"Sync папки {folder_id}: {SYNC_STATUS.get(st, 'ошибка')} (статус {st})")
        res["sync_key"] = col.text("AirSync:SyncKey") or sync_key
        res["more"] = col.has("AirSync:MoreAvailable")
        cmds = col.find("AirSync:Commands")
        for c in (cmds.children if cmds is not None else []):
            sid = c.text("AirSync:ServerId")
            if not sid:
                continue
            ad = c.find("AirSync:ApplicationData") or Node("AirSync:ApplicationData")
            if c.name == "Add":
                res["adds"].append((sid, ad))
            elif c.name == "Change":
                res["changes"].append((sid, ad))
            elif c.name == "Delete":
                res["deletes"].append(sid)
            elif c.name == "SoftDelete":
                res["soft_deletes"].append(sid)
        return res

    def ping(self, folders: list[tuple[str, str]], heartbeat: int = 900) -> dict:
        """Долгий опрос: ждёт до heartbeat секунд изменений в папках [(id, 'Email'|'Calendar')]."""
        req = el("Ping:Ping", el("Ping:HeartbeatInterval", heartbeat),
                 el("Ping:Folders", *[el("Ping:Folder", el("Ping:Id", fid), el("Ping:Class", cls))
                                      for fid, cls in folders]))
        t = self.command("Ping", req, timeout=heartbeat + 120)
        if t is None:
            return {"status": 1, "folders": [], "heartbeat": None, "max_folders": None}
        return {"status": t.int("Ping:Status", 1),
                "folders": [f.text() for f in t.findall("Ping:Folders/Ping:Folder") if f.text()],
                "heartbeat": t.int("Ping:HeartbeatInterval"),
                "max_folders": t.int("Ping:MaxFolders")}


def client_from_env(**kw) -> EASClient:
    """Клиент из .env.reader: ALFA_EAS_HOST, ALFA_EAS_USER, ALFA_EAS_PASSWORD, ALFA_EAS_DEVICE_ID, ALFA_CA_FILE."""
    missing = [n for n in ("ALFA_EAS_USER", "ALFA_EAS_PASSWORD", "ALFA_EAS_DEVICE_ID") if not config.env(n)]
    if missing:
        raise EASConfigError(f"не заданы {', '.join(missing)} в .env.reader")
    ca = config.env("ALFA_CA_FILE") or (str(DEFAULT_CA) if DEFAULT_CA.is_file() else None)
    return EASClient(config.env("ALFA_EAS_HOST", DEFAULT_HOST) or DEFAULT_HOST,
                     config.env("ALFA_EAS_USER"), config.env("ALFA_EAS_PASSWORD"),
                     config.env("ALFA_EAS_DEVICE_ID"), ca, **kw)


# ======================================================================
# Коллектор
# ======================================================================
class EASCollector(Collector):
    """Источник 'eas:alfa' (контекст alfa): «Входящие» -> email, «Календарь» -> calendar."""

    kind = "eas"

    def __init__(self, source: dict, client: EASClient | None = None, now: datetime | None = None,
                 force: bool = False, refresh_folders: bool = False):
        super().__init__(source)
        st = self.state if isinstance(self.state, dict) else {}
        if st.get("cursor") is None:
            st.pop("cursor", None)
        self.state = st
        self.cfg = source.get("config") or {}
        self._client = client
        self.now = now
        self.force = force
        self.refresh_folders = refresh_folders
        self._reset_run()

    def _reset_run(self) -> None:
        self._mail_patches: dict[tuple[str, int], dict] = {}   # (ServerId, эпоха синхронизации) -> флаги
        self._deleted_uids: set[str] = set()
        self._known_ids: set[str] = set()       # вхождения, отданные в этом прогоне
        self._known_uids: set[str] = set()
        self._moved_out: dict[str, Event] = {}  # встречи, уехавшие за окно: только обновить строку
        self._touched: set[str] = set()         # мастера встреч, пришедшие в этом прогоне (Add/Change)
        self.win = (None, None)
        self.counts = {"email": 0, "calendar": 0}

    @property
    def client(self) -> EASClient:
        if self._client is None:
            self._client = client_from_env()
        if not self._client.policy_key and self.state.get("policy_key"):
            self._client.policy_key = self.state["policy_key"]
        return self._client

    # ---------- защита от повторов, которые опасны ----------
    def _guard(self) -> None:
        if self.force:
            return
        last = self.source.get("last_error") or ""
        m = re.search(r"\[eas-stop:(\w+)\]", last)
        if m and m.group(1) == self.client.fingerprint:
            first = last.split("\n", 1)[0].split(": ", 1)[-1]
            first = re.sub(r"\s*\[eas-stop:\w+\]", "", first)
            raise EASDeviceBlocked(
                "Прошлая попытка закончилась ошибкой, которую повтор не исправит, поэтому в Exchange не "
                "стучусь (чтобы не заблокировать учётку). Исправь причину и запусти вручную "
                f"python -m collectors.eas --probe или --once. Причина: {first}")

    # ---------- основной проход ----------
    def collect(self) -> list[Event]:
        self._reset_run()
        try:
            self._guard()
            return self._collect()
        except EASError as e:
            if e.guard and "[eas-stop:" not in str(e) and self._client is not None:
                raise type(e)(f"{e} [eas-stop:{self._client.fingerprint}]") from e
            raise
        finally:
            if self._client is not None:
                self._save_client_state()

    def _save_client_state(self) -> None:
        cl = self._client
        self.state["policy_key"] = cl.policy_key
        if cl.policy_info:
            self.state["policy"] = {"info": cl.policy_info, "ack": cl.policy_ack}

    def _collect(self) -> list[Event]:
        st = self.state
        now = self.now or tu.now()
        past = int(self.cfg.get("past_days", 7))
        future = int(self.cfg.get("future_days", 60))
        self.win = (now - timedelta(days=past), now + timedelta(days=future))
        if self.refresh_folders or not st.get("folders"):
            self._folder_sync()
        inbox, cal = self._folder(2), self._folder(8)
        if inbox is None or cal is None:
            self._folder_sync()
            inbox, cal = self._folder(2), self._folder(8)
        if inbox is None or cal is None:
            raise EASError("в ящике не нашлись «Входящие» или «Календарь» (FolderSync без папок типа 2/8)")
        events: list[Event] = []
        events += self._sync_folder(inbox, "Email")
        self._sync_folder(cal, "Calendar")
        events += self._expand_calendar()
        st["synced_at"] = _iso(tu.now())
        self.counts["email"] = sum(1 for e in events if e.kind == "email")
        self.counts["calendar"] = sum(1 for e in events if e.kind == "calendar")
        return events

    def _folder_sync(self) -> None:
        cl = self.client
        cl.folder_sync_key = self.state.get("folder_sync_key") or "0"
        cl.folders = dict(self.state.get("folders") or {})
        folders = cl.folder_sync()
        self.state["folder_sync_key"] = cl.folder_sync_key
        self.state["folders"] = folders
        # ключи папок, которых больше нет, не держим
        keys = self.state.setdefault("sync_keys", {})
        for fid in list(keys):
            if fid not in folders:
                keys.pop(fid)

    def _folder(self, ftype: int) -> str | None:
        hits = [fid for fid, f in (self.state.get("folders") or {}).items() if int(f.get("type", 0)) == ftype]
        return sorted(hits, key=lambda x: (len(x), x))[0] if hits else None

    def _filter(self, cls: str) -> int:
        pol = (self.state.get("policy") or {}).get("info") or {}
        if cls == "Email":
            ft = int(self.cfg.get("mail_filter", 3))            # 3 = неделя
            cap = int(pol.get("MaxEmailAgeFilter") or 0)
        else:
            ft = int(self.cfg.get("cal_filter", 4))             # 4 = две недели
            cap = int(pol.get("MaxCalendarAgeFilter") or 0)
        if cap and (ft == 0 or ft > cap):                       # политика ограничивает глубину
            ft = cap
        return ft

    def _sync_folder(self, fid: str, cls: str) -> list[Event]:
        st = self.state
        cl = self.client
        keys = st.setdefault("sync_keys", {})
        key = keys.get(fid, "0")
        out: list[Event] = []
        resets = pages = 0
        hier_retry = False
        more = False
        trunc = MAIL_TRUNCATION if cls == "Email" else CAL_TRUNCATION
        while True:
            if cls == "Email" and key == "0":
                # новое состояние синхронизации: ServerId раздаются заново, старые
                # адреса писем (meta.server_id) с этого момента ничего не значат
                st["mail_epoch"] = int(st.get("mail_epoch") or 0) + 1
            try:
                r = cl.sync(fid, key, cls, WINDOW_SIZE, self._filter(cls), trunc)
            except EASHierarchyChanged:
                if hier_retry:
                    raise
                hier_retry = True
                self._folder_sync()
                if fid not in st["folders"]:
                    return out
                continue
            if r["status"] == 3:
                resets += 1
                if resets > 1:
                    raise EASError(f"Exchange дважды отверг ключ синхронизации папки {fid}")
                log.warning("Sync %s: ключ %s недействителен, синхронизирую папку заново", cls, key)
                key = keys[fid] = "0"
                if cls == "Calendar":
                    st.setdefault("resync", {})[fid] = []
                continue
            initial = key == "0"
            out += self._apply(fid, cls, r)
            key = keys[fid] = r["sync_key"]
            more = r["more"]
            if initial and key != "0":
                continue                                  # после SyncKey 0 сразу забираем содержимое
            if not more:
                break
            pages += 1
            if pages >= MAX_PAGES:
                log.info("Sync %s: остальное заберу в следующий прогон", cls)
                break
        resync = (st.get("resync") or {}).get(fid)
        if cls == "Calendar" and resync is not None and not more:
            seen = set(resync)
            masters = st.setdefault("cal_masters", {})
            for sid in [s for s in masters if s not in seen]:
                self._deleted_uids.add(masters.pop(sid)["uid"])
            st["resync"].pop(fid, None)
        return out

    def _apply(self, fid: str, cls: str, r: dict) -> list[Event]:
        st = self.state
        out: list[Event] = []
        if cls == "Email":
            fname = (st.get("folders", {}).get(fid) or {}).get("name") or "Inbox"
            epoch = int(st.get("mail_epoch") or 0)
            for sid, ad in r["adds"] + r["changes"]:
                if ad.has("Email:DateReceived"):
                    ev = parse_email(sid, ad, fname, epoch)
                    if ev:
                        out.append(ev)
                        self._check_quarantine(ev)
                else:
                    patch = _email_patch(ad)
                    if patch:
                        self._mail_patches.setdefault((sid, epoch), {}).update(patch)
            for sid in r["deletes"]:                      # SoftDelete = ушло за фильтр, не удаление
                self._mail_patches.setdefault((sid, epoch), {})["removed"] = True
            return out
        masters = st.setdefault("cal_masters", {})
        resync = (st.get("resync") or {}).get(fid)
        for sid, ad in r["adds"]:
            masters[sid] = parse_calendar(sid, ad)
            self._touched.add(sid)
            if resync is not None:
                resync.append(sid)
        for sid, ad in r["changes"]:
            self._touched.add(sid)
            parsed = parse_calendar(sid, ad)
            if ad.has("Calendar:StartTime") or sid not in masters:
                masters[sid] = parsed
            else:                                          # частичное изменение: сливаем
                cur = masters[sid]
                for k, tag in _CAL_FIELD.items():
                    if ad.has(tag):
                        cur[k] = parsed[k]
        for sid in r["deletes"]:
            m = masters.pop(sid, None)
            if m:
                self._deleted_uids.add(m["uid"])
        for sid in r["soft_deletes"]:                     # ушло за фильтр, это не отмена
            masters.pop(sid, None)
        return out

    def _check_quarantine(self, ev: Event) -> None:
        if QUARANTINE_RE.search(ev.title or "") and "exchange" in (ev.actor_raw or "").lower() + (ev.title or "").lower():
            self.state["quarantine_notice"] = {"subject": ev.title, "at": _iso(ev.occurred_at)}
            log.warning("похоже, устройство WorkOS в карантине Exchange: %s. Попроси ИТ одобрить DeviceId %s",
                        ev.title, self.client.device_id)

    def _expand_calendar(self) -> list[Event]:
        """Вхождения в окне -> события. Известными (_known_ids) считаются только
        отданные события: строка встречи, которую в этот раз не отдали, либо
        уехала за окно (её двигает finalize по _moved_out), либо пропала (отмена)."""
        win_start, win_end = self.win
        masters = self.state.setdefault("cal_masters", {})
        out: list[Event] = []
        for sid, m in list(masters.items()):
            stale = self._stale(m, win_start)
            if stale:
                # серию укоротили или встречу унесли далеко в прошлое: из состояния
                # убираем, но строки в окне ещё надо подвинуть или отменить
                masters.pop(sid)
            self._known_uids.add(m["uid"])
            emitted: set[str] = set()
            for inst in ([] if stale else expand(m, win_start, win_end)):
                ev = calendar_event(m, inst)
                emitted.add(ev.external_id)
                out.append(ev)
            self._known_ids |= emitted
            if sid not in self._touched:
                continue                        # без изменений: за окно сама не уедет
            for inst in outside_instances(m):
                ev = calendar_event(m, inst)
                if ev.external_id not in emitted:
                    self._moved_out[ev.external_id] = ev
        return out

    @staticmethod
    def _stale(m: dict, win_start: datetime) -> bool:
        """Мастер, у которого точно не будет вхождений в окне: из состояния убираем."""
        edge = win_start - timedelta(days=1)
        rec = m.get("recurrence")
        if not rec:
            return (_dt(m.get("end")) or _dt(m["start"])) < edge
        until = _dt(rec.get("until"))
        return until is not None and until < edge

    def _in_window(self, dt: datetime | None) -> bool:
        ws, we = self.win
        return dt is not None and ws is not None and ws <= dt <= we

    def finalize(self, conn, seen: set[str]) -> None:
        sid = self.source_id
        # флаги писем: строку ищем по адресу в текущем состоянии синхронизации
        # (server_id + эпоха), external_id от ServerId больше не зависит. Строки
        # старого формата (mail:<ServerId>, без эпохи) считаются эпохой 0.
        for (server_id, epoch), patch in self._mail_patches.items():
            conn.execute("UPDATE work.events SET meta = meta || %s "
                         "WHERE source_id = %s AND kind = 'email' AND meta->>'server_id' = %s "
                         "AND COALESCE(meta->>'sync_epoch', '0') = %s AND NOT (meta @> %s)",
                         (db.jb(patch), sid, server_id, str(epoch), db.jb(patch)))
        # встреча уехала за окно: строку двигаем туда же (новых строк за окном не создаём)
        for ext, ev in self._moved_out.items():
            meta = db.jb(ev.meta)
            conn.execute("UPDATE work.events SET occurred_at = %s, title = %s, meta = meta || %s "
                         "WHERE source_id = %s AND kind = 'calendar' AND external_id = %s "
                         "AND (occurred_at <> %s OR title IS DISTINCT FROM %s OR NOT (meta @> %s))",
                         (ev.occurred_at, ev.title, meta, sid, ext, ev.occurred_at, ev.title, meta))
        cancel = db.jb({"status": "cancelled", "busy": False})
        known = list(self._known_ids)
        if self._deleted_uids:
            conn.execute("UPDATE work.events SET meta = meta || %s "
                         "WHERE source_id = %s AND kind = 'calendar' AND meta->>'uid' = ANY(%s) "
                         "AND NOT (external_id = ANY(%s)) "
                         "AND COALESCE(meta->>'status', '') <> 'cancelled'",
                         (cancel, sid, list(self._deleted_uids), known))
        if self._known_uids:
            rows = conn.execute("SELECT id, external_id, occurred_at, meta->>'recurrence_id' AS rid "
                                "FROM work.events WHERE source_id = %s AND kind = 'calendar' "
                                "AND meta->>'uid' = ANY(%s) AND COALESCE(meta->>'status', '') <> 'cancelled'",
                                (sid, list(self._known_uids))).fetchall()
            stale = []
            for r in rows:
                if r["external_id"] in self._known_ids:
                    continue
                # не отдана, а исходное начало или текущее место строки в окне:
                # вхождение удалили или унесли за окно, призрака в окне не оставляем
                orig = _dt(r["rid"]) if r["rid"] else r["occurred_at"]
                if self._in_window(orig) or self._in_window(r["occurred_at"]):
                    stale.append(r["id"])
            if stale:
                conn.execute("UPDATE work.events SET meta = meta || %s WHERE id = ANY(%s)", (cancel, stale))


# поле мастера -> тег, по наличию которого в частичном Change поле обновляется
_CAL_FIELD = {"subject": "Calendar:Subject", "location": "Calendar:Location",
              "busy_status": "Calendar:BusyStatus", "meeting_status": "Calendar:MeetingStatus",
              "response_type": "Calendar:ResponseType", "attendees": "Calendar:Attendees",
              "dtstamp": "Calendar:DtStamp", "all_day": "Calendar:AllDayEvent",
              "recurrence": "Calendar:Recurrence", "exceptions": "Calendar:Exceptions",
              "tz": "Calendar:TimeZone", "organizer_email": "Calendar:OrganizerEmail",
              "organizer_name": "Calendar:OrganizerName", "body": "AirSyncBase:Body"}


# ======================================================================
# Точки входа
# ======================================================================
def _load_source() -> dict:
    with db.connect() as conn:
        src = conn.execute("SELECT * FROM work.sources WHERE id = %s", (SOURCE_ID,)).fetchone()
    if not src:
        raise EASConfigError(f"в work.sources нет строки {SOURCE_ID} (миграция 006)")
    return src


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    """Один проход Sync. dry_run: собрать и посчитать, в базу не писать."""
    src = _load_source()
    col = EASCollector(src, client=kw.get("client"), now=kw.get("now"), force=kw.get("force", False))
    if dry_run:
        try:
            col.collect()
        finally:
            if kw.get("client") is None and col._client is not None:
                col._client.close()
        return {"source": SOURCE_ID, "dry_run": True, **col.counts}
    try:
        return run_source(SOURCE_ID, col)
    finally:
        if kw.get("client") is None and col._client is not None:
            col._client.close()


def probe(client: EASClient | None = None) -> str:
    """OPTIONS + Provision + FolderSync, печатает папки. В базу ничего не пишет."""
    cl = client or client_from_env()
    lines = []
    info = cl.options()
    lines.append(f"Сервер: {cl.host} · Exchange ActiveSync {info['server'] or '?'}")
    lines.append(f"Версии протокола: {', '.join(info['versions']) or '?'} · работаем на {PROTOCOL}")
    need = {"Sync", "FolderSync", "Provision", "Ping"}
    missing = need - set(info["commands"]) if info["commands"] else set()
    lines.append("Команды: все нужные на месте" if not missing else f"Нет команд: {', '.join(sorted(missing))}")
    if info["backoff"]:
        lines.append(f"X-MS-BackOffDuration: {info['backoff']}")
    key = cl.provision()
    pol = ", ".join(f"{k}={v}" for k, v in sorted(cl.policy_info.items())) or "без требований"
    ack = {1: "применена", 3: "не применена (PIN/шифрование), ответили честно", None: "не требуется"}
    lines.append(f"Provision: ключ получен (…{key[-4:]}), политика {ack.get(cl.policy_ack, cl.policy_ack)}")
    lines.append(f"Политика: {pol}")
    folders = cl.folder_sync()
    lines.append(f"Папок: {len(folders)}")
    for fid, f in sorted(folders.items(), key=lambda kv: (kv[1]["type"] not in (2, 8), kv[1]["type"], kv[1]["name"])):
        mark = " <-" if f["type"] in (2, 8) else ""
        lines.append(f"  {fid:>8}  {FOLDER_TYPES.get(f['type'], f['type']):<16} {f['name']}{mark}")
    lines.append(f"DeviceId {cl.device_id}, DeviceType {DEVICE_TYPE}: в базу ничего не записано.")
    if client is None:
        cl.close()
    return "\n".join(lines)


def _note_error(err: Exception) -> None:
    try:
        with db.connect(autocommit=True) as conn:
            conn.execute("UPDATE work.sources SET last_error = %s WHERE id = %s",
                         (f"{type(err).__name__}: {err}", SOURCE_ID))
    except Exception:  # noqa: BLE001 — база тоже может лежать, главное не упасть здесь
        log.exception("не записал ошибку в work.sources")


def watch(heartbeat: int = 900, max_interval: int = 1800, max_cycles: int | None = None,
          client: EASClient | None = None, **kw) -> dict:
    """Сервис: Ping с длинным heartbeat, Sync при изменениях и не реже раза в 30 минут.

    SIGTERM/SIGINT: дожидаемся конца записи в базу и выходим. Неисправимые ошибки
    (пароль, карантин) останавливают цикл: status='fatal'. Источник выключен:
    status='disabled', нет настроек ALFA_EAS_*: status='config'. На все три main()
    выходит с кодом 3, и systemd (RestartPreventExitStatus=3) не перезапускает
    сервис каждые 10 секунд.
    """
    try:
        src = _load_source()
    except EASConfigError as e:
        log.error("eas-watch не запускаю: %s. Выход с кодом 3, без перезапуска", e)
        return {"source": SOURCE_ID, "status": "config", "error": str(e)}
    if not src.get("enabled") and not kw.get("force"):
        msg = "источник eas:alfa выключен (work.sources.enabled = false)"
        log.warning("eas-watch не запускаю: %s. Выход с кодом 3, systemd не перезапускает. Включи источник "
                    "и запусти сервис заново: systemctl restart workos-eas", msg)
        return {"source": SOURCE_ID, "status": "disabled", "message": msg}
    own_client = client is None
    try:
        cl = client or client_from_env()
    except EASConfigError as e:
        log.error("eas-watch не запускаю, не хватает настроек: %s. Выход с кодом 3, без перезапуска; "
                  "заполни .env.reader и запусти systemctl restart workos-eas", e)
        return {"source": SOURCE_ID, "status": "config", "error": str(e)}
    flags = {"stop": False, "critical": False}

    def on_signal(signum, frame):
        flags["stop"] = True
        log.info("получен сигнал %s, останавливаюсь", signum)
        if not flags["critical"]:
            raise _Stop()

    old = {}
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGTERM, signal.SIGINT):
            old[s] = signal.signal(s, on_signal)
    summary = {"source": SOURCE_ID, "status": "stopped", "collections": 0, "pings": 0, "errors": 0}
    need, refresh, last = True, False, None
    backoff = 30.0
    sleep = kw.get("sleep", time.sleep)
    folders: list[tuple[str, str]] = []
    try:
        while not flags["stop"]:
            phase = "sync"
            try:
                if need or last is None or time.monotonic() - last >= max_interval:
                    col = EASCollector(_load_source(), client=cl, now=kw.get("now"),
                                       force=kw.get("force", False), refresh_folders=refresh)
                    flags["critical"] = True
                    try:
                        res = run_source(SOURCE_ID, col)
                    finally:
                        flags["critical"] = False
                    log.info("Sync: %s", res)
                    summary["collections"] += 1
                    need, refresh, last = False, False, time.monotonic()
                    folders = [(col._folder(2), "Email"), (col._folder(8), "Calendar")]
                    folders = [f for f in folders if f[0]]
                    if flags["stop"]:
                        break
                if max_cycles is not None and summary["pings"] >= max_cycles:
                    break
                summary["pings"] += 1
                phase = "ping"
                r = cl.ping(folders, heartbeat)
                ps = r["status"]
                if ps == 2:
                    need = True
                elif ps == 5 and r.get("heartbeat"):
                    log.info("Exchange просит heartbeat %s вместо %s", r["heartbeat"], heartbeat)
                    heartbeat = int(r["heartbeat"])
                elif ps == 7:
                    need, refresh = True, True
                elif ps not in (1,):
                    raise (EASTransient if ps == 8 else EASError)(
                        f"Ping: {PING_STATUS.get(ps, 'ошибка')} (статус {ps})")
                backoff = 30.0
            except EASError as e:
                if e.fatal:
                    log.error("%s", e)
                    if phase == "ping":                 # ошибку прогона Sync уже записал run_source
                        _note_error(e)
                    summary.update(status="fatal", error=str(e))
                    break
                summary["errors"] += 1
                log.warning("ошибка EAS, пауза %.0f с: %s", backoff, e)
                need = True
                sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except (psycopg.Error, httpx.HTTPError, ValueError) as e:
                summary["errors"] += 1
                log.warning("ошибка (%s), пауза %.0f с: %s", type(e).__name__, backoff, e)
                need = True
                sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
    except _Stop:
        pass
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if own_client:
            cl.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m collectors.eas",
                                description="Exchange ActiveSync Альфы: почта и календарь в work.events")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--probe", action="store_true", help="OPTIONS + Provision + FolderSync, в базу ничего")
    g.add_argument("--once", action="store_true", help="один проход Sync (по умолчанию)")
    g.add_argument("--watch", action="store_true", help="Ping-цикл, Sync при изменениях")
    p.add_argument("--dry-run", action="store_true", help="с --once: собрать, но не писать в базу")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог (с деревьями WBXML)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if a.probe:
            print(probe())
            return 0
        if a.watch:
            res = watch()
            print(res)
            # 3 = «перезапуск не поможет» (RestartPreventExitStatus=3 в workos-eas.service)
            return 3 if res.get("status") in ("fatal", "disabled", "config") else 0
        print(run(dry_run=a.dry_run, force=True))
        return 0
    except EASError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 3 if e.fatal else 1


if __name__ == "__main__":
    sys.exit(main())

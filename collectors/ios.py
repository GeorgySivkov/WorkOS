"""Альфа через айфон (замена EAS): команда «WorkOS Alfa» в «Командах» iOS
по расписанию (7, 11, 15, 19) шлёт встречи календаря Альфы и свежие письма
ящика Alfabank на приёмник (collectors/ingest_server.py), источник вида 'ios'.

Новое устройство в Exchange не добавляется: данные читает сам айфон, на
котором ящик и календарь уже настроены. Формат запроса (POST /ingest/ios:alfa):

    {"now": "2026-11-22T11:00:00+01:00",            # время запуска, ISO 8601
     "calendar": [{"title", "start", "end", "all_day", "location"}],
     "mail": [{"account", "mailbox", "subject", "sender", "to", "cc",
               "received", "read", "body"}]}

Встречи: команда берёт «Find Calendar Events» по календарю Альфы на 60 дней
вперёд. У событий в «Командах» нет постоянного id, поэтому id = хеш
(начало, название): перенесённая встреча становится новой строкой, а старая
отменяется, как в iCal-коллекторе: вхождения этого источника с началом в окне
[now, now + 60 дней), которых нет в присланном списке, получают
status='cancelled'. Пустой список ничего не отменяет (скорее сбой, чем пустой
календарь на два месяца). Зеркало в Google-календарь «Альфа» видит их через
work.calendar_feed, как встречи из EAS.

Письма: только account из настроек источника (по умолчанию «Alfabank»),
остальные отбрасываются и на сервере. id = хеш (время получения, отправитель,
тема), как в EAS без размера тела. Повторная отправка того же письма
обновляет флаг «прочитано», а не плодит дубли.

Настройки источника (work.sources.config): {"account": "Alfabank",
"window_days": 60, "health_hours": 13}.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, time as dtime, timedelta, timezone

from collectors.base import Event
from core import config, timeutil as tu

UTC = timezone.utc
WINDOW_DAYS = 60
MAX_CAL = 2000
MAX_MAIL = 2000
BODY_LIMIT = 50_000
EMAIL_RE = re.compile(r"[\w.+\-']+@[\w\-]+(?:\.[\w\-]+)+")
TRUE = {"1", "true", "yes", "да", "on"}


class BadPayload(ValueError):
    pass


def _text(v, field: str, limit: int, required: bool = False) -> str:
    if v is None or v == "":
        if required:
            raise BadPayload(f"нет поля {field}")
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise BadPayload(f"поле {field} должно быть строкой")
    if isinstance(v, float) and not math.isfinite(v):
        raise BadPayload(f"поле {field}: не число")
    return str(v).strip()[:limit]


def _flag(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in TRUE


def _when(v, field: str, required: bool = True) -> datetime | None:
    s = _text(v, field, 64, required=required)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise BadPayload(f"{field}: ожидается ISO 8601, пришло {s[:40]}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)         # айфон без пояса: время по Мадриду
    return dt.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def _emails(s: str) -> list[str]:
    return [e.lower() for e in EMAIL_RE.findall(s or "")]


def _list(payload: dict, key: str, limit: int) -> list:
    raw = payload.get(key)
    if raw is None:
        return []
    if isinstance(raw, dict):                  # «Команды» отдают один элемент без списка
        raw = [raw]
    if not isinstance(raw, list):
        raise BadPayload(f"{key} должен быть списком")
    if len(raw) > limit:
        raise BadPayload(f"{key}: не больше {limit} за раз")
    for r in raw:
        if not isinstance(r, dict):
            raise BadPayload(f"{key}: элементы должны быть объектами")
    return raw


def calendar_event(r: dict) -> Event:
    title = _text(r.get("title"), "calendar.title", 1000)
    start = _when(r.get("start"), "calendar.start")
    end = _when(r.get("end"), "calendar.end", required=False) or start + timedelta(minutes=30)
    all_day = _flag(r.get("all_day"))
    if all_day:
        # как iCal и EAS: весь день хранится полночью по Мадриду
        d0 = start.astimezone(config.TZ).date()
        d1 = max(end - timedelta(seconds=1), start).astimezone(config.TZ).date() + timedelta(days=1)
        start, end = tu.at_local(d0, dtime(0, 0)), tu.at_local(d1, dtime(0, 0))
    if end < start:
        end = start
    uid = "ios:" + _hash(_iso(start), title)
    meta = {"ends_at": _iso(end), "uid": uid, "recurrence_id": "", "all_day": all_day,
            "location": _text(r.get("location"), "calendar.location", 500),
            "status": "confirmed", "busy": not all_day, "folder": "calendar", "via": "ios"}
    return Event(external_id=uid, kind="calendar", occurred_at=start, title=title, body=None, meta=meta)


def mail_event(r: dict) -> Event:
    received = _when(r.get("received"), "mail.received")
    sender = _text(r.get("sender"), "mail.sender", 500)
    subject = _text(r.get("subject"), "mail.subject", 1000)
    to = _text(r.get("to"), "mail.to", 4000)
    cc = _text(r.get("cc"), "mail.cc", 4000)
    body = _text(r.get("body"), "mail.body", BODY_LIMIT + 1)
    meta = {"from": sender, "from_email": (_emails(sender) or [""])[0],
            "to": to, "cc": cc, "to_emails": _emails(to), "cc_emails": _emails(cc),
            "read": _flag(r.get("read")), "importance": "normal",
            "folder": _text(r.get("mailbox"), "mail.mailbox", 200) or "inbox",
            "removed": False, "via": "ios"}
    if len(body) > BODY_LIMIT:
        body, meta["truncated"] = body[:BODY_LIMIT], True
    ext = "mail:" + _hash(received.isoformat(timespec="seconds"), sender, subject)
    return Event(external_id=ext, kind="email", occurred_at=received, title=subject,
                 body=body or None, meta=meta, actor_raw=sender or None)


def parse(payload: dict, cfg: dict | None = None) -> tuple[list[Event], list[Event], tuple[datetime, datetime] | None]:
    """(встречи, письма, окно отмены). Окно None: календарь не присылали или он пуст."""
    if not isinstance(payload, dict):
        raise BadPayload("ожидается объект")
    cfg = cfg or {}
    account = str(cfg.get("account") or "Alfabank").strip().lower()
    now = _when(payload.get("now"), "now")
    cal = [calendar_event(r) for r in _list(payload, "calendar", MAX_CAL)]
    mail = []
    for r in _list(payload, "mail", MAX_MAIL):
        if _text(r.get("account"), "mail.account", 200).lower() != account:
            continue                            # чужой ящик: не записываем
        mail.append(mail_event(r))
    window = None
    if cal:
        days = int(cfg.get("window_days") or WINDOW_DAYS)
        window = (now, now + timedelta(days=days))
    return cal, mail, window


CANCEL_SQL = """UPDATE work.events SET meta = meta || '{"status": "cancelled", "busy": false}'::jsonb
WHERE source_id = %s AND kind = 'calendar'
  AND occurred_at >= %s AND occurred_at < %s
  AND NOT (external_id = ANY(%s))
  AND COALESCE(meta->>'status', 'confirmed') <> 'cancelled'"""


def cancel_missing(conn, source_id: str, window: tuple[datetime, datetime], seen: list[str]) -> int:
    cur = conn.execute(CANCEL_SQL, (source_id, window[0], window[1], seen))
    return cur.rowcount or 0

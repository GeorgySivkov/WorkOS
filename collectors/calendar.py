"""Календари через секретный iCal-адрес (шаг 2.3), без OAuth.

Курсора у iCal нет: каждый прогон тянет ленту целиком, разворачивает
повторы в окне [сейчас - 7 дней, сейчас + 60 дней] и пишет каждое
вхождение отдельным событием kind='calendar':
  external_id = f"{UID}|{recurrence_id}", recurrence_id = ISO UTC начала
  вхождения по RECURRENCE-ID для повторяющихся встреч и '' для разовых.
Перенос встречи обновляет время и название (base.UPSERT так делает для
kind='calendar'). Вхождение, исчезнувшее из ленты внутри окна, помечается
meta.status='cancelled' в finalize(): строки не удаляются никогда.

Зеркало: WorkOS пишет копии встреч Инфатики и Альфы и блоки плана в
отдельный календарь, а здесь читается только адрес основного личного
календаря (и календаря Инфатики). Адрес календаря-зеркала сюда не
подключать: копии вернутся в базу и встречи задвоятся. Страховка на случай,
если подключат: события с UID вида <32 символа 0-9a-v>@google.com (так Google
экспортирует id копий зеркала и блоков плана, executors.calendar.mirror_id)
пропускаются.

Настройки источника (work.sources.config):
  {"url_env": "ICAL_PERSONAL_URL", "past_days": 7, "future_days": 60,
   "self_emails": ["george@..."]}
По умолчанию url_env выводится из id источника: ical:personal -> ICAL_PERSONAL_URL.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

import httpx
import icalendar
import recurring_ical_events

from collectors.base import Collector, Event, run_enabled
from core import config, timeutil as tu

UTC = timezone.utc
# id копий зеркала и блоков плана (sha1 в base32hex, 32 символа) в iCal-экспорте Google
OWN_UID_RE = re.compile(r"^[0-9a-v]{32}@google\.com$")


def default_url_env(source_id: str) -> str:
    suffix = source_id.split(":", 1)[1] if ":" in source_id else source_id
    return f"ICAL_{suffix.upper().replace('-', '_')}_URL"


def to_utc(v) -> datetime:
    """date -> локальная полночь в UTC; наивное время считается временем Europe/Madrid."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=config.TZ)
        return v.astimezone(UTC)
    if isinstance(v, date):
        return tu.at_local(v, time(0, 0))
    raise TypeError(f"не дата: {v!r}")


def rid_str(v) -> str:
    if isinstance(v, datetime):
        return to_utc(v).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _addr(v) -> tuple[str | None, str | None]:
    if v is None:
        return None, None
    email = str(v)
    if email.lower().startswith("mailto:"):
        email = email[7:]
    name = getattr(v, "params", {}).get("CN") if hasattr(v, "params") else None
    return (email or None), (str(name) if name else None)


class ICalCollector(Collector):
    kind = "ical"

    def __init__(self, source: dict, transport: httpx.BaseTransport | None = None,
                 now: datetime | None = None):
        super().__init__(source)
        self.cfg = source.get("config") or {}
        self.transport = transport
        self.now = now
        self.window: tuple[datetime, datetime] | None = None
        self.feed_events = 0

    def fetch(self) -> str:
        env_name = self.cfg.get("url_env") or default_url_env(self.source_id)
        url = config.env(env_name, required=True)
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        with httpx.Client(timeout=60, transport=self.transport, follow_redirects=True) as http:
            r = http.get(url)
        if r.status_code != 200:
            # в текст ошибки адрес не пишем: он секретный
            raise RuntimeError(f"iCal {env_name}: HTTP {r.status_code}")
        return r.text

    def collect(self) -> Iterable[Event]:
        now = self.now or tu.now()
        start = now - timedelta(days=int(self.cfg.get("past_days", 7)))
        end = now + timedelta(days=int(self.cfg.get("future_days", 60)))
        cal = icalendar.Calendar.from_ical(self.fetch())
        vevents = list(cal.walk("VEVENT"))
        self.feed_events = len(vevents)
        recurring = {str(c.get("UID")) for c in vevents
                     if c.get("RRULE") or c.get("RDATE") or c.get("RECURRENCE-ID")}
        selves = {e.lower() for e in self.cfg.get("self_emails") or []}
        out: dict[str, Event] = {}
        for comp in recurring_ical_events.of(cal).between(start, end):
            ev = self._event(comp, recurring, selves)
            if ev:
                out[ev.external_id] = ev
        self.window = (start, end)
        self.state = {"cursor": now.isoformat(), "feed_events": self.feed_events}
        return list(out.values())

    def _event(self, comp, recurring: set[str], selves: set[str]) -> Event | None:
        uid = str(comp.get("UID") or "")
        if not uid or comp.get("DTSTART") is None:
            return None
        if OWN_UID_RE.match(uid):
            return None                 # наша же копия или блок плана: не возвращаем в базу
        dtstart = comp.get("DTSTART").dt
        all_day = not isinstance(dtstart, datetime)
        starts = to_utc(dtstart)
        if comp.get("DTEND") is not None:
            ends = to_utc(comp.get("DTEND").dt)
        elif comp.get("DURATION") is not None:
            ends = starts + comp.get("DURATION").dt
        else:
            ends = starts + (timedelta(days=1) if all_day else timedelta(0))
        rid = ""
        if uid in recurring:
            r = comp.get("RECURRENCE-ID")
            rid = rid_str(r.dt) if r is not None else rid_str(dtstart)
        status = "cancelled" if str(comp.get("STATUS") or "").upper() == "CANCELLED" else "confirmed"
        attendees = []
        raw_att = comp.get("ATTENDEE") or []
        my_partstat = None
        for a in raw_att if isinstance(raw_att, list) else [raw_att]:
            email, name = _addr(a)
            if not email:
                continue
            attendees.append({"email": email, "name": name})
            if email.lower() in selves:
                my_partstat = str(a.params.get("PARTSTAT") or "").lower() or None
        org_email, org_name = _addr(comp.get("ORGANIZER"))
        transparent = str(comp.get("TRANSP") or "").upper() == "TRANSPARENT"
        busy = not transparent and not all_day and status != "cancelled" and my_partstat != "declined"
        lm = comp.get("LAST-MODIFIED")
        seq = comp.get("SEQUENCE")
        meta = {
            "ends_at": ends.isoformat(), "uid": uid, "recurrence_id": rid, "all_day": all_day,
            "location": str(comp.get("LOCATION")) if comp.get("LOCATION") else None,
            "status": status, "busy": busy,
            "updated": to_utc(lm.dt).isoformat() if lm is not None else (str(int(seq)) if seq is not None else None),
            "sequence": int(seq) if seq is not None else 0,
            "attendees": attendees, "organizer": org_email, "organizer_name": org_name,
        }
        if my_partstat:
            meta["my_status"] = my_partstat
        desc = comp.get("DESCRIPTION")
        return Event(external_id=f"{uid}|{rid}", kind="calendar", occurred_at=starts,
                     title=str(comp.get("SUMMARY") or "") or None,
                     body=str(desc) if desc else None, meta=meta, actor_raw=org_email,
                     thread_key=f"ical:{uid}")

    def finalize(self, conn, seen: set[str]) -> None:
        """Вхождения этого источника в окне, которых больше нет в ленте, становятся cancelled."""
        if not self.window or self.feed_events == 0:
            return              # пустая лента скорее сбой Google, чем отмена всего календаря
        start, end = self.window
        conn.execute(
            """UPDATE work.events SET meta = meta || '{"status": "cancelled", "busy": false}'::jsonb
               WHERE source_id = %s AND kind = 'calendar'
                 AND occurred_at >= %s AND occurred_at < %s
                 AND NOT (external_id = ANY(%s))
                 AND COALESCE(meta->>'status', 'confirmed') <> 'cancelled'""",
            (self.source_id, start, end, list(seen)))


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"ical": run_enabled("ical")}


if __name__ == "__main__":
    print(run())

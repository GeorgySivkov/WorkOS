"""Время: хранится в UTC, вводится и показывается по Europe/Madrid.

Логический день заканчивается в 4:00: задача, отмеченная в 00:30,
засчитывается сегодняшнему дню, а «завтра» после полуночи значит день,
который начнётся, когда George проснётся.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from core import config

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def local(dt: datetime | None = None) -> datetime:
    return (dt or now()).astimezone(config.TZ)


def parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def logical_date(dt: datetime | None = None) -> date:
    """Дата логического дня для момента dt."""
    return (local(dt) - timedelta(hours=config.LOGICAL_DAY_END_HOUR)).date()


def logical_today() -> date:
    return logical_date(now())


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def at_local(d: date, t: time) -> datetime:
    """Локальные дата и время -> aware datetime в UTC (переход часов учтён zoneinfo)."""
    return datetime.combine(d, t, tzinfo=config.TZ).astimezone(UTC)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    """Границы логического дня d в UTC: [d 04:00, d+1 04:00)."""
    h = config.LOGICAL_DAY_END_HOUR
    return at_local(d, time(h)), at_local(d + timedelta(days=1), time(h))


def waking_window(d: date) -> tuple[datetime, datetime]:
    """Окно бодрствования логического дня d: с подъёма до отбоя (отбой после полуночи)."""
    wake, sleep = parse_hm(config.WAKE), parse_hm(config.SLEEP)
    start = at_local(d, wake)
    end_day = d + timedelta(days=1) if sleep <= wake else d
    return start, at_local(end_day, sleep)


def is_quiet(dt: datetime | None = None) -> bool:
    """Тихие часы (по умолчанию 1:00-8:00): бот ничего не шлёт."""
    t = local(dt).time()
    qs, qe = parse_hm(config.QUIET_START), parse_hm(config.QUIET_END)
    if qs <= qe:
        return qs <= t < qe
    return t >= qs or t < qe


def next_quiet_end(dt: datetime | None = None) -> datetime:
    loc = local(dt)
    qe = parse_hm(config.QUIET_END)
    cand = datetime.combine(loc.date(), qe, tzinfo=config.TZ)
    if cand <= loc:
        cand += timedelta(days=1)
    return cand.astimezone(UTC)


RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
RU_WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря"]


def ru_date(d: date, weekday: bool = True) -> str:
    s = f"{d.day} {RU_MONTHS_GEN[d.month - 1]}"
    return f"{RU_WD_SHORT[d.weekday()].capitalize()}, {s}" if weekday else s


def hm(dt: datetime | None) -> str:
    return local(dt).strftime("%H:%M") if dt else ""

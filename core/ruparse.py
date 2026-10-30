"""Разбор даты и времени из русской фразы без модели.

«позвонить в банк в пт в 10» -> title «позвонить в банк», due пятница, 10:00.
Работает в мини-приложении (там модели нет) и как сверка для разбора моделью
в боте: явная дата из текста надёжнее догадки модели про «завтра».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core import timeutil as tu

_MONTHS = {m: i + 1 for i, m in enumerate(tu.RU_MONTHS_GEN)}
_WD = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среду": 2, "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятницу": 4, "пятница": 4, "пт": 4,
    "субботу": 5, "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}
_NUM = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5}

B = r"(?<![\w])"   # граница слова, совместимая с кириллицей
E = r"(?![\w])"

RX_DATE_NUM = re.compile(B + r"(?:на\s+|к\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?" + E)
RX_DATE_TXT = re.compile(B + r"(?:на\s+|к\s+)?(\d{1,2})\s+(" + "|".join(_MONTHS) + r")" + E, re.I)
RX_REL = re.compile(B + r"(?:на\s+)?(сегодня|завтра|послезавтра)" + E, re.I)
RX_IN = re.compile(B + r"через\s+(\d+|" + "|".join(_NUM) + r")?\s*(день|дня|дней|неделю|недели|недель)" + E, re.I)
RX_WD = re.compile(B + r"(?:в|во|к)?\s*(" + "|".join(sorted(_WD, key=len, reverse=True)) + r")" + E, re.I)
RX_WEEK = re.compile(B + r"(?:на\s+)?(этой|следующей)?\s*неделе" + E, re.I)
RX_LATER = re.compile(B + r"(потом|когда-нибудь|когда нибудь)" + E, re.I)
RX_TIME = re.compile(
    B + r"(?:в|к|до)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(?:ч|час(?:а|ов)?))?(?:\s+(утра|дня|вечера|ночи))?" + E, re.I)
RX_TIME_BARE = re.compile(B + r"(\d{1,2}):(\d{2})" + E)
RX_NOON = re.compile(B + r"в\s+полдень" + E, re.I)


@dataclass
class Parsed:
    title: str
    due: date | None = None
    week_of: date | None = None
    at: time | None = None
    remind_at: datetime | None = None
    explicit_later: bool = False

    @property
    def bucket(self) -> str:
        if self.due:
            return "day"
        if self.week_of:
            return "week"
        return "later"


def _hour(h: int, mer: str | None) -> int | None:
    if mer:
        mer = mer.lower()
        if mer in ("дня", "вечера") and h < 12:
            h += 12
        if mer == "ночи" and h == 12:
            h = 0
        if mer == "утра" and h == 12:
            h = 0
    return h if 0 <= h <= 23 else None


def parse(text: str, now: datetime | None = None) -> Parsed:
    now = now or tu.now()
    today = tu.logical_date(now)
    spans: list[tuple[int, int]] = []
    due: date | None = None
    week_of: date | None = None
    at: time | None = None
    later = False

    def take(m):
        spans.append(m.span())

    # время раньше дат: «в 10.30» не должно стать датой 10.30
    m = RX_NOON.search(text)
    if m:
        at = time(12, 0)
        take(m)
    if at is None:
        for m in RX_TIME.finditer(text):
            # «к 23.10» — это дата, а не время 23:10, если минуты похожи на месяц и нет «:»
            h = int(m.group(1))
            mins = m.group(2)
            if mins and "." in m.group(0) and int(mins) <= 12 and not m.group(3):
                continue
            hh = _hour(h, m.group(3))
            mm = int(mins) if mins else 0
            if hh is not None and 0 <= mm <= 59:
                at = time(hh, mm)
                take(m)
                break
    if at is None:
        m = RX_TIME_BARE.search(text)
        if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
            at = time(int(m.group(1)), int(m.group(2)))
            take(m)

    def free(m) -> bool:
        a, b = m.span()
        return all(b <= s or a >= e for s, e in spans)

    for m in RX_DATE_NUM.finditer(text):
        if not free(m):
            continue
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
            cand = date(year, mo, d)
            if not y and cand < today - timedelta(days=30):
                cand = date(year + 1, mo, d)
            due = cand
            take(m)
            break
        except ValueError:
            continue
    if due is None:
        m = RX_DATE_TXT.search(text)
        if m and free(m):
            try:
                cand = date(today.year, _MONTHS[m.group(2).lower()], int(m.group(1)))
                if cand < today - timedelta(days=30):
                    cand = cand.replace(year=today.year + 1)
                due = cand
                take(m)
            except ValueError:
                pass
    if due is None:
        m = RX_REL.search(text)
        if m and free(m):
            due = today + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1).lower()])
            take(m)
    if due is None:
        m = RX_IN.search(text)
        if m and free(m):
            n_raw = (m.group(1) or "1").lower()
            n = int(n_raw) if n_raw.isdigit() else _NUM.get(n_raw, 1)
            unit = m.group(2).lower()
            due = today + timedelta(days=n * (7 if unit.startswith("недел") else 1))
            take(m)
    if due is None:
        m = RX_WD.search(text)
        if m and free(m):
            wd = _WD[m.group(1).lower()]
            delta = (wd - today.weekday()) % 7
            due = today + timedelta(days=delta)
            take(m)
    if due is None:
        m = RX_WEEK.search(text)
        if m and free(m):
            nxt = (m.group(1) or "").lower() == "следующей"
            week_of = tu.week_monday(today) + timedelta(days=7 if nxt else 0)
            take(m)
    m = RX_LATER.search(text)
    if m and free(m) and due is None and week_of is None:
        later = True
        take(m)

    remind_at = None
    if at is not None:
        if due is None:
            # время без даты: ближайшее будущее внутри логических суток
            d = today + timedelta(days=1) if at.hour < 4 else today
            cand = tu.at_local(d, at)
            if cand <= now:
                d += timedelta(days=1)
                cand = tu.at_local(d, at)
            due = d
        remind_at = tu.at_local(due, at)

    title = text
    for a, b in sorted(spans, reverse=True):
        title = title[:a] + " " + title[b:]
    title = re.sub(r"\s+", " ", title).strip(" ,.;:-—")
    return Parsed(title=title or text.strip(), due=due, week_of=week_of, at=at,
                  remind_at=remind_at, explicit_later=later)

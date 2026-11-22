"""Периоды привычек из названия и статус «пора ли» на сегодня (мини-приложение, вариант А).

Период читается из названия практики:
    «3 раза в 7 дней»            3 отметки за календарную неделю (пн-вс)
    «Один день без работы из 7»  1 отметка за календарную неделю
    «раз в 14 дней», «раз в 2 дня»  следующий раз не позже чем через N дней после прошлого
    «в выходные»                 раз за выходные, видна только в сб и вс
    «в месяц»                    раз за календарный месяц
    «раз в год»                  раз в 365 дней
    остальное                    ежедневная

Периодическая привычка не висит невыполненной каждый день: на «Сегодня» она появляется,
только когда подходит срок (окно «пора»), и уходит до следующего окна, как только отмечена.
Серия считается по периодам: недели, промежутки, выходные, месяцы подряд.
Если отметок ещё не было, отсчёт идёт от дня, когда практика появилась в сетке.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

RX_TIMES = re.compile(r"(\d+)\s+раза?\s+в\s+(\d+)\s+д", re.I)                   # 3 раза в 7 дней
RX_OF = re.compile(r"(\d+|один|одна|одно)\s+(?:[^\s\d]+\s+){0,3}из\s+(\d+)\b", re.I)  # 1 день без работы из 7
RX_EVERY = re.compile(r"раз\s+в\s+(\d+)\s+д", re.I)                              # раз в 14 дней, раз в 2 дня
RX_YEAR = re.compile(r"раз\s+в\s+год", re.I)
RX_MONTH = re.compile(r"\bв\s+месяц", re.I)
RX_WEEKEND = re.compile(r"\bв\s+выходные", re.I)


def parse(title: str) -> dict:
    t = title or ""
    if m := RX_TIMES.search(t):
        k, n = int(m.group(1)), int(m.group(2))
        return {"kind": "quota", "times": k, "days": n} if n >= 2 else {"kind": "daily"}
    if m := RX_OF.search(t):
        k = int(m.group(1)) if m.group(1).isdigit() else 1
        return {"kind": "quota", "times": k, "days": int(m.group(2))}
    if m := RX_EVERY.search(t):
        n = int(m.group(1))
        return {"kind": "every", "days": n} if n >= 2 else {"kind": "daily"}
    if RX_YEAR.search(t):
        return {"kind": "every", "days": 365}
    if RX_WEEKEND.search(t):
        return {"kind": "weekend"}
    if RX_MONTH.search(t):
        return {"kind": "month"}
    return {"kind": "daily"}


def lead(n: int) -> int:
    """За сколько дней до срока привычка появляется на «Сегодня»."""
    if n <= 2:
        return 0
    if n <= 7:
        return 2
    if n <= 14:
        return 3
    if n <= 31:
        return 5
    if n <= 90:
        return 10
    return 30


def _dm(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def _left(n: int) -> str:
    return "пора · сегодня" if n <= 0 else f"пора · осталось {n} дн."


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def status(spec: dict, days: set[date], today: date, since: date | None = None) -> dict:
    """{due, note, streak} для периодической привычки. days = логические дни с отметкой."""
    done_today = today in days
    past = sorted(d for d in days if d <= today)
    kind = spec["kind"]

    if kind == "every":
        n = spec["days"]
        last = past[-1] if past else None
        base = last if last else (since or today)
        due_day = base + timedelta(days=n)
        streak = 0
        if last and today <= due_day:
            streak, prev = 1, last
            for d in reversed(past[:-1]):
                if (prev - d).days > n:
                    break
                streak, prev = streak + 1, d
        if done_today:
            return {"due": False, "note": "сделано · следующий к " + _dm(due_day), "streak": streak}
        if today < due_day - timedelta(days=lead(n)):
            return {"due": False, "note": "следующий к " + _dm(due_day), "streak": streak}
        if today <= due_day:
            return {"due": True, "note": _left((due_day - today).days), "streak": streak}
        return {"due": True, "note": "пора · срок был " + _dm(due_day), "streak": 0}

    if kind == "quota":
        k, n = spec["times"], spec["days"]
        if n == 7:                                   # календарная неделя
            start = _week_start(today)
            end = start + timedelta(days=6)
            got = sum(1 for d in past if d >= start)
            weeks, w = 0, start if got >= k else start - timedelta(days=7)
            while sum(1 for d in past if w <= d < w + timedelta(days=7)) >= k:
                weeks, w = weeks + 1, w - timedelta(days=7)
            left = (end - today).days + 1            # дней до конца недели, включая сегодня
            note = f"{min(got, k)}/{k} на неделе"
            due = got < k and not done_today and left <= (k - got) + 1
            return {"due": due, "note": ("пора · " if due else "") + note, "streak": weeks}
        got = sum(1 for d in past if d > today - timedelta(days=n))
        due = got < k and not done_today
        return {"due": due, "note": f"{min(got, k)}/{k} за {n} дн.", "streak": 0}

    if kind == "weekend":
        sat = _week_start(today) + timedelta(days=5)
        done = any(d >= sat for d in past) if today >= sat else False
        weekends, s = 0, sat if (today >= sat and done) else sat - timedelta(days=7)
        while any(s <= d <= s + timedelta(days=1) for d in past):
            weekends, s = weekends + 1, s - timedelta(days=7)
        if today < sat:
            return {"due": False, "note": "в выходные", "streak": weekends}
        if done:
            return {"due": False, "note": "сделано в эти выходные", "streak": weekends}
        return {"due": True, "note": _left((sat + timedelta(days=1) - today).days), "streak": weekends}

    if kind == "month":
        first = today.replace(day=1)
        last_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        done = any(d >= first for d in past)
        months, m = 0, first if done else (first - timedelta(days=1)).replace(day=1)
        while any(m <= d < (m + timedelta(days=32)).replace(day=1) for d in past):
            months, m = months + 1, (m - timedelta(days=1)).replace(day=1)
        if done:
            return {"due": False, "note": "в этом месяце сделано", "streak": months}
        left = (last_day - today).days
        if left > 7:
            return {"due": False, "note": "до конца месяца " + _dm(last_day), "streak": months}
        return {"due": True, "note": _left(left), "streak": months}

    # ежедневная: как раньше, серия дней подряд (сегодня ещё не отмечено, но вчера было - серия жива)
    d = today if done_today else today - timedelta(days=1)
    streak = 0
    while d in days:
        streak, d = streak + 1, d - timedelta(days=1)
    return {"due": not done_today, "note": "", "streak": streak}


FORMS = {"daily": ("день", "дня", "дней"), "every": ("раз", "раза", "раз"),
         "quota": ("неделя", "недели", "недель"), "weekend": ("выходные", "выходных", "выходных"),
         "month": ("месяц", "месяца", "месяцев")}


def forms(spec: dict) -> tuple[str, str, str]:
    """Слова для «серия N …»: дни подряд у ежедневной, недели у «3 раза в 7 дней» и т.д."""
    return FORMS.get(spec["kind"], FORMS["daily"])


def streak_of(title: str, days: set[date], today: date) -> tuple[int, tuple[str, str, str]]:
    spec = parse(title)
    return status(spec, days, today)["streak"], forms(spec)

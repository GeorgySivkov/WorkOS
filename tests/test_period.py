"""Периоды привычек: разбор названия и окно «пора» (rpg.period)."""
from datetime import date, timedelta

import pytest

from rpg import period as p

MON = date(2026, 11, 23)          # понедельник


def d(n: int) -> date:
    return MON + timedelta(days=n)


@pytest.mark.parametrize("title,spec", [
    ("Итальянский с женой 3 раза в 7 дней", {"kind": "quota", "times": 3, "days": 7}),
    ("Один день без работы из 7", {"kind": "quota", "times": 1, "days": 7}),
    ("Сноуборд 2 дня из 14 зимой", {"kind": "quota", "times": 2, "days": 14}),
    ("Созвон с родителями раз в 7 дней", {"kind": "every", "days": 7}),
    ("Силовая раз в 2 дня", {"kind": "every", "days": 2}),
    ("Поездка вдвоём раз в 60 дней", {"kind": "every", "days": 60}),
    ("Ветосмотр всех четверых раз в год", {"kind": "every", "days": 365}),
    ("Прогулка 20 000+ шагов в выходные", {"kind": "weekend"}),
    ("Отложить 3 000 € в месяц", {"kind": "month"}),
    ("Задача по Humagotchi каждый день", {"kind": "daily"}),
    ("Степпер 20+ минут в день", {"kind": "daily"}),
    ("Ужин вдвоём без экранов", {"kind": "daily"}),
])
def test_parse(title, spec):
    assert p.parse(title) == spec


def test_every_n_days_window():
    spec = {"kind": "every", "days": 7}
    done = {MON - timedelta(days=7), MON}                       # прошлый пн и этот пн: два раза в срок
    st = p.status(spec, done, d(3))                             # чт: срок 30.11, окно с 28.11
    assert st == {"due": False, "note": "следующий к 30.11", "streak": 2}
    assert p.status(spec, done, d(5)) == {"due": True, "note": "пора · осталось 2 дн.", "streak": 2}
    assert p.status(spec, done, d(7))["note"] == "пора · сегодня"
    assert p.status(spec, done, d(8)) == {"due": True, "note": "пора · срок был 30.11", "streak": 0}
    assert p.status(spec, done | {d(4)}, d(4)) == {"due": False, "note": "сделано · следующий к 04.12", "streak": 3}
    # отметок не было: отсчёт от появления в сетке
    assert p.status(spec, set(), d(1), since=MON) == {"due": False, "note": "следующий к 30.11", "streak": 0}


def test_every_two_days_only_on_the_day():
    spec = p.parse("Силовая раз в 2 дня")
    assert p.status(spec, {MON}, d(1))["due"] is False          # вт: день отдыха, не висит
    assert p.status(spec, {MON}, d(2)) == {"due": True, "note": "пора · сегодня", "streak": 1}


def test_quota_per_calendar_week():
    spec = p.parse("Итальянский с женой 3 раза в 7 дней")
    last_week = {MON - timedelta(days=k) for k in (1, 3, 5)}
    done = last_week | {MON}
    assert p.status(spec, done, d(3)) == {"due": False, "note": "1/3 на неделе", "streak": 1}   # чт: успеваю
    assert p.status(spec, done, d(4)) == {"due": True, "note": "пора · 1/3 на неделе", "streak": 1}
    full = done | {d(1), d(2)}
    assert p.status(spec, full, d(5)) == {"due": False, "note": "3/3 на неделе", "streak": 2}
    day_off = p.parse("Один день без работы из 7")
    assert p.status(day_off, set(), d(3))["due"] is False and p.status(day_off, set(), d(5))["due"] is True


def test_weekend_and_month():
    spec = p.parse("Прогулка 20 000+ шагов в выходные")
    assert p.status(spec, set(), d(2)) == {"due": False, "note": "в выходные", "streak": 0}
    assert p.status(spec, set(), d(5))["note"] == "пора · осталось 1 дн."
    assert p.status(spec, {d(5), MON - timedelta(days=1)}, d(6)) == {"due": False, "note": "сделано в эти выходные", "streak": 2}
    month = p.parse("Отложить 3 000 € в месяц")
    assert p.status(month, set(), date(2026, 11, 22))["due"] is False          # до конца месяца 8 дней
    assert p.status(month, set(), date(2026, 11, 24)) == {"due": True, "note": "пора · осталось 6 дн.", "streak": 0}
    paid = {date(2026, 10, 28), date(2026, 11, 3)}
    assert p.status(month, paid, date(2026, 11, 24)) == {"due": False, "note": "в этом месяце сделано", "streak": 2}


def test_daily_and_streak_units():
    spec = p.parse("Сон 7,5 часа")
    assert p.status(spec, {d(0), d(1)}, d(2)) == {"due": True, "note": "", "streak": 2}
    assert p.streak_of("Свидание с женой раз в 7 дней", {MON - timedelta(days=7), MON}, d(2)) == (2, ("раз", "раза", "раз"))
    assert p.streak_of("Итальянский 3 раза в 7 дней", set(), d(2))[1] == ("неделя", "недели", "недель")

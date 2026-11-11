"""Оценка дня: функция rpg.log_rating, команда /day, кнопки rt: и вечерний вопрос."""
from __future__ import annotations

from datetime import date, time as dtime, timedelta

import psycopg
import pytest

from bot import rating
from core import timeutil as tu
from tests.test_bot import OWNER, FakeTG, buttons, clock, make_bot, message, press, send, term, tg  # noqa: F401


def grid(admin):
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind, xp_base, auto_source) VALUES
        ('C', NULL, 'Квест', 'quest', 10, NULL),
        ('F', 'C', 'Энергия', 'attribute', 10, NULL),
        ('F3', 'F', 'Оценка дня от -10 до +10', 'habit', 10, 'rating:day'),
        ('F5', 'F', 'Месяц со средней оценкой +3', 'milestone', 150, 'rating:month:3')""")


def test_log_rating_function(admin, as_role):
    grid(admin)
    bot = as_role("workos_bot")
    r = bot.execute("SELECT rpg.log_rating(3, 'норм') AS r").fetchone()["r"]
    assert r["score"] == 3 and r["xp"] == 10 and r["month_days"] == 1
    r2 = bot.execute("SELECT rpg.log_rating(-2, NULL) AS r").fetchone()["r"]
    assert r2["xp"] == 0                                    # за день один раз
    row = admin.execute("SELECT score, note FROM rpg.day_ratings").fetchone()
    assert row == {"score": -2, "note": "норм"}             # оценка обновилась, заметка осталась
    with pytest.raises(psycopg.errors.RaiseException):
        bot.execute("SELECT rpg.log_rating(11)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_role("workos_reader").execute("SELECT rpg.log_rating(1)")


def test_month_milestone_once(admin, as_role):
    grid(admin)
    today = tu.logical_today()
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for i in range(20):
        admin.execute("INSERT INTO rpg.day_ratings (day, score) VALUES (%s, %s)", (prev + timedelta(days=i), 4))
    bot = as_role("workos_bot")
    r = bot.execute("SELECT rpg.log_rating(0) AS r").fetchone()["r"]
    assert r["milestone"]["xp"] == 150 and r["xp"] == 160
    admin.execute("DELETE FROM rpg.xp WHERE cell_id = 'F3'")
    admin.execute("DELETE FROM rpg.day_ratings WHERE day = %s", (today,))
    r2 = bot.execute("SELECT rpg.log_rating(0) AS r").fetchone()["r"]
    assert r2["milestone"] is None                          # этап разовый


def test_month_milestone_needs_20_days(admin, as_role):
    grid(admin)
    prev = (tu.logical_today().replace(day=1) - timedelta(days=1)).replace(day=1)
    for i in range(10):
        admin.execute("INSERT INTO rpg.day_ratings (day, score) VALUES (%s, 9)", (prev + timedelta(days=i),))
    r = as_role("workos_bot").execute("SELECT rpg.log_rating(5) AS r").fetchone()["r"]
    assert r["milestone"] is None


def test_parse():
    assert rating.parse("/day +3 устал, но доволен") == (3, "устал, но доволен")
    assert rating.parse("/day -2") == (-2, "")
    assert rating.parse("/day − 5") == (-5, "")
    assert rating.parse("/day@WorkOSBot 7") == (7, "")
    assert rating.parse("/day") == (None, "")
    assert rating.fmt(3) == "+3" and rating.fmt(-2) == "-2" and rating.fmt(0) == "0" and rating.fmt(2.4) == "+2,4"


def test_day_command_and_button(term, tg, admin, clock):
    clock.real()                                            # rpg.log_rating берёт now() базы
    grid(admin)
    send(term, message(term, "/day +3 хороший день", topic="plan"))
    reply = tg.last("sendMessage")
    assert "+3" in reply["text"] and "+10 опыта" in reply["text"]
    send(term, message(term, "/day 15", topic="plan"))
    assert "от -10 до +10" in tg.last("sendMessage")["text"]
    send(term, message(term, "/day", topic="plan"))
    assert "Сегодня: +3" in tg.last("sendMessage")["text"]
    press(term, "rt:5:0101", message_id=776)                # кнопка за другой день не пишет
    assert admin.execute("SELECT score FROM rpg.day_ratings").fetchone()["score"] == 3
    press(term, f"rt:-2:{tu.logical_today():%m%d}", message_id=777)
    assert admin.execute("SELECT score FROM rpg.day_ratings").fetchone()["score"] == -2
    assert "-2" in tg.last("editMessageText")["text"]


def test_evening_ask_once(term, tg, admin, clock):
    grid(admin)
    d = date(2026, 10, 21)
    clock.set(d, 23, 0)
    assert rating.ask(term.ctx) is False                    # рано
    clock.set(d + timedelta(days=1), 0, 40)                 # 00:40: логический день ещё 21-е
    assert rating.ask(term.ctx) is True
    assert rating.ask(term.ctx) is False                    # второй раз не спрашивает
    row = admin.execute("SELECT text, buttons FROM work.outbox WHERE topic = 'plan'").fetchone()
    assert row["text"].startswith(rating.ASK_PREFIX)
    assert "rt:10:1021" in [b["callback_data"] for r in row["buttons"] for b in r]
    admin.execute("DELETE FROM work.outbox")
    admin.execute("INSERT INTO rpg.day_ratings (day, score) VALUES (%s, 1)", (d,))
    assert rating.ask(term.ctx) is False                    # уже оценён


def test_me_shows_month_average(term, tg, admin, clock):
    clock.real()
    grid(admin)
    term.ctx.conn.execute("SELECT rpg.log_rating(4)")
    send(term, message(term, "/me", topic="plan"))
    assert "Оценка дня за" in tg.last("sendMessage")["text"]

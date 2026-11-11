"""Оценка дня: /day +3 [заметка], кнопки rt:<n> и вечерний вопрос «Как прошёл день?».

Число от -10 до +10 пишется за логический день (до 4:00 это ещё вчера) функцией
rpg.log_rating: она же начисляет опыт клеткам с auto_source rating:day и
rating:month:N. Бот сам опыт не пишет, у него только EXECUTE на функцию.
"""
from __future__ import annotations

import re
from datetime import timedelta

import psycopg

from bot.topics import Ctx
from core import config, timeutil as tu
from core.db import jb
from core.tg import btn, esc

RX = re.compile(r"^/day(?:@\w+)?(?:\s+([+\-−–]?\s*\d{1,2}))?\s*(.*)$", re.S | re.I)
ASK_PREFIX = "Как прошёл день?"
ASK_SCORES = [[-5, -2, 0, 2], [4, 6, 8, 10]]
USAGE = "Оценка дня от -10 до +10: <code>/day +3</code> или <code>/day -2 устал, но доволен</code>"


def fmt(n) -> str:
    """+3, -2, 0, +2,4: как George пишет оценки."""
    if n is None:
        return "нет"
    v = float(n)
    if v == 0:
        return "0"
    return (f"{v:+.1f}" if v != int(v) else f"{int(v):+d}").replace(".", ",")


def _month_ru(d) -> str:
    return ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
            "сентябрь", "октябрь", "ноябрь", "декабрь"][d.month - 1]


def status(conn) -> str:
    d = tu.logical_today()
    today = conn.execute("SELECT score, note FROM rpg.day_ratings WHERE day = %s", (d,)).fetchone()
    m = conn.execute("""SELECT round(avg(score), 1) AS avg, count(*) AS n FROM rpg.day_ratings
                        WHERE day >= date_trunc('month', %s::date)::date AND day <= %s""", (d, d)).fetchone()
    lines = [f"Сегодня: {fmt(today['score'])}" + (f" · {esc(today['note'])}" if today and today["note"] else "")
             if today else "Сегодня ещё не оценён."]
    if m["n"]:
        lines.append(f"Среднее за {_month_ru(d)}: {fmt(m['avg'])} ({m['n']} дн.)")
    lines.append(USAGE)
    return "\n".join(lines)


def record(conn, score: int, note: str | None = None) -> str:
    r = conn.execute("SELECT rpg.log_rating(%s, %s) AS r", (score, note or None)).fetchone()["r"]
    lines = [f"Оценка дня: <b>{fmt(r['score'])}</b>"
             + (f" · среднее за {_month_ru(tu.logical_today())} {fmt(r['month_avg'])} ({r['month_days']} дн.)"
                if r.get("month_days") else "")
             + (f" · за неделю {fmt(r['week_avg'])}" if r.get("week_avg") is not None else "")]
    if r.get("xp"):
        lines.append(f"+{r['xp']} опыта")
    boss = r.get("milestone")
    if boss:
        lines.append(f"⬆ Этап «{esc(boss['title'])}»: среднее за {boss['month']} {fmt(boss['avg'])}, "
                     f"+{boss['xp']} опыта")
    return "\n".join(lines)


def parse(text: str) -> tuple[int | None, str]:
    m = RX.match(text.strip())
    if not m or not m.group(1):
        return None, ""
    raw = re.sub(r"\s+", "", m.group(1)).replace("−", "-").replace("–", "-")
    return int(raw), m.group(2).strip()


def handle_command(ctx: Ctx, msg: dict, text: str) -> None:
    score, note = parse(text)
    if score is None:
        ctx.reply(msg, status(ctx.conn))
        return
    if not -10 <= score <= 10:
        ctx.reply(msg, "Оценка от -10 до +10. " + USAGE)
        return
    try:
        ctx.reply(msg, record(ctx.conn, score, note))
    except psycopg.Error as e:
        ctx.reply(msg, "Не записал оценку: " + esc(str(e).splitlines()[0][:200]))


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    try:
        score = int(parts[1])
    except (IndexError, ValueError):
        return "Не понял кнопку"
    if not -10 <= score <= 10:
        return "Не понял кнопку"
    if len(parts) > 2 and parts[2] != tu.logical_today().strftime("%m%d"):
        return "Этот вопрос был про прошлый день. Сегодняшний оцени через /day"
    text = record(ctx.conn, score)
    msg_id = (cq.get("message") or {}).get("message_id")
    if msg_id:
        ctx.edit(msg_id, text, [])
    return f"Записал {fmt(score)}"


def ask(ctx: Ctx, now=None) -> bool:
    """Раз в логический день, после WORKOS_RATING_ASK (по умолчанию 00:30, после тренировки),
    если день ещё не оценён: вопрос с кнопками в «План». Через outbox, так что тихие часы соблюдаются."""
    now = now or tu.now()
    hm = config.env("WORKOS_RATING_ASK", "00:30")
    if not hm or hm == "off":
        return False
    d = tu.logical_date(now)
    if now < tu.local_dt_for(d, tu.parse_hm(hm)):
        return False
    conn = ctx.conn
    if conn.execute("SELECT 1 FROM rpg.day_ratings WHERE day = %s", (d,)).fetchone():
        return False
    # дата в тексте: и тебе понятно, за какой день вопрос в 00:30, и дедуп не зависит от часов базы
    head = f"{ASK_PREFIX} {tu.ru_date(d)}."
    asked = conn.execute("""SELECT 1 FROM work.outbox WHERE topic = 'plan' AND text LIKE %s
                            AND created_at > now() - interval '3 days'""", (head + "%",)).fetchone()
    if asked:
        return False
    tag = d.strftime("%m%d")        # кнопка помнит свой день: нажатие наутро не запишет чужую оценку
    buttons = [[btn(fmt(s), f"rt:{s}:{tag}") for s in row] for row in ASK_SCORES]
    conn.execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('plan', %s, %s)",
                 (f"{head} Оценка от -10 до +10, или точнее: /day N заметка", jb(buttons)))
    return True


def month_line(conn) -> str | None:
    """Строка для /me: среднее за текущий месяц, если оценки есть."""
    d = tu.logical_today()
    m = conn.execute("""SELECT round(avg(score), 1) AS avg, count(*) AS n FROM rpg.day_ratings
                        WHERE day >= date_trunc('month', %s::date)::date AND day <= %s""", (d, d)).fetchone()
    if not m or not m["n"]:
        return None
    return f"Оценка дня за {_month_ru(d)}: {fmt(m['avg'])} в среднем ({m['n']} дн.)"

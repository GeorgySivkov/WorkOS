"""П.2 · /me: лист персонажа. Только чтение rpg.character, rpg.attributes и rpg.xp."""
from __future__ import annotations

from datetime import timedelta

from core import timeutil as tu
from core.tg import esc

from bot.topics import plural


def bar(done: float, total: float, width: int = 5) -> str:
    k = 0 if total <= 0 else max(0, min(width, int(round(width * done / total))))
    return "▰" * k + "▱" * (width - k)


def streaks(conn) -> list[tuple[str, int]]:
    rows = conn.execute("""SELECT c.title, array_agg(DISTINCT rpg.lday(x.earned_at)) AS days
                           FROM rpg.cells c JOIN rpg.xp x ON x.cell_id = c.id
                           WHERE c.kind = 'habit' AND c.active AND x.earned_at > now() - interval '120 days'
                           GROUP BY c.id, c.title""").fetchall()
    today = tu.logical_today()
    out = []
    for r in rows:
        days = set(r["days"] or [])
        d = today if today in days else today - timedelta(days=1)
        n = 0
        while d in days:
            n += 1
            d -= timedelta(days=1)
        if n:
            out.append((r["title"], n))
    return sorted(out, key=lambda x: -x[1])


def sheet(conn) -> str:
    attrs = conn.execute("""SELECT id, title, xp, level, xp_into_level, level_span, rusty, xp_week
                            FROM rpg.attributes ORDER BY id""").fetchall()
    if not attrs:
        return "Сетка ещё не засеяна: в rpg.cells нет характеристик."
    ch = conn.execute("SELECT xp, level, level_floor, next_level_at FROM rpg.character").fetchone()
    into, span = ch["xp"] - ch["level_floor"], ch["next_level_at"] - ch["level_floor"]
    lines = [f"<b>Уровень {ch['level']}</b> · {ch['xp']} XP",
             f"{bar(into, span, 10)} до {ch['level'] + 1}-го ещё {ch['next_level_at'] - ch['xp']} XP", ""]
    for a in attrs:
        lines.append(f"{esc(a['title'])} · ур. {a['level']} {bar(a['xp_into_level'], a['level_span'])} "
                     f"{a['xp_into_level']}/{a['level_span']}")
    lag = min(attrs, key=lambda a: (a["xp"], a["xp_week"]))
    lines += ["", f"Отстаёт: {esc(lag['title'])} (ур. {lag['level']}, {lag['xp_week']} XP за неделю)"]
    rusty = [a["title"] for a in attrs if a["rusty"]]
    if rusty:
        lines.append("Заржавели (14 дней без опыта): " + esc(", ".join(rusty)))
    from bot import rating
    rl = rating.month_line(conn)
    if rl:
        lines.append(rl)
    st = streaks(conn)
    if st:
        lines.append("Серии: " + " · ".join(f"{esc(t)} {n} {plural(n, 'день', 'дня', 'дней')}" for t, n in st[:6]))
    return "\n".join(lines)

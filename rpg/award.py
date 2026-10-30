"""П.1 · начислятор опыта (workos-reader, каждые 5 минут).

1. Страховка: отмеченные задачи с клеткой, по которым ещё нет опыта, проходят
   через rpg.award_task (те же правила, что у мини-приложения).
2. Уровни: сравнивает уровни характеристик и персонажа с rpg.level_marks и
   сообщает о повышении в тему «План» ровно один раз. Первый прогон только
   запоминает уровни. Если на новом уровне лежит награда, открывается сундук.
3. Ачивки за разовые события: первый квест, серия привычки 7 дней,
   первый босс, план принят 7 вечеров подряд.
"""
from __future__ import annotations

import psycopg

from core import db
from core.tg import esc

LOCK_KEY = 7_340_001          # pg_try_advisory_xact_lock: два прогона не шлют одно повышение дважды

SAFETY_NET = """
SELECT t.id FROM work.tasks t
WHERE t.done_at IS NOT NULL AND t.dropped_at IS NULL AND t.cell_id IS NOT NULL
  AND t.done_at > now() - interval '30 days'
  AND NOT EXISTS (SELECT 1 FROM rpg.xp x WHERE x.source = 'task' AND x.source_ref = t.id::text)
ORDER BY t.done_at
"""

# острова подряд идущих дней: d - row_number() одинаков внутри серии
STREAK_SQL = """
WITH days AS (
  SELECT DISTINCT {key} AS k, {day} AS d FROM {src}
), isl AS (
  SELECT k, d, d - (row_number() OVER (PARTITION BY k ORDER BY d))::int AS g FROM days
)
SELECT k, count(*) AS n, max(d) AS last_day FROM isl GROUP BY k, g HAVING count(*) >= %(n)s
ORDER BY last_day LIMIT 1
"""


def award_pending(conn) -> int:
    total = 0
    for r in conn.execute(SAFETY_NET).fetchall():
        total += conn.execute("SELECT rpg.award_task(%s) AS n", (r["id"],)).fetchone()["n"] or 0
    return total


def _rewards(conn, scope: str, old: int, new: int) -> list[str]:
    return [r["title"] for r in conn.execute(
        "SELECT title FROM rpg.rewards WHERE scope = %s AND level > %s AND level <= %s ORDER BY level",
        (scope, old, new))]


def _outbox(conn, text: str, topic: str = "plan", buttons=None) -> None:
    conn.execute("INSERT INTO work.outbox (topic, text, buttons) VALUES (%s, %s, %s)",
                 (topic, text, db.jb(buttons or [])))


def check_levels(conn) -> list[str]:
    """Повышения уровней -> outbox. Возвращает тексты отправленных сообщений."""
    current = [(r["id"], r["title"], r["level"]) for r in
               conn.execute("SELECT id, title, level FROM rpg.attributes ORDER BY id")]
    if not current:
        return []                                     # сетки ещё нет
    ch = conn.execute("SELECT level FROM rpg.character").fetchone()
    current.append(("character", "персонаж", ch["level"] if ch else 0))
    marks = {r["scope"]: r["level"] for r in conn.execute("SELECT scope, level FROM rpg.level_marks")}
    sent = []
    for scope, title, level in current:
        old = marks.get(scope)
        if old is None:                               # первый прогон по этой шкале: только запомнить
            conn.execute("INSERT INTO rpg.level_marks (scope, level) VALUES (%s, %s) ON CONFLICT (scope) DO NOTHING",
                         (scope, level))
            continue
        if level <= old:
            continue                                  # опыт сняли: отметку не опускаем, повтор не придёт
        conn.execute("UPDATE rpg.level_marks SET level = %s, updated_at = now() WHERE scope = %s",
                     (level, scope))
        text = f"⬆ Уровень повышен: <b>{esc(title)} {level}</b>"
        for rw in _rewards(conn, scope, old, level):
            text += f"\nОткрыт сундук: {esc(rw)}"
        _outbox(conn, text)
        sent.append(text)
    return sent


def _achieve(conn, code: str, title: str, detail: str | None = None) -> str | None:
    r = conn.execute("INSERT INTO rpg.achievements (code, title, detail) VALUES (%s, %s, %s) "
                     "ON CONFLICT (code) DO NOTHING RETURNING code", (code, title, detail)).fetchone()
    if not r:
        return None
    text = f"🏆 Ачивка: <b>{esc(title)}</b>" + (f"\n{esc(detail)}" if detail else "")
    _outbox(conn, text)
    return code


def check_achievements(conn) -> list[str]:
    have = {r["code"] for r in conn.execute("SELECT code FROM rpg.achievements")}
    got = []
    if "first_quest" not in have:
        r = conn.execute("""SELECT t.title FROM rpg.xp x JOIN work.tasks t ON t.id::text = x.source_ref
                            WHERE x.source = 'task' ORDER BY x.earned_at LIMIT 1""").fetchone()
        if r and _achieve(conn, "first_quest", "Первый квест закрыт", r["title"]):
            got.append("first_quest")
    if "first_boss" not in have:
        r = conn.execute("""SELECT c.title FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
                            WHERE c.kind = 'milestone' ORDER BY x.earned_at LIMIT 1""").fetchone()
        if r and _achieve(conn, "first_boss", "Первый босс повержен", r["title"]):
            got.append("first_boss")
    if "habit_streak_7" not in have:
        sql = STREAK_SQL.format(
            key="x.cell_id", day="rpg.lday(x.earned_at)",
            src="rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id WHERE c.kind = 'habit' "
                "AND x.earned_at > now() - interval '90 days'")
        r = conn.execute(sql, {"n": 7}).fetchone()
        if r:
            title = conn.execute("SELECT title FROM rpg.cells WHERE id = %s", (r["k"],)).fetchone()["title"]
            if _achieve(conn, "habit_streak_7", "Серия 7 дней", title):
                got.append("habit_streak_7")
    if "plan_streak_7" not in have:
        sql = STREAK_SQL.format(
            key="1", day="(a.payload->>'for_date')::date",
            src="work.actions a WHERE a.type = 'plan_batch' AND a.status IN ('approved','executed') "
                "AND jsonb_array_length(COALESCE(a.payload->'items', '[]'::jsonb)) > 0")
        r = conn.execute(sql, {"n": 7}).fetchone()
        if r and _achieve(conn, "plan_streak_7", "План принят 7 вечеров подряд"):
            got.append("plan_streak_7")
    return got


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    out: dict = {}
    with db.connect() as conn:
        with conn.transaction():
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
                return {"skipped": "уже идёт другой прогон"}
            if not conn.execute("SELECT 1 FROM rpg.cells WHERE kind = 'attribute' LIMIT 1").fetchone():
                return {"skipped": "сетки нет"}
            out["awarded_xp"] = award_pending(conn)
            out["levelups"] = check_levels(conn)
            out["achievements"] = check_achievements(conn)
            if dry_run:
                out["dry_run"] = True
                raise psycopg.Rollback()             # показать, но ничего не записать
    return out


if __name__ == "__main__":
    print(run())

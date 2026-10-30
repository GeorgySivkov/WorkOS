"""П.5 · недельный разбор персонажа (workos-reader, вс 20:00).

В тему «План»: опыт по характеристикам за неделю, что ржавеет (14 дней без опыта),
три квеста под самые отстающие характеристики (одной карточкой plan_batch
в список «Неделя»), награды, открытые за неделю, и напоминание о новом сезоне,
когда меняется квартал. Квесты выбираются детерминированно; модель (задача
'weekly') по желанию только переформулирует их конкретнее.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from core import config, db, llm, timeutil as tu
from core.tg import esc

QUEST_SIZE = {"habit": "S", "milestone": "M"}

ATTR_SQL = """
SELECT a.id, a.title, a.level, a.xp, a.xp_into_level, a.level_span,
       COALESCE(sum(x.amount) FILTER (WHERE rpg.lday(x.earned_at) BETWEEN %(mon)s AND %(sun)s), 0)::int AS week_xp,
       max(x.earned_at) FILTER (WHERE x.earned_at <= %(now)s) AS last_xp_at
FROM rpg.attributes a
LEFT JOIN rpg.cells c ON c.id = a.id OR c.parent = a.id
LEFT JOIN rpg.xp x ON x.cell_id = c.id
GROUP BY a.id, a.title, a.level, a.xp, a.xp_into_level, a.level_span
ORDER BY a.id
"""

CANDIDATES_SQL = """
SELECT c.id, c.title, c.kind,
       COALESCE(sum(x.amount) FILTER (WHERE x.earned_at > %(now)s - interval '14 days'), 0) AS recent
FROM rpg.cells c LEFT JOIN rpg.xp x ON x.cell_id = c.id
WHERE c.active AND c.parent = %(attr)s AND c.kind IN ('habit','milestone')
GROUP BY c.id, c.title, c.kind
HAVING NOT (c.kind = 'milestone' AND count(x.id) > 0)      -- взятый этап второй раз не предлагаем
ORDER BY recent, c.id
"""


def bar(into: int, span: int, n: int = 5) -> str:
    filled = min(n, max(0, round(n * (into or 0) / span))) if span else 0
    return "▰" * filled + "▱" * (n - filled)


def quarter(d: date) -> int:
    return (d.month - 1) // 3


def lagging(attrs: list[dict]) -> list[dict]:
    """Самые отстающие: сначала ржавые, потом по уровню, опыту за неделю и всему опыту."""
    return sorted(attrs, key=lambda a: (not a["rusty"], a["level"], a["week_xp"], a["xp"], a["id"]))


def pick_quests(conn, attrs: list[dict], now: datetime, n: int = 3) -> list[dict]:
    out = []
    for a in lagging(attrs):
        cand = conn.execute(CANDIDATES_SQL, {"now": now, "attr": a["id"]}).fetchone()
        if not cand:
            continue
        out.append({"title": cand["title"], "size": QUEST_SIZE.get(cand["kind"], "S"),
                    "context_id": "personal", "cell_id": cand["id"], "attr": a["title"]})
        if len(out) == n:
            break
    return out


def reword(quests: list[dict]) -> None:
    """Модель (дешёвая) делает из названия клетки конкретный квест на неделю. Ошибка не страшна."""
    system = ("Ты помогаешь George планировать неделю. Для каждой клетки сетки целей сформулируй один "
              "конкретный проверяемый квест на эту неделю, до 60 символов, по-русски, без длинных тире. "
              'Формат: {"<cell_id>": "<квест>"}.')
    user = "\n".join(f"{q['cell_id']}: {q['title']} (характеристика {q['attr']})" for q in quests)
    try:
        got = llm.complete_json("weekly", system, user)
    except Exception:  # noqa: BLE001
        return
    if isinstance(got, dict):
        for q in quests:
            t = got.get(q["cell_id"])
            if isinstance(t, str) and t.strip():
                q["title"] = t.strip().replace("—", ":")[:120]


def rewards_reached(conn, start: datetime, now: datetime) -> list[dict]:
    """Награды, порог которых пересечён в [start, now]: по накопленному опыту шкалы."""
    rewards = conn.execute("SELECT scope, level, title FROM rpg.rewards ORDER BY scope, level").fetchall()
    if not rewards:
        return []
    active = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM rpg.attributes")}
    rows = conn.execute("""SELECT x.earned_at, x.amount, CASE WHEN c.kind = 'attribute' THEN c.id ELSE c.parent END AS attr
                           FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
                           WHERE x.earned_at <= %s ORDER BY x.earned_at, x.id""", (now,)).fetchall()
    out = []
    for rw in rewards:
        scope, lvl = rw["scope"], rw["level"]
        threshold = 50 * lvl * (lvl + 1)
        total, crossed = 0, None
        for r in rows:
            if (scope == "character" and r["attr"] in active) or r["attr"] == scope:
                total += r["amount"]
                if total >= threshold:
                    crossed = r["earned_at"]
                    break
        if crossed and crossed >= start:
            who = "персонаж" if scope == "character" else active.get(scope, scope)
            out.append({"scope": scope, "level": lvl, "title": rw["title"], "who": who})
    return out


def propose_quests(conn, quests: list[dict], for_date: date) -> int:
    from agents.planner import propose_action  # noqa: PLC0415
    payload = {"for_date": for_date.isoformat(), "items": [],
               "week_items": [{k: q[k] for k in ("title", "size", "context_id", "cell_id")} for q in quests]}
    return propose_action(conn, "plan_batch", "personal", payload,
                          rationale="Недельный разбор: квесты под отстающие характеристики")


def build_text(mon: date, sun: date, attrs, char, rusty, quests, rewards, season: bool) -> str:
    lines = [f"<b>Неделя {tu.ru_date(mon, weekday=False)} · {tu.ru_date(sun, weekday=False)}</b>", "",
             "<b>Опыт за неделю</b>"]
    for a in sorted(attrs, key=lambda a: (-a["week_xp"], a["id"])):
        lines.append(f"{esc(a['title'])} {bar(a['xp_into_level'], a['level_span'])} ур. {a['level']}"
                     + (f" · +{a['week_xp']}" if a["week_xp"] else " · 0"))
    total = sum(a["week_xp"] for a in attrs)
    lines.append(f"Персонаж: ур. {char['level'] if char else 0} · +{total} за неделю")
    if rusty:
        lines += ["", "Ржавеют (14 дней без опыта): " + ", ".join(esc(a["title"]) for a in rusty)]
    if quests:
        lines += ["", "<b>Квесты на неделю</b>"]
        lines += [f"· {esc(q['title'])} ({esc(q['attr'])})" for q in quests]
        lines.append("Карточка ниже: принять в список «Неделя» одним нажатием.")
    if rewards:
        lines += ["", "<b>Награды</b>"]
        lines += [f"Открыт сундук: {esc(r['title'])} ({esc(r['who'])} {r['level']})" for r in rewards]
    if season:
        lines += ["", "🗓 новый сезон: пересмотри сетку на странице «Мандарат»"]
    return "\n".join(lines)


def run(date: str | None = None, dry_run: bool = False, force: bool = False, now: datetime | None = None,
        model: bool | None = None, **kw) -> dict:
    now = now or tu.now()
    sun = datetime.fromisoformat(date).date() if date else tu.logical_date(now)
    mon = tu.week_monday(sun)
    next_mon = mon + timedelta(days=7)
    key = f"weekly:{sun.isoformat()}"
    use_model = model if model is not None else config.env("WORKOS_WEEKLY_MODEL") == "1"
    with db.connect() as conn:
        if not force and not dry_run and conn.execute("SELECT 1 FROM work.notices WHERE key = %s", (key,)).fetchone():
            return {"skipped": "разбор за эту неделю уже был"}
        params = {"mon": mon, "sun": sun, "now": now}
        attrs = [dict(r) for r in conn.execute(ATTR_SQL, params)]
        if not attrs:
            return {"skipped": "сетки нет"}
        for a in attrs:
            a["rusty"] = a["last_xp_at"] is None or a["last_xp_at"] < now - timedelta(days=config.RUST_DAYS)
        char = conn.execute("SELECT level, xp FROM rpg.character").fetchone()
        rusty = [a for a in attrs if a["rusty"]]
        quests = pick_quests(conn, attrs, now)
        if quests and use_model:
            reword(quests)
        rewards = rewards_reached(conn, tu.day_bounds(mon)[0], now)
        season = quarter(mon) != quarter(next_mon)
        text = build_text(mon, sun, attrs, char, rusty, quests, rewards, season)
        result = {"week": mon.isoformat(), "quests": [q["cell_id"] for q in quests],
                  "rusty": [a["id"] for a in rusty], "rewards": len(rewards), "season": season}
        if dry_run:
            print(text)
            return {**result, "dry_run": True}
        with conn.transaction():
            if conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                            (key,)).fetchone() is None and not force:
                return {"skipped": "разбор за эту неделю уже был"}
            action_id = propose_quests(conn, quests, next_mon) if quests else None
            conn.execute("INSERT INTO work.outbox (topic, text, ref_action) VALUES ('plan', %s, %s)",
                         (text, action_id))
        result["action_id"] = action_id
        return result


if __name__ == "__main__":
    print(run())

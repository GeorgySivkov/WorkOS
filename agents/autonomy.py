"""Автономия действий (шаг 6.2) и единая точка «предложить действие».

propose() зовут все читатели: экстрактор, драфтер, планировщик. Она сама решает,
пойдёт действие на подтверждение или в автономию, и держит семплинг: каждое
AUTONOMY_SAMPLE_EVERY-е автономное действие всё равно идёт на подтверждение,
иначе accept_rate замирает и деградация не сработает никогда.

run() раз в час пересчитывает статистику исходов за 30 дней и двигает режимы:
вверх при accept_rate > 0.85 и n >= 20, вниз при accept_rate < 0.7. Типы с внешней
видимостью (config.NEVER_AUTO) не продвигаются никогда, запертые строки (/manual)
не трогаются. Каждое изменение режима пишется в outbox темой «Система».
"""
from __future__ import annotations

from core import config, db
from core.tg import esc

WINDOW_DAYS = 30


# ---------- предложить ----------

def _lock_rows(conn, action_type: str, context_id: str) -> list[dict]:
    """Строки автономии, которые касаются пары: своя, (тип, '*') и общий рубильник ('*', '*')."""
    return conn.execute(
        "SELECT * FROM work.autonomy WHERE (action_type, context_id) IN ((%s, %s), (%s, '*'), ('*', '*'))",
        (action_type, context_id, action_type)).fetchall()


def decide_mode(conn, action_type: str, context_id: str | None) -> tuple[str, dict | None]:
    """('auto'|'approval', строка автономии, которая решила). Без побочных эффектов."""
    if action_type in config.NEVER_AUTO:
        return "approval", None
    ctx = context_id or "*"
    rows = _lock_rows(conn, action_type, ctx)
    if any(r["locked"] for r in rows):
        return "approval", None
    own = [r for r in rows if r["action_type"] == action_type and r["context_id"] == ctx]
    star = [r for r in rows if r["action_type"] == action_type and r["context_id"] == "*"]
    row = (own or star or [None])[0]
    if row and row["mode"] == "auto":
        return "auto", row
    return "approval", row


def find_existing(conn, action_type: str, payload: dict, source_event: int | None) -> int | None:
    r = conn.execute(
        "SELECT id FROM work.actions WHERE type = %s AND source_event IS NOT DISTINCT FROM %s "
        "AND payload = %s ORDER BY id LIMIT 1",
        (action_type, source_event, db.jb(payload))).fetchone()
    return r["id"] if r else None


def propose(conn, type: str, context_id: str | None, payload: dict, rationale: str | None = None,  # noqa: A002
            source_event: int | None = None) -> int:
    """Положить предложение в work.actions (status='proposed'). Возвращает id действия.

    Повтор того же (type, source_event, payload) не создаёт вторую строку, а возвращает
    существующую. Карточку по предложению рисует бот, outbox не нужен.
    """
    existing = find_existing(conn, type, payload, source_event)
    if existing:
        return existing
    ctx = context_id or "personal"
    mode, row = decide_mode(conn, type, ctx)
    if mode == "auto" and row is not None:
        n = conn.execute(
            "UPDATE work.autonomy SET auto_count = auto_count + 1 WHERE action_type = %s AND context_id = %s "
            "RETURNING auto_count", (row["action_type"], row["context_id"])).fetchone()["auto_count"]
        if config.AUTONOMY_SAMPLE_EVERY and n % config.AUTONOMY_SAMPLE_EVERY == 0:
            mode = "approval"          # семплинг: держит контур замкнутым
    r = conn.execute(
        "INSERT INTO work.actions (context_id, type, payload, rationale, source_event, status, autonomy) "
        "VALUES (%s, %s, %s, %s, %s, 'proposed', %s) RETURNING id",
        (ctx, type, db.jb(payload), rationale, source_event, mode)).fetchone()
    return r["id"]


# ---------- продвижение и деградация ----------

STATS_SQL = f"""
SELECT action_type, context_id, count(*) AS n,
       count(*) FILTER (WHERE verdict = 'accepted') AS accepted,
       round(count(*) FILTER (WHERE verdict = 'accepted')::numeric / NULLIF(count(*), 0), 2) AS accept_rate
FROM work.outcomes
WHERE decided_at > now() - interval '{WINDOW_DAYS} days'
GROUP BY 1, 2
ORDER BY 1, 2
"""


def _pct(rate) -> str:
    return f"{round(float(rate) * 100)}%"


def evaluate(stat: dict, row: dict | None, global_locked: bool, inherited: str = "approval") -> tuple[str | None, str]:
    """Что сделать с парой: ('auto'|'approval'|None, причина). None = режим не меняется.
    inherited: режим строки (тип, '*'), если своей строки у пары ещё нет."""
    t, n, rate = stat["action_type"], stat["n"], float(stat["accept_rate"] or 0)
    mode = row["mode"] if row else inherited
    if (row and row["locked"]) or global_locked:
        return None, "заперто вручную (/manual)"
    if t in config.NEVER_AUTO:
        if mode == "auto":
            return "approval", "тип с внешней видимостью не бывает автономным"
        return None, "тип с внешней видимостью"
    if mode != "auto" and rate > config.AUTONOMY_PROMOTE_RATE and n >= config.AUTONOMY_PROMOTE_MIN_N:
        return "auto", (f"принято {stat['accepted']} из {n} за {WINDOW_DAYS} дней ({_pct(rate)}), "
                        f"порог {_pct(config.AUTONOMY_PROMOTE_RATE)} при n от {config.AUTONOMY_PROMOTE_MIN_N}")
    if mode == "auto" and rate < config.AUTONOMY_DEMOTE_RATE:
        return "approval", (f"принято {stat['accepted']} из {n} за {WINDOW_DAYS} дней ({_pct(rate)}), "
                            f"ниже порога {_pct(config.AUTONOMY_DEMOTE_RATE)}")
    return None, ""


def _message(stat: dict, new_mode: str, reason: str) -> str:
    who = f"<b>{esc(stat['action_type'])}</b> ({esc(stat['context_id'])})"
    if new_mode == "auto":
        return (f"Автономия: {who} теперь без подтверждения. {esc(reason)}. "
                f"Буду присылать уведомление постфактум с кнопкой «откатить», "
                f"а каждое {config.AUTONOMY_SAMPLE_EVERY}-е всё равно спрошу.")
    return f"Автономия: {who} вернул на подтверждение. {esc(reason)}."


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    changes: list[dict] = []
    with db.connect() as conn, conn.transaction():
        stats = conn.execute(STATS_SQL).fetchall()
        rows = {(r["action_type"], r["context_id"]): r for r in conn.execute("SELECT * FROM work.autonomy")}
        global_locked = bool(rows.get(("*", "*"), {}).get("locked"))
        for st in stats:
            key = (st["action_type"], st["context_id"])
            row = rows.get(key)
            star = rows.get((st["action_type"], "*"))
            inherited = star["mode"] if star else "approval"
            new_mode, reason = evaluate(st, row, global_locked or bool(star and star["locked"]), inherited)
            if dry_run:
                if new_mode:
                    changes.append({"type": key[0], "context": key[1], "mode": new_mode, "reason": reason})
                continue
            if row is None:      # своя строка перекрывает '*': без изменения наследуем её режим
                conn.execute(
                    "INSERT INTO work.autonomy (action_type, context_id, mode, n, accept_rate, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (key[0], key[1], new_mode or inherited, st["n"], st["accept_rate"], reason or None))
            elif new_mode:
                conn.execute(
                    "UPDATE work.autonomy SET mode = %s, n = %s, accept_rate = %s, reason = %s, changed_at = now() "
                    "WHERE action_type = %s AND context_id = %s",
                    (new_mode, st["n"], st["accept_rate"], reason, key[0], key[1]))
            else:
                conn.execute(
                    "UPDATE work.autonomy SET n = %s, accept_rate = %s WHERE action_type = %s AND context_id = %s",
                    (st["n"], st["accept_rate"], key[0], key[1]))
            if new_mode:
                conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)",
                             (_message(st, new_mode, reason),))
                changes.append({"type": key[0], "context": key[1], "mode": new_mode, "reason": reason})
    return {"pairs": len(stats), "changes": changes, "dry_run": dry_run}


if __name__ == "__main__":
    print(run())

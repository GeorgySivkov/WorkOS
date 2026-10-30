"""Автономия действий (шаг 6.2) и единая точка «предложить действие».

propose() зовут все читатели: экстрактор, драфтер, планировщик. Она только кладёт
предложение (status='proposed', autonomy='approval'), режим не решает: пойдёт ли
действие без подтверждения, решает бот своей выборкой из work.autonomy (правило
безопасности: прочитанный текст не должен уметь сам себя одобрить). Там же, в боте,
семплинг: каждое AUTONOMY_SAMPLE_EVERY-е автономное всё равно идёт карточкой.

run() раз в час зовёт work.autonomy_recompute() (SECURITY DEFINER, 009): пересчёт
статистики исходов за 30 дней и сдвиг режимов целиком в SQL. Вверх при accept rate
> 0.85 и n >= 20, только внутренние типы (план и задачи); вниз при < 0.7; запертые
строки и общий рубильник /manual не трогаются. Писать в work.autonomy сам читатель
не может. Каждое изменение режима уходит в outbox темой «Система».
"""
from __future__ import annotations

from core import config, db
from core.tg import esc

WINDOW_DAYS = 30


# ---------- предложить ----------

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
    существующую. Карточку (или автономное одобрение) делает бот, outbox не нужен.
    """
    existing = find_existing(conn, type, payload, source_event)
    if existing:
        return existing
    r = conn.execute(
        "INSERT INTO work.actions (context_id, type, payload, rationale, source_event, status, autonomy) "
        "VALUES (%s, %s, %s, %s, %s, 'proposed', 'approval') RETURNING id",
        (context_id or "personal", type, db.jb(payload), rationale, source_event)).fetchone()
    return r["id"]


# ---------- продвижение и деградация ----------

def _message(ch: dict) -> str:
    who = f"<b>{esc(ch['action_type'])}</b> ({esc(ch['context_id'])})"
    if ch["new_mode"] == "auto":
        return (f"Автономия: {who} теперь без подтверждения. {esc(ch['reason'])}. "
                f"Буду присылать уведомление постфактум с кнопкой «Откатить», "
                f"а каждое {config.AUTONOMY_SAMPLE_EVERY}-е всё равно спрошу.")
    return f"Автономия: {who} вернул на подтверждение. {esc(ch['reason'])}."


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    changes: list[dict] = []
    with db.connect() as conn, conn.transaction():
        rows = conn.execute("SELECT * FROM work.autonomy_recompute(%s)", (bool(dry_run),)).fetchall()
        for ch in rows:
            if not dry_run:
                conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (_message(ch),))
            changes.append({"type": ch["action_type"], "context": ch["context_id"], "mode": ch["new_mode"],
                            "reason": ch["reason"]})
    return {"changes": changes, "dry_run": dry_run}


if __name__ == "__main__":
    print(run())

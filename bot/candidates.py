"""3.2 · Кнопки карточек «Кто это?» от extract.people.

callback_data: idc:<candidate_id>:<person_id|new|skip>
person -> обличье в work.identities (confidence 1.0, это решил George), кандидат resolved;
new -> новый человек в work.people + контекст + обличье; skip -> кандидат ignored.

Голое имя («Саша»), которое в контексте носит ещё кто-то, при выборе существующего
человека обличьем на весь контекст НЕ пишется (правило 3.2: голое имя с двумя
кандидатами не разрешается автоматически). Выбор George тогда действует только на эту
встречу: человек добавляется в meta.participant_ids события-источника, и карточка
прямо говорит об этом.
"""
from __future__ import annotations

from core import db
from core.tg import esc
from extract import people as names

from bot.topics import Ctx

IDENTITY_KINDS = {"email", "slack", "trello", "jira", "telegram", "display_name"}


def _in_context(conn, person_id: int, cand: dict) -> None:
    if cand["context_id"]:
        conn.execute("""INSERT INTO work.person_contexts (person_id, context_id) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING""", (person_id, cand["context_id"]))


def _identity(conn, person_id: int, cand: dict) -> None:
    kind = cand["kind"] if cand["kind"] in IDENTITY_KINDS else "display_name"
    conn.execute("""INSERT INTO work.identities (person_id, kind, value, context_id, confidence)
                    VALUES (%s, %s, %s, %s, 1.0) ON CONFLICT DO NOTHING""",
                 (person_id, kind, cand["raw"], cand["context_id"]))
    _in_context(conn, person_id, cand)


def _shared(conn, cand: dict, person_id: int | None) -> bool:
    """Голое имя, которое в контексте носит кто-то ещё: на весь контекст не запоминаем."""
    if cand["kind"] not in ("display_name", None):
        return False
    return names.shared_bare_name(conn, cand["raw"], cand["context_id"], person_id)


def _scope_to_meeting(conn, person_id: int, cand: dict) -> bool:
    """Человек становится участником только этой встречи (meta.participant_ids)."""
    if not cand.get("source_event"):
        return False
    row = conn.execute("SELECT meta FROM work.events WHERE id = %s", (cand["source_event"],)).fetchone()
    if not row:
        return False
    ids = []
    for x in (row["meta"] or {}).get("participant_ids") or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if person_id not in ids:
        conn.execute("UPDATE work.events SET meta = meta || jsonb_build_object('participant_ids', %s::jsonb) "
                     "WHERE id = %s", (db.jb(sorted(set(ids) | {person_id})), cand["source_event"]))
    return True


def _remember(conn, person_id: int, cand: dict, shared: bool) -> str:
    """Записать выбор George. Возвращает приписку к карточке (пусто, если запомнено на контекст)."""
    if not shared:
        _identity(conn, person_id, cand)
        return ""
    _in_context(conn, person_id, cand)
    raw = esc(cand["raw"])
    if _scope_to_meeting(conn, person_id, cand):
        return f"\nИмя «{raw}» в этом контексте носят несколько человек, поэтому запомнил только для этой встречи."
    return f"\nИмя «{raw}» в этом контексте носят несколько человек, поэтому не запоминаю: в следующий раз спрошу снова."


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    conn = ctx.conn
    try:
        cid, what = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        return "Не понял кнопку"
    msg_id = (cq.get("message") or {}).get("message_id")
    with conn.transaction():
        cand = conn.execute("""SELECT c.id, c.raw, c.kind, c.context_id, c.status, c.source_event,
                                      x.title AS ctx_title
                               FROM work.identity_candidates c LEFT JOIN work.contexts x ON x.id = c.context_id
                               WHERE c.id = %s""", (cid,)).fetchone()
        if not cand:
            return "Кандидат не найден"
        if cand["status"] != "open":
            if msg_id:
                ctx.edit(msg_id, None, [])
            return "Уже разобран"
        where = f" ({esc(cand['ctx_title'])})" if cand["ctx_title"] else ""
        if what == "skip":
            conn.execute("UPDATE work.identity_candidates SET status = 'ignored' WHERE id = %s", (cid,))
            text = f"«{esc(cand['raw'])}»{where}: пропустил."
        elif what == "new":
            # новый человек и так носит это имя как display_name: обличье с тем же текстом
            # разрешающей силы не добавляет, поэтому здесь запрета на голое имя нет
            pid = conn.execute("INSERT INTO work.people (display_name) VALUES (%s) RETURNING id",
                               (cand["raw"],)).fetchone()["id"]
            _identity(conn, pid, cand)
            conn.execute("UPDATE work.identity_candidates SET status = 'resolved' WHERE id = %s", (cid,))
            text = f"«{esc(cand['raw'])}»{where}: завёл нового человека."
        else:
            try:
                pid = int(what)
            except ValueError:
                return "Не понял кнопку"
            person = conn.execute("SELECT id, display_name FROM work.people WHERE id = %s", (pid,)).fetchone()
            if not person:
                return "Такого человека нет"
            note = _remember(conn, pid, cand, _shared(conn, cand, pid))
            conn.execute("UPDATE work.identity_candidates SET status = 'resolved' WHERE id = %s", (cid,))
            text = f"«{esc(cand['raw'])}»{where}: это {esc(person['display_name'])}.{note}"
    if msg_id:
        ctx.edit(msg_id, text, [])
    return "Запомнил"

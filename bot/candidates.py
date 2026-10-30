"""3.2 · Кнопки карточек «Кто это?» от extract.people.

callback_data: idc:<candidate_id>:<person_id|new|skip>
person -> обличье в work.identities (confidence 1.0, это решил George), кандидат resolved;
new -> новый человек в work.people + контекст + обличье; skip -> кандидат ignored.
"""
from __future__ import annotations

from core.tg import esc

from bot.topics import Ctx

IDENTITY_KINDS = {"email", "slack", "trello", "jira", "telegram", "display_name"}


def _identity(conn, person_id: int, cand: dict) -> None:
    kind = cand["kind"] if cand["kind"] in IDENTITY_KINDS else "display_name"
    conn.execute("""INSERT INTO work.identities (person_id, kind, value, context_id, confidence)
                    VALUES (%s, %s, %s, %s, 1.0) ON CONFLICT DO NOTHING""",
                 (person_id, kind, cand["raw"], cand["context_id"]))
    if cand["context_id"]:
        conn.execute("""INSERT INTO work.person_contexts (person_id, context_id) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING""", (person_id, cand["context_id"]))


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    conn = ctx.conn
    try:
        cid, what = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        return "Не понял кнопку"
    msg_id = (cq.get("message") or {}).get("message_id")
    with conn.transaction():
        cand = conn.execute("""SELECT c.id, c.raw, c.kind, c.context_id, c.status, x.title AS ctx_title
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
            _identity(conn, pid, cand)
            conn.execute("UPDATE work.identity_candidates SET status = 'resolved' WHERE id = %s", (cid,))
            text = f"«{esc(cand['raw'])}»{where}: это {esc(person['display_name'])}."
    if msg_id:
        ctx.edit(msg_id, text, [])
    return "Запомнил"

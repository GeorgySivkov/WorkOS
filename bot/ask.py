"""3.4 · Тема «Спросить»: вопрос к памяти через retrieve.py.

Имена разрешаются до поиска: кандидатов больше одного — всегда спрашиваем
кнопками, ноль — честное «не знаю такого». Ожидающий вопрос хранится в meta
события вопроса, так что переживает перезапуск бота.

callback_data: ask:<event_id>:<person_id>
"""
from __future__ import annotations

import importlib
import logging

from core.tg import btn, esc

from bot import capture
from bot.topics import Ctx

log = logging.getLogger("workos.bot")

NOT_READY = "Поиск по памяти ещё не настроен (шаг 3.4): вопрос сохранил, отвечу, когда он появится."


def _retrieve():
    try:
        mod = importlib.import_module("retrieve")
    except Exception as e:
        log.warning("retrieve недоступен: %s", e)
        return None
    if not (hasattr(mod, "prepare") and hasattr(mod, "answer")):
        return None
    return mod


def _ctx_titles(conn) -> dict:
    return {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM work.contexts").fetchall()}


def _label(c: dict, titles: dict) -> str:
    ctxs = [titles.get(x, x) for x in (c.get("contexts") or [])]
    name = c.get("name") or f"#{c.get('person_id')}"
    return f"{name} ({', '.join(ctxs)})" if ctxs else name


def _choice_msg(conn, eid: int, choice: dict) -> tuple[str, list]:
    titles = _ctx_titles(conn)
    rows = [[btn(_label(c, titles)[:60], f"ask:{eid}:{int(c['person_id'])}")]
            for c in (choice.get("candidates") or [])[:8]]
    names = " или ".join(_label(c, titles) for c in (choice.get("candidates") or [])[:3])
    return f"«{esc(choice.get('raw'))}»: это {esc(names)}?", rows


def _answer(ctx: Ctx, msg_or_none: dict | None, eid: int, question: str, person_ids: list[int],
            reply_to: int | None = None) -> None:
    mod = _retrieve()
    if not mod:
        ctx.send("ask", NOT_READY, reply_to=reply_to)
        return
    ctx.action(ctx.thread_of("ask"))
    res = mod.answer(ctx.conn, question, person_ids) or {}
    text = res.get("text") or "Ничего не нашёл."
    refs = res.get("refs") or []
    if refs and isinstance(refs, list) and isinstance(refs[0], dict) and "Источники" not in text:
        lab = [esc(r.get("label") or r.get("title") or f"#{r.get('event_id', r.get('id', ''))}")
               for r in refs[:8]]
        text += "\n\n<i>Источники: " + "; ".join(lab) + "</i>"
    capture.merge_meta(ctx.conn, eid, {"ask": {"state": "answered", "person_ids": person_ids}})
    ctx.send("ask", text, reply_to=reply_to)


def _step(ctx: Ctx, eid: int, question: str, prep: dict, resolved: list[int], reply_to: int | None) -> None:
    conn = ctx.conn
    choices = [c for c in (prep.get("choices") or []) if c.get("candidates")]
    unknown = prep.get("unknown") or []
    if choices:
        capture.merge_meta(conn, eid, {"ask": {"state": "choose", "choices": choices,
                                               "resolved": resolved, "reply_to": reply_to}})
        text, rows = _choice_msg(conn, eid, choices[0])
        ctx.send("ask", text, buttons=rows, reply_to=reply_to)
        return
    if unknown:
        names = ", ".join(str(u.get("raw") if isinstance(u, dict) else u) for u in unknown)
        capture.merge_meta(conn, eid, {"ask": {"state": "unknown", "unknown": unknown}})
        ctx.send("ask", f"Не знаю такого: {esc(names)}.", reply_to=reply_to)
        return
    _answer(ctx, None, eid, question, resolved, reply_to=reply_to)


def handle(ctx: Ctx, msg: dict) -> int | None:
    question = capture.msg_text(msg)
    eid = capture.store_event(ctx, msg, body=question, kind="question", meta={"ask": {"state": "new"}})
    if eid is None or not question:
        return eid
    mod = _retrieve()
    if not mod:
        ctx.reply(msg, NOT_READY)
        return eid
    ctx.action(msg.get("message_thread_id"))
    prep = mod.prepare(ctx.conn, question) or {}
    resolved = [int(x) for x in (prep.get("person_ids") or [])]
    _step(ctx, eid, question, prep, resolved, msg.get("message_id"))
    return eid


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    conn = ctx.conn
    try:
        eid, pid = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return "Не понял кнопку"
    ev = conn.execute("SELECT body, meta FROM work.events WHERE id = %s AND kind = 'question'", (eid,)).fetchone()
    st = ((ev or {}).get("meta") or {}).get("ask") or {}
    if not ev or st.get("state") != "choose":
        return "Этот вопрос уже разобран"
    choices = st.get("choices") or []
    if not choices or pid not in [int(c["person_id"]) for c in choices[0].get("candidates") or []]:
        return "Не тот человек"
    msg_id = (cq.get("message") or {}).get("message_id")
    titles = _ctx_titles(conn)
    picked = next(c for c in choices[0]["candidates"] if int(c["person_id"]) == pid)
    if msg_id:
        ctx.edit(msg_id, f"«{esc(choices[0].get('raw'))}»: {esc(_label(picked, titles))}", [])
    resolved = list(st.get("resolved") or []) + [pid]
    _step(ctx, eid, ev["body"] or "", {"choices": choices[1:]}, resolved, st.get("reply_to"))
    return None

"""1.2 · Захват: всё, что George кидает во «Входящие», ложится в work.events.

Дешёвая модель размечает по двум осям (что это и какой контекст), явная дата
из текста (core.ruparse) надёжнее догадки модели. Задача сразу ложится в
work.tasks: George её сам написал, подтверждать нечего. Ответ в той же теме
с кнопками поправки; поправка пишет класс до и после в meta (сигнал обучения).

callback_data: cap:<event_id>:task|idea|fact · cap:<event_id>:ctx:<context_id>
· cap:<event_id>:cell · cap:<event_id>:c:<cell_id|->
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone

from core import config, db, llm, ruparse, timeutil as tu
from core.tg import btn, esc

from bot.topics import Ctx, contexts, short_date

log = logging.getLogger("workos.bot")

SOURCE = "telegram:inbox"
KINDS = ("task", "idea", "fact", "question")
KIND_LABEL = {"task": "Задача", "idea": "Идея", "fact": "В базу", "question": "Вопрос"}
SURE = 0.7
RX_URL = re.compile(r"https?://\S+", re.I)

SYSTEM = (
    "Ты разбираешь короткую заметку, которую George кинул в свой Telegram-бот. "
    "Определи: kind — task (дело, которое надо сделать), idea (идея или мысль на потом), "
    "fact (рабочий факт, информация для базы), question (вопрос, на который нужен ответ); "
    "context_id — ровно один id из списка контекстов; confidence — уверенность в контексте от 0 до 1; "
    "title — короткая формулировка без даты и времени; due — дата YYYY-MM-DD или null; "
    "time — HH:MM или null (дни считай от логического «сегодня» из ввода, день кончается в 4:00); "
    "size — S (до 30 минут), M (около часа), L (два часа и больше); "
    "cell_id — id клетки сетки, к которой относится дело, или null. "
    'Формат: {"kind": "...", "context_id": "...", "confidence": 0.0, "title": "...", '
    '"due": null, "time": null, "size": "S", "cell_id": null}'
)


# ---------- событие ----------
def forward_name(msg: dict) -> str | None:
    o = msg.get("forward_origin")
    if o:
        t = o.get("type")
        if t == "user":
            u = o.get("sender_user") or {}
            return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username")
        if t == "hidden_user":
            return o.get("sender_user_name")
        if t in ("chat", "channel"):
            c = o.get("sender_chat") or o.get("chat") or {}
            name = c.get("title") or c.get("username")
            sig = o.get("author_signature")
            return f"{name} ({sig})" if sig and name else (name or sig)
    if msg.get("forward_from"):
        u = msg["forward_from"]
        return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username")
    if msg.get("forward_sender_name"):
        return msg["forward_sender_name"]
    if msg.get("forward_from_chat"):
        return msg["forward_from_chat"].get("title")
    return None


def msg_text(msg: dict) -> str:
    return (msg.get("text") or msg.get("caption") or "").strip()


def has_link(msg: dict) -> bool:
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("type") in ("url", "text_link"):
            return True
    return bool(RX_URL.search(msg_text(msg)))


def tg_meta(msg: dict) -> dict:
    m = {"message_id": msg.get("message_id"), "thread_id": msg.get("message_thread_id"),
         "date": msg.get("date")}
    if msg.get("photo"):
        m["photo_file_id"] = msg["photo"][-1].get("file_id")
    for k in ("voice", "audio", "video_note", "document"):
        if msg.get(k):
            m[f"{k}_file_id"] = msg[k].get("file_id")
    if msg.get("reply_to_message"):
        m["reply_to"] = msg["reply_to_message"].get("message_id")
    o = msg.get("forward_origin")
    if o and o.get("date"):
        m["forward_date"] = o["date"]
    return m


def store_event(ctx: Ctx, msg: dict, body: str | None = None, kind: str = "note",
                meta: dict | None = None, context_id: str = "personal") -> int | None:
    """INSERT в work.events, идемпотентно по id сообщения. None = уже было (повторная доставка)."""
    body = msg_text(msg) if body is None else body
    fwd = forward_name(msg)
    full = {"tg": tg_meta(msg), "forwarded": bool(fwd or msg.get("forward_origin")),
            "has_photo": bool(msg.get("photo")), "has_link": has_link(msg),
            "voice": bool(msg.get("voice") or msg.get("audio"))}
    full.update(meta or {})
    occurred = datetime.fromtimestamp(msg.get("date") or tu.now().timestamp(), timezone.utc)
    title = (body.splitlines()[0][:120] if body else None)
    row = ctx.conn.execute(
        """INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at,
                                    actor_raw, title, body, meta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source_id, external_id) DO NOTHING RETURNING id""",
        (context_id, SOURCE, str(msg["message_id"]), kind, occurred, fwd, title, body or None,
         db.jb(full))).fetchone()
    return row["id"] if row else None


def merge_meta(conn, event_id: int, patch: dict) -> None:
    conn.execute("UPDATE work.events SET meta = meta || %s WHERE id = %s", (db.jb(patch), event_id))


# ---------- разметка ----------
def cells(conn) -> dict[str, str]:
    rows = conn.execute("""SELECT id, title FROM rpg.cells
                           WHERE active AND kind IN ('attribute','habit','milestone','quality')
                           ORDER BY id""").fetchall()
    return {r["id"]: r["title"] for r in rows}


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _time(v) -> time | None:
    m = re.match(r"^(\d{1,2}):(\d{2})", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def classify(conn, text: str, now: datetime | None = None, fwd: str | None = None) -> dict:
    """Класс захвата: модель + сверка дат с ruparse (явная дата из текста побеждает)."""
    now = now or tu.now()
    today = tu.logical_date(now)
    ctxs = contexts(conn)
    grid = cells(conn)
    p = ruparse.parse(text, now)
    loc = tu.local(now)
    user = (f"Сейчас: {loc:%Y-%m-%d %H:%M} ({tu.RU_WEEKDAYS[loc.weekday()]}), "
            f"логическое сегодня: {today.isoformat()}.\n"
            "Контексты: " + "; ".join(f"{k} — {v}" for k, v in ctxs.items()) + "\n"
            + ("Клетки сетки: " + "; ".join(f"{k} — {v}" for k, v in grid.items()) + "\n" if grid else "")
            + (f"Переслано от: {fwd}\n" if fwd else "")
            + f"Заметка:\n{text}")
    model = config.model_for("capture")
    try:
        r = llm.complete_json("capture", SYSTEM, user)
        if not isinstance(r, dict):
            raise llm.LLMError("не объект")
    except Exception as e:  # модель недоступна: разбор без неё
        log.warning("capture: модель не ответила: %s", e)
        r = {"kind": "task" if (p.due or p.at or p.week_of) else "fact", "confidence": 0.0}
        model = "ruparse"
    kind = r.get("kind") if r.get("kind") in KINDS else "fact"
    cid = r.get("context_id") if r.get("context_id") in ctxs else None
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    if cid is None:
        cid, conf = ("personal" if "personal" in ctxs else next(iter(ctxs), "personal")), 0.0
    size = r.get("size") if r.get("size") in ("S", "M", "L") else "S"
    cell = r.get("cell_id") if r.get("cell_id") in grid else None
    title = (str(r.get("title") or "").strip() or p.title or text)[:200]

    due, week_of, at = _date(r.get("due")), None, _time(r.get("time"))
    if due and due < today:
        due = None                        # прошедшая дата от модели почти всегда ошибка
    if p.due or p.week_of:                # явная дата или время из текста (время ruparse дополняет датой)
        due, week_of, at = p.due, p.week_of, p.at or (at if p.due else None)
    if at and not due and not week_of:
        due = today if tu.at_local(today, at) > now else today + timedelta(days=1)
    return {"kind": kind, "context": cid, "confidence": round(conf, 2), "title": title,
            "due": due.isoformat() if due else None, "week_of": week_of.isoformat() if week_of else None,
            "time": at.strftime("%H:%M") if at else None, "size": size, "cell_id": cell, "model": model}


def create_task(conn, cls: dict) -> int:
    due, at = _date(cls.get("due")), _time(cls.get("time"))
    remind = tu.at_local(due, at) if (due and at) else None
    return conn.execute(
        """INSERT INTO work.tasks (context_id, title, due, week_of, remind_at, size, cell_id, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'capture') RETURNING id""",
        (cls.get("context"), cls["title"], due, _date(cls.get("week_of")), remind,
         cls.get("size") or "S", cls.get("cell_id"))).fetchone()["id"]


# ---------- ответ ----------
def when_label(cls: dict) -> str:
    d = _date(cls.get("due"))
    if d:
        return short_date(d) + (f" {cls['time']}" if cls.get("time") else "")
    if cls.get("week_of"):
        return "на неделе"
    return "потом"


def render(conn, event_id: int, cls: dict, note: str | None = None) -> tuple[str, list]:
    ctxs = contexts(conn)
    grid = cells(conn)
    kind = cls["kind"]
    parts = [f"<b>{KIND_LABEL[kind]}</b>", esc(ctxs.get(cls.get("context"), cls.get("context") or ""))]
    if kind == "task":
        parts.append(when_label(cls))
        if cls.get("cell_id") and cls["cell_id"] in grid:
            parts.append(f"клетка «{esc(grid[cls['cell_id']])}»")
    text = " · ".join(p for p in parts if p)
    if kind == "task":
        text += f"\n{esc(cls['title'])}"
    elif kind == "idea":
        text += "\nСохранил, её найдёт поиск."
    elif kind == "question":
        text += "\nСохранил. Ответ по памяти: задай вопрос в «Спросить»."
    if note:
        text += f"\n<i>{esc(note)}</i>"
    mark = lambda k, label: ("✓ " if kind == k else "") + label  # noqa: E731
    rows = [[btn(mark("task", "Задача"), f"cap:{event_id}:task"),
             btn(mark("idea", "Идея"), f"cap:{event_id}:idea"),
             btn(mark("fact", "Только в базу"), f"cap:{event_id}:fact")]]
    if float(cls.get("confidence") or 0) < SURE:
        row = [btn(("✓ " if cls.get("context") == k else "") + v, f"cap:{event_id}:ctx:{k}")
               for k, v in ctxs.items()]
        rows += [row[i:i + 2] for i in range(0, len(row), 2)]
    if kind == "task" and grid:
        rows.append([btn("Сменить клетку", f"cap:{event_id}:cell")])
    return text, rows


# ---------- вход ----------
def handle(ctx: Ctx, msg: dict, text: str | None = None, voice: bool = False,
           extra_meta: dict | None = None) -> int | None:
    """Захват одного сообщения. Возвращает id события или None (повторная доставка)."""
    conn = ctx.conn
    body = msg_text(msg) if text is None else text
    meta = dict(extra_meta or {})
    if voice:
        meta["voice"] = True
    eid = store_event(ctx, msg, body=body, meta=meta)
    if eid is None:
        return None
    if not body:
        what = "Фото" if msg.get("photo") else "Сообщение"
        ctx.reply(msg, f"{what} сохранил в базу.")
        return eid
    now = tu.now()
    cls = classify(conn, body, now, forward_name(msg))
    with conn.transaction():
        patch = {"class": cls}
        if cls["kind"] == "task":
            patch["task_id"] = create_task(conn, cls)
        conn.execute("UPDATE work.events SET context_id = %s, meta = meta || %s WHERE id = %s",
                     (cls["context"], db.jb(patch), eid))
    text_out, rows = render(conn, eid, cls, note="модель недоступна, разобрал по датам" if cls["model"] == "ruparse" else None)
    sent = ctx.reply(msg, text_out, rows)
    if sent and sent.get("message_id"):
        merge_meta(conn, eid, {"reply_id": sent["message_id"]})
    return eid


def _load(conn, eid: int) -> dict | None:
    return conn.execute("SELECT id, context_id, body, meta FROM work.events WHERE id = %s AND source_id = %s",
                        (eid, SOURCE)).fetchone()


def _task_state(conn, task_id) -> dict | None:
    if not task_id:
        return None
    return conn.execute("SELECT id, done_at, dropped_at FROM work.tasks WHERE id = %s", (task_id,)).fetchone()


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    """cap:<event_id>:<op>[:<arg>] -> текст для answerCallbackQuery."""
    conn = ctx.conn
    try:
        eid = int(parts[1])
    except (IndexError, ValueError):
        return "Не понял кнопку"
    op = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else None
    ev = _load(conn, eid)
    if not ev or not (ev["meta"] or {}).get("class"):
        return "Не нашёл захват"
    meta = ev["meta"]
    cls = dict(meta["class"])
    msg_id = (cq.get("message") or {}).get("message_id")
    grid = cells(conn)

    if op == "cell":
        rows, row = [], []
        for cid, title in grid.items():
            row.append(btn(("✓ " if cls.get("cell_id") == cid else "") + title[:28], f"cap:{eid}:c:{cid}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows = rows[:40] + [[btn("Без клетки", f"cap:{eid}:c:-")]]
        if msg_id:
            ctx.edit(msg_id, None, rows)
        return None

    before = dict(cls)
    task = _task_state(conn, meta.get("task_id"))
    patch: dict = {}
    with conn.transaction():
        if op in ("task", "idea", "fact"):
            cls["kind"] = op
            if op == "task":
                if task and task["dropped_at"] is not None and task["done_at"] is None:
                    conn.execute("UPDATE work.tasks SET dropped_at = NULL WHERE id = %s", (task["id"],))
                elif not task:
                    patch["task_id"] = create_task(conn, cls)
            elif task and task["dropped_at"] is None and task["done_at"] is None:
                conn.execute("UPDATE work.tasks SET dropped_at = now() WHERE id = %s", (task["id"],))
        elif op == "ctx" and arg in contexts(conn):
            cls["context"], cls["confidence"] = arg, 1.0
            conn.execute("UPDATE work.events SET context_id = %s WHERE id = %s", (arg, eid))
            if task:
                conn.execute("UPDATE work.tasks SET context_id = %s WHERE id = %s", (arg, task["id"]))
        elif op == "c":
            cell = None if arg in (None, "-") else arg
            if cell is not None and cell not in grid:
                return "Такой клетки нет"
            cls["cell_id"] = cell
            if task:
                conn.execute("UPDATE work.tasks SET cell_id = %s WHERE id = %s", (cell, task["id"]))
        else:
            return "Не понял кнопку"
        if before != cls:
            corr = list(meta.get("corrections") or [])
            corr.append({"at": tu.now().isoformat(), "op": op, "arg": arg})
            patch.update({"class": cls, "class_after": cls, "corrections": corr})
            if "class_before" not in meta:
                patch["class_before"] = before
        if patch:
            merge_meta(conn, eid, patch)
    if msg_id:
        text_out, rows = render(conn, eid, cls)
        ctx.edit(msg_id, text_out, rows)
    return "Поправил" if before != cls else None


def on_edit(ctx: Ctx, msg: dict) -> None:
    """Правка уже отправленного сообщения: текст в meta, у открытой задачи обновляются название и дата."""
    conn = ctx.conn
    row = conn.execute("SELECT id, meta FROM work.events WHERE source_id = %s AND external_id = %s",
                       (SOURCE, str(msg.get("message_id")))).fetchone()
    if not row:
        return
    text = msg_text(msg)
    meta = row["meta"] or {}
    patch = {"edited_text": text, "edited_at": tu.now().isoformat()}
    cls = meta.get("class")
    task = _task_state(conn, meta.get("task_id"))
    with conn.transaction():
        if cls and cls.get("kind") == "task" and task and task["done_at"] is None and task["dropped_at"] is None:
            p = ruparse.parse(text, tu.now())
            cls = dict(cls, title=p.title or cls["title"])
            if p.due or p.week_of:
                cls.update(due=p.due.isoformat() if p.due else None,
                           week_of=p.week_of.isoformat() if p.week_of else None,
                           time=p.at.strftime("%H:%M") if p.at else None)
            due, at = _date(cls.get("due")), _time(cls.get("time"))
            conn.execute("""UPDATE work.tasks SET title = %s, due = %s, week_of = %s, remind_at = %s,
                                   reminded_at = NULL WHERE id = %s""",
                         (cls["title"], due, _date(cls.get("week_of")),
                          tu.at_local(due, at) if due and at else None, task["id"]))
            patch["class"] = cls
        merge_meta(conn, row["id"], patch)
    if cls and meta.get("reply_id"):
        t, rows = render(conn, row["id"], cls)
        ctx.edit(meta["reply_id"], t, rows)

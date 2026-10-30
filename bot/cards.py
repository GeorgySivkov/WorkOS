"""5.1 / 5.4 / 6.2 · Карточки действий, одобрение, исходы, автономия.

Правила, на которых держится безопасность:
- одобрение приходит только из нажатия кнопки (terminal пускает callback только
  с user id George), ответ модели до этих обработчиков не доходит;
- хеш payload считается SQL-выражением в момент показа карточки и пишется в
  shown_hash, первые 8 hex едут в callback; при нажатии UPDATE проходит только
  если хеш текущего payload равен shown_hash и префиксу на кнопке, и только тогда
  ставится approved_hash: текста, которого George не видел, исполнитель не получит;
- нажатие одобрения принимается только с сообщения, чья строка outbox создана
  самим ботом и ссылается на это действие (is_card);
- без нажатия бот одобряет только внутренние типы (AUTO_ALLOWED) и только по своей
  выборке из work.autonomy, actions.autonomy (его пишет предлагающий) не читается;
- обрезанную карточку нельзя ни отправить, ни оставить черновиком;
- «Поправить» не меняет строку, а создаёт новую (replaces) со своей карточкой.

callback_data: act:<id>:ok|dr|no|fix|bk:<h8> · act:<id>:fx:short|formal|cut|tomorrow · rb:<id>
"""
from __future__ import annotations

import copy
import difflib
import json
import logging
import secrets
from datetime import date, datetime, time, timedelta

from core import config, db, llm, timeutil as tu
from core.tg import btn, esc

from bot.topics import Ctx, contexts, short_date

log = logging.getLogger("workos.bot")

H = db.payload_hash_sql("payload")
INTERNAL = {"plan_batch", "task_add"}          # исполняет сам бот
# без нажатия бывают только внутренние типы: инъекция в письме или расшифровке
# не должна дойти до общей доски Trello или чужого ящика без кнопки George
AUTO_ALLOWED = frozenset({"plan_batch", "task_add"})
MAX_CARD = 3800
TYPE_LABEL = {
    "email_reply": "Ответ на письмо",
    "email_send": "Письмо",
    "trello_card": "Карточка Trello",
    "trello_comment": "Комментарий в Trello",
    "slack_post": "Сообщение в Slack",
    "task_add": "Добавить задачу",
    "plan_batch": "План",
    "calendar_block": "Блок в календаре",
}
FIX_INSTR = {
    "short": "Сделай текст короче, примерно вдвое. Сохрани суть, факты, имена и тон.",
    "formal": "Сделай текст формальнее и вежливее, без канцелярита. Факты и имена не меняй.",
}
REWRITE_SYSTEM = (
    "Ты правишь черновик George по короткой инструкции. На вход JSON с текстовыми полями. "
    "Меняй только формулировки, не добавляй фактов, адресатов и обещаний, не используй длинное тире. "
    "Верни JSON с теми же ключами и строковыми значениями (checklist_items — список строк)."
)
FIX_TTL = 900                                   # сколько ждём свободный текст после «Поправить»


# ---------- выборка и хеш ----------
def fetch(conn, aid: int) -> dict | None:
    return conn.execute(
        f"""SELECT id, type, context_id, payload, rationale, status, autonomy, source_event,
                   replaces, executed_at, result, {H} AS h, left({H}, 8) AS h8
            FROM work.actions WHERE id = %s""", (aid,)).fetchone()


def topic_for(atype: str) -> str:
    return "plan" if atype == "plan_batch" else "decisions"


def outcome(conn, row: dict, verdict: str, diff: str | None = None) -> None:
    conn.execute("""INSERT INTO work.outcomes (action_id, action_type, context_id, verdict, diff)
                    VALUES (%s, %s, %s, %s, %s)""",
                 (row["id"], row["type"], row["context_id"], verdict, diff))


def approve(conn, aid: int, h8: str | None, status: str = "approved", auto: bool = False) -> dict | None:
    """Одобрение одним UPDATE: хеш считается и сверяется в SQL по текущему payload.

    При показе карточки полный хеш показанного payload кладётся в shown_hash,
    первые 8 hex едут в callback. Одобрение проходит, только если хеш текущего
    payload равен shown_hash целиком, а префикс shown_hash равен тому, что на
    нажатой кнопке; тогда же ставится approved_hash (его и сверяет исполнитель).
    auto=True: автономный путь, только внутренние типы, одобряется то, что лежит сейчас
    (бот сразу исполняет это сам в той же транзакции).
    """
    if auto:
        cond = "AND type = ANY(%(allowed)s)"
    else:
        if not h8:
            return None
        cond = f"AND shown_hash = {H} AND left(shown_hash, 8) = %(h8)s"
    return conn.execute(
        f"""UPDATE work.actions
               SET status = %(st)s, approved_hash = {H}, approval_nonce = %(nonce)s,
                   approved_at = now(), decided_at = now()
             WHERE id = %(id)s AND status = 'proposed' {cond}
         RETURNING id, type, context_id, payload, rationale, approved_hash""",
        {"st": status, "nonce": secrets.token_hex(16), "id": aid, "h8": h8,
         "allowed": sorted(AUTO_ALLOWED)}).fetchone()


# ---------- задачи из внутренних действий ----------
def _valid_ids(conn) -> tuple[set, set]:
    cx = {r["id"] for r in conn.execute("SELECT id FROM work.contexts").fetchall()}
    cl = {r["id"] for r in conn.execute("SELECT id FROM rpg.cells").fetchall()}
    return cx, cl


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _t(v) -> time | None:
    try:
        h, m = str(v).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _ts(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=config.TZ)
    except (TypeError, ValueError):
        return None


def insert_task_add(conn, row: dict) -> list[int]:
    p = row["payload"] or {}
    cx, cl = _valid_ids(conn)
    due, at = _d(p.get("due")), _t(p.get("time"))
    ctx_id = p.get("context_id") if p.get("context_id") in cx else row["context_id"]
    tid = conn.execute(
        """INSERT INTO work.tasks (context_id, title, due, remind_at, size, cell_id, source, action_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (ctx_id, str(p.get("title") or "Без названия")[:300], due,
         remind_at(due, at),
         p.get("size") if p.get("size") in ("S", "M", "L") else "S",
         p.get("cell_id") if p.get("cell_id") in cl else None,
         "commitment" if p.get("commitment_id") else "plan", row["id"])).fetchone()["id"]
    return [tid]


def remind_at(due: date | None, at: time | None) -> datetime | None:
    """Время логического дня due: 00:00-03:59 уже следующая календарная дата."""
    if not due or not at:
        return None
    return tu.at_local(due + timedelta(days=1) if at.hour < config.LOGICAL_DAY_END_HOUR else due, at)


def execute_internal(conn, row: dict, selected: list[int] | None = None) -> list[int]:
    """plan_batch / task_add: задачи кладёт сам бот, статус executed, id задач в result."""
    if row["type"] == "plan_batch":
        from bot import plancard
        ids = plancard.insert_tasks(conn, row, selected)
    else:
        ids = insert_task_add(conn, row)
    conn.execute("UPDATE work.actions SET status = 'executed', result = %s WHERE id = %s",
                 (db.jb({"task_ids": ids, "selected": selected, "by": "bot"}), row["id"]))
    return ids


# ---------- текст карточки ----------
def _join(v) -> str:
    return ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "")


def trello_list_name(conn, list_id) -> str | None:
    """Имя списка (и доски) по событиям коллектора Trello; None, если таких карточек не видели."""
    if conn is None or not list_id:
        return None
    try:
        with conn.transaction():          # точка сохранения: сбой подписи не ломает карточку
            r = conn.execute("""SELECT meta->>'board' AS board, meta->>'list' AS list FROM work.events
                                WHERE meta @> jsonb_build_object('list_id', %s::text)
                                ORDER BY occurred_at DESC LIMIT 1""", (str(list_id),)).fetchone()
    except Exception:
        return None
    if not r or not r["list"] or r["list"] == str(list_id):
        return None
    return f"{r['board']} / {r['list']}" if r["board"] else r["list"]


def _lines(atype: str, p: dict, conn=None) -> list[str]:
    if atype in ("email_reply", "email_send"):
        out = []
        if p.get("account"):
            out.append(f"Ящик: {esc(p['account'])}")
        out.append("Кому: " + esc(_join(p.get("to"))))
        if p.get("cc"):
            out.append("Копия: " + esc(_join(p["cc"])))
        out.append(f"Тема: <b>{esc(p.get('subject'))}</b>")
        out.append(f"Ветка Gmail: <code>{esc(p['thread_id'])}</code>" if p.get("thread_id")
                   else "Ветка Gmail: новое письмо, не ответ в ветке")
        out += ["", esc(p.get("body"))]
        return out
    if atype == "trello_card":
        name = trello_list_name(conn, p.get("list_id"))
        out = ["Список: " + (f"{esc(name)} (<code>{esc(p.get('list_id'))}</code>)" if name
                             else f"<code>{esc(p.get('list_id'))}</code>"),
               f"<b>{esc(p.get('name'))}</b>"]
        if p.get("desc"):
            out.append(esc(p["desc"]))
        if p.get("labels"):
            out.append("Метки: " + esc(_join(p["labels"])))
        ch = p.get("checklist") or {}
        if ch.get("items"):
            out.append(f"Чеклист «{esc(ch.get('name') or 'Чеклист')}»:")
            out += [f"☐ {esc(i)}" for i in ch["items"]]
        if p.get("due"):
            dt = _ts(p["due"])
            out.append("Срок: " + (f"{short_date(tu.local(dt).date())} {tu.hm(dt)}" if dt else esc(p["due"])))
        return out
    if atype == "slack_post":
        out = [f"Канал: {esc(p.get('channel'))}" + (" (в ветке)" if p.get("thread_ts") else ""), "",
               esc(p.get("text"))]
        return out
    if atype == "task_add":
        d = _d(p.get("due"))
        when = (short_date(d) + (f" {p['time']}" if p.get("time") else "")) if d else "потом"
        return [f"<b>{esc(p.get('title'))}</b>", f"{when} · {esc(p.get('size') or 'S')}"]
    return [f"<pre>{esc(json.dumps(p, ensure_ascii=False, indent=1))}</pre>"]


def card_text(conn, row: dict) -> tuple[str, bool]:
    """Текст карточки и флаг «обрезано» (обрезанное нельзя «Отправить»: George его не видел)."""
    ctxs = contexts(conn)
    head = f"<b>{TYPE_LABEL.get(row['type'], esc(row['type']))}</b> · {esc(ctxs.get(row['context_id'], row['context_id']))}"
    parts = [head]
    if row.get("rationale"):
        parts.append(f"<i>{esc(row['rationale'])}</i>")
    body = "\n".join(_lines(row["type"], row["payload"] or {}, conn))
    text = "\n".join(parts) + "\n\n" + body
    if len(text) <= MAX_CARD:
        return text, False
    cut = text[:MAX_CARD - 200]
    cut = cut[:cut.rfind("\n")] if "\n" in cut[-300:] else cut
    # закрытые теги не гарантированы после обрезки: убираем разметку из хвоста
    from bot.topics import strip_tags
    return (esc(strip_tags(cut)) + "\n…\n" + TRUNCATED_NOTE), True


TRUNCATED_NOTE = ("<i>Текст длинный и показан не целиком: ни отправить, ни оставить черновиком "
                  "нельзя. «Поправить» → «Короче», или «Отклонить».</i>")


def main_buttons(row: dict, h8: str, truncated: bool = False) -> list:
    i = row["id"]
    if truncated:
        # непоказанный текст никуда не уходит: ни отправкой, ни черновиком (черновик Trello
        # виден команде), только сократить или отклонить
        return [[btn("Поправить", f"act:{i}:fix:{h8}"), btn("Отклонить", f"act:{i}:no:{h8}")]]
    if row["type"] == "task_add":
        return [[btn("В список", f"act:{i}:ok:{h8}"), btn("Не надо", f"act:{i}:no:{h8}")],
                [btn("Поправить", f"act:{i}:fix:{h8}")]]
    return [[btn("Отправить", f"act:{i}:ok:{h8}"), btn("Оставить черновиком", f"act:{i}:dr:{h8}")],
            [btn("Поправить", f"act:{i}:fix:{h8}"), btn("Отклонить", f"act:{i}:no:{h8}")]]


def fix_buttons(row: dict, h8: str) -> list:
    i = row["id"]
    rows = [[btn("Короче", f"act:{i}:fx:short"), btn("Формальнее", f"act:{i}:fx:formal")],
            [btn("Убрать последний абзац", f"act:{i}:fx:cut")],
            [btn("На завтра", f"act:{i}:fx:tomorrow"), btn("Назад", f"act:{i}:bk:{h8}")]]
    if row["type"] == "task_add":
        rows = [[btn("На завтра", f"act:{i}:fx:tomorrow"), btn("Короче", f"act:{i}:fx:short")],
                [btn("Назад", f"act:{i}:bk:{h8}")]]
    return rows


def render(conn, row: dict) -> tuple[str, list]:
    if row["type"] == "plan_batch":
        from bot import plancard
        return plancard.render(conn, row, row["h8"])
    text, truncated = card_text(conn, row)
    return text, main_buttons(row, row["h8"], truncated)


# ---------- показ ----------
def is_card(conn, aid: int, msg_id) -> bool:
    """Одобрять можно только с карточки, которую нарисовал сам бот (не с чужой строки outbox).

    created_by ставит триггер 009 (= роль, вставившая строку), подделать его нельзя."""
    if not msg_id or aid is None:
        return False
    return conn.execute("""SELECT 1 FROM work.outbox WHERE tg_message_id = %s AND ref_action = %s
                           AND created_by = current_user""", (msg_id, aid)).fetchone() is not None


def _has_card(conn, aid: int) -> bool:
    return conn.execute("SELECT 1 FROM work.outbox WHERE ref_action = %s AND created_by = current_user",
                        (aid,)).fetchone() is not None


def post_card(ctx: Ctx, aid: int, direct: bool = False, force: bool = False) -> dict | None:
    """Карточка действия. direct: отправить сейчас (ответ на действие George), иначе через outbox.

    Строка outbox с ref_action = отметка «карточка есть»; замок общий у потоков,
    поэтому одну карточку не нарисуют дважды.
    """
    conn = ctx.conn
    with ctx.lock:
        if not force and _has_card(conn, aid):
            return None
        with conn.transaction():
            # снимок: payload для текста и его хеш берутся одним UPDATE ... RETURNING;
            # approved_hash не трогаем, его ставит только нажатие (approve)
            row = conn.execute(
                f"""UPDATE work.actions SET shown_hash = {H}
                    WHERE id = %s AND status = 'proposed'
                    RETURNING id, type, context_id, payload, rationale, status, autonomy, source_event,
                              replaces, executed_at, result, shown_hash AS h, left(shown_hash, 8) AS h8""",
                (aid,)).fetchone()
            if not row:
                return None
            text, buttons = render(conn, row)
            topic = topic_for(row["type"])
            oid = conn.execute("""INSERT INTO work.outbox (topic, text, buttons, ref_action)
                                  VALUES (%s, %s, %s, %s) RETURNING id""",
                               (topic, text, db.jb(buttons), aid)).fetchone()["id"]
            if not direct:
                return {"queued": True}
            m = ctx.send(topic, text, buttons)
            conn.execute("UPDATE work.outbox SET sent_at = now(), tg_message_id = %s WHERE id = %s",
                         (m.get("message_id"), oid))
        return m


# ---------- автономия ----------
def auto_row(conn, atype: str, context_id: str | None) -> dict | None:
    """Строка work.autonomy, по которой действие идёт без нажатия, или None.

    Решает только эта выборка бота: своя строка (тип, контекст), иначе (тип, '*');
    любой замок, включая общий рубильник ('*', '*'), значит подтверждение.
    actions.autonomy не читается: его пишет тот, кто предлагает."""
    if atype not in AUTO_ALLOWED or atype in config.NEVER_AUTO:
        return None
    cx = context_id or "*"
    rows = conn.execute("""SELECT action_type, context_id, mode, locked FROM work.autonomy
                           WHERE (action_type, context_id) IN ((%s, %s), (%s, '*'), ('*', '*'))""",
                        (atype, cx, atype)).fetchall()
    if any(r["locked"] for r in rows):
        return None
    own = [r for r in rows if r["action_type"] == atype and r["context_id"] == cx]
    star = [r for r in rows if r["action_type"] == atype and r["context_id"] == "*"]
    row = (own or star or [None])[0]
    return row if row and row["mode"] == "auto" else None


def auto_allowed(conn, atype: str, context_id: str | None) -> bool:
    return auto_row(conn, atype, context_id) is not None


def sampled(conn, row: dict) -> bool:
    """Семплинг: каждое AUTONOMY_SAMPLE_EVERY-е автономное всё равно идёт карточкой,
    иначе accept rate замирает и деградация не сработает никогда."""
    r = conn.execute("""UPDATE work.autonomy SET auto_count = auto_count + 1
                        WHERE action_type = %s AND context_id = %s RETURNING auto_count""",
                     (row["action_type"], row["context_id"])).fetchone()
    every = config.AUTONOMY_SAMPLE_EVERY
    return bool(r and every and r["auto_count"] % every == 0)


def auto_approve(ctx: Ctx, aid: int) -> bool:
    conn = ctx.conn
    with conn.transaction():
        row = approve(conn, aid, None, auto=True)
        if not row:
            return False
        if row["type"] in INTERNAL:
            ids = execute_internal(conn, row)
            if row["type"] == "plan_batch":
                from bot import plancard
                text = "🤖 Сам добавил в список: " + plancard.summary(row["payload"]) + "\n" + \
                       plancard.items_text(row["payload"])
            else:
                text = f"🤖 Сам добавил задачу: {esc((row['payload'] or {}).get('title'))}"
            text += f"\n<i>Задач в списке: {len(ids)}. Не то, жми «Откатить».</i>"
        else:
            body, _ = card_text(conn, row)
            text = "🤖 Сам одобрил, исполнитель отправит:\n" + body
        conn.execute("INSERT INTO work.outbox (topic, text, buttons, ref_action) VALUES (%s, %s, %s, %s)",
                     (topic_for(row["type"]), text, db.jb([[btn("Откатить", f"rb:{aid}")]]), aid))
    return True


def process_proposed(ctx: Ctx, limit: int = 20) -> dict:
    """Тик: карточки по новым proposed, автономные одобряются сами."""
    conn = ctx.conn
    stats = {"cards": 0, "auto": 0}
    with ctx.lock:
        rows = conn.execute(
            """SELECT a.id, a.type, a.context_id, a.replaces FROM work.actions a
               WHERE a.status = 'proposed' AND a.created_at <= now()
                 AND NOT EXISTS (SELECT 1 FROM work.outbox o
                                 WHERE o.ref_action = a.id AND o.created_by = current_user)
               ORDER BY a.id LIMIT %s""", (limit,)).fetchall()
        for r in rows:
            try:
                # поправленное George (replaces) всегда карточкой: он ждёт её увидеть
                arow = None if r["replaces"] else auto_row(conn, r["type"], r["context_id"])
                if arow is not None and not sampled(conn, arow):
                    if auto_approve(ctx, r["id"]):
                        stats["auto"] += 1
                        continue
                if post_card(ctx, r["id"]):
                    stats["cards"] += 1
            except Exception:
                log.exception("карточка действия %s", r["id"])
                ctx.notify_system(f"Не смог показать действие #{r['id']}, смотри журнал бота.",
                                  key=f"card:{r['id']}")
    return stats


# ---------- нажатия ----------
DONE_NOTE = {
    "approved": "✅ Одобрено, исполнитель отправит.",
    "drafted": "📝 Оставлено черновиком: исполнитель создаст черновик и не отправит.",
    "rejected": "✖️ Отклонено.",
}


def _stale(ctx: Ctx, row: dict | None, msg_id: int | None) -> str:
    if not row:
        return "Действие не найдено"
    if row["status"] != "proposed":
        if msg_id:
            ctx.edit(msg_id, None, [])
        return f"Уже решено: {row['status']}"
    # статус proposed, а UPDATE не прошёл: текст поменялся после показа карточки
    if msg_id:
        ctx.edit(msg_id, "Карточка устарела: текст поменялся после показа. Новая карточка ниже.", [])
    post_card(ctx, row["id"], direct=True, force=True)
    return "Текст изменился после показа карточки, одобрение не принято"


def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    conn = ctx.conn
    try:
        aid = int(parts[1])
        op = parts[2]
    except (IndexError, ValueError):
        return "Не понял кнопку"
    arg = parts[3] if len(parts) > 3 else None
    msg_id = (cq.get("message") or {}).get("message_id")

    if op in ("ok", "dr"):
        st = "approved" if op == "ok" else "drafted"
        cur = fetch(conn, aid)
        if cur and cur["type"] == "plan_batch":
            return "Это карточка плана, жми кнопки на ней"
        if op == "dr" and cur and cur["type"] in INTERNAL:
            return "У задачи черновика не бывает"
        if not is_card(conn, aid, msg_id):
            return "Одобрить можно только с карточки действия"
        if cur and cur["status"] == "proposed" and card_text(conn, cur)[1]:
            return "Текст показан не целиком: сначала «Поправить» → «Короче»"
        with ctx.lock, conn.transaction():
            row = approve(conn, aid, arg, status=st)
            if row:
                if row["type"] in INTERNAL:
                    execute_internal(conn, row)
                outcome(conn, row, "accepted" if op == "ok" else "drafted")
        if not row:
            return _stale(ctx, fetch(conn, aid), msg_id)
        if msg_id:
            text, _ = card_text(conn, row)
            note = "✅ В списке." if row["type"] in INTERNAL else DONE_NOTE[st]
            ctx.edit(msg_id, text + "\n\n" + note, [])
        return "Готово"

    if op == "no":
        with ctx.lock, conn.transaction():
            row = conn.execute("""UPDATE work.actions SET status = 'rejected', decided_at = now()
                                  WHERE id = %s AND status = 'proposed'
                                  RETURNING id, type, context_id, payload, rationale""", (aid,)).fetchone()
            if row:
                outcome(conn, row, "rejected")
        if not row:
            return _stale(ctx, fetch(conn, aid), msg_id)
        if msg_id:
            text, _ = card_text(conn, row)
            ctx.edit(msg_id, text + "\n\n" + DONE_NOTE["rejected"], [])
        return "Отклонено"

    if op in ("fix", "bk"):
        row = fetch(conn, aid)
        if not row or row["status"] != "proposed":
            return _stale(ctx, row, msg_id)
        h8 = arg or row["h8"]
        if msg_id:
            text, truncated = card_text(conn, row)
            if op == "fix":
                ctx.state["fix"] = {"aid": aid, "msg_id": msg_id, "at": tu.now()}
                ctx.edit(msg_id, text + "\n\n<i>Выбери правку или ответь на эту карточку своими словами.</i>",
                         fix_buttons(row, h8))
            else:
                ctx.edit(msg_id, text, main_buttons(row, h8, truncated))
        return None

    if op == "fx":
        res = rewrite(ctx, aid, arg or "", old_msg_id=msg_id)
        return res
    return "Не понял кнопку"


def on_rollback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    """rb:<id>: откат автономного действия."""
    conn = ctx.conn
    try:
        aid = int(parts[1])
    except (IndexError, ValueError):
        return "Не понял кнопку"
    msg_id = (cq.get("message") or {}).get("message_id")
    if not is_card(conn, aid, msg_id):
        return "Откатить можно только с уведомления бота"
    with ctx.lock, conn.transaction():
        row = fetch(conn, aid)
        if not row:
            return "Действие не найдено"
        if conn.execute("SELECT 1 FROM work.outcomes WHERE action_id = %s AND verdict = 'rejected'",
                        (aid,)).fetchone():
            return "Уже откатил"
        if row["type"] in INTERNAL:
            dropped = conn.execute("""UPDATE work.tasks SET dropped_at = now()
                                      WHERE action_id = %s AND dropped_at IS NULL AND done_at IS NULL
                                      RETURNING id""", (aid,)).fetchall()
            conn.execute("UPDATE work.actions SET status = 'rejected' WHERE id = %s", (aid,))
            note = f"↩️ Откатил: убрал из списка {len(dropped)} {_tasks_word(len(dropped))}."
        else:
            stopped = conn.execute("""UPDATE work.actions SET status = 'rejected'
                                      WHERE id = %s AND status IN ('approved','drafted') AND executed_at IS NULL
                                      RETURNING id""", (aid,)).fetchone()
            if stopped:
                note = "↩️ Остановил: исполнитель это не отправит."
            else:
                res = row.get("result") or {}
                where = res.get("url") or res.get("id") or ""
                note = ("↩️ Уже исполнено, откатить сам не могу: убери руками"
                        + (f" ({esc(where)})" if where else "") + ". Отметил как отклонённое.")
        outcome(conn, row, "rejected", "rollback")
    if msg_id:
        ctx.edit(msg_id, None, [])
        ctx.send(topic_for(row["type"]), note, reply_to=msg_id)
    return "Откатил"


def _tasks_word(n: int) -> str:
    from bot.topics import plural
    return plural(n, "задачу", "задачи", "задач")


# ---------- «Поправить» ----------
def text_fields(atype: str, p: dict) -> dict:
    if atype in ("email_reply", "email_send"):
        keys = ["subject", "body"]
    elif atype == "slack_post":
        keys = ["text"]
    elif atype == "trello_card":
        keys = ["name", "desc"]
    elif atype == "task_add":
        keys = ["title"]
    else:
        keys = [k for k in ("title", "subject", "name", "text", "body", "desc", "message") if k in p]
    out = {k: p.get(k) for k in keys if isinstance(p.get(k), str)}
    if atype == "trello_card" and isinstance((p.get("checklist") or {}).get("items"), list):
        out["checklist_items"] = [str(x) for x in p["checklist"]["items"]]
    return out


def apply_fields(p: dict, fields: dict, allowed: dict) -> dict:
    new = copy.deepcopy(p)
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "checklist_items":
            if isinstance(v, list) and all(isinstance(x, str) for x in v) and new.get("checklist"):
                new["checklist"]["items"] = [x[:500] for x in v][:50]
        elif isinstance(v, str) and v.strip():
            new[k] = v[:20000]
    return new


def diff_text(atype: str, old: dict, new: dict) -> str:
    def flat(p):
        out = []
        for k, v in text_fields(atype, p).items():
            vals = v if isinstance(v, list) else str(v).splitlines() or [""]
            out += [f"{k}: {x}" for x in vals]
        for k in ("due", "time"):
            if p.get(k):
                out.append(f"{k}: {p[k]}")
        return out
    return "\n".join(difflib.unified_diff(flat(old), flat(new), "proposed", "edited", lineterm=""))


def _cut_last(p: dict, atype: str) -> dict | None:
    key = next((k for k in ("body", "text", "desc") if isinstance(p.get(k), str) and p[k].strip()), None)
    if not key:
        return None
    import re
    paras = [x for x in re.split(r"\n\s*\n", p[key].strip()) if x.strip()]
    if len(paras) < 2:
        return None
    new = copy.deepcopy(p)
    new[key] = "\n\n".join(paras[:-1])
    return new


def _tomorrow(p: dict, atype: str) -> tuple[dict | None, bool]:
    """(новый payload, отложить карточку). Есть срок: двигаем срок, иначе карточка вернётся утром."""
    tmr = tu.logical_today() + timedelta(days=1)
    if atype == "task_add":
        return {**p, "due": tmr.isoformat()}, False
    if p.get("due"):
        dt = _ts(p["due"])
        if dt:
            loc = tu.local(dt)
            return {**p, "due": tu.at_local(tmr, loc.time().replace(tzinfo=None)).isoformat()}, False
        return {**p, "due": tmr.isoformat()}, False
    return copy.deepcopy(p), True


def rewrite(ctx: Ctx, aid: int, how: str, free_text: str | None = None,
            old_msg_id: int | None = None) -> str:
    conn = ctx.conn
    row = fetch(conn, aid)
    if not row or row["status"] != "proposed":
        return f"Уже решено: {row['status']}" if row else "Действие не найдено"
    old = row["payload"] or {}
    snooze = False
    if how == "tomorrow":
        new, snooze = _tomorrow(old, row["type"])
    elif how == "cut":
        new = _cut_last(old, row["type"])
        if new is None:
            return "Абзац один, убирать нечего"
    else:
        instr = FIX_INSTR.get(how) or (free_text or "").strip()
        fields = text_fields(row["type"], old)
        if not instr or not fields:
            return "Нечего переписывать"
        ctx.action(ctx.thread_of("decisions"))
        try:
            got = llm.complete_json("rewrite", REWRITE_SYSTEM,
                                    f"Инструкция: {instr}\n\nПоля:\n{json.dumps(fields, ensure_ascii=False)}")
        except Exception as e:
            log.warning("rewrite: %s", e)
            return "Модель не ответила, попробуй ещё раз"
        if not isinstance(got, dict):
            return "Модель вернула не то"
        new = apply_fields(old, got, fields)
    if new == old and not snooze:
        return "Ничего не поменялось"
    created = tu.at_local(tu.logical_today() + timedelta(days=1), tu.parse_hm(config.WAKE)) if snooze else None
    with ctx.lock:
        with conn.transaction():
            done = conn.execute("""UPDATE work.actions SET status = 'edited', decided_at = now()
                                   WHERE id = %s AND status = 'proposed' RETURNING id""", (aid,)).fetchone()
            if not done:
                return "Уже решено"
            new_id = conn.execute(
                """INSERT INTO work.actions (context_id, type, payload, rationale, source_event, status,
                                             autonomy, replaces, created_at)
                   VALUES (%s, %s, %s, %s, %s, 'proposed', 'approval', %s, COALESCE(%s, now()))
                   RETURNING id""",
                (row["context_id"], row["type"], db.jb(new), row["rationale"], row["source_event"], aid,
                 created)).fetchone()["id"]
            if not snooze:
                outcome(conn, row, "edited", diff_text(row["type"], old, new))
        ctx.state.pop("fix", None)
        if old_msg_id:
            text, _ = card_text(conn, row)
            note = ("⏰ Отложил: карточка вернётся завтра утром." if snooze
                    else "✏️ Поправлено, новая карточка ниже.")
            ctx.edit(old_msg_id, text + "\n\n" + note, [])
        if not snooze:
            post_card(ctx, new_id, direct=True)
    return "Отложил до утра" if snooze else "Поправил"


def on_fix_text(ctx: Ctx, msg: dict, allow_pending: bool = True) -> bool:
    """Свободный текст правки: ответ на карточку или (в «Решениях») первое сообщение после «Поправить»."""
    conn = ctx.conn
    text = (msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        return False
    aid, old_msg = None, None
    rt = (msg.get("reply_to_message") or {}).get("message_id")
    if rt:
        r = conn.execute("""SELECT ref_action FROM work.outbox WHERE tg_message_id = %s AND ref_action IS NOT NULL
                              AND created_by = current_user
                            ORDER BY id DESC LIMIT 1""", (rt,)).fetchone()
        if r:
            aid, old_msg = r["ref_action"], rt
    if aid is None and allow_pending:
        st = ctx.state.get("fix")
        if st and (tu.now() - st["at"]).total_seconds() < FIX_TTL:
            aid, old_msg = st["aid"], st["msg_id"]
    if aid is None:
        return False
    row = fetch(conn, aid)
    if not row or row["status"] != "proposed" or row["type"] == "plan_batch":
        return False
    res = rewrite(ctx, aid, "free", free_text=text, old_msg_id=old_msg)
    if res != "Поправил":
        ctx.reply(msg, esc(res))
    return True


# ---------- рубильник ----------
def manual(ctx: Ctx) -> str:
    """Всё на подтверждение и запереть, плюс общий рубильник ('*', '*'): он держит и типы,
    у которых строки ещё нет, и не даёт разбору статистики двигать режимы."""
    conn = ctx.conn
    with ctx.lock, conn.transaction():
        n = conn.execute("""UPDATE work.autonomy SET mode = 'approval', locked = true, changed_at = now(),
                                   reason = '/manual'
                            WHERE NOT (action_type = '*' AND context_id = '*')
                            RETURNING action_type""").fetchall()
        conn.execute("""INSERT INTO work.autonomy (action_type, context_id, mode, locked, changed_at, reason)
                        VALUES ('*', '*', 'approval', true, now(), '/manual')
                        ON CONFLICT (action_type, context_id)
                        DO UPDATE SET mode = 'approval', locked = true, changed_at = now(), reason = '/manual'""")
    return (f"Ручной режим: {len(n)} {_types_word(len(n))} на подтверждении и заперты, общий рубильник "
            "включён. Автономные действия, которые ещё ждут, тоже придут карточками. Снять: /auto.")


def auto(ctx: Ctx) -> str:
    conn = ctx.conn
    with ctx.lock, conn.transaction():
        n = conn.execute("""UPDATE work.autonomy SET locked = false, changed_at = now(), reason = '/auto'
                            WHERE locked RETURNING action_type, context_id""").fetchall()
    k = sum(1 for r in n if r["action_type"] != "*")
    return (f"Разблокировал {k} {_types_word(k)} и общий рубильник. В автономию их вернёт разбор статистики "
            "(не меньше 20 решений и accept rate выше 0,85), и только план и задачи: внешние типы "
            "без подтверждения не бывают никогда.")


def _types_word(n: int) -> str:
    from bot.topics import plural
    return plural(n, "тип", "типа", "типов")

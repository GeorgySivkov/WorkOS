"""Триаж входящих (шаг 4.2): нужен ли ответ, срочность, дедлайн, тред.

Высокочастотная и простая задача, поэтому дешёвая модель (config.MODELS['triage'])
и пачки до BATCH штук на вызов. Результат кладётся в events.meta.triage, по нему
работают драфтер и бриф. Альфа (почта EAS и А-Чат) идёт на общих основаниях
(шаг 7.1), вложения не тянем никогда: в промпт попадает только текст тела.

Какие поля meta читаются (контракт с коллекторами, всё необязательное):
  email:   from, to, cc, subject, message_id (или external_id), references, thread_id,
           account, labels (SENT = своё), attachments (только счёт), from_me
  message: channel, channel_name, channel_type (im|mpim|channel), ts (или external_id
           «канал:ts»), thread_ts, is_reply, user, is_dm, mention / mentions_me, from_me
Своё («от меня») определяется по meta.from_me/is_me/outgoing, адресам из
WORKOS_SELF_EMAILS, Slack id из WORKOS_SELF_HANDLES или WORKOS_ME_PERSON_ID.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import getaddresses, parseaddr

from core import config, db, llm, timeutil as tu

BATCH = 20
BODY_CHARS = 1500
URGENCY = {"low", "normal", "high"}

SYSTEM = (
    "Ты размечаешь входящие письма и сообщения Slack для George, продакт-лида. "
    "Для каждого элемента реши: нужен ли от George ответ (needs_reply), срочность "
    "(urgency: low|normal|high), есть ли дедлайн (deadline: YYYY-MM-DD или null, относительные "
    "даты считай от даты элемента), к какому треду или теме относится (thread: коротко или null) "
    "и summary: одна фраза по-русски, что от него хотят. Рассылки, уведомления сервисов, "
    "автоответы и сообщения, где George просто в копии без вопроса к нему, не требуют ответа. "
    "Длинные тире не используй. "
    'Формат: {"items": [{"id": 0, "needs_reply": true, "urgency": "normal", '
    '"deadline": null, "thread": "...", "summary": "..."}]}, по одному объекту на каждый id.'
)


# ---------- кто такой «я» ----------

def _env_set(name: str) -> set[str]:
    return {x.strip().lower() for x in (config.env(name, "") or "").split(",") if x.strip()}


def me_emails() -> set[str]:
    # одно имя на всю систему: WORKOS_SELF_EMAILS (WORKOS_ME_EMAILS понимается для совместимости)
    return _env_set("WORKOS_SELF_EMAILS") | _env_set("WORKOS_ME_EMAILS")


def me_slack_ids() -> set[str]:
    return _env_set("WORKOS_SELF_HANDLES") | _env_set("WORKOS_ME_SLACK_IDS")


def addr(x) -> tuple[str, str]:
    """(имя, email) из строки «Имя <a@b>», словаря {name, email} или голого адреса."""
    if isinstance(x, dict):
        return (x.get("name") or "").strip(), (x.get("email") or x.get("address") or "").strip().lower()
    name, email = parseaddr(str(x or ""))
    return name.strip(), email.strip().lower()


def addr_list(x) -> list[tuple[str, str]]:
    if not x:
        return []
    if isinstance(x, str):
        return [(n.strip(), e.strip().lower()) for n, e in getaddresses([x]) if e]
    out = []
    for item in x:
        n, e = addr(item)
        if e:
            out.append((n, e))
    return out


def sender(ev: dict) -> tuple[str, str]:
    meta = ev.get("meta") or {}
    return addr(meta.get("from") or meta.get("sender") or ev.get("actor_raw"))


def is_mine(ev: dict) -> bool:
    meta = ev.get("meta") or {}
    if any(meta.get(k) is True for k in ("from_me", "is_me", "outgoing", "mine")):
        return True
    if "SENT" in (meta.get("labels") or []):           # Gmail: своё отправленное
        return True
    me_pid = config.env("WORKOS_ME_PERSON_ID")
    if me_pid and str(ev.get("actor_person_id") or "") == me_pid:
        return True
    if ev.get("kind") == "email":
        return sender(ev)[1] in me_emails()
    uid = str(meta.get("user") or meta.get("user_id") or "").lower()
    return bool(uid) and uid in me_slack_ids()


def is_addressed(ev: dict) -> bool | None:
    """Slack: адресовано ли сообщение George. None, если коллектор этого не размечает."""
    meta = ev.get("meta") or {}
    flags = [meta.get(k) for k in ("mention", "mentions_me", "is_dm") if k in meta]
    if meta.get("channel_type") in ("im", "mpim") or str(meta.get("channel") or "").startswith("D"):
        return True                                     # личка (id каналов лички в Slack начинаются с D)
    if any(f is True for f in flags):
        return True
    body = ev.get("body") or ""
    if any(f"<@{sid.upper()}>" in body for sid in me_slack_ids()):
        return True
    if meta.get("is_reply"):                            # ответ в треде: пусть решает модель
        return None
    if flags or "channel_type" in meta:
        return False
    return None


# ---------- разбор ответа ----------

def _date(s) -> str | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        return None


def clean(item: dict) -> dict:
    urg = str(item.get("urgency") or "normal").lower()
    return {
        "needs_reply": bool(item.get("needs_reply")),
        "urgency": urg if urg in URGENCY else "normal",
        "deadline": _date(item.get("deadline")),
        "thread": (str(item["thread"])[:200] if item.get("thread") else None),
        "summary": re.sub(r"\s*[—–]\s*", ", ", str(item.get("summary") or ""))[:300],
    }


def _render(i: int, ev: dict) -> str:
    meta = ev.get("meta") or {}
    when = tu.local(ev["occurred_at"]).strftime("%Y-%m-%d %H:%M")
    head = [f"### id={i} · {ev['kind']} · {ev['context_id']} · {when}"]
    if ev["kind"] == "email":
        n, e = sender(ev)
        head.append(f"От: {n} <{e}>" if e else f"От: {ev.get('actor_raw') or '?'}")
        to = ", ".join(e for _, e in addr_list(meta.get("to")))
        cc = ", ".join(e for _, e in addr_list(meta.get("cc")))
        if to:
            head.append(f"Кому: {to}")
        if cc:
            head.append(f"Копия: {cc}")
        head.append(f"Тема: {ev.get('title') or meta.get('subject') or ''}")
        att = meta.get("attachments")
        if att:
            head.append(f"Вложений: {len(att) if isinstance(att, list) else att} (не открываем)")
    else:
        ch = meta.get("channel_name") or meta.get("channel") or ""
        head.append(f"Канал: {ch}; автор: {ev.get('actor_raw') or meta.get('user') or '?'}")
    body = (ev.get("body") or "").strip()
    head.append(body[:BODY_CHARS] + (" [...]" if len(body) > BODY_CHARS else ""))
    return "\n".join(head)


def triage_batch(events: list[dict]) -> dict[int, dict]:
    """Один вызов модели на пачку. Возвращает {event_id: triage}."""
    user = (f"Сегодня {tu.logical_today().isoformat()}. Элементов: {len(events)}.\n\n"
            + "\n\n".join(_render(i, ev) for i, ev in enumerate(events)))
    data = llm.complete_json("triage", SYSTEM, user)
    items = data.get("items", []) if isinstance(data, dict) else data
    out: dict[int, dict] = {}
    for pos, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        idx = item.get("id", pos)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(events):
            out[events[idx]["id"]] = clean(item)
    return out


SELECT = """
SELECT e.id, e.context_id, e.source_id, e.kind, e.occurred_at, e.title, e.body, e.meta,
       e.actor_raw, e.actor_person_id, e.thread_key
FROM work.events e
WHERE e.kind IN ('email', 'message')
  AND NOT (e.meta ? 'triage')
  AND e.source_id NOT LIKE 'telegram%%'
ORDER BY e.occurred_at
LIMIT %s
"""


def _mark(conn, event_id: int, tri: dict) -> None:
    conn.execute("UPDATE work.events SET meta = meta || jsonb_build_object('triage', %s::jsonb) WHERE id = %s",
                 (db.jb(tri), event_id))


def run(date: str | None = None, dry_run: bool = False, limit: int = 200, **kw) -> dict:
    stamp = tu.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = conn.execute(SELECT, (limit,)).fetchall()
    skipped, todo = [], []
    for ev in rows:
        if is_mine(ev):
            skipped.append((ev["id"], "mine"))
        elif ev["kind"] == "message" and is_addressed(ev) is False:
            skipped.append((ev["id"], "not_addressed"))
        else:
            todo.append(ev)
    results: dict[int, dict] = {}
    errors = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            results.update(triage_batch(chunk))
        except llm.LLMError:
            errors += 1
    if not dry_run:
        with db.connect() as conn, conn.transaction():
            for eid, why in skipped:
                _mark(conn, eid, {"needs_reply": False, "urgency": "low", "deadline": None,
                                  "thread": None, "summary": "", "skipped": why, "at": stamp})
            for eid, tri in results.items():
                _mark(conn, eid, {**tri, "at": stamp})
    return {"seen": len(rows), "triaged": len(results), "skipped": len(skipped),
            "needs_reply": sum(1 for t in results.values() if t["needs_reply"]),
            "batches": (len(todo) + BATCH - 1) // BATCH, "errors": errors, "dry_run": dry_run}


if __name__ == "__main__":
    print(run())

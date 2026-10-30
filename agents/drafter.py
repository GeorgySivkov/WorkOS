"""Черновики ответов и карточек (шаги 5.2, 5.3, 6.3).

1. По каждому входящему, которое триаж пометил needs_reply, пишет ответ от имени
   George (задача 'drafter', фронтир) и предлагает email_reply (письмо) или
   slack_post (упоминание или личка в Slack) через autonomy.propose. В промпт идут
   корпус стиля style/<context>.md и последние три сообщения треда целиком.
2. По моим задачам без срока со встреч Инфатики (events.meta.extract.tasks)
   предлагает trello_card в формате George: английское название, Context +
   Acceptance criteria (Definition of Done) в описании, лейблы, чеклист. Только
   если задан TRELLO_PROPOSE_LIST_ID (список, куда класть, обычно Backlog).

Отправляет не драфтер: он только предлагает. Письмо исполнитель кладёт черновиком.
"""
from __future__ import annotations

import re
from datetime import date as _date

from agents import autonomy
from core import config, db, llm, timeutil as tu
from extract.triage import addr_list, is_mine, me_emails, sender

STYLE_DIR = config.ROOT / "style"
LIMIT = 10
MAX_ATTEMPTS = 3
THREAD_N = 3
MSG_CHARS = 8000
RECENT_DAYS = 7

HARD_RULES = """Жёсткие правила George для любого текста от его имени:
- Никаких длинных тире (ни em, ни en). Нужен дефис в диапазоне, ставь обычный «-», лучше переформулируй
  через запятую, двоеточие или точку.
- Разговорный тон, как человек реально печатает: просто, прямо, коротко.
- Самоирония допустима, канцелярит и «надеюсь, это письмо застанет вас в добром здравии» нет.
- Минимум форматирования: без заголовков, жирного и списков, если их не просят.
- Отвечай на языке треда."""

# Лейблы доски Proxy/SDK (скилл trello-card): приоритет + тип + компонент
DEFAULT_LABELS = ["P0-blocker", "P1-high", "P2-normal", "P3-low", "task", "bug", "research", "ops",
                  "sdk", "proxy-server", "proxy-partner", "statistic", "docs", "monitoring-alerting"]


def nodash(s: str) -> str:
    s = re.sub(r"(\d)\s*[—–]\s*(\d)", r"\1-\2", str(s or ""))
    s = re.sub(r"\s+[—–]\s+", ", ", s)
    return s.replace("—", ", ").replace("–", "-")


def load_style(context_id: str | None) -> tuple[str, str | None]:
    """(текст корпуса, имя файла). Нет файла для контекста: только жёсткие правила."""
    p = STYLE_DIR / f"{context_id}.md"
    if context_id and p.exists():
        return p.read_text(encoding="utf-8"), p.name
    return HARD_RULES, None


# ---------- ответы ----------

CANDIDATES = f"""
SELECT e.id, e.context_id, e.source_id, e.external_id, e.kind, e.occurred_at, e.title, e.body, e.meta,
       e.actor_raw, e.actor_person_id, e.thread_key
FROM work.events e
WHERE e.kind IN ('email', 'message')
  AND e.meta->'triage'->>'needs_reply' = 'true'
  AND e.occurred_at > now() - interval '{RECENT_DAYS} days'
  AND (NOT (e.meta ? 'draft')
       OR (e.meta->'draft' ? 'error' AND COALESCE((e.meta->'draft'->>'attempts')::int, 0) < {MAX_ATTEMPTS}))
  AND NOT EXISTS (SELECT 1 FROM work.actions a
                  WHERE a.source_event = e.id AND a.type IN ('email_reply', 'slack_post'))
ORDER BY CASE e.meta->'triage'->>'urgency' WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, e.occurred_at
LIMIT %s
"""


def thread_messages(conn, ev: dict, n: int = THREAD_N) -> list[dict]:
    if not ev.get("thread_key"):
        return [ev]
    rows = conn.execute(
        "SELECT id, kind, occurred_at, title, body, meta, actor_raw, actor_person_id FROM work.events "
        "WHERE thread_key = %s AND context_id = %s AND kind = %s AND occurred_at <= %s "
        "ORDER BY occurred_at DESC, id DESC LIMIT %s",
        (ev["thread_key"], ev["context_id"], ev["kind"], ev["occurred_at"], n)).fetchall()
    return list(reversed(rows)) or [ev]


def answered_later(conn, ev: dict) -> bool:
    """George уже ответил в треде после этого сообщения: черновик не нужен."""
    if not ev.get("thread_key"):
        return False
    later = conn.execute(
        "SELECT id, kind, meta, actor_raw, actor_person_id FROM work.events WHERE thread_key = %s "
        "AND context_id = %s AND occurred_at > %s ORDER BY occurred_at LIMIT 30",
        (ev["thread_key"], ev["context_id"], ev["occurred_at"])).fetchall()
    return any(is_mine(r) for r in later)


def _who(ev: dict) -> str:
    if ev["kind"] == "email":
        n, e = sender(ev)
        return n or e or ev.get("actor_raw") or "?"
    m = ev.get("meta") or {}
    return ev.get("actor_raw") or m.get("user_name") or m.get("user") or "?"


def _render_thread(msgs: list[dict]) -> str:
    out = []
    for m in msgs:
        when = tu.local(m["occurred_at"]).strftime("%Y-%m-%d %H:%M")
        mine = " (это George)" if is_mine(m) else ""
        head = f"### {_who(m)}{mine}, {when}"
        if m["kind"] == "email" and m.get("title"):
            head += f"\nТема: {m['title']}"
        body = (m.get("body") or "").strip()
        out.append(head + "\n" + body[:MSG_CHARS] + (" [...]" if len(body) > MSG_CHARS else ""))
    return "\n\n".join(out)


def draft_system(context_id: str, kind: str) -> tuple[str, str | None]:
    style, fname = load_style(context_id)
    where = "письмо" if kind == "email" else "сообщение в Slack"
    system = (
        f"Ты пишешь черновик ответа от имени George на {where}. Контекст: {context_id}. "
        "Пиши его голосом по корпусу стиля ниже: правила обязательны, примеры задают тон. "
        "Отвечай по существу последнего сообщения треда. Если для ответа нужны факты, которых нет "
        "в переписке, не выдумывай: оставь короткую заглушку в квадратных скобках, например [дата релиза]. "
        "Без подписи-шаблона и без темы письма, только текст ответа. "
        'Формат: {"body": "текст ответа", "skip": false, "reason": null}; skip=true, только если '
        "отвечать на самом деле не нужно.\n\n"
        f"=== Корпус стиля ===\n{style}\n=== Конец корпуса ===\n\n{HARD_RULES}")
    return system, fname


def _subject(ev: dict) -> str:
    s = (ev.get("title") or (ev.get("meta") or {}).get("subject") or "").strip()
    return s if re.match(r"^(re|aw|ответ|rv)\s*:", s, re.I) else f"Re: {s}".strip()


def email_payload(ev: dict, body: str) -> dict | None:
    meta = ev.get("meta") or {}
    me = me_emails()
    reply_to = addr_list(meta.get("reply_to"))
    s_name, s_email = (reply_to[0] if reply_to else sender(ev))
    if not s_email:
        return None
    seen = {s_email} | me
    cc = []
    for _, e in addr_list(meta.get("to")) + addr_list(meta.get("cc")):
        if e not in seen:
            cc.append(e)
            seen.add(e)
    msg_id = meta.get("message_id") or (ev["external_id"] if "@" in str(ev.get("external_id")) else None)
    refs = meta.get("references") or []
    refs = refs.split() if isinstance(refs, str) else list(refs)
    if msg_id and msg_id not in refs:
        refs.append(msg_id)
    p = {"account": meta.get("account") or ev["source_id"], "to": [s_email], "cc": cc,
         "subject": _subject(ev), "body": body}
    thread_id = meta.get("thread_id") or ev.get("thread_key")
    if thread_id:
        p["thread_id"] = str(thread_id)
    if msg_id:
        p["in_reply_to"] = msg_id
    if refs:
        p["references"] = " ".join(refs)
    return p


def slack_payload(ev: dict, body: str) -> dict | None:
    meta = ev.get("meta") or {}
    channel = meta.get("channel") or meta.get("channel_id")
    if not channel:
        return None
    p = {"channel": str(channel), "text": body}
    ext = str(ev.get("external_id") or "")
    own_ts = meta.get("ts") or (ext.rpartition(":")[2] if ":" in ext else None)   # external_id «канал:ts»
    is_dm = meta.get("channel_type") == "im" or str(channel).startswith("D")
    ts = meta.get("thread_ts") or (None if is_dm else own_ts)
    if ts:
        p["thread_ts"] = str(ts)
    return p


def _rationale(ev: dict) -> str:
    tri = (ev.get("meta") or {}).get("triage") or {}
    src = "Письмо" if ev["kind"] == "email" else "Slack"
    parts = [f"{src} от {_who(ev)}: {tri.get('summary') or ev.get('title') or 'нужен ответ'}"]
    if tri.get("urgency") == "high":
        parts.append("срочно")
    if tri.get("deadline"):
        try:
            parts.append(f"дедлайн {tu.ru_date(_date.fromisoformat(tri['deadline']))}")
        except ValueError:
            pass
    return nodash(" · ".join(parts))


def _mark(conn, event_id: int, info: dict) -> None:
    conn.execute("UPDATE work.events SET meta = meta || jsonb_build_object('draft', %s::jsonb) WHERE id = %s",
                 (db.jb(info), event_id))


def draft_replies(conn, limit: int = LIMIT, dry_run: bool = False) -> dict:
    stamp = tu.now().isoformat(timespec="seconds")
    res = {"candidates": 0, "proposed": 0, "skipped": 0, "errors": 0, "styles": []}
    rows = conn.execute(CANDIDATES, (limit,)).fetchall()
    res["candidates"] = len(rows)
    for ev in rows:
        if answered_later(conn, ev):
            if not dry_run:
                _mark(conn, ev["id"], {"skipped": "answered", "at": stamp})
            res["skipped"] += 1
            continue
        if ev["kind"] == "email":
            atype = "email_reply"
        elif str(ev["source_id"]).startswith("slack"):
            atype = "slack_post"
        else:                      # А-Чат и прочие мессенджеры: исполнителя нет, не тратим модель
            if not dry_run:
                _mark(conn, ev["id"], {"skipped": "no_executor", "at": stamp})
            res["skipped"] += 1
            continue
        system, fname = draft_system(ev["context_id"], ev["kind"])
        if fname and fname not in res["styles"]:
            res["styles"].append(fname)
        tri = (ev.get("meta") or {}).get("triage") or {}
        user = (f"Тред (последние {THREAD_N} сообщения целиком, старые сверху):\n\n"
                f"{_render_thread(thread_messages(conn, ev))}\n\n"
                f"Отвечаем на сообщение от {_who(ev)}. Триаж: {tri.get('summary') or ''}; "
                f"срочность {tri.get('urgency') or 'normal'}; дедлайн {tri.get('deadline') or 'нет'}.")
        attempts = int(((ev.get("meta") or {}).get("draft") or {}).get("attempts") or 0)
        try:
            out = llm.complete_json("drafter", system, user)
            if not isinstance(out, dict):
                raise llm.LLMError("ответ не объект")
        except llm.LLMError as e:
            res["errors"] += 1
            if not dry_run:
                _mark(conn, ev["id"], {"error": str(e)[:200], "attempts": attempts + 1, "at": stamp})
            continue
        body = nodash((out.get("body") or "").strip())
        if out.get("skip") or not body:
            if not dry_run:
                _mark(conn, ev["id"], {"skipped": "model", "reason": out.get("reason"), "at": stamp})
            res["skipped"] += 1
            continue
        payload = email_payload(ev, body) if atype == "email_reply" else slack_payload(ev, body)
        if payload is None:
            if not dry_run:
                _mark(conn, ev["id"], {"skipped": "no_address", "at": stamp})
            res["skipped"] += 1
            continue
        if dry_run:
            res["proposed"] += 1
            continue
        with conn.transaction():
            aid = autonomy.propose(conn, atype, ev["context_id"], payload, _rationale(ev), ev["id"])
            _mark(conn, ev["id"], {"action_id": aid, "type": atype, "style": fname, "at": stamp})
        res["proposed"] += 1
    return res


# ---------- карточки Trello ----------

TRELLO_SYSTEM = (
    "You turn George's own action items from a work meeting into Trello cards in his format. "
    "For each task, in the same order, return a card: "
    "name: English, verb + object + key identifier, no hashtags; "
    "desc: Markdown with strict blocks in this order: a first line '**Роли.** Owner: George Sivkov' "
    "(add ' · Reviewer: <name>' only if the reviewer is clear from the meeting), then '**Context**' "
    "(why and where it came from: the meeting, the decision it serves), '**Problem**' (what exactly to do), "
    "'**Inputs**' (concrete facts, numbers, names from the meeting), and '**Acceptance criteria**' "
    "(numbered, checkable Definition of Done). Dense, no filler, do not invent facts. "
    "labels: at least 3 from the allowed list: one priority, exactly one type, at least one component. "
    "checklist: {name: 'Чеклист', items: [full meaningful steps, verb + object + specifics]} when the task has "
    "more than one step, else null. Never use em or en dashes, use a plain hyphen or rephrase. "
    'Format: {"cards": [{"name": "", "desc": "", "labels": [], "checklist": {"name": "Чеклист", "items": []}}]}'
)


def _labels_allowed() -> list[str]:
    raw = config.env("TRELLO_PROPOSE_LABELS")
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else DEFAULT_LABELS


def card_payload(card: dict, list_id: str, allowed: list[str]) -> dict | None:
    name = nodash((card.get("name") or "").strip())
    desc = nodash((card.get("desc") or "").strip())
    if not name or not desc:
        return None
    ok = {a.lower(): a for a in allowed}
    labels = []
    for lb in card.get("labels") or []:
        a = ok.get(str(lb).strip().lower())
        if a and a not in labels:
            labels.append(a)
    p = {"list_id": list_id, "name": name[:200], "desc": desc, "labels": labels}
    ch = card.get("checklist")
    if isinstance(ch, dict):
        items = [nodash(str(i).strip()) for i in ch.get("items") or [] if str(i).strip()]
        if items:
            p["checklist"] = {"name": ch.get("name") or "Чеклист", "items": items}
    return p


TRELLO_CANDIDATES = """
SELECT e.id, e.context_id, e.title, e.occurred_at, e.meta
FROM work.events e
WHERE e.kind = 'meeting' AND e.context_id = 'infatica'
  AND jsonb_array_length(COALESCE(e.meta->'extract'->'tasks', '[]'::jsonb)) > 0
  AND e.occurred_at > now() - interval '14 days'
  AND NOT EXISTS (SELECT 1 FROM work.notices n WHERE n.key = 'trello:' || e.id)
ORDER BY e.occurred_at
LIMIT %s
"""


def propose_trello(conn, limit: int = 5, dry_run: bool = False) -> dict:
    list_id = config.env("TRELLO_PROPOSE_LIST_ID")
    res = {"meetings": 0, "cards": 0, "errors": 0}
    if not list_id:
        return {**res, "disabled": "TRELLO_PROPOSE_LIST_ID не задан"}
    allowed = _labels_allowed()
    for ev in conn.execute(TRELLO_CANDIDATES, (limit,)).fetchall():
        res["meetings"] += 1
        tasks = ev["meta"]["extract"]["tasks"]
        decisions = conn.execute("SELECT statement, rationale FROM work.decisions WHERE source_event = %s "
                                 "ORDER BY id", (ev["id"],)).fetchall()
        d = tu.local(ev["occurred_at"]).date()
        user = "\n".join(
            [f"Meeting: «{ev['title'] or 'untitled'}», {d.isoformat()}, context Infatica.",
             "Decisions made:"] + [f"- {x['statement']}" + (f" (why: {x['rationale']})" if x["rationale"] else "")
                                   for x in decisions] +
            ["", f"Allowed labels: {', '.join(allowed)}", "", "George's tasks (one card each):"] +
            [f"{i + 1}. {t['title']}" for i, t in enumerate(tasks)])
        try:
            out = llm.complete_json("drafter", TRELLO_SYSTEM, user)
            cards = out.get("cards", []) if isinstance(out, dict) else out
        except llm.LLMError:
            res["errors"] += 1
            continue
        if dry_run:
            res["cards"] += len(cards or [])
            continue
        with conn.transaction():
            for task, card in zip(tasks, cards or []):
                p = card_payload(card, list_id, allowed) if isinstance(card, dict) else None
                if not p:
                    continue
                why = nodash(f"Моя задача со встречи «{ev['title'] or 'без названия'}» {tu.ru_date(d)}: "
                             f"{task['title']}")
                autonomy.propose(conn, "trello_card", ev["context_id"], p, why, ev["id"])
                res["cards"] += 1
            conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING",
                         (f"trello:{ev['id']}",))
    return res


def run(date: str | None = None, dry_run: bool = False, limit: int = LIMIT, **kw) -> dict:
    with db.connect(autocommit=True) as conn:
        replies = draft_replies(conn, limit, dry_run)
        cards = propose_trello(conn, dry_run=dry_run)
    return {"replies": replies, "trello": cards, "dry_run": dry_run}


if __name__ == "__main__":
    print(run())

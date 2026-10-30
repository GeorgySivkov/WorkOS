"""Встреча -> решения, задачи, обещания (шаг 4.1).

Каждая новая запись Plaud (events.kind='meeting' с транскриптом в body) один раз
проходит через модель (задача 'extract'). В промпт обязательно подаётся список
участников: meta.participant_ids -> люди плюс приглашённые из календарного события
(meta.calendar_event_id). С готовым списком модель раскладывает реплики точнее.

Имя из транскрипта разрешается ТОЛЬКО среди участников этой встречи (правило 3.2):
глобального поиска по именам нет. Не разрешилось или кандидатов двое, значит
person_id NULL, а имя остаётся в тексте обещания.

Мои обещания с датой сразу предлагаются задачей (task_add) через autonomy.propose.
Мои задачи без даты остаются в events.meta.extract.tasks: драфтер предложит по
ним карточку Trello (Инфатика). Обработанная встреча отмечается в work.notices
ключом 'extract:<event_id>'.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from agents import autonomy
from core import config, db, llm, timeutil as tu
from extract.triage import addr, me_emails

LIMIT = 10
HEAD_CHARS, TAIL_CHARS = 30000, 15000      # аргумент claude -p ограничен ~128 КБ
ME_WORDS = {"me", "я", "george", "георгий", "жора", "гоша", "мне", "мой", "моё"}
SIZES = {"S", "M", "L"}

SYSTEM = (
    "Ты разбираешь транскрипт рабочей встречи George (продакт-лид) и достаёшь три сущности. "
    "1) decisions: принятые на встрече решения, statement коротко и однозначно, rationale: почему так решили "
    "(если прозвучало, иначе null). "
    "2) tasks: конкретные задачи, owner: 'me' если делает George, иначе имя исполнителя; due: YYYY-MM-DD или null; "
    "size: S (до 30 минут), M (около часа) или L (2 часа и больше). "
    "3) commitments: обещания в обе стороны. direction 'mine': George что-то пообещал, person: кому; "
    "direction 'theirs': George или команде пообещали, person: кто обещал. what: что именно, due: YYYY-MM-DD или null, "
    "size для mine как у задач, summary для mine: короткая фраза от первого лица в прошедшем времени, "
    "например «Обещал Яну методологию до пятницы». "
    "Относительные даты («до пятницы», «завтра») считай от даты встречи. Имена бери из списка участников, "
    "как они там написаны; если человека нет в списке, пиши имя как в транскрипте. "
    "Только то, что явно прозвучало, ничего не выдумывай. Длинные тире не используй. "
    'Формат: {"decisions": [{"statement": "", "rationale": null}], '
    '"tasks": [{"title": "", "owner": "me", "due": null, "size": "S"}], '
    '"commitments": [{"direction": "mine", "person": "", "what": "", "due": null, "size": "S", "summary": ""}]}'
)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(s or "").lower().replace("ё", "е"))).strip()


def _nodash(s) -> str:
    return re.sub(r"\s*[—–]\s*", ", ", str(s or "")).strip()


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


# ---------- участники ----------

def calendar_event(conn, ref) -> dict | None:
    if ref in (None, ""):
        return None
    s = str(ref)
    row = None
    if s.isdigit():
        row = conn.execute("SELECT id, title, occurred_at, meta, context_id FROM work.events "
                           "WHERE id = %s AND kind = 'calendar'", (int(s),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, title, occurred_at, meta, context_id FROM work.events "
                           "WHERE kind = 'calendar' AND (external_id = %s OR meta->>'uid' = %s) "
                           "ORDER BY occurred_at DESC LIMIT 1", (s, s)).fetchone()
    return row


def people_by_ids(conn, pids: list[int], context_id: str | None) -> dict[int, dict]:
    if not pids:
        return {}
    out = {r["id"]: {"person_id": r["id"], "name": r["display_name"], "aliases": [], "email": None}
           for r in conn.execute("SELECT id, display_name FROM work.people WHERE id = ANY(%s)", (pids,))}
    for r in conn.execute(
            "SELECT person_id, kind, value FROM work.identities WHERE person_id = ANY(%s) "
            "AND kind IN ('display_name', 'email') AND (context_id IS NULL OR context_id = %s)",
            (pids, context_id)):
        p = out.get(r["person_id"])
        if not p:
            continue
        if r["kind"] == "email":
            p["email"] = p["email"] or r["value"]
        elif _norm(r["value"]) != _norm(p["name"]):
            p["aliases"].append(r["value"])
    return out


def attendees_to_people(conn, attendees, context_id: str | None) -> list[dict]:
    """Приглашённые календаря -> участники. Почта однозначна (confidence 1.0), имя не угадываем."""
    me = me_emails()
    me_pid = config.env("WORKOS_ME_PERSON_ID")
    out = []
    for a in attendees or []:
        name, email = addr(a)
        if not email or email in me:
            continue
        r = conn.execute("SELECT person_id FROM work.identities WHERE kind = 'email' AND lower(value) = %s "
                         "AND (context_id IS NULL OR context_id = %s) ORDER BY context_id NULLS LAST LIMIT 1",
                         (email, context_id)).fetchone()
        pid = r["person_id"] if r else None
        if pid and me_pid and str(pid) == me_pid:
            continue
        out.append({"person_id": pid, "name": name or email, "aliases": [], "email": email})
    return out


def participants(conn, ev: dict) -> list[dict]:
    meta = ev.get("meta") or {}
    me_pid = config.env("WORKOS_ME_PERSON_ID")
    pids = [int(x) for x in (meta.get("participant_ids") or []) if str(x).isdigit() and str(x) != me_pid]
    found = people_by_ids(conn, pids, ev.get("context_id"))
    cal = calendar_event(conn, meta.get("calendar_event_id"))
    att = (cal or {}).get("meta", {}).get("attendees") if cal else meta.get("attendees")
    for p in attendees_to_people(conn, att, ev.get("context_id")):
        if p["person_id"] and p["person_id"] in found:
            continue
        if p["person_id"]:
            extra = people_by_ids(conn, [p["person_id"]], ev.get("context_id"))
            found.update(extra or {p["person_id"]: p})
        else:
            found[f"email:{p['email']}"] = p
    return list(found.values())


def resolve_person(name, parts: list[dict]) -> int | None:
    """Имя -> person_id только среди участников. Самая длинная совпавшая форма побеждает,
    ничья (два Саши) значит None: такие случаи разбирает George через identity_candidates."""
    n = _norm(name)
    if not n or n in ME_WORDS:
        return None
    toks = set(n.split())
    best, best_score = set(), 0
    for p in parts:
        if not p.get("person_id"):
            continue
        score = 0
        for form in [p["name"], *p.get("aliases", []), (p.get("email") or "").split("@")[0]]:
            ft = set(_norm(form).split())
            if not ft:
                continue
            if ft == toks:
                score = max(score, 100 + len(ft))
            elif ft <= toks or toks <= ft:
                score = max(score, len(ft & toks))
        if score > best_score:
            best, best_score = {p["person_id"]}, score
        elif score and score == best_score:
            best.add(p["person_id"])
    return next(iter(best)) if len(best) == 1 else None


# ---------- промпт ----------

def _transcript(body: str) -> str:
    body = body.strip()
    if len(body) <= HEAD_CHARS + TAIL_CHARS:
        return body
    return body[:HEAD_CHARS] + "\n\n[... середина обрезана ...]\n\n" + body[-TAIL_CHARS:]


def build_prompt(ev: dict, parts: list[dict]) -> str:
    d = tu.local(ev["occurred_at"]).date()
    lines = [f"Встреча: «{ev.get('title') or 'без названия'}», контекст {ev['context_id']}, "
             f"дата {tu.ru_date(d)} {d.year} ({d.isoformat()}, {tu.RU_WEEKDAYS[d.weekday()]}).",
             "Участники, кроме George:"]
    for p in parts:
        extra = []
        if p.get("aliases"):
            extra.append("также: " + ", ".join(p["aliases"]))
        if p.get("email"):
            extra.append(p["email"])
        lines.append(f"- {p['name']}" + (f" ({'; '.join(extra)})" if extra else ""))
    if not parts:
        lines.append("- (список неизвестен)")
    lines += ["", "Транскрипт:", _transcript(ev.get("body") or "")]
    return "\n".join(lines)


# ---------- запись ----------

def task_day(deadline: date, today: date) -> date:
    """В список за рабочий день до срока: «до пятницы» -> четверг. Прошедшее -> сегодня."""
    d = deadline - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    if d < today:
        d = max(deadline, today)
    return d


def _is_me(owner) -> bool:
    return _norm(owner) in ME_WORDS or not _norm(owner)


def normalize(data: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (decisions, commitments, my_tasks_without_due). Задачи с исполнителем превращаются
    в обещания (мои с датой и чужие), если их нет среди явных обещаний."""
    decisions = [{"statement": _nodash(d.get("statement")), "rationale": _nodash(d.get("rationale")) or None}
                 for d in (data.get("decisions") or []) if isinstance(d, dict) and d.get("statement")]
    commitments = []
    for c in data.get("commitments") or []:
        if not isinstance(c, dict) or not c.get("what"):
            continue
        direction = "theirs" if str(c.get("direction")).lower() == "theirs" else "mine"
        commitments.append({"direction": direction, "person": c.get("person"), "what": _nodash(c["what"]),
                            "due": _date(c.get("due")), "size": str(c.get("size") or "S").upper(),
                            "summary": _nodash(c.get("summary")) or None})
    known = {_norm(c["what"]) for c in commitments}
    my_tasks = []
    for t in data.get("tasks") or []:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        title, due = _nodash(t["title"]), _date(t.get("due"))
        size = str(t.get("size") or "S").upper()
        nt = _norm(title)
        if any(nt and (nt in k or k in nt) for k in known if k):
            continue
        if _is_me(t.get("owner")):
            if due:
                commitments.append({"direction": "mine", "person": None, "what": title, "due": due,
                                    "size": size, "summary": None})
            else:
                my_tasks.append({"title": title, "size": size if size in SIZES else "S"})
        else:
            commitments.append({"direction": "theirs", "person": t.get("owner"), "what": title, "due": due,
                                "size": size, "summary": None})
        known.add(nt)
    for c in commitments:
        c["size"] = c["size"] if c["size"] in SIZES else "S"
    return decisions, commitments, my_tasks


def _rationale(c: dict, person_name: str | None, ev: dict, day: date) -> str:
    base = c.get("summary")
    if not base:
        due = tu.ru_date(c["due"])
        base = (f"{person_name} ждёт: {c['what']}, срок {due}" if person_name
                else f"Обещал на встрече «{ev.get('title') or 'без названия'}»: {c['what']}, срок {due}")
    return f"{base} → в список на {tu.ru_date(day)}"


def save(conn, ev: dict, parts: list[dict], data: dict, today: date) -> dict:
    ctx = ev["context_id"]
    pids = [p["person_id"] for p in parts if p.get("person_id")]
    decisions, commitments, my_tasks = normalize(data)
    if (ev.get("meta") or {}).get("personal"):
        decisions = []          # личная запись: берём только действия, без содержания
    for d in decisions:
        conn.execute("INSERT INTO work.decisions (context_id, decided_at, statement, rationale, participants, "
                     "source_event) VALUES (%s, %s, %s, %s, %s, %s)",
                     (ctx, ev["occurred_at"], d["statement"], d["rationale"], db.jb(pids), ev["id"]))
    proposed = 0
    names = {p["person_id"]: p["name"] for p in parts if p.get("person_id")}
    for c in commitments:
        pid = resolve_person(c["person"], parts) if c["person"] else None
        what = c["what"]
        raw = (c["person"] or "").strip()
        if raw and not pid and not _is_me(raw) and _norm(raw) not in _norm(what):
            what = f"{raw}: {what}"
        cid = conn.execute(
            "INSERT INTO work.commitments (context_id, direction, person_id, what, due, source_event) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (ctx, c["direction"], pid, what, c["due"], ev["id"])).fetchone()["id"]
        if c["direction"] == "mine" and c["due"]:
            day = task_day(c["due"], today)
            title = c["what"][:1].upper() + c["what"][1:]
            payload = {"title": title, "due": day.isoformat(), "size": c["size"], "context_id": ctx,
                       "commitment_id": cid}
            who = names.get(pid) or (raw if raw and not _is_me(raw) else None)
            autonomy.propose(conn, "task_add", ctx, payload, _rationale(c, who, ev, day), ev["id"])
            proposed += 1
    summary = {"at": tu.now().isoformat(timespec="seconds"), "decisions": len(decisions),
               "commitments": len(commitments), "tasks": my_tasks, "proposed": proposed}
    conn.execute("UPDATE work.events SET meta = meta || jsonb_build_object('extract', %s::jsonb) WHERE id = %s",
                 (db.jb(summary), ev["id"]))
    conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING", (f"extract:{ev['id']}",))
    return summary


SELECT = """
SELECT e.id, e.context_id, e.title, e.body, e.meta, e.occurred_at
FROM work.events e
WHERE e.kind = 'meeting' AND COALESCE(btrim(e.body), '') <> ''
  AND NOT EXISTS (SELECT 1 FROM work.notices n WHERE n.key = 'extract:' || e.id)
ORDER BY e.occurred_at
LIMIT %s
"""


def run(date: str | None = None, dry_run: bool = False, limit: int = LIMIT, **kw) -> dict:
    today = _date(date) or tu.logical_today()
    done, errors, totals = [], [], {"decisions": 0, "commitments": 0, "proposed": 0, "tasks": 0}
    with db.connect(autocommit=True) as conn:      # модель не зовём внутри открытой транзакции
        meetings = conn.execute(SELECT, (limit,)).fetchall()
        for ev in meetings:
            parts = participants(conn, ev)
            try:
                data = llm.complete_json("extract", SYSTEM, build_prompt(ev, parts))
            except llm.LLMError as e:
                errors.append({"event": ev["id"], "error": str(e)[:200]})
                continue
            if not isinstance(data, dict):
                errors.append({"event": ev["id"], "error": "ответ не объект"})
                continue
            if dry_run:
                d, c, t = normalize(data)
                done.append({"event": ev["id"], "decisions": len(d), "commitments": len(c), "tasks": len(t)})
                continue
            with conn.transaction():
                s = save(conn, ev, parts, data, today)
            done.append(ev["id"])
            for k in ("decisions", "commitments", "proposed"):
                totals[k] += s[k]
            totals["tasks"] += len(s["tasks"])
    return {"meetings": len(meetings), "processed": len(done), **totals, "errors": errors, "dry_run": dry_run}


if __name__ == "__main__":
    print(run())

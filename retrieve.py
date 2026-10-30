"""Вопрос к памяти (шаг 3.4): разрешение имён, гибридный поиск, ответ со ссылками.

Работает внутри бота (роль workos_bot: SELECT на events, people, identities, person_contexts,
decisions, commitments, contexts). Порядок в теме «Спросить»:

    p = prepare(conn, вопрос)
    "unknown" in p  -> честное «не знаю такого: …»
    "choices" in p  -> кнопки «Ян (Infatica) / Ян (Альфа-Банк)», без попыток угадать
    иначе           -> answer(conn, вопрос, person_ids=p["person_ids"])

Имя из вопроса разрешается в person_id ДО поиска, и id идёт в WHERE, а не в промпт:
чужие встречи физически не попадают в выдачу. Здесь глобальный поиск по именам разрешён,
потому что результат это выбор, который делает George, а не автоматическое слияние.

Поиск: вектор (pgvector <=>, запрос через core.embed kind='query', только строки той же
embedding_model) плюс русский полнотекст, слияние reciprocal rank fusion. Без pgvector
(тестовая база) и без файлов модели остаётся полнотекст. Вектор в боте включается явно
WORKOS_ASK_VECTOR=1 (модель держит в памяти ~400 МБ, на дроплете 1 ГБ это риск OOM);
по умолчанию 0, то есть только полнотекст.

Ручная проверка: python -m retrieve "что мы решили по SDK в июне"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, time, timedelta

from core import config, db, embed, llm, tg, timeutil as tu
from extract.people import NameIndex, norm, self_keys, unique_mentions

log = logging.getLogger("workos.retrieve")

RRF_K = 60          # константа reciprocal rank fusion
LIST_K = 60         # сколько берём из каждого списка до слияния
PERSON_K = 15       # решений и обещаний человека без привязки к словам вопроса
SNIPPET_CHARS = 1500
FTS_DOC = "to_tsvector('russian', coalesce(e.title, '') || ' ' || coalesce(e.body, ''))"
HEADLINE_OPTS = 'MaxFragments=3, MaxWords=40, MinWords=15, StartSel="", StopSel="", FragmentDelimiter=" … "'

KIND_LABEL = {"meeting": "Встреча", "calendar": "Встреча в календаре", "email": "Письмо", "message": "Сообщение",
              "card": "Карточка", "note": "Заметка", "decision": "Решение", "commitment": "Обещание"}
RU_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

SYSTEM_ASK = (
    "Ты отвечаешь George на вопросы по его рабочей памяти: встречам, письмам, сообщениям, решениям и обещаниям. "
    "Опирайся только на пронумерованные фрагменты, ничего не додумывай. "
    "Каждое утверждение подкрепляй ссылкой на номер фрагмента в квадратных скобках: [1], [2]. "
    "Всегда называй, о ком речь, с контекстом в скобках и датой, например: "
    "«Ян (Infatica) на встрече 14 августа сказал…». "
    "Если в вопросе названы конкретные люди, речь только о них: однофамильцев и тёзок из других контекстов не смешивай. "
    "Если во фрагментах ответа нет, так и скажи: «В памяти этого нет». "
    "Пиши по-русски, коротко, простым текстом без разметки и без длинных тире."
)


# ---------------------------------------------------------------------------
# Имена
# ---------------------------------------------------------------------------
PEOPLE_SQL = """
SELECT p.id, p.display_name,
       coalesce((SELECT array_agg(DISTINCT i.value) FROM work.identities i
                 WHERE i.person_id = p.id AND i.kind = 'display_name'), '{}') AS names,
       coalesce((SELECT array_agg(c.title ORDER BY c.id) FROM work.person_contexts pc
                 JOIN work.contexts c ON c.id = pc.context_id WHERE pc.person_id = p.id), '{}') AS contexts
FROM work.people p
"""


_QUALIFIER = re.compile(r"\s*(?:из|в|во|с|со)\s+([A-Za-zА-Яа-яЁё]{4,})", re.I)


def _context_prefixes(conn) -> dict[str, str]:
    """Первые три буквы (в транслите) id и слов названия контекста -> название: «альфы», «инфатике»."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT id, title FROM work.contexts").fetchall():
        for w in [r["id"], *re.split(r"[\s\-]+", r["title"] or "")]:
            k = norm(w)
            if len(k) >= 3:
                out.setdefault(k[:3], r["title"])
    return out


def resolve_names(conn, text: str) -> list[dict]:
    """Имена людей в вопросе -> [{raw, candidates: [{person_id, name, contexts: [названия]}]}].

    Самая длинная форма первой: «Ян Иванов» даёт одного кандидата, голое «Ян» всех Янов.
    Явное уточнение прямо после имени («Ян из Альфы», «Саша в Инфатике») сужает кандидатов
    до этого контекста; по текущему контексту или теме вопроса не угадываем никогда.
    Имя из словаря, которого нет среди людей, приходит с пустым candidates.
    """
    idx = NameIndex()
    info: dict[int, dict] = {}
    for r in conn.execute(PEOPLE_SQL).fetchall():
        idx.add(r["id"], r["display_name"], r["names"])
        info[r["id"]] = {"name": r["display_name"], "contexts": list(r["contexts"])}
    mine = self_keys()
    prefixes: dict[str, str] | None = None
    out = []
    for m in unique_mentions(idx.scan(text or "", require_cap=False)):
        if not m.pids and m.key[0] in mine:
            continue                         # «Жора» в вопросе это сам George
        cands = [{"person_id": p, "name": info[p]["name"], "contexts": info[p]["contexts"]} for p in sorted(m.pids)]
        q = _QUALIFIER.match(text, m.start + len(m.surface)) if len(cands) > 1 else None
        if q:
            prefixes = prefixes if prefixes is not None else _context_prefixes(conn)
            title = prefixes.get(norm(q.group(1))[:3])
            narrowed = [c for c in cands if title and title in c["contexts"]]
            cands = narrowed or cands
        out.append({"raw": m.raw, "candidates": cands})
    return out


def prepare(conn, question: str) -> dict:
    """Развилка до поиска: {"unknown": [raw]} | {"choices": [...]} | {"person_ids": [...]}.

    person_ids (однозначно разрешённые) есть в ответе всегда, чтобы бот мог добавить к ним выбор.
    """
    names = resolve_names(conn, question)
    pids = sorted({n["candidates"][0]["person_id"] for n in names if len(n["candidates"]) == 1})
    unknown = [n["raw"] for n in names if not n["candidates"]]
    if unknown:
        return {"unknown": unknown, "person_ids": pids}
    choices = [n for n in names if len(n["candidates"]) > 1]
    if choices:
        return {"choices": choices, "person_ids": pids}
    return {"person_ids": pids}


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------
def _ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return tu.at_local(v, time(0))
    return datetime.fromisoformat(str(v))


def _tsquery(conn, query: str) -> str:
    """websearch_to_tsquery, но через ИЛИ: вопрос длинный, и все слова сразу не встречаются нигде."""
    q = conn.execute("SELECT replace(websearch_to_tsquery('russian', %s)::text, ' & ', ' | ') AS q",
                     (query or "",)).fetchone()["q"]
    return q or ""


def _query_vector(conn, query: str) -> tuple[str, str] | None:
    if config.env("WORKOS_ASK_VECTOR", "0") != "1":
        return None
    vtype = embed.vector_type(conn, "work.events")
    if not vtype:
        return None                          # нет pgvector (тесты): только полнотекст
    try:
        return vtype, embed.literal(embed.embed([query], kind="query")[0])
    except Exception as e:  # noqa: BLE001 — нет файлов модели в боте: работаем полнотекстом
        log.warning("вектор запроса не посчитан: %s", e)
        return None


def _filters(pids: list[int], context_id, since, until, *, alias: str, ts: str, person_sql: str) -> tuple[str, dict]:
    where, params = ["TRUE"], {}
    if context_id:
        where.append(f"{alias}.context_id = %(ctx)s")
        params["ctx"] = context_id
    if since:
        where.append(f"{alias}.{ts} >= %(since)s")
        params["since"] = since
    if until:
        where.append(f"{alias}.{ts} < %(until)s")
        params["until"] = until
    if pids:
        where.append(person_sql)
        params["pids"] = pids
        params["pmeta"] = [json.dumps({"participant_ids": [p]}) for p in pids]
        params["parr"] = [json.dumps([p]) for p in pids]
    return " AND ".join(where), params


EVENT_PERSON = """(e.actor_person_id = ANY(%(pids)s::bigint[])
  OR e.meta @> ANY(%(pmeta)s::text[]::jsonb[])
  OR (e.actor_raw IS NOT NULL AND EXISTS (
        SELECT 1 FROM work.identities i
        WHERE i.person_id = ANY(%(pids)s::bigint[]) AND i.kind IN ('email', 'slack', 'trello')
          AND (lower(e.actor_raw) = lower(i.value)
               OR (i.kind = 'email' AND strpos(lower(e.actor_raw), i.value) > 0)))))"""
DECISION_PERSON = """(d.participants @> ANY(%(parr)s::text[]::jsonb[])
  OR EXISTS (SELECT 1 FROM work.events se WHERE se.id = d.source_event
             AND se.meta @> ANY(%(pmeta)s::text[]::jsonb[])))"""
COMMITMENT_PERSON = "c.person_id = ANY(%(pids)s::bigint[])"


def _ids(conn, sql: str, params: dict) -> list[int]:
    return [r["id"] for r in conn.execute(sql, params).fetchall()]


def _ranked_lists(conn, query: str, pids: list[int], context_id, since, until) -> tuple[list[list], str]:
    tsq = _tsquery(conn, query)
    qv = _query_vector(conn, query)
    model = embed.model_name()
    lists: list[list[tuple[str, int]]] = []

    ew, ep = _filters(pids, context_id, since, until, alias="e", ts="occurred_at", person_sql=EVENT_PERSON)
    ep.update(tsq=tsq, k=LIST_K, model=model)
    if tsq:
        lists.append([("e", i) for i in _ids(conn, f"""
            SELECT e.id FROM work.events e, CAST(%(tsq)s AS tsquery) q
            WHERE {FTS_DOC} @@ q AND {ew}
            ORDER BY ts_rank_cd({FTS_DOC}, q) DESC, e.occurred_at DESC LIMIT %(k)s""", ep)])
    if qv:
        ep["qv"] = qv[1]
        lists.append([("e", i) for i in _ids(conn, f"""
            SELECT e.id FROM work.events e
            WHERE e.embedding IS NOT NULL AND e.embedding_model = %(model)s AND {ew}
            ORDER BY e.embedding <=> %(qv)s::{qv[0]} LIMIT %(k)s""", ep)])

    dw, dp = _filters(pids, context_id, since, until, alias="d", ts="decided_at", person_sql=DECISION_PERSON)
    dp.update(tsq=tsq, k=LIST_K, model=model)
    d_doc = "to_tsvector('russian', d.statement || ' ' || coalesce(d.rationale, ''))"
    if tsq:
        lists.append([("d", i) for i in _ids(conn, f"""
            SELECT d.id FROM work.decisions d, CAST(%(tsq)s AS tsquery) q
            WHERE {d_doc} @@ q AND {dw} ORDER BY ts_rank_cd({d_doc}, q) DESC LIMIT %(k)s""", dp)])
    dvt = embed.vector_type(conn, "work.decisions") if qv else None
    if qv and dvt:
        dp["qv"] = qv[1]
        lists.append([("d", i) for i in _ids(conn, f"""
            SELECT d.id FROM work.decisions d
            WHERE d.embedding IS NOT NULL AND d.embedding_model = %(model)s AND {dw}
            ORDER BY d.embedding <=> %(qv)s::{dvt} LIMIT %(k)s""", dp)])
    if pids:
        dp["pk"] = PERSON_K
        lists.append([("d", i) for i in _ids(conn, f"""
            SELECT d.id FROM work.decisions d WHERE {dw} ORDER BY d.decided_at DESC LIMIT %(pk)s""", dp)])

    cw, cp = _filters(pids, context_id, since, until, alias="c", ts="created_at", person_sql=COMMITMENT_PERSON)
    cp.update(tsq=tsq, k=LIST_K, pk=PERSON_K)
    if tsq:
        lists.append([("c", i) for i in _ids(conn, f"""
            SELECT c.id FROM work.commitments c, CAST(%(tsq)s AS tsquery) q
            WHERE to_tsvector('russian', c.what) @@ q AND {cw}
            ORDER BY ts_rank_cd(to_tsvector('russian', c.what), q) DESC LIMIT %(k)s""", cp)])
    if pids:
        lists.append([("c", i) for i in _ids(conn, f"""
            SELECT c.id FROM work.commitments c WHERE {cw}
            ORDER BY (c.status = 'open') DESC, c.created_at DESC LIMIT %(pk)s""", cp)])
    return lists, tsq


def _fuse(lists: list[list[tuple[str, int]]]) -> list[tuple[tuple[str, int], float]]:
    score: dict[tuple[str, int], float] = {}
    for lst in lists:
        for rank, key in enumerate(lst, start=1):
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(score.items(), key=lambda kv: -kv[1])


def _clean(s: str | None, n: int = SNIPPET_CHARS) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def search(conn, query: str, person_ids=None, context_id: str | None = None, since=None, until=None,
           limit: int = 30) -> list[dict]:
    """Гибридный поиск -> [{event_id, kind, title, occurred_at, context_id, snippet, score, ...}].

    Фильтр по людям стоит в WHERE: участник встречи (meta.participant_ids), автор события
    (actor_person_id или его почта/id в actor_raw), участники решения, адресат обещания.
    Кроме событий в выдаче решения (kind 'decision') и обещания (kind 'commitment'):
    у них event_id = исходное событие, а id = строка своей таблицы.
    """
    pids = sorted({int(p) for p in person_ids or []})
    since, until = _ts(since), _ts(until)
    lists, tsq = _ranked_lists(conn, query, pids, context_id, since, until)
    fused = _fuse(lists)[:limit]
    if not fused:
        return []
    by_kind: dict[str, list[int]] = {"e": [], "d": [], "c": []}
    for (k, i), _ in fused:
        by_kind[k].append(i)
    rows: dict[tuple[str, int], dict] = {}
    if by_kind["e"]:
        headline = (f"ts_headline('russian', e.body, CAST(%(tsq)s AS tsquery), '{HEADLINE_OPTS}')" if tsq
                    else f"left(e.body, {SNIPPET_CHARS})")
        for r in conn.execute(f"""
                SELECT e.id, e.kind, e.title, e.occurred_at, e.context_id, e.actor_raw, e.actor_person_id,
                       e.meta->'participant_ids' AS participant_ids,
                       CASE WHEN octet_length(coalesce(e.body, '')) <= {SNIPPET_CHARS} THEN coalesce(e.body, '')
                            ELSE {headline} END AS snippet
                FROM work.events e WHERE e.id = ANY(%(ids)s::bigint[])""",
                {"ids": by_kind["e"], "tsq": tsq}).fetchall():
            rows[("e", r["id"])] = {
                "id": r["id"], "event_id": r["id"], "kind": r["kind"], "title": r["title"] or "",
                "occurred_at": r["occurred_at"], "context_id": r["context_id"], "snippet": _clean(r["snippet"]),
                "participant_ids": sorted({int(x) for x in (r["participant_ids"] or []) if str(x).isdigit()}
                                          | ({r["actor_person_id"]} if r["actor_person_id"] else set())),
                "actor_raw": r["actor_raw"]}
    if by_kind["d"]:
        for r in conn.execute("""
                SELECT id, statement, rationale, decided_at, context_id, source_event, participants
                FROM work.decisions WHERE id = ANY(%s::bigint[])""", (by_kind["d"],)).fetchall():
            rows[("d", r["id"])] = {
                "id": r["id"], "event_id": r["source_event"], "kind": "decision", "title": r["statement"],
                "occurred_at": r["decided_at"], "context_id": r["context_id"],
                "snippet": _clean(f"{r['statement']}. {r['rationale'] or ''}".strip(" .") + "."),
                "participant_ids": sorted({int(x) for x in (r["participants"] or []) if str(x).isdigit()})}
    if by_kind["c"]:
        for r in conn.execute("""
                SELECT id, direction, person_id, what, due, status, created_at, context_id, source_event
                FROM work.commitments WHERE id = ANY(%s::bigint[])""", (by_kind["c"],)).fetchall():
            rows[("c", r["id"])] = {
                "id": r["id"], "event_id": r["source_event"], "kind": "commitment", "title": r["what"],
                "occurred_at": r["created_at"], "context_id": r["context_id"],
                "direction": r["direction"], "person_id": r["person_id"], "due": r["due"], "status": r["status"],
                "snippet": _clean(r["what"]),
                "participant_ids": [r["person_id"]] if r["person_id"] else []}
    out = []
    for key, sc in fused:
        if key in rows:
            out.append({**rows[key], "score": round(sc, 5)})
    return out


# ---------------------------------------------------------------------------
# Ответ
# ---------------------------------------------------------------------------
_MONTH_RX = re.compile(r"(?<![\w])(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|"
                       r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?![\w])(?:\s+(20\d\d))?", re.I)
_MONTH_NUM = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
              "окт": 10, "ноя": 11, "дек": 12}


def _month_start(y: int, m: int) -> date:
    return date(y + (m - 1) // 12, (m - 1) % 12 + 1, 1)


def period(question: str, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Период из вопроса: «в июне», «вчера», «на прошлой неделе», «в прошлом месяце»."""
    q = (question or "").lower()
    today = tu.logical_date(now or tu.now())
    if re.search(r"(?<![\w])вчера(?![\w])", q):
        return tu.day_bounds(today - timedelta(days=1))
    if re.search(r"(?<![\w])сегодня(?![\w])", q):
        return tu.day_bounds(today)
    mon = tu.week_monday(today)
    if re.search(r"прошл\w+\s+недел", q):
        return tu.day_bounds(mon - timedelta(days=7))[0], tu.day_bounds(mon)[0]
    if re.search(r"(эт\w+|текущ\w+)\s+недел", q):
        return tu.day_bounds(mon)[0], tu.day_bounds(mon + timedelta(days=7))[0]
    first = today.replace(day=1)
    if re.search(r"прошл\w+\s+месяц", q):
        prev = _month_start(first.year, first.month - 1)
        return tu.at_local(prev, time(0)), tu.at_local(first, time(0))
    if re.search(r"(эт\w+|текущ\w+)\s+месяц", q):
        return tu.at_local(first, time(0)), tu.at_local(_month_start(first.year, first.month + 1), time(0))
    m = _MONTH_RX.search(q)
    if m:
        word = m.group(1)
        month = next(n for pre, n in sorted(_MONTH_NUM.items(), key=lambda kv: -len(kv[0])) if word.startswith(pre))
        year = int(m.group(2)) if m.group(2) else (today.year if month <= today.month else today.year - 1)
        start = date(year, month, 1)
        return tu.at_local(start, time(0)), tu.at_local(_month_start(year, month + 1), time(0))
    return None, None


def _short_date(dt) -> str:
    if not dt:
        return ""
    d = tu.local(dt).date() if isinstance(dt, datetime) else dt
    s = f"{d.day} {RU_MONTHS_SHORT[d.month - 1]}"
    return s if d.year == tu.local().year else f"{s} {d.year}"


def _long_date(dt) -> str:
    if not dt:
        return ""
    d = tu.local(dt).date() if isinstance(dt, datetime) else dt
    return f"{tu.ru_date(d, weekday=False)} {d.year}"


def _people_info(conn, pids: set[int]) -> dict[int, str]:
    """person_id -> «Ян Иванов (Infatica)»."""
    if not pids:
        return {}
    rows = conn.execute("""
        SELECT p.id, p.display_name,
               coalesce((SELECT string_agg(c.title, ', ' ORDER BY c.id) FROM work.person_contexts pc
                         JOIN work.contexts c ON c.id = pc.context_id WHERE pc.person_id = p.id), '') AS ctx
        FROM work.people p WHERE p.id = ANY(%s::bigint[])""", (sorted(pids),)).fetchall()
    return {r["id"]: f"{r['display_name']} ({r['ctx']})" if r["ctx"] else r["display_name"] for r in rows}


def _contexts(conn) -> dict[str, str]:
    return {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM work.contexts").fetchall()}


def _fragment(n: int, h: dict, ctx_titles: dict, names: dict) -> str:
    label = KIND_LABEL.get(h["kind"], "Запись")
    head = [f"[{n}] {label} «{_clean(h['title'], 120) or 'без названия'}»", _long_date(h["occurred_at"]),
            ctx_titles.get(h["context_id"], h["context_id"] or "")]
    people = [names[p] for p in h.get("participant_ids") or [] if p in names]
    if h["kind"] == "commitment":
        who = names.get(h.get("person_id"), "?")
        head.append(f"я обещал: {who}" if h.get("direction") == "mine" else f"обещал(а) мне: {who}")
        if h.get("due"):
            head.append(f"срок {_short_date(h['due'])}")
        head.append(f"статус {h.get('status')}")
    elif people:
        head.append("участники: " + ", ".join(people[:12]))
    elif h.get("actor_raw"):
        head.append(f"автор: {_clean(h['actor_raw'], 80)}")
    return " · ".join(x for x in head if x) + "\n" + (h.get("snippet") or "")


def _ref_line(n: int, h: dict) -> str:
    label = KIND_LABEL.get(h["kind"], "Запись")
    return f"[{n}] {label} «{tg.esc(_clean(h['title'], 70) or 'без названия')}» · {_short_date(h['occurred_at'])}"


def answer(conn, question: str, person_ids=None, context_id: str | None = None,
           since=None, until=None, limit: int = 30) -> dict:
    """Ответ на вопрос: {"text": HTML для Telegram, "refs": [event_id, ...] в порядке ссылок}."""
    pids = sorted({int(p) for p in person_ids or []})
    auto_period = since is None and until is None
    if auto_period:
        since, until = period(question)
    hits = search(conn, question, pids, context_id, since, until, limit)
    if not hits and auto_period and (since or until):
        since = until = None                    # период угадан по словам вопроса: пробуем без него
        hits = search(conn, question, pids, context_id, None, None, limit)
    if not hits:
        return {"text": "В памяти ничего не нашлось по этому вопросу.", "refs": []}

    ctx_titles = _contexts(conn)
    all_pids = set(pids)
    for h in hits:
        all_pids.update(h.get("participant_ids") or [])
        if h.get("person_id"):
            all_pids.add(h["person_id"])
    names = _people_info(conn, all_pids)
    user = [f"Вопрос: {question.strip()}"]
    if pids:
        user.append("Люди из вопроса (фильтр уже применён, речь только о них): "
                    + "; ".join(names.get(p, str(p)) for p in pids))
    if since or until:
        user.append(f"Период: с {_long_date(since) or 'начала'} до {_long_date(until) or 'сегодня'}")
    user.append("\nФрагменты:\n\n" + "\n\n".join(_fragment(i, h, ctx_titles, names) for i, h in enumerate(hits, 1)))
    try:
        raw = llm.complete("ask", SYSTEM_ASK, "\n".join(user))
    except llm.LLMError as e:
        return {"text": f"Модель не ответила: {tg.esc(str(e)[:300])}", "refs": []}

    raw = (raw or "").strip().replace("—", "-").replace("–", "-")
    cited: list[int] = []
    for m in re.finditer(r"\[(\d{1,3})\]", raw):
        n = int(m.group(1))
        if 1 <= n <= len(hits) and n not in cited:
            cited.append(n)
    shown = cited or list(range(1, min(5, len(hits)) + 1))
    ref_lines = [_ref_line(n, hits[n - 1]) for n in shown]
    refs: list[int] = []
    for n in shown:
        ev = hits[n - 1].get("event_id")
        if ev and ev not in refs:
            refs.append(ev)
    tail = "\n\n" + "\n".join(ref_lines)
    body = tg.esc(raw)
    room = 4000 - len(tail)
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"
    return {"text": body + tail, "refs": refs}


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m retrieve", description="Вопрос к памяти WorkOS")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--person", type=int, action="append", help="person_id (можно несколько)")
    ap.add_argument("--context", help="infatica | alfa | consulting | personal")
    args = ap.parse_args(argv)
    q = " ".join(args.question)
    with db.connect(autocommit=True) as conn:
        pids = args.person
        if not pids:
            p = prepare(conn, q)
            if "unknown" in p:
                print("Не знаю таких людей: " + ", ".join(p["unknown"]))
                return 1
            if "choices" in p:
                for c in p["choices"]:
                    opts = "; ".join(f"{x['person_id']}: {x['name']} ({', '.join(x['contexts'])})"
                                     for x in c["candidates"])
                    print(f"«{c['raw']}» неоднозначно: {opts}")
                print("Уточни: --person ID")
                return 2
            pids = p["person_ids"]
        r = answer(conn, q, pids, args.context)
    print(re.sub(r"<[^>]+>", "", r["text"]).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    print("refs:", r["refs"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

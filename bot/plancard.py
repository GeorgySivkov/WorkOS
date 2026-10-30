"""1.4 мост вечернего плана + 2.8 карточки плана.

Вставленный в «План» чеклист (секция «Завтра», можно с «Эта неделя») становится
действием plan_batch со статусом proposed; карточку рисует тот же код, что и для
планов от agents.planner. Выбор задач живёт только в callback (битовая маска).

callback_data: pb:<id>:ok:<h8> · pb:<id>:no:<h8> · pb:<id>:sel:<mask>:<h8>
· pb:<id>:t:<i>:<mask>:<h8> · pb:<id>:go:<mask>:<h8> · pb:<id>:bk:<h8>
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from core import config, db, ruparse, timeutil as tu
from core.tg import btn, esc

from bot import capture, cards
from bot.topics import Ctx, plural

log = logging.getLogger("workos.bot")

MAX_TOGGLES = 40
RX_ITEM = re.compile(r"^\s*(?:[-*+]\s+|[•◦▫▪·]\s*|\d{1,2}[.)]\s+|(?=\[))(?:\[([ xXхХ✓✔]?)\]\s*)?(.*\S)\s*$")
RX_BOX = re.compile(r"^\s*[☐☑✅]\s*(.*\S)\s*$")
RX_SIZE = re.compile(r"[(\[]\s*([SMLМ])\s*[)\]]")
RX_DUR = re.compile(r"(?:[~≈]\s*|\()(\d+(?:[.,]\d+)?)\s*(ч|час(?:а|ов)?|h|мин|min|m)\.?\)?(?![\w])", re.I)
RX_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?:\s*[-–—]\s*(\d{1,2}):(\d{2}))?(?![\d:])")


# ---------- разбор ----------
def _section(line: str) -> str | None:
    s = re.sub(r"[#*_>`:]+", " ", line).strip().lower()
    if not s or len(s) > 60:
        return None
    if "завтра" in s or "сегодня" in s or re.search(r"план на", s):
        return "day"
    if "недел" in s:
        return "week"
    if "потом" in s or "когда-нибудь" in s or "бэклог" in s:
        return "skip"
    return None


def _size(minutes: float | None, letter: str | None) -> str:
    if letter:
        return "M" if letter in ("M", "М") else letter
    if minutes is None:
        return "S"
    return "S" if minutes <= 45 else "M" if minutes <= 90 else "L"


def _item(text: str, day: date | None) -> dict | None:
    t = text
    letter = None
    m = RX_SIZE.search(t)
    if m:
        letter = m.group(1)
        t = t[:m.start()] + " " + t[m.end():]
    minutes = None
    m = RX_DUR.search(t)
    if m:
        n = float(m.group(1).replace(",", "."))
        minutes = n * 60 if m.group(2).lower().startswith(("ч", "h")) else n
        t = t[:m.start()] + " " + t[m.end():]
    start = end = None
    m = RX_TIME.search(t)
    if m and day and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        def at(h, mi):
            return tu.local_dt_for(day, tu.parse_hm(f"{int(h)}:{mi}"))
        start = at(m.group(1), m.group(2))
        if m.group(3) and int(m.group(3)) <= 23 and int(m.group(4)) <= 59:
            end = at(m.group(3), m.group(4))
        t = t[:m.start()] + " " + t[m.end():]
    size = _size(minutes, letter)
    if start and (not end or end <= start):
        end = start + timedelta(minutes=minutes or config.SIZE_MINUTES[size])
    t = re.sub(r"~~|\*\*|__", "", t)
    title = re.sub(r"\s+", " ", t).strip(" ,.;:-—–·")
    if not title:
        return None
    item = {"title": title[:300], "size": size}
    if start:
        # время George написал сам: это ручной план, replan и dayplan его не двигают
        item["start"], item["end"], item["fixed"] = start.isoformat(), end.isoformat(), True
    return item


def parse_checklist(text: str, now: datetime | None = None) -> dict | None:
    """Чеклист -> payload plan_batch или None, если на чеклист не похоже."""
    now = now or tu.now()
    today = tu.logical_date(now)
    hour = tu.local(now).hour
    default_day = today + timedelta(days=1) if (hour >= 18 or hour < config.LOGICAL_DAY_END_HOUR) else today
    section = "day"
    for_date: date | None = None
    headers = 0
    raw: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = RX_ITEM.match(line)
        box = RX_BOX.match(line) if not m else None
        if m or box:
            mark = (m.group(1) if m else "") or ""
            body = m.group(2) if m else box.group(1)
            if mark.strip() or line.strip().startswith(("☑", "✅")):
                continue                          # уже сделано
            if section != "skip":
                raw.append((section, body))
            continue
        sec = _section(line)
        if sec:
            headers += 1
            section = sec
            if sec == "day" and for_date is None:
                p = ruparse.parse(line, now)
                if p.due:
                    for_date = p.due
                elif "сегодня" in line.lower():
                    for_date = today
                elif "завтра" in line.lower():
                    for_date = today + timedelta(days=1)
    if not raw or (len(raw) < 2 and not headers):
        return None
    for_date = for_date or default_day
    items, week = [], []
    for sec, body in raw:
        it = _item(body, for_date if sec == "day" else None)
        if not it:
            continue
        if sec == "week":
            it.pop("start", None)
            it.pop("end", None)
            week.append(it)
        else:
            items.append(it)
    if not items and not week:
        return None
    return {"for_date": for_date.isoformat(), "items": items, "week_items": week}


def handle_paste(ctx: Ctx, msg: dict) -> bool:
    """Сообщение в «Плане». True, если это чеклист и он стал карточкой (или уже был)."""
    text = capture.msg_text(msg)
    payload = parse_checklist(text, tu.now())
    if not payload:
        return False
    conn = ctx.conn
    eid = capture.store_event(ctx, msg, body=text, meta={"plan_paste": True})
    if eid is None:
        return True                                # повторная доставка: карточка уже есть
    # под общим замком: фоновый тик не успеет увидеть строку без карточки и одобрить её сам
    with ctx.lock:
        with conn.transaction():
            aid = conn.execute(
                """INSERT INTO work.actions (context_id, type, payload, rationale, source_event, status, autonomy)
                   VALUES ('personal', 'plan_batch', %s, %s, %s, 'proposed', 'approval') RETURNING id""",
                (db.jb(payload), "Вечерний план, вставлен в чат", eid)).fetchone()["id"]
            capture.merge_meta(conn, eid, {"action_id": aid})
        try:
            cards.post_card(ctx, aid, direct=True)
        except Exception:
            log.exception("карточка плана %s", aid)      # тик покажет её через outbox
    return True


# ---------- карточка ----------
def _for_date(p: dict) -> date | None:
    try:
        return date.fromisoformat(str(p.get("for_date"))[:10])
    except ValueError:
        return None


def all_items(p: dict) -> list[dict]:
    return list(p.get("items") or []) + list(p.get("week_items") or [])


def summary(p: dict) -> str:
    n, w = len(p.get("items") or []), len(p.get("week_items") or [])
    d = _for_date(p)
    parts = []
    if n or not w:
        parts.append(f"{n} {plural(n, 'задача', 'задачи', 'задач')} на "
                     + (tu.ru_date(d)[0].lower() + tu.ru_date(d)[1:] if d else "день"))
    if w:
        parts.append(f"{w} на неделю" if parts else f"{w} {plural(w, 'задача', 'задачи', 'задач')} на неделю")
    return " + ".join(parts)


def _when(it: dict) -> str:
    try:
        s = datetime.fromisoformat(it["start"])
        return tu.hm(s) + " "
    except (KeyError, TypeError, ValueError):
        return ""


def _moved(it: dict) -> str:
    """Пункт двигает уже существующую задачу (task_id), а не заводит новую."""
    return " · перенос" if it.get("task_id") else ""


def items_text(p: dict, mask: int | None = None) -> str:
    lines = []
    items = p.get("items") or []
    for i, it in enumerate(items):
        off = mask is not None and not (mask >> i) & 1
        line = f"{_when(it)}{esc(it.get('title'))} · {esc(it.get('size') or 'S')}{_moved(it)}"
        lines.append(f"• <s>{line}</s>" if off else f"• {line}")
    week = p.get("week_items") or []
    if week:
        lines.append("<b>На неделю</b>")
        for j, it in enumerate(week):
            i = len(items) + j
            off = mask is not None and not (mask >> i) & 1
            line = esc(it.get("title")) + _moved(it)
            lines.append(f"• <s>{line}</s>" if off else f"• {line}")
    return "\n".join(lines)


def card_text(row: dict, mask: int | None = None) -> str:
    p = row["payload"] or {}
    text = f"<b>{summary(p)}</b>"
    if row.get("rationale"):
        text += f"\n<i>{esc(row['rationale'])}</i>"
    return text + "\n\n" + items_text(p, mask)


def full_mask(p: dict) -> int:
    return (1 << len(all_items(p))) - 1


def main_buttons(aid: int, p: dict, h8: str) -> list:
    rows = [btn("В список", f"pb:{aid}:ok:{h8}")]
    n = len(all_items(p))
    if 1 < n <= MAX_TOGGLES:
        rows.append(btn("Выбрать", f"pb:{aid}:sel:{full_mask(p):x}:{h8}"))
    rows.append(btn("Не надо", f"pb:{aid}:no:{h8}"))
    return [rows]


def toggle_buttons(aid: int, p: dict, mask: int, h8: str) -> list:
    rows = []
    for i, it in enumerate(all_items(p)[:MAX_TOGGLES]):
        on = (mask >> i) & 1
        title = str(it.get("title") or "")
        title = title if len(title) <= 32 else title[:31] + "…"
        rows.append([btn(("✅ " if on else "▫️ ") + title, f"pb:{aid}:t:{i}:{mask:x}:{h8}")])
    k = bin(mask).count("1")
    rows.append([btn(f"Готово ({k})", f"pb:{aid}:go:{mask:x}:{h8}"), btn("Назад", f"pb:{aid}:bk:{h8}")])
    return rows


def render(conn, row: dict, h8: str) -> tuple[str, list]:
    p = row["payload"] or {}
    return card_text(row), main_buttons(row["id"], p, h8)


# ---------- задачи ----------
def insert_tasks(conn, row: dict, selected: list[int] | None) -> list[int]:
    p = row["payload"] or {}
    d = _for_date(p) or tu.logical_today()
    items = p.get("items") or []
    week = p.get("week_items") or []
    sel = set(range(len(items) + len(week))) if selected is None else set(selected)
    cx, cl = cards._valid_ids(conn)
    monday = tu.week_monday(d)
    ids = []
    for i, it in enumerate(items + week):
        if i not in sel:
            continue
        is_week = i >= len(items)
        start = cards._ts(it.get("start")) if not is_week else None
        end = cards._ts(it.get("end")) if start else None
        ctx_id = it.get("context_id") if it.get("context_id") in cx else None
        # время из вставленного текста George = ручное; время от расстановщика = auto
        by = ("manual" if it.get("fixed") else "auto") if start else None
        tid = it.get("task_id")
        if tid is not None:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                tid = None
        if tid is not None:
            # задача уже есть (перенос, просрочка): двигаем её, а не заводим дубль;
            # время, поставленное руками, не трогаем; planned_at только если время сдвинулось
            ex = conn.execute("""UPDATE work.tasks SET due = %(due)s, week_of = %(wk)s,
                                   planned_start = CASE WHEN planned_by = 'manual' THEN planned_start ELSE %(s)s END,
                                   planned_end   = CASE WHEN planned_by = 'manual' THEN planned_end ELSE %(e)s END,
                                   planned_by    = CASE WHEN planned_by = 'manual' THEN planned_by ELSE %(by)s END,
                                   planned_at    = CASE WHEN planned_by = 'manual'
                                                         OR (planned_start IS NOT DISTINCT FROM %(s)s::timestamptz
                                                             AND planned_end IS NOT DISTINCT FROM %(e)s::timestamptz)
                                                        THEN planned_at ELSE now() END
                                 WHERE id = %(id)s AND done_at IS NULL AND dropped_at IS NULL RETURNING id""",
                              {"due": None if is_week else d, "wk": monday if is_week else None,
                               "s": start, "e": end, "by": by, "id": tid}).fetchone()
            if ex:
                ids.append(ex["id"])
                continue
            if conn.execute("SELECT 1 FROM work.tasks WHERE id = %s", (tid,)).fetchone():
                continue                   # уже сделана или снята: дубль не заводим
        ids.append(conn.execute(
            """INSERT INTO work.tasks (context_id, title, due, week_of, size, cell_id, source, action_id,
                                       planned_start, planned_end, planned_at, planned_by)
               VALUES (%s, %s, %s, %s, %s, %s, 'plan', %s, %s, %s, %s, %s) RETURNING id""",
            (ctx_id, str(it.get("title") or "Без названия")[:300],
             None if is_week else d, monday if is_week else None,
             it.get("size") if it.get("size") in ("S", "M", "L") else "S",
             it.get("cell_id") if it.get("cell_id") in cl else None,
             row["id"], start, end, tu.now() if start else None, by)).fetchone()["id"])
    return ids


# ---------- нажатия ----------
def on_callback(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    conn = ctx.conn
    try:
        aid, op = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        return "Не понял кнопку"
    h8 = parts[-1]
    msg_id = (cq.get("message") or {}).get("message_id")
    row = cards.fetch(conn, aid)
    if not row or row["type"] != "plan_batch":
        return "План не найден"
    if row["status"] != "proposed":
        if msg_id:
            ctx.edit(msg_id, None, [])
        return "Уже решено"
    if row["h8"] != h8:
        return cards._stale(ctx, row, msg_id)
    p = row["payload"] or {}
    total = len(all_items(p))

    def mask_at(k):
        try:
            return int(parts[k], 16) & full_mask(p)
        except (IndexError, ValueError):
            return full_mask(p)

    if op == "sel":
        if msg_id:
            ctx.edit(msg_id, card_text(row, mask_at(3)), toggle_buttons(aid, p, mask_at(3), h8))
        return None
    if op == "t":
        try:
            i = int(parts[3])
        except (IndexError, ValueError):
            return "Не понял кнопку"
        mask = mask_at(4) ^ (1 << i) if 0 <= i < total else mask_at(4)
        if msg_id:
            ctx.edit(msg_id, card_text(row, mask), toggle_buttons(aid, p, mask, h8))
        return None
    if op == "bk":
        if msg_id:
            ctx.edit(msg_id, card_text(row), main_buttons(aid, p, h8))
        return None
    if op not in ("ok", "go", "no"):
        return "Не понял кнопку"

    if op in ("ok", "go") and not cards.is_card(conn, aid, msg_id):
        return "Одобрить можно только с карточки плана"
    mask = full_mask(p) if op == "ok" else (0 if op == "no" else mask_at(3))
    selected = [i for i in range(total) if (mask >> i) & 1]
    titles = [str(it.get("title")) for it in all_items(p)]
    with ctx.lock, conn.transaction():
        if not selected:
            got = conn.execute(f"""UPDATE work.actions SET status = 'rejected', decided_at = now()
                                   WHERE id = %s AND status = 'proposed' AND left({cards.H}, 8) = %s
                                   RETURNING id, type, context_id, payload""", (aid, h8)).fetchone()
            if got:
                cards.outcome(conn, got, "rejected")
            ids = []
        else:
            got = cards.approve(conn, aid, h8)
            if got:
                ids = cards.execute_internal(conn, got, None if len(selected) == total else selected)
                if len(selected) == total:
                    cards.outcome(conn, got, "accepted")
                else:
                    removed = [titles[i] for i in range(total) if i not in selected]
                    cards.outcome(conn, got, "edited", "\n".join(f"- {t}" for t in removed))
    if not got:
        return cards._stale(ctx, cards.fetch(conn, aid), msg_id)
    if msg_id:
        if not selected:
            result = f"Не надо: {summary(p)}. В список ничего не ушло."
        elif len(selected) == total:
            result = f"✅ В списке: {summary(p)}."
        else:
            removed = [titles[i] for i in range(total) if i not in selected]
            result = (f"✅ В списке {len(ids)} из {total}: {summary(p)}.\n"
                      f"Убрал: {esc(', '.join(removed))}")
        ctx.edit(msg_id, result, [])
    return "Готово"

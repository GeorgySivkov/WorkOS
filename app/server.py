"""Мини-приложение WorkOS: API и статика (процесс workos-app; шаги 1.3, П.3, 2.9).

    python -m app.server          # слушает 127.0.0.1:${APP_PORT:-8787}

Наружу его выставляет Tailscale Funnel, поэтому каждый /api/* (кроме /api/health)
проверяет initData Telegram по публичному ключу (app.initdata). Токена бота у процесса
нет, в базу он ходит ролью workos_app: видит дела, встречи без тел и персонажа,
пишет только дела и опыт через функции rpg.*. Контракт: docs/miniapp-api.md.
"""
from __future__ import annotations

import os

if __name__ == "__main__":
    os.environ.setdefault("WORKOS_ROLE", "app")       # .env.app до того, как core.config его прочтёт

import hmac
import json
import queue
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg

from agents import scheduler
from app import initdata
from core import config, db, ruparse, timeutil as tu

STATIC = Path(__file__).resolve().parent / "static"
NAME = "Георгий"
COLORS = {"infatica": "#4FB6C2", "alfa": "#F08FB0", "consulting": "#E3C26B", "personal": "#A3B0FF"}
NO_COLOR = "#A9B0D6"
CTX_TITLES = {"infatica": "Инфатика", "alfa": "Альфа", "consulting": "Консалтинг", "personal": "Личное"}
GAP_MIN = 45
MAX_BODY = 64 * 1024
TITLE_MAX = 500
DEV_TOKEN_MIN = 32          # короче: перебор по сети реален, токен не принимается
SOCKET_TIMEOUT = 15         # медленный клиент не держит поток-обработчик вечно
MAX_THREADS = 32            # больше одновременных соединений не берём, лишние закрываются сразу
TRAIN_RX = re.compile(r"трениров", re.I)

CSP = ("default-src 'self'; script-src 'self' https://telegram.org; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; "
       "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".json": "application/json", ".webmanifest": "application/manifest+json",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}

TASK_SQL = """
SELECT t.id, t.title, t.context_id, t.size, t.cell_id, t.due, t.week_of, t.remind_at, t.done_at,
       t.dropped_at, t.planned_start, t.planned_end, t.planned_by,
       ctx.title AS ctx_title, cell.title AS cell_own_title, attr.title AS attr_title
FROM work.tasks t
LEFT JOIN work.contexts ctx ON ctx.id = t.context_id
LEFT JOIN rpg.cells cell ON cell.id = t.cell_id
LEFT JOIN rpg.cells attr ON attr.id = CASE WHEN cell.kind = 'attribute' THEN cell.id ELSE cell.parent END
"""


class HttpError(Exception):
    def __init__(self, status: int, error: str, detail: str | None = None):
        super().__init__(error)
        self.status = status
        self.payload = {"error": error} if detail is None else {"error": error, "detail": detail}


def log(msg: str) -> None:
    print(f"workos-app: {msg}", file=sys.stderr, flush=True)


# ---------- форматирование ----------

def iso(v) -> str | None:
    return v.isoformat() if v is not None else None


def dur_label(minutes: float) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def date_label(d: date) -> str:
    return f"{tu.RU_WEEKDAYS[d.weekday()].capitalize()}, {tu.ru_date(d, weekday=False)}"


def group_label(d: date, today: date) -> str:
    s = tu.ru_date(d)
    return s if d.year == today.year else f"{s} {d.year}"


def size_of(row) -> str:
    s = row.get("size") or "S"
    return s if s in config.SIZE_MINUTES else "S"


def task_times(r) -> tuple[datetime | None, datetime | None]:
    """Время задачи: блок плана, а если его нет, то «в 10» из текста (remind_at в день задачи)."""
    s, e = r["planned_start"], r["planned_end"]
    if s is None and r["remind_at"] is not None and r["due"] is not None \
            and tu.logical_date(r["remind_at"]) == r["due"]:
        s = r["remind_at"]
        e = None
    if s is not None and e is None:
        e = s + timedelta(minutes=config.SIZE_MINUTES[size_of(r)])
    return s, e


def shift_remind(remind: datetime | None, old_due: date | None, new_due: date) -> datetime | None:
    """Напоминание едет вместе с днём задачи: на (new_due - old_due) дней, в то же местное время."""
    if remind is None:
        return None
    base = tu.logical_date(remind)
    days = (new_due - (old_due or base)).days
    if not days:
        return remind
    return tu.local_dt_for(base + timedelta(days=days), tu.local(remind).time())


def task_obj(r, today: date) -> dict:
    size = size_of(r)
    s, e = task_times(r)
    cid = r["context_id"]
    done = r["done_at"] is not None
    return {
        "id": r["id"], "title": r["title"], "context_id": cid,
        "context_title": CTX_TITLES.get(cid) or r.get("ctx_title") or "",
        "color": COLORS.get(cid, NO_COLOR), "size": size, "xp": config.SIZE_XP[size],
        "cell_id": r["cell_id"], "cell_title": r.get("attr_title") or r.get("cell_own_title"),
        "due": iso(r["due"]), "week_of": iso(r["week_of"]),
        "time": tu.hm(s) if s else None, "start": iso(s), "end": iso(e),
        "done": done, "overdue": (not done and r["due"] is not None and r["due"] < today),
        "remind_at": iso(r["remind_at"]), "planned_by": r["planned_by"],
    }


def streak(days: set[date], today: date) -> int:
    """Дней подряд с опытом; серия жива, если сегодня ещё не отмечено, но вчера было."""
    d = today if today in days else today - timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= timedelta(days=1)
    return n


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


# ---------- соединения ----------

class Pool:
    """Несколько соединений на поток-обработчик; сломанные выбрасываются."""

    def __init__(self, connect, size: int = 4):
        self._connect, self._size = connect, size
        self._q: queue.LifoQueue = queue.LifoQueue()

    @contextmanager
    def conn(self):
        try:
            c = self._q.get_nowait()
        except queue.Empty:
            c = self._connect()
        if c.closed or c.broken:
            c = self._connect()
        healthy = True
        try:
            yield c
        except psycopg.OperationalError:
            healthy = False
            raise
        finally:
            if healthy and not c.closed and not c.broken and self._q.qsize() < self._size:
                self._q.put(c)
            else:
                try:
                    c.close()
                except Exception:
                    pass

    def close(self):
        while True:
            try:
                self._q.get_nowait().close()
            except queue.Empty:
                return


# ---------- API ----------

class Api:
    ROUTES = [
        ("GET", re.compile(r"/api/today"), "today"),
        ("GET", re.compile(r"/api/list"), "list"),
        ("POST", re.compile(r"/api/tasks"), "create"),
        ("POST", re.compile(r"/api/tasks/(\d{1,18})/(done|undone|tomorrow|drop)"), "task_action"),
        ("POST", re.compile(r"/api/habits/([A-Za-z0-9_.:-]{1,32})/toggle"), "habit"),
        ("POST", re.compile(r"/api/rating"), "rating"),
        ("POST", re.compile(r"/api/replan"), "replan"),
        ("GET", re.compile(r"/api/character"), "character"),
        ("GET", re.compile(r"/api/grid"), "grid"),
    ]

    def __init__(self, connect=None, clock=None, *, bot_id=None, owner_id=None, public_key=None,
                 dev_token: str | None = None, pool_size: int = 4):
        self.pool = Pool(connect or (lambda: db.connect(autocommit=True)), pool_size)
        self.clock = clock or tu.now
        self.auth_kw = {"bot_id": bot_id, "owner_id": owner_id, "public_key": public_key}
        tok = (config.env("APP_DEV_TOKEN") or "") if dev_token is None else dev_token
        if tok and len(tok) < DEV_TOKEN_MIN:
            log(f"APP_DEV_TOKEN короче {DEV_TOKEN_MIN} символов: игнорирую, вход dev выключен")
            tok = ""
        self.dev_token = tok

    # --- вход ---
    def authorize(self, header: str | None) -> dict:
        scheme, _, value = (header or "").strip().partition(" ")
        scheme, value = scheme.lower(), value.strip()
        if scheme == "tma":
            try:
                return initdata.validate(value, **self.auth_kw)
            except initdata.InitDataError as e:
                log(f"401: {e}")
        elif scheme == "dev" and len(self.dev_token or "") >= DEV_TOKEN_MIN and value \
                and hmac.compare_digest(value.encode(), self.dev_token.encode()):
            return {"user": {"id": "dev"}}
        raise HttpError(401, "unauthorized")

    def handle(self, method: str, path: str, query: dict, headers, body: bytes) -> tuple[int, dict]:
        if path == "/api/health":
            return 200, {"ok": True}
        self.authorize(headers.get("Authorization"))
        for m, rx, name in self.ROUTES:
            mt = rx.fullmatch(path)
            if not mt:
                continue
            if m != method:
                raise HttpError(405, "method_not_allowed")
            with self.pool.conn() as conn, conn.transaction():
                return 200, getattr(self, "r_" + name)(conn, *mt.groups(), query=query, body=body)
        raise HttpError(404, "not_found")

    @staticmethod
    def _json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HttpError(400, "bad_json") from e
        if not isinstance(data, dict):
            raise HttpError(400, "bad_json")
        return data

    def _today(self) -> tuple[datetime, date]:
        now = self.clock()
        return now, tu.logical_date(now)

    # --- общие запросы ---
    @staticmethod
    def character_of(conn) -> dict:
        r = conn.execute("SELECT xp, level, level_floor, next_level_at FROM rpg.character").fetchone()
        return {"name": NAME, "level": int(r["level"]), "xp": int(r["xp"]),
                "level_floor": int(r["level_floor"]), "next_level_at": int(r["next_level_at"])}

    @staticmethod
    def level_up(conn, before: dict, after: dict) -> bool:
        """Повышение показывается один раз: выше наибольшего уже достигнутого уровня
        (сделал, снял, снова сделал: второй раз салюта нет)."""
        if after["level"] <= before["level"]:
            return False
        return bool(conn.execute("SELECT rpg.app_level_up(%s, %s) AS up",
                                 (before["level"], after["level"])).fetchone()["up"])

    @staticmethod
    def _task_row(conn, task_id: int):
        return conn.execute(TASK_SQL + " WHERE t.id = %s", (task_id,)).fetchone()

    @staticmethod
    def _xp_days(conn, kinds=("habit",), since_days: int = 400) -> dict[str, dict]:
        rows = conn.execute(
            """SELECT x.cell_id, count(*) AS n,
                      array_agg(DISTINCT rpg.lday(x.earned_at))
                        FILTER (WHERE x.earned_at > now() - make_interval(days => %s)) AS days
               FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
               WHERE c.kind = ANY(%s)
               GROUP BY x.cell_id""", (since_days, list(kinds))).fetchall()
        return {r["cell_id"]: {"n": r["n"], "days": set(r["days"] or [])} for r in rows}

    def _habits(self, conn, today: date) -> list[dict]:
        lo, hi = tu.day_bounds(today)
        rows = conn.execute(
            """SELECT c.id, c.title, c.auto_source, c.xp_base,
                      EXISTS (SELECT 1 FROM rpg.xp x WHERE x.cell_id = c.id
                              AND x.earned_at >= %s AND x.earned_at < %s) AS done
               FROM rpg.cells c WHERE c.kind = 'habit' AND c.active ORDER BY c.id""", (lo, hi)).fetchall()
        facts = self._xp_days(conn)
        out = []
        for r in rows:
            n = streak(facts.get(r["id"], {}).get("days", set()), today)
            src = r["auto_source"]
            if src:
                note = ("из Garmin" if src.startswith("garmin")
                        else "оценкой дня" if src.startswith("rating") else "сама")
            elif n:
                note = f"серия {n}"
            else:
                note = f"+{r['xp_base']} опыта"
            via = None if not src else "garmin" if src.startswith("garmin") \
                else "rating" if src.startswith("rating") else "auto"
            out.append({"cell_id": r["id"], "title": r["title"], "note": note, "done": bool(r["done"]),
                        "auto": src is not None, "via": via, "streak": n})
        return out

    # --- GET /api/today ---
    def build_today(self, conn) -> dict:
        now, day = self._today()
        lo, hi = tu.day_bounds(day)
        ws, we = tu.waking_window(day)
        rows = conn.execute(TASK_SQL + """
            WHERE t.dropped_at IS NULL
              AND ((t.done_at IS NULL AND t.due <= %(day)s)
                   OR (t.done_at >= %(lo)s AND t.done_at < %(hi)s))
            ORDER BY t.due NULLS LAST, t.id""", {"day": day, "lo": lo, "hi": hi}).fetchall()

        items: list[tuple[datetime, datetime, int, dict]] = []
        unscheduled: list[dict] = []
        tasks = []
        for r in rows:
            t = task_obj(r, day)
            tasks.append(t)
            s, e = task_times(r)
            if s is not None and lo <= s < hi:
                items.append((s, e, 2, {"type": "task", **t}))
            else:
                unscheduled.append(t)
        unscheduled.sort(key=lambda t: (t["done"], not t["overdue"], t["due"] or "9999", t["id"]))

        busy, seen = [], set()
        for ev in scheduler.feed_events(conn, day):
            s, e = ev["starts_at"], ev["ends_at"]
            if ev["busy"]:
                busy.append((s, e))
            key = ((ev["title"] or "").strip().lower(), s, e)
            if key in seen:            # одна встреча в двух календарях
                continue
            seen.add(key)
            cid = ev["context_id"]
            ctx = CTX_TITLES.get(cid, "")
            items.append((s, e, 1, {
                "type": "meeting", "id": ev["id"], "title": ev["title"] or "Встреча", "context_id": cid,
                "color": COLORS.get(cid, NO_COLOR), "time": tu.hm(s), "start": iso(s), "end": iso(e),
                "meta": f"{tu.hm(s)}–{tu.hm(e)}" + (f" · {ctx}" if ctx else ""), "location": ev["location"]}))

        if not any(TRAIN_RX.search(t["title"] or "") for t in tasks):
            fixed = [{"id": f"t{x['id']}", "planned_by": "manual", "planned_start": s, "planned_end": e}
                     for s, e, _, x in items if x["type"] == "task"]
            slot = scheduler.place(day, fixed, busy).get("training")
            if slot:
                s, e = slot
                attr, done = self._training_info(conn, lo, hi)
                items.append((s, e, 3, {
                    "type": "training", "time": tu.hm(s), "title": "Тренировка",
                    "meta": f"{tu.hm(s)}–{tu.hm(e)} · {attr}", "start": iso(s), "end": iso(e), "done": done}))

        cur = max(ws, scheduler.ceil5(now))
        gaps = []
        for s, e in scheduler.merge([(s, e) for s, e, _, _ in items]):
            if e <= cur:
                continue
            end = min(s, we)
            if (end - cur) >= timedelta(minutes=GAP_MIN):
                mins = (end - cur).total_seconds() / 60
                gaps.append((cur, end, 0, {"type": "gap", "time": tu.hm(cur), "start": iso(cur), "end": iso(end),
                                           "minutes": int(mins), "title": f"Привал · свободно {dur_label(mins)}"}))
            cur = max(cur, e)
        items += gaps
        items.append((now, now, -1, {"type": "now", "time": tu.hm(now), "start": iso(now)}))
        timeline = [x for _, _, _, x in sorted(items, key=lambda i: (i[0], i[2]))]

        xp_today = conn.execute("SELECT COALESCE(sum(amount), 0)::int AS n FROM rpg.xp "
                                "WHERE earned_at >= %s AND earned_at < %s", (lo, hi)).fetchone()["n"]
        return {
            "date": day.isoformat(), "date_label": date_label(day), "week": day.isocalendar()[1],
            "now": iso(now), "character": self.character_of(conn),
            "summary": {"done": sum(t["done"] for t in tasks), "total": len(tasks), "xp_today": xp_today},
            "habits": self._habits(conn, day), "rating": self._rating(conn),
            "timeline": timeline, "unscheduled": unscheduled,
        }

    @staticmethod
    def _rating(conn) -> dict:
        """Оценка текущего логического дня и среднее за месяц. День берётся из часов базы,
        как в rpg.log_rating: показываем ровно тот день, в который запишется ползунок."""
        r = conn.execute(
            """WITH d AS (SELECT rpg.lday(now()) AS day)
               SELECT d.day, r.score, r.note,
                      (SELECT round(avg(score), 1) FROM rpg.day_ratings
                        WHERE day >= date_trunc('month', d.day)::date AND day <= d.day) AS month_avg,
                      (SELECT count(*) FROM rpg.day_ratings
                        WHERE day >= date_trunc('month', d.day)::date AND day <= d.day) AS month_days
               FROM d LEFT JOIN rpg.day_ratings r ON r.day = d.day""").fetchone()
        return {"day": r["day"].isoformat(), "score": r["score"], "note": r["note"],
                "month_avg": float(r["month_avg"]) if r["month_avg"] is not None else None,
                "month_days": int(r["month_days"])}

    @staticmethod
    def _training_info(conn, lo, hi) -> tuple[str, bool]:
        r = conn.execute(
            """SELECT p.title AS attr,
                      EXISTS (SELECT 1 FROM rpg.xp x WHERE x.cell_id = c.id
                              AND x.earned_at >= %s AND x.earned_at < %s) AS done
               FROM rpg.cells c LEFT JOIN rpg.cells p ON p.id = c.parent
               WHERE c.active AND c.kind = 'habit' AND c.title ILIKE '%%трениров%%'
               ORDER BY c.id LIMIT 1""", (lo, hi)).fetchone()
        if not r:
            return "Здоровье", False
        return r["attr"] or "Здоровье", bool(r["done"])

    def r_today(self, conn, query, body):
        return self.build_today(conn)

    # --- GET /api/list ---
    def r_list(self, conn, query, body):
        view = (query.get("view") or ["week"])[0]
        if view not in ("week", "later"):
            raise HttpError(400, "bad_request", "view: week или later")
        _, today = self._today()
        monday = tu.week_monday(today)
        sunday = monday + timedelta(days=6)
        p = {"today": today, "monday": monday, "sunday": sunday}
        open_ = "WHERE t.done_at IS NULL AND t.dropped_at IS NULL AND "
        if view == "week":
            rows = conn.execute(TASK_SQL + open_ + """
                ((t.due > %(today)s AND t.due <= %(sunday)s)
                 OR (t.due IS NULL AND t.week_of IS NOT NULL AND t.week_of <= %(monday)s))
                ORDER BY t.due NULLS LAST, t.id""", p).fetchall()
        else:
            rows = conn.execute(TASK_SQL + open_ + """
                (t.due > %(sunday)s OR (t.due IS NULL AND (t.week_of IS NULL OR t.week_of > %(monday)s)))
                ORDER BY COALESCE(t.due, t.week_of) NULLS LAST, t.id""", p).fetchall()

        groups: dict[tuple, dict] = {}
        for r in rows:
            if r["due"] is not None:
                key = (0, r["due"])
                g = groups.setdefault(key, {"title": group_label(r["due"], today), "date": r["due"].isoformat()})
            elif view == "week":
                key = (1, None)
                g = groups.setdefault(key, {"title": "На неделе, без дня", "date": None})
            elif r["week_of"] is not None:
                key = (0, r["week_of"])
                g = groups.setdefault(key, {"title": f"Неделя с {tu.ru_date(r['week_of'], weekday=False)}",
                                            "date": r["week_of"].isoformat(), "kind": "week"})
            else:
                key = (2, None)
                g = groups.setdefault(key, {"title": "Без даты", "date": None})
            g.setdefault("items", []).append(task_obj(r, today))
        ordered = [groups[k] for k in sorted(groups, key=lambda k: (k[0], k[1] or date.min))]
        week_no = today.isocalendar()[1]
        span = f"{monday.day}–{tu.ru_date(sunday, weekday=False)}" if monday.month == sunday.month else \
            f"{tu.ru_date(monday, weekday=False)}–{tu.ru_date(sunday, weekday=False)}"
        return {"view": view, "title": f"Неделя {week_no} · {span}", "groups": ordered}

    # --- POST /api/tasks ---
    def r_create(self, conn, query, body):
        data = self._json(body)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "bad_request", "пустой текст")
        text = " ".join(text.split())
        if len(text) > TITLE_MAX:
            raise HttpError(400, "bad_request", "слишком длинный текст")
        bucket = data.get("bucket")
        if bucket not in (None, "today", "week", "later"):
            raise HttpError(400, "bad_request", "bucket: today, week или later")
        now, today = self._today()
        p = ruparse.parse(text, now)
        due, week_of = p.due, p.week_of
        if due is None and week_of is None and not p.explicit_later:
            if bucket == "today":
                due = today
            elif bucket == "week":
                week_of = tu.week_monday(today)
        ps = pe = None
        if p.remind_at is not None and due is not None:
            # время из текста назначил George: ручной блок, расстановщик его не двигает
            ps, pe = p.remind_at, p.remind_at + timedelta(minutes=config.SIZE_MINUTES["S"])
        new_id = conn.execute(
            """INSERT INTO work.tasks (title, due, week_of, remind_at, source,
                                       planned_start, planned_end, planned_by, planned_at)
               VALUES (%s, %s, %s, %s, 'app', %s, %s, %s, CASE WHEN %s THEN now() END)
               RETURNING id""",
            (p.title[:TITLE_MAX], due, week_of, p.remind_at, ps, pe,
             "manual" if ps else None, ps is not None)).fetchone()["id"]
        return task_obj(self._task_row(conn, new_id), today)

    # --- POST /api/tasks/{id}/... ---
    def r_task_action(self, conn, task_id, action, query, body):
        tid = int(task_id)
        now, today = self._today()
        row = self._task_row(conn, tid)
        if row is None or (row["dropped_at"] is not None and action != "drop"):
            raise HttpError(404, "not_found")

        # planned_at = «время задачи менялось в базе»: писатель календаря по нему решает, что
        # база свежее ручного переноса в Google. Отметка «сделано» время не меняет и его не трогает.
        if action == "done":
            before = self.character_of(conn)
            if row["done_at"] is None:
                conn.execute("UPDATE work.tasks SET done_at = %s WHERE id = %s", (now, tid))
            xp = conn.execute("SELECT rpg.award_task(%s) AS n", (tid,)).fetchone()["n"] or 0
            after = self.character_of(conn)
            return {"task": task_obj(self._task_row(conn, tid), today), "xp": int(xp),
                    "level_up": self.level_up(conn, before, after), "character": after}

        if action == "undone":
            if row["done_at"] is not None:
                conn.execute("UPDATE work.tasks SET done_at = NULL WHERE id = %s", (tid,))
            conn.execute("SELECT rpg.revoke_task(%s)", (tid,))
            return {"task": task_obj(self._task_row(conn, tid), today), "character": self.character_of(conn)}

        if action == "tomorrow":
            if row["done_at"] is not None:
                raise HttpError(400, "bad_request", "задача уже сделана")
            tomorrow = today + timedelta(days=1)
            remind = shift_remind(row["remind_at"], row["due"], tomorrow)
            conn.execute(
                """UPDATE work.tasks SET due = %s, week_of = NULL, remind_at = %s,
                          reminded_at = CASE WHEN remind_at IS DISTINCT FROM %s THEN NULL ELSE reminded_at END,
                          planned_at = CASE WHEN planned_start IS NOT NULL OR planned_end IS NOT NULL
                                            THEN now() ELSE planned_at END,
                          planned_start = NULL, planned_end = NULL, planned_by = NULL
                   WHERE id = %s""", (tomorrow, remind, remind, tid))
            return task_obj(self._task_row(conn, tid), today)

        # drop: «удалить» не стирает, а помечает; блок в календаре снимет писатель календаря
        if row["dropped_at"] is None:
            conn.execute(
                """UPDATE work.tasks SET dropped_at = %s,
                          planned_at = CASE WHEN planned_start IS NOT NULL OR planned_end IS NOT NULL
                                            THEN now() ELSE planned_at END,
                          planned_start = NULL, planned_end = NULL, planned_by = NULL WHERE id = %s""",
                (now, tid))
        return {"ok": True}

    # --- POST /api/habits/{cell}/toggle ---
    def r_habit(self, conn, cell_id, query, body):
        cell = conn.execute("SELECT id, auto_source FROM rpg.cells WHERE id = %s AND active AND kind = 'habit'",
                            (cell_id,)).fetchone()
        if cell is None:
            raise HttpError(404, "not_found")
        if cell["auto_source"]:
            raise HttpError(400, "auto", "эта привычка отмечается сама")
        before = self.character_of(conn)
        # руками переключается только ручная отметка; отметку задачей на этой клетке
        # (или из Garmin, пока у клетки был источник) кнопка не снимает и не задваивает
        st = conn.execute(
            """SELECT COALESCE(bool_or(x.source = 'manual' AND x.source_ref = k.ref), false) AS manual,
                      COALESCE(bool_or(NOT (x.source = 'manual' AND x.source_ref = k.ref)), false) AS other
               FROM (SELECT %(c)s || ':' || to_char(rpg.lday(now()), 'YYYY-MM-DD') AS ref) k
               LEFT JOIN rpg.xp x ON x.cell_id = %(c)s AND rpg.lday(x.earned_at) = rpg.lday(now())""",
            {"c": cell_id}).fetchone()
        if st["manual"]:
            conn.execute("SELECT rpg.revoke_manual(%s)", (cell_id,))
            xp = 0
        elif st["other"]:
            return {"done": True, "xp": 0, "level_up": False, "locked": True, "character": before}
        else:
            xp = conn.execute("SELECT rpg.log_manual(%s) AS n", (cell_id,)).fetchone()["n"] or 0
        done = conn.execute("SELECT EXISTS (SELECT 1 FROM rpg.xp WHERE cell_id = %s "
                            "AND rpg.lday(earned_at) = rpg.lday(now())) AS d", (cell_id,)).fetchone()["d"]
        after = self.character_of(conn)
        return {"done": bool(done), "xp": int(xp), "level_up": self.level_up(conn, before, after),
                "locked": False, "character": after}

    # --- POST /api/rating ---
    def r_rating(self, conn, query, body):
        data = self._json(body)
        score, note = data.get("score"), data.get("note")
        if isinstance(score, bool) or not isinstance(score, int) or not -10 <= score <= 10:
            raise HttpError(400, "bad_request", "score: целое от -10 до +10")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise HttpError(400, "bad_request", "note: строка до 500 символов")
        before = self.character_of(conn)
        r = conn.execute("SELECT rpg.log_rating(%s, %s) AS r", (score, note or None)).fetchone()["r"]
        after = self.character_of(conn)
        return {"rating": self._rating(conn), "xp": int(r.get("xp") or 0), "milestone": r.get("milestone"),
                "week_avg": r.get("week_avg"), "level_up": self.level_up(conn, before, after),
                "character": after}

    # --- POST /api/replan ---
    def r_replan(self, conn, query, body):
        now, today = self._today()
        summary = scheduler.replan(conn, today, now)
        log(f"replan {summary}")
        out = self.build_today(conn)
        out["replan"] = summary
        return out

    # --- GET /api/character ---
    def r_character(self, conn, query, body):
        now, today = self._today()
        ch = self.character_of(conn)
        season = conn.execute("SELECT COALESCE(max(season), 1) AS s FROM rpg.cells WHERE active").fetchone()["s"]
        mq = conn.execute("SELECT title FROM rpg.cells WHERE id = 'C'").fetchone()
        attrs = []
        for a in conn.execute("""SELECT id, title, xp, level, xp_into_level, level_span, last_xp_at, rusty
                                 FROM rpg.attributes ORDER BY id""").fetchall():
            span = int(a["level_span"] or 0)
            attrs.append({
                "id": a["id"], "title": a["title"], "level": int(a["level"]), "xp": int(a["xp"]),
                "pct": int(int(a["xp_into_level"]) * 100 / span) if span else 0, "rusty": bool(a["rusty"]),
                "idle_days": (today - tu.logical_date(a["last_xp_at"])).days if a["last_xp_at"] else None})
        rw = conn.execute("""SELECT level, title FROM rpg.rewards
                             WHERE scope = 'character' AND claimed_at IS NULL AND level > %s
                             ORDER BY level LIMIT 1""", (ch["level"],)).fetchone()
        opened = conn.execute("""SELECT level, title FROM rpg.rewards
                                 WHERE scope = 'character' AND claimed_at IS NULL AND level <= %s
                                 ORDER BY level""", (ch["level"],)).fetchall()
        ach = conn.execute("SELECT code, title, detail, earned_at FROM rpg.achievements "
                           "ORDER BY earned_at DESC, code").fetchall()
        habits = conn.execute("SELECT id, title FROM rpg.cells WHERE kind = 'habit' AND active ORDER BY id").fetchall()
        facts = self._xp_days(conn)
        streaks = [{"cell_id": h["id"], "title": h["title"],
                    "days": streak(facts.get(h["id"], {}).get("days", set()), today)} for h in habits]
        streaks.sort(key=lambda s: (-s["days"], s["cell_id"]))
        return {**ch, "season": f"Сезон {season}", "main_quest": mq["title"] if mq else None,
                "attributes": attrs,
                "next_reward": {"level": rw["level"], "title": rw["title"]} if rw else None,
                "open_rewards": [{"level": r["level"], "title": r["title"]} for r in opened],
                "achievements": [{"code": a["code"], "title": a["title"], "detail": a["detail"],
                                  "earned_at": iso(a["earned_at"])} for a in ach],
                "streaks": streaks}

    # --- GET /api/grid ---
    def r_grid(self, conn, query, body):
        now, today = self._today()
        cells = conn.execute("SELECT id, parent, title, kind, xp_base, auto_source, season FROM rpg.cells "
                             "WHERE active ORDER BY id").fetchall()
        attrs = {a["id"]: a for a in conn.execute("SELECT id, level, rusty FROM rpg.attributes").fetchall()}
        facts = self._xp_days(conn, kinds=("habit", "milestone", "quality"))
        center = next((c for c in cells if c["id"] == "C"), None) or next((c for c in cells if c["kind"] == "quest"), None)
        cur_week = tu.week_monday(today)
        last4 = {cur_week - timedelta(weeks=i) for i in range(4)}
        dirs = []
        for d in [c for c in cells if c["kind"] == "attribute"][:8]:
            kids = [c for c in cells if c["parent"] == d["id"] and c["kind"] in ("habit", "milestone", "quality")][:8]
            out_cells = []
            for c in kids:
                f = facts.get(c["id"], {"n": 0, "days": set()})
                if c["kind"] == "habit":
                    stat = f"серия {streak(f['days'], today)}"
                elif c["kind"] == "milestone":
                    stat = "сделано" if f["n"] else ("босс" if c["xp_base"] >= 200 else "впереди")
                else:
                    weeks = {tu.week_monday(x) for x in f["days"]} & last4
                    stat = f"{len(weeks)} из 4 недель"
                out_cells.append({"id": c["id"], "title": c["title"], "kind": c["kind"], "stat": stat,
                                  "auto": c["auto_source"] is not None})
            a = attrs.get(d["id"])
            dirs.append({"id": d["id"], "title": d["title"], "level": int(a["level"]) if a else 0,
                         "filled": len(out_cells), "rusty": bool(a["rusty"]) if a else True, "cells": out_cells})
        season = max((c["season"] for c in cells), default=1)
        return {"center": {"id": center["id"], "title": center["title"]} if center else None,
                "season": f"Сезон {season}", "dirs": dirs}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "workos-app"
    sys_version = ""
    timeout = SOCKET_TIMEOUT

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_PUT(self):
        self._send_json(405, {"error": "method_not_allowed"})

    do_DELETE = do_PATCH = do_PUT

    def _serve(self, method: str):
        try:
            u = urlsplit(self.path)
            path = unquote(u.path)
            if path == "/api" or path.startswith("/api/"):
                body = self._read_body() if method == "POST" else b""
                status, obj = self.server.api.handle(method, path, parse_qs(u.query), self.headers, body)
                self._send_json(status, obj)
            elif method == "GET":
                self._static(path)
            else:
                self._send_json(405, {"error": "method_not_allowed"})
        except HttpError as e:
            self._send_json(e.status, e.payload)
        except (TimeoutError, ConnectionError):
            self.close_connection = True          # клиент не дослал тело или ушёл: отвечать некому
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._send_json(500, {"error": "internal"})

    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise HttpError(400, "bad_request") from e
        if n < 0 or n > MAX_BODY:
            raise HttpError(413, "too_large")
        return self.rfile.read(n) if n else b""

    def _common_headers(self, ctype: str, length: int, cache: str):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", len(data), "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            f = STATIC / "index.html"
        elif path.startswith("/static/"):
            f = (STATIC / path[len("/static/"):]).resolve()
            if not f.is_relative_to(STATIC):
                raise HttpError(404, "not_found")
        else:
            raise HttpError(404, "not_found")
        ctype = TYPES.get(f.suffix.lower())
        if ctype is None or not f.is_file():
            raise HttpError(404, "not_found")
        data = f.read_bytes()
        self.send_response(200)
        # Telegram держит webview долго: без no-cache после выкладки висела бы старая версия
        self._common_headers(ctype, len(data), "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("workos-app: %s\n" % (fmt % args))


class Server(ThreadingHTTPServer):
    """Поток на соединение, но не больше MAX_THREADS сразу: лишние закрываются без ответа."""
    daemon_threads = True

    def __init__(self, addr, handler, max_threads: int = MAX_THREADS):
        super().__init__(addr, handler)
        self.slots = threading.BoundedSemaphore(max_threads)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(host: str = "127.0.0.1", port: int | None = None, api: Api | None = None,
                max_threads: int = MAX_THREADS) -> ThreadingHTTPServer:
    port = config.env_int("APP_PORT", 8787) if port is None else port
    srv = Server((host, port), Handler, max_threads)
    srv.api = api or Api()
    return srv


def serve() -> None:
    for name in ("TELEGRAM_BOT_ID", "TELEGRAM_OWNER_ID"):
        if not config.env(name):
            log(f"{name} не задан: все запросы к /api получат 401")
    srv = make_server()
    host, port = srv.server_address[:2]
    log(f"слушаю http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        srv.api.pool.close()


if __name__ == "__main__":
    serve()

"""Коллектор Plaud (шаг 2.3): записи встреч с готовым транскриптом.

Событие появляется, только когда транскрипт готов. Каждый прогон
перечитывает записи за lookback_days (по времени начала записи). Запись без
транскрипта запоминается в state.pending и перепроверяется каждый прогон до
30 дней, а запись, загруженная с телефона поздно (старая по времени, но новая
для нас), тоже подхватывается: см. collect().

Встреча сопоставляется с календарём: событие из work.calendar_feed,
пересекающееся с записью (±15 минут), даёт calendar_event_id и контекст.
Не нашлось встречи: контекст personal и meta.personal = true.

API: неофициальный веб-API Plaud (как в github.com/arbuzmell/plaud-api).
Все пути и ключи JSON собраны в классе PlaudAPI: если Plaud что-то
поменяет, править только его.

Секреты: PLAUD_TOKEN (значение заголовка Authorization из web.plaud.ai без
слова bearer), PLAUD_API_BASE (по умолчанию https://api.plaud.ai; для
европейского аккаунта может понадобиться региональный хост).
Настройки источника: {"token_env": "PLAUD_TOKEN", "base_env": "PLAUD_API_BASE",
"match_minutes": 15, "page_size": 50, "pending_days": 30}.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

from collectors.base import Collector, Event, run_enabled
from core import config, db, timeutil as tu

UTC = timezone.utc
PENDING_DAYS = 30        # столько ждём транскрипт записи, потом забываем
MAX_PAGES = 20           # страниц списка за прогон
DEEP_SCAN_MINUTES = 60   # как часто листать список до конца срока ожидания


def _parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class PlaudError(RuntimeError):
    pass


class PlaudAPI:
    """Все знания о веб-API Plaud в одном месте."""

    LIST_PATH = "/file/simple/web"     # GET: список записей, ключ data_file_list
    DETAIL_PATH = "/file/list"         # POST [ids]: полные записи с trans_result и ai_content

    def __init__(self, token: str, base: str = "https://api.plaud.ai",
                 transport: httpx.BaseTransport | None = None):
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=60, transport=transport, headers={
            "Authorization": f"bearer {token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://web.plaud.ai",
            "Referer": "https://web.plaud.ai/",
            "app-platform": "web",
            "edit-from": "web",
        })

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code == 401:
            raise PlaudError("Plaud 401: токен истёк, возьми новый в web.plaud.ai (DevTools, Authorization)")
        if r.status_code >= 400:
            raise PlaudError(f"Plaud {r.request.url.path}: {r.status_code} {r.text[:200]}")
        data = r.json()
        if isinstance(data, dict) and (data.get("status") == -302 or data.get("msg") == "user region mismatch"):
            raise PlaudError("Plaud: аккаунт в другом регионе, задай PLAUD_API_BASE (например https://api-euc1.plaud.ai)")
        return data

    def list_recordings(self, skip: int = 0, limit: int = 50) -> list[dict]:
        """Записи от новых к старым по времени начала."""
        r = self.http.get(self.LIST_PATH, params={"skip": skip, "limit": limit, "is_trash": 0,
                                                   "sort_by": "start_time", "is_desc": "true",
                                                   "r": random.random()})
        return self._check(r).get("data_file_list") or []

    def get_details(self, file_ids: list[str]) -> list[dict]:
        if not file_ids:
            return []
        r = self.http.post(self.DETAIL_PATH, json=file_ids)
        return self._check(r).get("data_file_list") or []

    # ---- разбор полей записи ----
    @staticmethod
    def file_id(rec: dict) -> str:
        return str(rec.get("id") or rec.get("file_id"))

    @staticmethod
    def started_at(rec: dict) -> datetime | None:
        ts = rec.get("start_time")
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts / 1000, UTC)
        for key in ("created_at", "start_time"):
            v = rec.get(key)
            if isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
                except ValueError:
                    pass
        return None

    @staticmethod
    def duration_ms(rec: dict) -> int:
        return int(rec.get("duration") or rec.get("duration_ms") or 0)

    @staticmethod
    def transcript_ready(rec: dict) -> bool:
        """Флаг из списка: транскрипт уже есть. Нет флага: проверим по самому транскрипту."""
        return bool(rec.get("is_trans")) if "is_trans" in rec else True

    @staticmethod
    def segments(detail: dict) -> list[dict]:
        """[{speaker, text, start_ms, end_ms}] из trans_result."""
        raw = detail.get("trans_result") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        if isinstance(raw, dict):
            raw = raw.get("segments") or raw.get("list") or []
        out = []
        for s in raw if isinstance(raw, list) else []:
            if not isinstance(s, dict):
                continue
            text = (s.get("content") or s.get("text") or "").strip()
            if text:
                out.append({"speaker": (s.get("speaker") or "").strip() or "Спикер",
                            "text": text, "start_ms": s.get("start_time"), "end_ms": s.get("end_time")})
        return out

    @staticmethod
    def summary(detail: dict) -> str | None:
        raw = detail.get("ai_content")
        if not raw and detail.get("summary_list"):
            raw = detail["summary_list"][0]
        if not raw:
            return None
        if isinstance(raw, dict):
            c = raw.get("markdown") or raw.get("content")
            if isinstance(c, dict):
                c = c.get("markdown")
            return c if isinstance(c, str) and c else None
        if not isinstance(raw, str):
            return str(raw)
        if raw.lstrip().startswith("{"):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(obj, dict):
                for cand in (obj.get("markdown"),
                             (obj.get("content") or {}).get("markdown") if isinstance(obj.get("content"), dict) else None,
                             obj.get("summary")):
                    if isinstance(cand, str) and cand:
                        return cand
        return raw


def format_transcript(segments: list[dict]) -> str:
    """«Спикер: текст», подряд идущие реплики одного спикера склеены."""
    lines: list[list[str]] = []
    for s in segments:
        if lines and lines[-1][0] == s["speaker"]:
            lines[-1][1] += " " + s["text"]
        else:
            lines.append([s["speaker"], s["text"]])
    return "\n".join(f"{sp}: {tx}" for sp, tx in lines)


MATCH_SQL = """
SELECT f.id, f.context_id, f.title, f.starts_at
FROM work.calendar_feed f JOIN work.events e ON e.id = f.id
WHERE f.status <> 'cancelled' AND NOT f.all_day
  AND f.starts_at < %(end)s + make_interval(mins => %(pad)s)
  AND f.ends_at   > %(start)s - make_interval(mins => %(pad)s)
ORDER BY (jsonb_array_length(COALESCE(e.meta->'attendees', '[]'::jsonb)) > 0) DESC,
         abs(extract(epoch FROM f.starts_at - %(start)s))
LIMIT 1
"""


def match_calendar(conn, start: datetime, end: datetime, pad_minutes: int = 15) -> dict | None:
    return conn.execute(MATCH_SQL, {"start": start, "end": end, "pad": pad_minutes}).fetchone()


class PlaudCollector(Collector):
    kind = "plaud"

    def __init__(self, source: dict, transport: httpx.BaseTransport | None = None):
        super().__init__(source)
        self.cfg = source.get("config") or {}
        self.transport = transport

    def api(self) -> PlaudAPI:
        token = config.env(self.cfg.get("token_env") or "PLAUD_TOKEN", required=True)
        base = config.env(self.cfg.get("base_env") or "PLAUD_API_BASE", "https://api.plaud.ai")
        return PlaudAPI(token, base, transport=self.transport)

    def _known(self, conn, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        return {r["external_id"] for r in conn.execute(
            "SELECT external_id FROM work.events WHERE source_id = %s AND external_id = ANY(%s)",
            (self.source_id, ids)).fetchall()}

    def collect(self) -> Iterable[Event]:
        """Список от новых к старым. Запись, у которой ещё нет транскрипта, попадает в
        state.pending и перепроверяется через get_details каждый прогон, пока не
        дозреет или не станет старше pending_days (30). Запись старше метки, которой
        нет ни в базе, ни в pending, значит загружена поздно: её тоже берём.

        Обычный прогон листает, пока не встретит страницу, где всё старше метки и
        уже учтено (в базе или в pending). Поздняя загрузка может прятаться за такой
        страницей, поэтому не реже раза в deep_scan_minutes (60) прогон листает
        глубоко: до страницы, где всё старше 30 дней (state.scanned_at).
        state.floor = метка первого прогона: что записано раньше, до WorkOS, не тянем."""
        started = tu.now()
        cutoff = self.since(default_days=max(self.lookback.days, 3))
        st = self.state if isinstance(self.state, dict) else {}
        horizon = started - timedelta(days=int(self.cfg.get("pending_days") or PENDING_DAYS))
        floor = _parse_iso(st.get("floor")) or cutoff
        lower = max(floor, horizon)
        pending: dict[str, str] = {}
        for fid, iso in (st.get("pending") or {}).items():
            when = _parse_iso(iso)
            if when is None or when >= horizon:
                pending[str(fid)] = iso
        scanned = _parse_iso(st.get("scanned_at"))
        deep = scanned is None or started - scanned >= timedelta(
            minutes=int(self.cfg.get("deep_scan_minutes", DEEP_SCAN_MINUTES)))
        api = self.api()
        page = int(self.cfg.get("page_size") or 50)
        events: list[Event] = []
        with db.connect() as conn:
            candidates: dict[str, dict] = {}        # транскрипт по списку готов: за деталями
            not_ready: set[str] = set()              # список прямо сейчас говорит «ещё нет»
            recheck = set(pending)                   # ждали с прошлых прогонов
            skip = pages = 0
            while True:
                batch = api.list_recordings(skip=skip, limit=page)
                if not batch:
                    break
                known = self._known(conn, [api.file_id(r) for r in batch])
                settled = True                      # вся страница старше метки и уже учтена
                beyond = True                       # вся страница старше нижней границы
                for rec in batch:
                    fid = api.file_id(rec)
                    when = api.started_at(rec)
                    if when is None or when >= lower:
                        beyond = False
                    if when is None or when >= cutoff:
                        settled = False             # свежая: перечитываем всегда (сводка дозревает)
                    elif when < lower or fid in known or fid in pending:
                        continue                    # старое и уже учтённое (pending проверим ниже)
                    else:
                        settled = False             # поздняя загрузка: старая по времени, но новая
                    if api.transcript_ready(rec):
                        candidates[fid] = rec
                    elif fid not in known:
                        not_ready.add(fid)
                        pending.setdefault(fid, (when or started).isoformat())
                pages += 1
                if (beyond or (settled and not deep)) or len(batch) < page or pages >= MAX_PAGES:
                    break
                skip += page

            ids = list(dict.fromkeys([*candidates, *(p for p in pending if p in recheck and p not in not_ready)]))
            if ids:
                pad = int(self.cfg.get("match_minutes") or 15)
                details: dict[str, dict] = {}
                for i in range(0, len(ids), 20):
                    for d in api.get_details(ids[i:i + 20]):
                        details[api.file_id(d)] = d
                known = self._known(conn, ids)
                for fid in ids:
                    rec = {**candidates.get(fid, {}), **details.get(fid, {})}
                    if not rec:
                        continue                    # Plaud о записи молчит: подождём, срок ограничен
                    ev = self._event(api, conn, rec, pad, fid in known)
                    if ev:
                        events.append(ev)
                        pending.pop(fid, None)
                    elif fid not in known:
                        when = api.started_at(rec)
                        pending.setdefault(fid, (when or started).isoformat())
        self.state = {"cursor": started.isoformat(), "floor": floor.isoformat(), "pending": pending,
                      "scanned_at": started.isoformat() if deep else (scanned or started).isoformat()}
        return events

    def _event(self, api: PlaudAPI, conn, rec: dict, pad: int, known: bool = False) -> Event | None:
        segs = api.segments(rec)
        if not segs:
            return None                  # флаг есть, а текста ещё нет: подождём следующего прогона
        start = api.started_at(rec) or tu.now()
        dur = api.duration_ms(rec)
        end = start + timedelta(milliseconds=dur or 60_000)
        cal = match_calendar(conn, start, end, pad)
        ctx = cal["context_id"] if cal else "personal"
        speakers = list(dict.fromkeys(s["speaker"] for s in segs))
        meta = {
            "duration_ms": dur, "speakers": speakers, "segments_count": len(segs),
            "summary": api.summary(rec), "calendar_event_id": cal["id"] if cal else None,
            "calendar_title": cal["title"] if cal else None,
            "personal": ctx == "personal", "participant_ids": [],
        }
        if known:
            # уже записанная встреча: участников, привязку и личность могли уточнить
            # сведение людей и бот, их не трогаем, обновляем только то, что дозревает
            meta = {k: meta[k] for k in ("duration_ms", "speakers", "segments_count", "summary")}
        if meta.get("summary") is None:
            meta.pop("summary", None)
        return Event(external_id=api.file_id(rec), kind="meeting", occurred_at=start,
                     title=rec.get("filename") or (cal["title"] if cal else None),
                     body=format_transcript(segs), meta=meta, context_id=ctx,
                     thread_key=f"plaud:{api.file_id(rec)}")


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"plaud": run_enabled("plaud")}


if __name__ == "__main__":
    print(run())

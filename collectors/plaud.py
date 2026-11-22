"""Коллектор Plaud (шаг 2.3): записи встреч с готовым транскриптом.

Событие появляется, только когда транскрипт готов. Каждый прогон
перечитывает записи за lookback_days (по времени начала записи). Запись без
транскрипта запоминается в state.pending и перепроверяется каждый прогон до
30 дней, а запись, загруженная с телефона поздно (старая по времени, но новая
для нас), тоже подхватывается: см. collect().

Встреча сопоставляется с календарём: событие из work.calendar_feed,
пересекающееся с записью (±15 минут), даёт calendar_event_id и контекст.
Не нашлось встречи: контекст personal и meta.personal = true.

API: официальный API Plaud для своих данных (platform.plaud.ai/developer/api),
тот же, что у Plaud CLI (@plaud-ai/cli 0.3.x). Вход один раз на Маке
(`plaud login`), файл ~/.plaud/tokens.json переносится на дроплет, дальше
коллектор сам обновляет токен и перезаписывает файл. Все пути и ключи JSON
собраны в классе PlaudAPI: если Plaud что-то поменяет, править только его.

Секреты: файл токенов PLAUD_TOKENS_FILE (по умолчанию
/var/lib/workos-reader/.plaud/tokens.json, 600, владелец workos-reader).
Необязательно: PLAUD_PLATFORM_BASE (по умолчанию https://platform.plaud.ai/developer/api).
Настройки источника: {"tokens_env": "PLAUD_TOKENS_FILE", "base_env": "PLAUD_PLATFORM_BASE",
"match_minutes": 15, "page_size": 50, "pending_days": 30}.
Проверка: python -m collectors.plaud --probe (печатает только структуру, без текстов).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
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


class PlaudAuthError(PlaudError):
    """Plaud отверг вход: нужен новый plaud login на Маке."""


DEFAULT_BASE = "https://platform.plaud.ai/developer/api"
DEFAULT_TOKENS = "/var/lib/workos-reader/.plaud/tokens.json"
RELOGIN = ("Plaud: вход истёк или отозван. На Маке: plaud login, потом перенеси ~/.plaud/tokens.json "
           "на дроплет (SETUP, фаза 2, шаг Plaud)")


class TokenStore:
    """Файл токенов в формате Plaud CLI: access_token, refresh_token, token_type, expires_at (мс)."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise PlaudAuthError(f"Plaud: нет файла токенов {self.path}. " + RELOGIN) from None
        except (OSError, json.JSONDecodeError) as e:
            raise PlaudError(f"Plaud: не читается файл токенов {self.path}: {type(e).__name__}") from None
        if not isinstance(data, dict) or not data.get("access_token"):
            raise PlaudAuthError("Plaud: в файле токенов нет access_token. " + RELOGIN)
        return data

    def save(self, data: dict) -> None:
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tokens.", dir=d)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class PlaudAPI:
    """Все знания об API Plaud в одном месте."""

    LIST_PATH = "/open/third-party/files/"          # GET ?page&page_size: {data: [...], page}
    FILE_PATH = "/open/third-party/files/{id}"      # GET: запись с source_list и note_list
    ME_PATH = "/open/third-party/users/current"
    REFRESH_PATH = "/oauth/third-party/access-token/refresh"   # POST form refresh_token
    TRANSCRIPT_BLOCK = "transaction"
    SUMMARY_NOTE = "auto_sum_note"
    SKEW_MS = 60_000

    def __init__(self, tokens_file: str, base: str = DEFAULT_BASE,
                 transport: httpx.BaseTransport | None = None):
        self.store = TokenStore(tokens_file)
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60, transport=transport,
                                 headers={"Accept": "application/json"})
        # содержимое блоков по data_link: подписанная ссылка на хранилище, без нашего токена
        self.blobs = httpx.Client(timeout=60, transport=transport, follow_redirects=True)
        self._tokens: dict | None = None

    # ---- вход ----
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def tokens(self) -> dict:
        if self._tokens is None:
            self._tokens = self.store.load()
        return self._tokens

    def refresh(self) -> dict:
        cur = self.tokens()
        rt = cur.get("refresh_token")
        if not rt:
            raise PlaudAuthError("Plaud: в файле токенов нет refresh_token. " + RELOGIN)
        r = self.http.post(self.REFRESH_PATH, data={"refresh_token": rt})
        if r.status_code in (400, 401, 403):
            raise PlaudAuthError(f"Plaud отклонил обновление токена ({r.status_code}). " + RELOGIN)
        if r.status_code >= 400:
            raise PlaudError(f"Plaud: обновление токена не удалось: {r.status_code}")
        data = r.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise PlaudError("Plaud: в ответе обновления нет access_token")
        new = {"access_token": data["access_token"],
               "refresh_token": data.get("refresh_token") or rt,
               "token_type": data.get("token_type") or "Bearer"}
        if data.get("expires_in"):
            new["expires_at"] = self._now_ms() + int(data["expires_in"]) * 1000
        self.store.save(new)
        self._tokens = new
        return new

    def access_token(self) -> str:
        t = self.tokens()
        exp = t.get("expires_at")
        if isinstance(exp, (int, float)) and self._now_ms() > exp - self.SKEW_MS:
            t = self.refresh()
        return t["access_token"]

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        for attempt in (0, 1):
            r = self.http.get(path, params=params,
                              headers={"Authorization": f"Bearer {self.access_token()}"})
            if r.status_code == 401 and attempt == 0:
                self.refresh()                       # токен отозвали раньше срока: одна попытка
                continue
            return r
        return r

    def _check(self, r: httpx.Response):
        if r.status_code == 401:
            raise PlaudAuthError("Plaud 401. " + RELOGIN)
        if r.status_code >= 400:
            raise PlaudError(f"Plaud {r.request.url.path}: {r.status_code} {r.text[:200]}")
        return r.json()

    # ---- данные ----
    def me(self) -> dict:
        return self._check(self._get(self.ME_PATH)) or {}

    def list_recordings(self, skip: int = 0, limit: int = 50) -> list[dict]:
        """Записи от новых к старым (как их отдаёт Plaud и как читает CLI)."""
        limit = max(1, min(100, int(limit)))
        page = skip // limit + 1
        data = self._check(self._get(self.LIST_PATH, {"page": page, "page_size": limit}))
        items = data.get("data") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    def get_file(self, file_id: str) -> dict | None:
        r = self._get(self.FILE_PATH.format(id=file_id))
        if r.status_code == 404:
            return None
        data = self._check(r)
        if isinstance(data, dict) and isinstance(data.get("data"), dict) and "id" not in data:
            data = data["data"]
        return data if isinstance(data, dict) else None

    def block_text(self, block: dict | None) -> str:
        if not isinstance(block, dict):
            return ""
        inline = block.get("data_content")
        if isinstance(inline, str) and inline:
            return inline
        link = block.get("data_link")
        if isinstance(link, str) and link.startswith("https://"):
            r = self.blobs.get(link)
            if r.status_code >= 400:
                raise PlaudError(f"Plaud: содержимое блока не скачалось: {r.status_code}")
            return r.text
        return ""

    def get_details(self, file_ids: list[str]) -> list[dict]:
        """Полные записи; транскрипт и сводка разложены в trans_result и ai_content."""
        out = []
        for fid in file_ids:
            f = self.get_file(fid)
            if not f:
                continue
            src = next((b for b in f.get("source_list") or []
                        if isinstance(b, dict) and b.get("data_type") == self.TRANSCRIPT_BLOCK), None)
            note = next((n for n in f.get("note_list") or []
                         if isinstance(n, dict) and n.get("data_type") == self.SUMMARY_NOTE), None)
            f["trans_result"] = self.block_text(src) if src else []
            summary = self.block_text(note) if note else ""
            if summary:
                f["ai_content"] = summary
            out.append(f)
        return out

    # ---- разбор полей записи ----
    @staticmethod
    def file_id(rec: dict) -> str:
        return str(rec.get("id") or rec.get("file_id"))

    @staticmethod
    def started_at(rec: dict) -> datetime | None:
        for key in ("start_at", "start_time"):
            ts = rec.get(key)
            if isinstance(ts, (int, float)) and ts > 0:
                return datetime.fromtimestamp(ts / 1000 if ts > 10**11 else ts, UTC)
        for key in ("start_at", "created_at", "start_time"):
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
        return make_api(self.cfg, self.transport)

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
                     title=rec.get("name") or rec.get("filename") or (cal["title"] if cal else None),
                     body=format_transcript(segs), meta=meta, context_id=ctx,
                     thread_key=f"plaud:{api.file_id(rec)}")


def make_api(cfg: dict | None = None, transport: httpx.BaseTransport | None = None) -> PlaudAPI:
    cfg = cfg or {}
    tokens = config.env(cfg.get("tokens_env") or "PLAUD_TOKENS_FILE", DEFAULT_TOKENS)
    base = config.env(cfg.get("base_env") or "PLAUD_PLATFORM_BASE", DEFAULT_BASE)
    return PlaudAPI(tokens, base, transport=transport)


def _jwt_exp(tok) -> str:
    try:
        import base64
        part = str(tok).split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return datetime.fromtimestamp(payload["exp"], UTC).isoformat()
    except Exception:
        return "неизвестно"


def _shape(v):
    if isinstance(v, dict):
        return {k: _shape(x) if isinstance(x, (dict, list)) else type(x).__name__ for k, x in v.items()}
    if isinstance(v, list):
        return [_shape(v[0])] if v and isinstance(v[0], (dict, list)) else f"list[{len(v)}]"
    return type(v).__name__


def probe(transport: httpx.BaseTransport | None = None) -> dict:
    """Проверка входа и формы ответов. Тексты, имена и токены не печатает."""
    api = make_api(transport=transport)
    t = api.tokens()
    out = {"access_exp": _jwt_exp(t.get("access_token")), "refresh_exp": _jwt_exp(t.get("refresh_token"))}
    me = api.me()
    out["me_keys"] = sorted(me.keys()) if isinstance(me, dict) else type(me).__name__
    items = api.list_recordings(0, 10)
    out["list_count"] = len(items)
    if items:
        first = items[0]
        out["list_item"] = _shape(first)
        out["first_started_at"] = str(api.started_at(first))
        f = api.get_file(api.file_id(first)) or {}
        out["file_keys"] = sorted(f.keys())
        out["source_types"] = [b.get("data_type") for b in f.get("source_list") or [] if isinstance(b, dict)]
        out["note_types"] = [n.get("data_type") for n in f.get("note_list") or [] if isinstance(n, dict)]
        d = api.get_details([api.file_id(first)])
        segs = api.segments(d[0]) if d else []
        out["segments"] = len(segs)
        out["summary_chars"] = len(api.summary(d[0]) or "") if d else 0
    out["tokens_after"] = {"access_exp": _jwt_exp(api.tokens().get("access_token"))}
    return out


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"plaud": run_enabled("plaud")}


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
    else:
        print(run())

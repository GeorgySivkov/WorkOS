"""Приёмник событий с Мака (шаг 7.2): А-Чат читается локально и POST-ится сюда.

Крошечный HTTP-сервер на stdlib, работает под workos-reader (роль базы
workos_reader). Слушает INGEST_BIND (по умолчанию 127.0.0.1:8788; на
дроплете ставится tailnet-адрес, наружу порт не открывается).

    POST /ingest/<source_id>
    Authorization: Bearer <INGEST_TOKEN>
    {"events": [{"external_id", "kind", "occurred_at" (ISO), "title", "body",
                 "actor_raw", "thread_key", "meta"}], "watermark": "..."}

Принимается только для включённых источников вида 'achat'. Запись
идемпотентна (collectors.base.upsert_events), last_ok_at и watermark
обновляются. Тело запроса не больше 2 МБ. kind всегда 'message' (другое: 400),
в meta только известные ключи А-Чата со строками и числами (META_KEYS), остальное: 400,
чтобы снаружи нельзя было подложить служебные метки (plan_paste, domain, action_id и т.п.).

Запуск: python workctl.py ingest  или  python -m collectors.ingest_server
"""
from __future__ import annotations

import hmac
import json
import math
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collectors.base import Event, upsert_events
from core import config, db

UTC = timezone.utc
MAX_BODY = 2 * 1024 * 1024
MAX_EVENTS = 5000
PATH_RX = re.compile(r"^/ingest/([A-Za-z0-9:_\-.]{1,100})/?$")
ALLOWED_KINDS = {"achat"}
EVENT_KIND = "message"
# что шлёт mac/achat_collector.py: чат, автор, режим чтения и идентификаторы приложения
META_KEYS = {"chat", "author", "mode", "chat_id", "author_id", "message_id", "app", "bundle_id"}
META_STR_LIMIT = 300


class BadRequest(ValueError):
    pass


def _str(v, field: str, limit: int, required: bool = False) -> str | None:
    if v is None or v == "":
        if required:
            raise BadRequest(f"нет поля {field}")
        return None
    if not isinstance(v, (str, int, float)):
        raise BadRequest(f"поле {field} должно быть строкой")
    return str(v)[:limit]


def _meta(raw) -> dict:
    """meta только из белого списка ключей, значения строки или числа (или null)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BadRequest("meta должна быть объектом")
    out = {}
    for k, v in raw.items():
        if k not in META_KEYS:
            raise BadRequest(f"meta: ключ {str(k)[:40]} не принимается")
        if v is None:
            out[k] = None
        elif (isinstance(v, bool) or not isinstance(v, (str, int, float))
              or (isinstance(v, float) and not math.isfinite(v))):
            raise BadRequest(f"meta.{k} должно быть строкой или числом")
        else:
            out[k] = v[:META_STR_LIMIT] if isinstance(v, str) else v
    return out


def parse_event(raw: dict) -> Event:
    if not isinstance(raw, dict):
        raise BadRequest("событие должно быть объектом")
    occurred = _str(raw.get("occurred_at"), "occurred_at", 64, required=True)
    try:
        dt = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    except ValueError as e:
        raise BadRequest(f"occurred_at не ISO: {occurred}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    kind = _str(raw.get("kind"), "kind", 32) or EVENT_KIND
    if kind != EVENT_KIND:
        raise BadRequest(f"kind {kind} не принимается, только {EVENT_KIND}")
    meta = _meta(raw.get("meta"))
    return Event(external_id=_str(raw.get("external_id"), "external_id", 300, required=True),
                 kind=kind, occurred_at=dt,
                 title=_str(raw.get("title"), "title", 1000),
                 body=_str(raw.get("body"), "body", 200_000),
                 meta=meta,
                 actor_raw=_str(raw.get("actor_raw"), "actor_raw", 300),
                 thread_key=_str(raw.get("thread_key"), "thread_key", 300))


def ingest(source_id: str, payload: dict) -> tuple[int, dict]:
    """Записать пачку событий. Возвращает (HTTP-код, ответ)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("events", []), list):
        return 400, {"error": "ожидается {\"events\": [...]}"}
    raw_events = payload.get("events") or []
    if len(raw_events) > MAX_EVENTS:
        return 413, {"error": f"не больше {MAX_EVENTS} событий за раз"}
    try:
        events = [parse_event(e) for e in raw_events]
    except BadRequest as e:
        return 400, {"error": str(e)}
    wm = payload.get("watermark")
    if wm is not None and not isinstance(wm, (str, int, float)):
        return 400, {"error": "watermark должна быть строкой"}
    with db.connect() as conn, conn.transaction():
        src = conn.execute("SELECT * FROM work.sources WHERE id = %s FOR UPDATE", (source_id,)).fetchone()
        if not src:
            return 404, {"error": "нет такого источника"}
        if src["kind"] not in ALLOWED_KINDS:
            return 403, {"error": "этот источник не принимает события снаружи"}
        if not src["enabled"]:
            return 403, {"error": "источник выключен"}
        ins, upd, seen = upsert_events(conn, src, events)
        conn.execute("UPDATE work.sources SET watermark = COALESCE(%s, watermark), last_ok_at = now(), "
                     "last_error = NULL WHERE id = %s",
                     (str(wm)[:500] if wm is not None else None, source_id))
    return 200, {"inserted": ins, "updated": upd, "seen": len(seen)}


def make_handler(token: str):
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "workos-ingest"
        sys_version = ""

        def _send(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):      # без заголовков и тел: там токен и переписка
            sys.stderr.write(f"ingest {self.address_string()} {fmt % args}\n")

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            auth = (self.headers.get("Authorization") or "").encode()
            if not hmac.compare_digest(auth, expected):
                return self._send(401, {"error": "unauthorized"})
            m = PATH_RX.match(self.path.split("?", 1)[0])
            if not m:
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "Content-Length"})
            if length <= 0:
                return self._send(400, {"error": "пустое тело"})
            if length > MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": "тело больше 2 МБ"})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, {"error": "не JSON"})
            try:
                code, body = ingest(m.group(1), payload)
            except Exception as e:  # noqa: BLE001: ответить Маку и записать ошибку источнику
                try:
                    with db.connect(autocommit=True) as conn:
                        conn.execute("UPDATE work.sources SET last_error = %s WHERE id = %s AND kind = 'achat'",
                                     (f"ingest: {type(e).__name__}: {e}"[:1500], m.group(1)))
                except Exception:  # noqa: BLE001
                    pass
                return self._send(500, {"error": type(e).__name__})
            return self._send(code, body)

    return Handler


def make_server(bind: str | None = None, token: str | None = None) -> ThreadingHTTPServer:
    token = token or config.env("INGEST_TOKEN", required=True)
    if len(token) < 16:
        raise RuntimeError("INGEST_TOKEN слишком короткий: сгенерируй openssl rand -hex 32")
    bind = bind or config.env("INGEST_BIND", "127.0.0.1:8788")
    host, _, port = bind.rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(token))
    srv.daemon_threads = True
    return srv


def serve(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    srv = make_server(kw.get("bind"), kw.get("token"))
    host, port = srv.server_address[:2]
    print(f"ingest слушает {host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return {"ingest": "stopped"}


if __name__ == "__main__":
    serve()

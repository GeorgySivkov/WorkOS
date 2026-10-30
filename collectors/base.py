"""Каркас коллекторов (шаг 2.1).

Два правила, которые нельзя нарушать:
1. Коллектор не удаляет и не переписывает body задним числом. Он вставляет,
   дополняет meta и заполняет body, только если тот был пуст (транскрипт
   Plaud дозревает). У календарных событий обновляются время и название:
   перенос встречи — это факт, а не правка истории.
2. Водяная метка идёт по прибытию, а не по времени события, и каждый прогон
   перекрывает lookback_days назад.

Токены коллекторов — только read-only скоупы, лежат в .env.reader.
"""
from __future__ import annotations

import importlib
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from core import db, timeutil as tu


@dataclass
class Event:
    external_id: str
    kind: str                       # email|meeting|message|card|calendar|note
    occurred_at: datetime
    title: str | None = None
    body: str | None = None
    meta: dict = field(default_factory=dict)
    actor_raw: str | None = None
    thread_key: str | None = None
    context_id: str | None = None   # если не задан, берётся из sources.context_id


class Collector:
    kind = "base"

    def __init__(self, source: dict):
        self.source = source
        self.source_id: str = source["id"]
        self.lookback = timedelta(days=source.get("lookback_days") or 0)
        raw = source.get("watermark")
        try:
            self.state = json.loads(raw) if raw and raw.strip().startswith("{") else {"cursor": raw}
        except json.JSONDecodeError:
            self.state = {"cursor": raw}

    # ---- для наследников ----
    def collect(self) -> Iterable[Event]:
        raise NotImplementedError

    def watermark(self) -> str | None:
        """Новая метка после прогона. По умолчанию — весь state в JSON."""
        return json.dumps(self.state, ensure_ascii=False, default=str) if self.state else None

    def finalize(self, conn, seen: set[str]) -> None:
        """Хук после записи: например, пометить исчезнувшие из ленты встречи отменёнными."""

    def since(self, default_days: int = 7) -> datetime:
        """Нижняя граница по прибытию: метка минус lookback, либо default_days назад."""
        cur = self.state.get("cursor") if isinstance(self.state, dict) else None
        try:
            base = datetime.fromisoformat(cur) if cur else None
        except (TypeError, ValueError):
            base = None
        if base is None:
            return tu.now() - timedelta(days=default_days)
        return base - self.lookback


UPSERT = """
INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at,
                         actor_raw, title, body, meta, thread_key)
VALUES (%(context_id)s, %(source_id)s, %(external_id)s, %(kind)s, %(occurred_at)s,
        %(actor_raw)s, %(title)s, %(body)s, %(meta)s, %(thread_key)s)
ON CONFLICT (source_id, external_id) DO UPDATE SET
  meta        = work.events.meta || EXCLUDED.meta,
  body        = COALESCE(work.events.body, EXCLUDED.body),
  title       = CASE WHEN work.events.kind = 'calendar' THEN EXCLUDED.title
                     ELSE COALESCE(work.events.title, EXCLUDED.title) END,
  occurred_at = CASE WHEN work.events.kind = 'calendar' THEN EXCLUDED.occurred_at
                     ELSE work.events.occurred_at END,
  actor_raw   = COALESCE(work.events.actor_raw, EXCLUDED.actor_raw),
  thread_key  = COALESCE(work.events.thread_key, EXCLUDED.thread_key)
WHERE work.events.meta IS DISTINCT FROM work.events.meta || EXCLUDED.meta
   OR (work.events.body IS NULL AND EXCLUDED.body IS NOT NULL)
   OR (work.events.kind = 'calendar' AND (work.events.title IS DISTINCT FROM EXCLUDED.title
                                          OR work.events.occurred_at <> EXCLUDED.occurred_at))
RETURNING (xmax = 0) AS inserted
"""


def upsert_events(conn, source: dict, events: Iterable[Event]) -> tuple[int, int, set[str]]:
    ins = upd = 0
    seen: set[str] = set()
    for ev in events:
        seen.add(ev.external_id)
        row = conn.execute(UPSERT, {
            "context_id": ev.context_id or source.get("context_id") or "personal",
            "source_id": source["id"], "external_id": ev.external_id, "kind": ev.kind,
            "occurred_at": ev.occurred_at, "actor_raw": ev.actor_raw, "title": ev.title,
            "body": ev.body, "meta": db.jb(ev.meta or {}), "thread_key": ev.thread_key,
        }).fetchone()
        if row is None:
            continue
        if row["inserted"]:
            ins += 1
        else:
            upd += 1
    return ins, upd, seen


REGISTRY = {
    "trello": "collectors.trello:TrelloCollector",
    "plaud": "collectors.plaud:PlaudCollector",
    "ical": "collectors.calendar:ICalCollector",
    "eas": "collectors.eas:EASCollector",
    "slack": "collectors.slack:SlackCollector",
    "gmail": "collectors.gmail:GmailCollector",
}


def collector_for(source: dict) -> Collector:
    path = REGISTRY.get(source["kind"])
    if not path:
        raise KeyError(f"нет коллектора для вида {source['kind']}")
    mod, cls = path.split(":")
    return getattr(importlib.import_module(mod), cls)(source)


def run_source(source_id: str, collector: Collector | None = None) -> dict:
    """Один прогон одного источника: собрать, записать, сдвинуть метку."""
    with db.connect() as conn:
        src = conn.execute("SELECT * FROM work.sources WHERE id = %s", (source_id,)).fetchone()
        if not src:
            raise KeyError(source_id)
    col = collector or collector_for(src)
    try:
        events = list(col.collect())
        with db.connect() as conn, conn.transaction():
            ins, upd, seen = upsert_events(conn, src, events)
            col.finalize(conn, seen)
            conn.execute("UPDATE work.sources SET watermark = %s, last_ok_at = now(), last_error = NULL "
                         "WHERE id = %s", (col.watermark(), source_id))
        return {"source": source_id, "inserted": ins, "updated": upd, "seen": len(seen)}
    except Exception as e:  # noqa: BLE001 — ошибку пишем в sources, heartbeat её увидит
        with db.connect(autocommit=True) as conn:
            conn.execute("UPDATE work.sources SET last_error = %s WHERE id = %s",
                         (f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", source_id))
        raise


def run_enabled(kind: str | None = None) -> list[dict]:
    """Все включённые источники, кроме тех, у кого свой процесс:
    telegram (бот), achat (приём с Мака), eas (сервис eas-watch на Ping)."""
    with db.connect() as conn:
        q = "SELECT id FROM work.sources WHERE enabled AND kind NOT IN ('telegram','achat','eas')"
        rows = conn.execute(q + (" AND kind = %s" if kind else "") + " ORDER BY id",
                            (kind,) if kind else ()).fetchall()
    out = []
    for r in rows:
        try:
            out.append(run_source(r["id"]))
        except Exception as e:  # noqa: BLE001
            out.append({"source": r["id"], "error": str(e)[:300]})
    return out

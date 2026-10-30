#!/usr/bin/env python3
"""Коллектор А-Чата на Маке (шаг 7.2). Только stdlib: работает на системном python3 (3.9+).

Читает локальный стор А-Чата и отправляет новые сообщения на дроплет
(collectors/ingest_server.py). Живой файл базы не читается никогда:
сначала снапшот через sqlite3 backup API во временную копию, потом
запрос к копии, копия удаляется.

Настройки: ~/.workos/achat.json (chmod 600, там токен)
{
  "mode": "sqlite",                      // или "notifications" (фолбэк)
  "db_path": "~/Library/Containers/<bundle-id>/Data/.../store.sqlite",
  "query": "SELECT id, ts, chat, author, text FROM ... WHERE ts > :since ORDER BY ts",
  "ts_format": "auto",                   // auto | unix | unix_ms | coredata | coredata_ns | iso
  "bundle_id": "ru.example.achat",       // для режима notifications
  "ingest_url": "http://100.x.y.z:8788",
  "token": "<INGEST_TOKEN с дроплета>",
  "source_id": "achat:alfa",
  "batch_size": 200, "overlap_minutes": 60, "initial_days": 2
}
Запрос должен вернуть колонки id, ts, chat, author, text (именно с такими
именами, через AS). Параметр :since необязателен: если он есть, в него
подставится метка в единицах ts_format, иначе фильтр по времени сделает скрипт.

Метка: ~/.workos/achat.state. Сдвигается только после успешной отправки,
каждый прогон перекрывает overlap_minutes назад (на дроплете запись идемпотентна).

    python3 achat_collector.py              # один прогон
    python3 achat_collector.py --dry-run    # показать, что ушло бы, ничего не отправлять
    python3 achat_collector.py --list-apps  # bundle id из базы уведомлений
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

UTC = timezone.utc
HOME = Path.home()
CONFIG = HOME / ".workos" / "achat.json"
STATE = HOME / ".workos" / "achat.state"
NOTIF_DB = HOME / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
COREDATA_EPOCH = 978307200          # 2001-01-01 в unix-секундах
MAX_BATCH_BYTES = 1_500_000         # приёмник режет на 2 МБ


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ---------- время ----------
def to_dt(v, fmt: str = "auto") -> datetime | None:
    if v is None or v == "":
        return None
    if fmt == "iso" or (fmt == "auto" and isinstance(v, str) and not _is_number(v)):
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    x = float(v)
    if fmt == "auto":
        if x > 1e17:
            fmt = "coredata_ns"
        elif x > 1e11:
            fmt = "unix_ms"
        elif x < 1e9:
            fmt = "coredata"
        else:
            fmt = "unix"
    if fmt == "unix":
        return datetime.fromtimestamp(x, UTC)
    if fmt == "unix_ms":
        return datetime.fromtimestamp(x / 1000, UTC)
    if fmt == "coredata":
        return datetime.fromtimestamp(COREDATA_EPOCH + x, UTC)
    if fmt == "coredata_ns":
        return datetime.fromtimestamp(COREDATA_EPOCH + x / 1e9, UTC)
    raise ValueError(f"неизвестный ts_format {fmt}")


def from_dt(dt: datetime, fmt: str):
    """Метка в единицах колонки ts (для :since в запросе)."""
    u = dt.timestamp()
    if fmt in ("unix", "auto"):
        return u
    if fmt == "unix_ms":
        return int(u * 1000)
    if fmt == "coredata":
        return u - COREDATA_EPOCH
    if fmt == "coredata_ns":
        return int((u - COREDATA_EPOCH) * 1e9)
    if fmt == "iso":
        return dt.astimezone(UTC).isoformat()
    raise ValueError(f"неизвестный ts_format {fmt}")


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ---------- снапшот ----------
def snapshot(db_path: Path) -> Path:
    """Копия базы через backup API: читаем копию, а не живой файл с WAL посреди записи."""
    if not db_path.exists():
        raise FileNotFoundError(f"нет базы {db_path}")
    tmpdir = Path(tempfile.mkdtemp(prefix="workos-achat-"))
    os.chmod(tmpdir, 0o700)
    dst_path = tmpdir / "snapshot.db"
    src = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dst_path


def _drop(snap: Path) -> None:
    shutil.rmtree(snap.parent, ignore_errors=True)


# ---------- чтение ----------
def sqlite_rows(cfg: dict, since: datetime) -> list[dict]:
    fmt = cfg.get("ts_format") or "auto"
    query = cfg["query"]
    if ":since" in query and fmt == "auto":
        raise ValueError("в запросе есть :since, укажи ts_format явно (unix, unix_ms, coredata, coredata_ns, iso)")
    snap = snapshot(Path(os.path.expanduser(cfg["db_path"])))
    try:
        con = sqlite3.connect(str(snap))
        con.row_factory = sqlite3.Row
        params = {"since": from_dt(since, fmt)} if ":since" in query else {}
        rows = con.execute(query, params).fetchall()
        con.close()
    finally:
        _drop(snap)
    out = []
    for r in rows:
        keys = r.keys()
        get = (lambda k, i: r[k] if k in keys else (r[i] if len(keys) > i else None))
        ts = to_dt(get("ts", 1), fmt)
        if ts is None or ts <= since:
            continue
        text = get("text", 4)
        if text is None or str(text).strip() == "":
            continue
        out.append({"id": str(get("id", 0)), "ts": ts, "chat": _s(get("chat", 2)),
                    "author": _s(get("author", 3)), "text": str(text)})
    return out


NOTIF_SQL = """
SELECT r.rec_id AS rec_id, r.uuid AS uuid, r.delivered_date AS delivered, r.data AS data
FROM record r JOIN app a ON a.app_id = r.app_id
WHERE a.identifier = ? AND r.delivered_date > ?
ORDER BY r.delivered_date
"""


def notification_rows(cfg: dict, since: datetime) -> list[dict]:
    """Фолбэк: уведомления А-Чата из базы уведомлений macOS (нужен Full Disk Access)."""
    bundle = cfg.get("bundle_id")
    if not bundle:
        raise ValueError("для режима notifications нужен bundle_id")
    snap = snapshot(Path(os.path.expanduser(cfg.get("db_path") or str(NOTIF_DB))))
    try:
        con = sqlite3.connect(str(snap))
        con.row_factory = sqlite3.Row
        rows = con.execute(NOTIF_SQL, (bundle, from_dt(since, "coredata"))).fetchall()
        con.close()
    finally:
        _drop(snap)
    out = []
    for r in rows:
        try:
            obj = plistlib.loads(r["data"])
        except Exception:  # noqa: BLE001: незнакомый формат записи пропускаем
            continue
        req = obj.get("req") or {}
        title, subtitle, body = _s(req.get("titl")), _s(req.get("subt")), _s(req.get("body"))
        if not body:
            continue
        uid = r["uuid"].hex() if isinstance(r["uuid"], (bytes, bytearray)) else (r["uuid"] or r["rec_id"])
        out.append({"id": f"notif:{uid}", "ts": to_dt(r["delivered"], "coredata"),
                    "chat": title, "author": subtitle or title, "text": body})
    return out


def list_apps(cfg: dict) -> list[str]:
    snap = snapshot(Path(os.path.expanduser(cfg.get("notif_db_path") or str(NOTIF_DB))))
    try:
        con = sqlite3.connect(str(snap))
        apps = [r[0] for r in con.execute("SELECT identifier FROM app ORDER BY identifier")]
        con.close()
    finally:
        _drop(snap)
    return apps


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------- отправка ----------
def to_event(row: dict, mode: str) -> dict:
    chat = row.get("chat") or "А-Чат"
    return {
        "external_id": row["id"], "kind": "message", "occurred_at": row["ts"].astimezone(UTC).isoformat(),
        "title": chat, "body": row["text"], "actor_raw": row.get("author"),
        "thread_key": f"achat:{chat}", "meta": {"chat": chat, "author": row.get("author"), "mode": mode},
    }


def batches(events: list[dict], size: int):
    cur, cur_bytes = [], 0
    for e in events:
        n = len(json.dumps(e, ensure_ascii=False).encode())
        if cur and (len(cur) >= size or cur_bytes + n > MAX_BATCH_BYTES):
            yield cur
            cur, cur_bytes = [], 0
        cur.append(e)
        cur_bytes += n
    if cur:
        yield cur


def post(cfg: dict, events: list[dict], watermark: str) -> dict:
    base = cfg["ingest_url"].rstrip("/")
    url = base if "/ingest/" in base else f"{base}/ingest/{quote(cfg['source_id'], safe=':')}"
    data = json.dumps({"events": events, "watermark": watermark}, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode() or "{}")


# ---------- метка ----------
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log(f"внимание: {path} читают другие пользователи, сделай chmod 600 (там токен)")
    for key in ("ingest_url", "token", "source_id"):
        if not cfg.get(key):
            raise ValueError(f"в {path} нет {key}")
    return cfg


def run(cfg: dict, dry_run: bool = False) -> dict:
    mode = cfg.get("mode") or "sqlite"
    state = load_state()
    now = datetime.now(UTC)
    wm = to_dt(state.get("watermark"), "iso") if state.get("watermark") else None
    since = (wm - timedelta(minutes=int(cfg.get("overlap_minutes", 60)))) if wm \
        else now - timedelta(days=int(cfg.get("initial_days", 2)))
    rows = notification_rows(cfg, since) if mode == "notifications" else sqlite_rows(cfg, since)
    rows.sort(key=lambda r: r["ts"])
    events = [to_event(r, mode) for r in rows]
    if dry_run:
        for e in events[:50]:
            print(json.dumps(e, ensure_ascii=False))
        return {"rows": len(rows), "dry_run": True}
    sent = 0
    for chunk in batches(events, int(cfg.get("batch_size", 200))):
        chunk_wm = chunk[-1]["occurred_at"]
        post(cfg, chunk, chunk_wm)
        sent += len(chunk)
        save_state({"watermark": chunk_wm, "at": now.isoformat()})
    return {"rows": len(rows), "sent": sent}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="А-Чат -> WorkOS")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-apps", action="store_true", help="bundle id всех приложений в базе уведомлений")
    args = ap.parse_args(argv)
    try:
        if args.list_apps:
            cfg = json.loads(Path(args.config).read_text()) if Path(args.config).exists() else {}
            for a in list_apps(cfg):
                print(a)
            return 0
        cfg = load_config(Path(args.config))
        log(f"ok {json.dumps(run(cfg, args.dry_run), ensure_ascii=False)}")
        return 0
    except urllib.error.HTTPError as e:
        log(f"ошибка приёмника: HTTP {e.code} {e.read()[:200]!r}")
    except urllib.error.URLError as e:
        log(f"дроплет недоступен ({e.reason}): метка не сдвинута, повторю в следующий раз")
    except sqlite3.DatabaseError as e:
        log(f"ошибка базы: {e}. Если путь в ~/Library/Containers, дай python3 Full Disk Access")
    except Exception as e:  # noqa: BLE001
        log(f"ошибка: {type(e).__name__}: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

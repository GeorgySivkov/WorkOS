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
  "query": "SELECT Z_PK AS seq, ZSERVERID AS server_id, Z_PK AS id, ZDATE AS ts, ZCHAT AS chat,
            ZAUTHOR AS author, ZTEXT AS text FROM ZMESSAGE WHERE Z_PK > :seq ORDER BY Z_PK",
  "ts_format": "auto",                   // auto | unix | unix_ms | coredata | coredata_ns | iso
  "bundle_id": "ru.example.achat",       // для режима notifications
  "ingest_url": "http://100.x.y.z:8788",
  "token": "<INGEST_TOKEN с дроплета>",
  "source_id": "achat:alfa",
  "batch_size": 200, "overlap_minutes": 60, "initial_days": 2
}

Колонки запроса (имена именно такие, через AS):
  ts, chat, author, text   обязательны: время сообщения, чат, автор, текст;
  id                       локальный id строки (обязателен, если нет server_id);
  seq                      необязательно, но очень желательно: монотонный счётчик
                           вставки (rowid, Z_PK). С ним метка идёт по нему, а не по
                           времени: чат, который приложение докачало задним числом
                           (старые сообщения с новыми Z_PK), не теряется;
  server_id                необязательно: id сообщения на сервере А-Чата. С ним
                           external_id = srv:<server_id> и после переустановки
                           приложения (локальные id другие) дублей нет. Если id
                           уникален только внутри чата, склей в SQL:
                           ZCHATID || ':' || ZSERVERID AS server_id.
Параметры: :seq (последний отправленный seq, в первый прогон -1) и :since (метка
по времени в единицах ts_format). Оба необязательны, фильтр сделает и скрипт.
С колонкой seq фильтруй в WHERE по :seq, а не по :since, иначе SQL сам отрежет
докачанные старые сообщения. Первый прогон в любом случае берёт только последние
initial_days дней. Смена id на server_id один раз задвоит сообщения за overlap.

Метка: ~/.workos/achat.state ({"seq", "watermark", "at"}). Сдвигается только после
успешной отправки. Без seq каждый прогон перекрывает overlap_minutes назад (на
дроплете запись идемпотентна). Если нового нет, на дроплет всё равно уходит пустая
пачка: так дроплет видит, что Мак жив, и не шлёт «А-Чат молчит» в тихие выходные.

Нужен Full Disk Access для программы, которая запускает скрипт (python3, Terminal
или launchd): без него чтение ~/Library/Containers и базы уведомлений падает, и в
лог пишется, где это включить.

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
FDA_HINT = ("Дай Full Disk Access программе, которая запускает скрипт (python3, Terminal или "
            "launchd-агент): Системные настройки, Конфиденциальность и безопасность, Полный доступ к диску")
ACCESS_MARKERS = ("unable to open database", "authorization denied", "not authorized",
                  "operation not permitted", "permission denied")


class AccessDenied(PermissionError):
    """macOS (TCC) не пускает к файлу: нужен Full Disk Access."""


def _is_access_error(e: BaseException) -> bool:
    if isinstance(e, PermissionError):
        return True
    return isinstance(e, (sqlite3.Error, OSError)) and any(m in str(e).lower() for m in ACCESS_MARKERS)


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
    try:
        exists = db_path.exists()
    except PermissionError as e:
        raise AccessDenied(f"нет доступа к {db_path} ({e})") from e
    if not exists:
        raise FileNotFoundError(f"нет базы {db_path} (или macOS прячет её без Full Disk Access)")
    tmpdir = Path(tempfile.mkdtemp(prefix="workos-achat-"))
    os.chmod(tmpdir, 0o700)
    dst_path = tmpdir / "snapshot.db"
    try:
        src = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except (sqlite3.Error, OSError) as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if _is_access_error(e):
            raise AccessDenied(f"нет доступа к {db_path} ({e})") from e
        raise
    return dst_path


def _drop(snap: Path) -> None:
    shutil.rmtree(snap.parent, ignore_errors=True)


# ---------- чтение ----------
def _seq(v) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def sqlite_rows(cfg: dict, since: datetime, last_seq: int | None = None) -> dict:
    """Строки запроса к снапшоту. Возвращает {rows, max_seq, top_key, anchor_key}:
    rows: новые сообщения; max_seq: наибольший seq среди ВСЕХ строк запроса (None,
    если колонки seq нет); top_key: отпечаток строки с max_seq (якорь на следующий
    прогон); anchor_key: отпечаток строки с seq == last_seq в этом снапшоте.

    Есть seq и метка по нему: берём seq > last_seq, время не важно (докачанный
    задним числом чат приходит со старым ts, но новым seq). Иначе фильтр по ts > since.
    В :seq уходит last_seq - 1, чтобы строка-якорь тоже вернулась."""
    fmt = cfg.get("ts_format") or "auto"
    query = cfg["query"]
    if ":since" in query and fmt == "auto":
        raise ValueError("в запросе есть :since, укажи ts_format явно (unix, unix_ms, coredata, coredata_ns, iso)")
    snap = snapshot(Path(os.path.expanduser(cfg["db_path"])))
    try:
        con = sqlite3.connect(str(snap))
        con.row_factory = sqlite3.Row
        params: dict = {}
        if ":since" in query:
            params["since"] = from_dt(since, fmt)
        if ":seq" in query:
            params["seq"] = last_seq - 1 if last_seq is not None else -1
        rows = con.execute(query, params).fetchall()
        con.close()
    finally:
        _drop(snap)
    res: dict = {"rows": [], "max_seq": None, "top_key": None, "anchor_key": None}
    for r in rows:
        keys = r.keys()
        named = "seq" in keys or "server_id" in keys       # новые колонки: только по именам
        get = (lambda k, i: r[k] if k in keys else (r[i] if not named and len(keys) > i else None))
        seq = _seq(r["seq"]) if "seq" in keys else None
        ts = to_dt(get("ts", 1), fmt)
        server_id = _s(r["server_id"]) if "server_id" in keys else None
        local_id = get("id", 0)
        if server_id is None and local_id is None:
            raise ValueError("запрос должен вернуть колонку id или server_id")
        ext_id = f"srv:{server_id}" if server_id else str(local_id)
        if seq is not None:
            fp = f"{ext_id}|{ts.isoformat() if ts else ''}"
            if res["max_seq"] is None or seq > res["max_seq"]:
                res["max_seq"], res["top_key"] = seq, fp
            if seq == last_seq:
                res["anchor_key"] = fp
        if ts is None:
            continue
        if seq is not None and last_seq is not None:
            if seq <= last_seq:
                continue                                  # уже отправлено
        elif ts <= since:
            continue
        text = get("text", 4)
        if text is None or str(text).strip() == "":
            continue
        res["rows"].append({"id": ext_id, "seq": seq, "ts": ts, "chat": _s(get("chat", 2)),
                            "author": _s(get("author", 3)), "text": str(text)})
    return res


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


def batches(events: list, size: int, key=lambda e: e):
    """Пачки не больше size штук и MAX_BATCH_BYTES байт. key достаёт событие из элемента."""
    cur, cur_bytes = [], 0
    for e in events:
        n = len(json.dumps(key(e), ensure_ascii=False).encode())
        if cur and (len(cur) >= size or cur_bytes + n > MAX_BATCH_BYTES):
            yield cur
            cur, cur_bytes = [], 0
        cur.append(e)
        cur_bytes += n
    if cur:
        yield cur


def post(cfg: dict, events: list[dict], watermark: str | None) -> dict:
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
    last_seq = _seq(state.get("seq"))
    since = (wm - timedelta(minutes=int(cfg.get("overlap_minutes", 60)))) if wm \
        else now - timedelta(days=int(cfg.get("initial_days", 2)))
    anchor = state.get("anchor")
    top_key = None
    if mode == "notifications":
        rows, max_seq = notification_rows(cfg, since), None
    else:
        res = sqlite_rows(cfg, since, last_seq)
        if last_seq is not None and anchor and res["anchor_key"] != anchor:
            # строка-якорь пропала или стала другой: хранилище пересоздано (переустановка,
            # новый вход), Z_PK начались заново. Метку по seq сбрасываем, берём по времени
            log("хранилище А-Чата пересоздано: метка по seq сброшена, беру по времени с перекрытием")
            last_seq = None
            res = sqlite_rows(cfg, since, None)
        rows, max_seq, top_key = res["rows"], res["max_seq"], res["top_key"]
    by_seq = max_seq is not None
    rows.sort(key=(lambda r: (r["seq"] if r.get("seq") is not None else -1, r["ts"])) if by_seq
              else (lambda r: r["ts"]))
    pairs = [(r, to_event(r, mode)) for r in rows]
    if dry_run:
        for _, e in pairs[:50]:
            print(json.dumps(e, ensure_ascii=False))
        return {"rows": len(rows), "dry_run": True}
    sent = 0
    time_wm = wm
    seq_wm = last_seq
    chunk_anchor = anchor if last_seq is not None else None
    for chunk in batches(pairs, int(cfg.get("batch_size", 200)), key=lambda p: p[1]):
        chunk_ts = max(r["ts"] for r, _ in chunk)
        time_wm = chunk_ts if time_wm is None else max(time_wm, chunk_ts)
        with_seq = [r for r, _ in chunk if r.get("seq") is not None]
        if with_seq:
            top = max(with_seq, key=lambda r: r["seq"])
            if seq_wm is None or top["seq"] > seq_wm:
                seq_wm, chunk_anchor = top["seq"], f"{top['id']}|{top['ts'].isoformat()}"
        post(cfg, [e for _, e in chunk], time_wm.isoformat())
        sent += len(chunk)
        save_state(_state(time_wm, seq_wm, now, chunk_anchor))
    if max_seq is not None:
        seq_wm = max_seq                     # все строки запроса пройдены, включая пропущенные
    if not sent:
        # нового нет: пустая пачка = «Мак жив», дроплет обновит last_ok_at
        post(cfg, [], time_wm.isoformat() if time_wm else None)
    save_state(_state(time_wm, seq_wm, now, top_key if max_seq is not None else None))
    return {"rows": len(rows), "sent": sent}


def _state(time_wm: datetime | None, seq_wm: int | None, now: datetime, anchor: str | None = None) -> dict:
    st: dict = {"at": now.isoformat()}
    if time_wm is not None:
        st["watermark"] = time_wm.isoformat()
    if seq_wm is not None:
        st["seq"] = seq_wm
    if anchor:
        st["anchor"] = anchor
    return st


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
    except Exception as e:  # noqa: BLE001
        if _is_access_error(e):
            log(f"нет доступа к базе: {e}. {FDA_HINT}. Метка не сдвинута")
        elif isinstance(e, sqlite3.DatabaseError):
            log(f"ошибка базы: {e}. Если путь в ~/Library, проверь Full Disk Access: {FDA_HINT}")
        else:
            log(f"ошибка: {type(e).__name__}: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

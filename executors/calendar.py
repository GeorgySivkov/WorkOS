"""Писатель календаря (шаги 2.5 и 2.9, сторона календаря). Без модели и без апрува.

mirror(): встречи Инфатики и Альфы из work.calendar_feed копируются в отдельные
календари личного Google. Копия = название, время, место, «занят»; без участников
и описания (решение 21.10). id копии = mirror_id(источник, UID, вхождение), поэтому
повтор обновляет копию, а перенос встречи id не меняет. Зеркальные календари
принадлежат зеркалу: помеченное workos=mirror, чего нет в источнике, удаляется.
Непомеченное не трогается никогда.

blocks(): задачи со временем (work.tasks.planned_*) пишутся блоками в календарь
«План», id блока = mirror_id('task', id задачи, ''). Ручное действие в Google
побеждает автоматическое: перетащенный блок меняет время задачи (planned_by='manual'),
удалённый снимает с задачи время. Изменения читаются через syncToken, который
лежит в файле состояния в EXEC_STATE_DIR (по умолчанию /var/lib/workos-exec).

.env.executor: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_WRITE_REFRESH_TOKEN,
GCAL_MIRROR_MAP='{"infatica": "<id календаря>", "alfa": "<id календаря>"}',
GCAL_PLAN_CALENDAR=<id календаря «План»>, EXEC_STATE_DIR (необязательно).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from core import config, db, google
from core import timeutil as tu
from executors import base

log = logging.getLogger("executors.calendar")

API = "https://www.googleapis.com/calendar/v3"
UTC = timezone.utc
TOKEN_ENV = "GOOGLE_WRITE_REFRESH_TOKEN"
MIRROR_BACK = timedelta(days=7)
MIRROR_AHEAD = timedelta(days=60)
BLOCK_DAYS_BACK, BLOCK_DAYS_AHEAD = 1, 7
DONE_MARK = "✓ "
ALERT_EVERY = timedelta(hours=6)
NO_TITLE = "(без названия)"


def mirror_id(source: str, uid: str, recurrence_id: str = "") -> str:
    """Стабильный id копии: тот же для перенесённой встречи, другой для другого вхождения."""
    raw = hashlib.sha1(f"{source}|{uid}|{recurrence_id}".encode()).digest()
    return base64.b32hexencode(raw).decode().lower()  # 32 символа из 0-9 и a-v


def block_id(task_id: int) -> str:
    return mirror_id("task", str(task_id), "")


# ---------- состояние (syncToken, время алертов) ----------
_STATE_MEM: dict = {}


def _state_path() -> Path:
    return Path(config.env("EXEC_STATE_DIR", "/var/lib/workos-exec")) / "gcal_state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except FileNotFoundError:
        return json.loads(json.dumps(_STATE_MEM))
    except (OSError, ValueError) as e:
        log.warning("состояние календаря не читается (%s), начинаю с пустого", e)
        return json.loads(json.dumps(_STATE_MEM))


def save_state(state: dict) -> None:
    _STATE_MEM.clear()
    _STATE_MEM.update(json.loads(json.dumps(state)))
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except OSError as e:
        # без файла работает и так: держим в памяти процесса, а после
        # перезапуска изменения читаются по updatedMin
        log.warning("состояние календаря не сохраняется в %s: %s", path, e)


# ---------- Google Calendar ----------
class SyncTokenGone(Exception):
    pass


class GCal:
    def __init__(self, http):
        self.http = http

    @staticmethod
    def _url(cal: str, event_id: str | None = None) -> str:
        url = f"{API}/calendars/{quote(cal, safe='')}/events"
        return url + (f"/{quote(event_id, safe='')}" if event_id else "")

    def list(self, cal: str, params: dict) -> tuple[list[dict], str | None]:
        items: list[dict] = []
        page = None
        while True:
            q = dict(params)
            if page:
                q["pageToken"] = page
            r = base.request(self.http, "GET", self._url(cal), params=q)
            if "syncToken" in params and (r.status_code == 410 or (
                    r.status_code == 400 and "sync" in r.text.lower())):
                raise SyncTokenGone()
            base.check(r, "Google Calendar: список событий")
            data = r.json()
            items.extend(data.get("items") or [])
            page = data.get("nextPageToken")
            if not page:
                return items, data.get("nextSyncToken")

    def insert(self, cal: str, body: dict) -> dict | None:
        """None = такой id уже есть (или был удалён): 409."""
        r = base.request(self.http, "POST", self._url(cal), json=body)
        if r.status_code == 409:
            return None
        return base.check(r, "Google Calendar: создать событие").json()

    def get(self, cal: str, event_id: str) -> dict | None:
        r = base.request(self.http, "GET", self._url(cal, event_id))
        if r.status_code in (404, 410):
            return None
        return base.check(r, "Google Calendar: событие").json()

    def patch(self, cal: str, event_id: str, body: dict) -> dict:
        r = base.request(self.http, "PATCH", self._url(cal, event_id), json=body)
        return base.check(r, "Google Calendar: обновить событие").json()

    def delete(self, cal: str, event_id: str) -> bool:
        r = base.request(self.http, "DELETE", self._url(cal, event_id))
        if r.status_code in (404, 410):
            return False
        base.check(r, "Google Calendar: удалить событие")
        return True

    def list_marked(self, cal: str, kind: str, start: datetime, end: datetime) -> list[dict]:
        items, _ = self.list(cal, {"privateExtendedProperty": f"workos={kind}", "timeMin": rfc(start),
                                   "timeMax": rfc(end), "showDeleted": "false", "maxResults": 2500})
        return [ev for ev in items if marker(ev) == kind]


def rfc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def marker(ev: dict) -> str | None:
    return private(ev).get("workos")


def private(ev: dict) -> dict:
    return ((ev.get("extendedProperties") or {}).get("private")) or {}


def _when(dt: datetime) -> dict:
    return {"dateTime": tu.local(dt).isoformat(timespec="seconds"), "timeZone": str(config.TZ)}


def _parse_when(part: dict | None) -> datetime | date | None:
    if not part:
        return None
    if part.get("dateTime"):
        return base.parse_iso(part["dateTime"])
    if part.get("date"):
        return date.fromisoformat(part["date"])
    return None


def _same_when(a: dict | None, b: dict) -> bool:
    if "date" in b:
        return bool(a) and a.get("date") == b["date"] and not a.get("dateTime")
    got = _parse_when(a)
    return isinstance(got, datetime) and got == base.parse_iso(b["dateTime"])


def differs(ev: dict, body: dict) -> bool:
    """Отличается ли событие в Google от того, каким его хочет видеть база."""
    if ev.get("status") == "cancelled":
        return True
    if (ev.get("summary") or "") != (body.get("summary") or ""):
        return True
    if "location" in body and (ev.get("location") or "") != (body.get("location") or ""):
        return True
    if "transparency" in body and (ev.get("transparency") or "opaque") != body["transparency"]:
        return True
    if not _same_when(ev.get("start"), body["start"]) or not _same_when(ev.get("end"), body["end"]):
        return True
    have = private(ev)
    return any(have.get(k) != v for k, v in body["extendedProperties"]["private"].items())


def _insert_or_get(api: GCal, cal: str, body: dict, kind: str) -> tuple[str, dict | None]:
    """Вставить с нашим id. 409 (id уже есть или был удалён) -> прочитать событие.

    Возвращает ("inserted", None) | ("existing", событие) | ("foreign", событие)."""
    if api.insert(cal, body) is not None:
        return "inserted", None
    ev = api.get(cal, body["id"])
    if ev is None:
        raise base.ExecError(f"событие {body['id']}: 409 при вставке, но прочитать его не вышло")
    if ev.get("status") != "cancelled" and marker(ev) != kind:
        return "foreign", ev           # без нашей метки не трогаем никогда
    return "existing", ev


def _upsert(api: GCal, cal: str, body: dict, kind: str) -> str:
    """Вставить; на 409 поправить, только если отличается."""
    verdict, ev = _insert_or_get(api, cal, body, kind)
    if verdict != "existing":
        return verdict
    return _patch_if_changed(api, cal, ev, body)


def _patch_if_changed(api: GCal, cal: str, ev: dict, body: dict) -> str:
    if not differs(ev, body):
        return "unchanged"
    patch = {k: v for k, v in body.items() if k != "id"}
    patch["status"] = "confirmed"      # копия, удалённая раньше, оживает с тем же id
    api.patch(cal, ev["id"], patch)
    return "restored" if ev.get("status") == "cancelled" else "updated"


# ---------- 2.5 · зеркало ----------
def mirror_map() -> dict[str, str]:
    raw = config.env("GCAL_MIRROR_MAP")
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) and v
                                             for k, v in data.items()):
        raise ValueError("GCAL_MIRROR_MAP: ожидался JSON {context_id: calendarId}")
    return data


def mirror_body(row: dict) -> dict:
    eid = mirror_id(row["source_id"], row["uid"], row["recurrence_id"] or "")
    if row["all_day"]:
        d0 = tu.local(row["starts_at"]).date()
        d1 = tu.local(row["ends_at"]).date()
        if d1 <= d0:
            d1 = d0 + timedelta(days=1)
        start, end = {"date": d0.isoformat()}, {"date": d1.isoformat()}
    else:
        start, end = _when(row["starts_at"]), _when(max(row["ends_at"], row["starts_at"]))
    return {
        "id": eid,
        "summary": row["title"] or NO_TITLE,
        "location": row["location"] or "",
        "start": start,
        "end": end,
        "transparency": "opaque" if row["busy"] else "transparent",
        "extendedProperties": {"private": {"workos": "mirror", "src": row["source_id"]}},
    }


def mirror(conn=None, api: GCal | None = None, now: datetime | None = None) -> dict:
    cmap = mirror_map()
    if not cmap:
        return {"skipped": "GCAL_MIRROR_MAP не задан"}
    now = now or tu.now()
    win0, win1 = now - MIRROR_BACK, now + MIRROR_AHEAD
    by_cal: dict[str, list[str]] = {}
    for ctx, cal in cmap.items():
        by_cal.setdefault(cal, []).append(ctx)
    own_conn = conn is None
    conn = conn or db.connect(autocommit=True)
    own_api = api is None
    api = api or GCal(google.client(TOKEN_ENV, transport=base.transport()))
    out = {"desired": 0, "inserted": 0, "updated": 0, "restored": 0, "unchanged": 0,
           "deleted": 0, "foreign": 0, "errors": []}
    try:
        for cal, contexts in by_cal.items():
            rows = conn.execute(
                """SELECT source_id, uid, recurrence_id, title, starts_at, ends_at, all_day, location, busy
                   FROM work.calendar_feed
                   WHERE context_id = ANY(%s) AND status <> 'cancelled'
                     AND ends_at > %s AND starts_at < %s
                   ORDER BY starts_at, id""", (contexts, win0, win1)).fetchall()
            desired = {}
            for row in rows:
                body = mirror_body(row)
                desired[body["id"]] = body
            out["desired"] += len(desired)
            existing = {ev["id"]: ev for ev in api.list_marked(cal, "mirror", win0, win1)}
            for eid, body in desired.items():
                try:
                    ev = existing.get(eid)
                    if ev is None:
                        verdict = _upsert(api, cal, body, "mirror")
                    else:
                        verdict = _patch_if_changed(api, cal, ev, body)
                    out[verdict] += 1
                except Exception as e:  # noqa: BLE001  одна битая встреча не роняет зеркало
                    out["errors"].append(f"{body['summary'][:40]}: {e}"[:300])
            for eid, ev in existing.items():
                if eid in desired or marker(ev) != "mirror":
                    continue
                try:
                    api.delete(cal, eid)
                    out["deleted"] += 1
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"удаление {eid}: {e}"[:300])
    finally:
        if own_conn:
            conn.close()
        if own_api:
            api.http.close()
    return out


# ---------- 2.9 · блоки плана ----------
TASK_COLS = "id, title, planned_start, planned_end, planned_at, planned_by, block_synced_at, done_at, dropped_at"


def block_body(t: dict) -> dict:
    start, end = t["planned_start"], t["planned_end"]
    if end <= start:
        end = start + timedelta(minutes=config.SIZE_MINUTES["S"])
    return {
        "id": block_id(t["id"]),
        "summary": (DONE_MARK if t["done_at"] else "") + (t["title"] or NO_TITLE),
        "start": _when(start),
        "end": _when(end),
        "extendedProperties": {"private": {"workos": "block", "task_id": str(t["id"])}},
    }


def manual_change(t: dict, ev: dict) -> str | None:
    """Ручное действие в Google, которое надо принять в задачу: 'moved' | 'deleted' | None.

    Правило: блок изменён в Google после последней сверки, его время отличается
    от задачи, и в базе нет более свежего ручного изменения (ручное побеждает авто,
    из двух ручных побеждает последнее)."""
    synced = t["block_synced_at"]
    if synced is None or not ev.get("updated"):
        return None
    updated = base.parse_iso(ev["updated"])
    if updated <= synced:
        return None
    if t["planned_by"] == "manual" and t["planned_at"] and t["planned_at"] >= updated:
        return None
    if ev.get("status") == "cancelled":
        return "deleted" if t["planned_start"] is not None else None
    start, end = _parse_when(ev.get("start")), _parse_when(ev.get("end"))
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None                    # блок утащили в «весь день»: время не принимаем
    if start == t["planned_start"] and end == t["planned_end"]:
        return None
    return "moved"


def accept_manual(conn, t: dict, ev: dict, kind: str) -> bool:
    """Записать ручное действие в задачу. Условие по старым значениям: если задачу
    за это время поменяли в базе, ничего не пишем, разберёмся на следующем проходе."""
    guard = ("AND planned_start IS NOT DISTINCT FROM %s AND planned_end IS NOT DISTINCT FROM %s "
             "AND planned_at IS NOT DISTINCT FROM %s")
    old = (t["planned_start"], t["planned_end"], t["planned_at"])
    if kind == "deleted":
        cur = conn.execute("UPDATE work.tasks SET planned_start = NULL, planned_end = NULL, planned_by = NULL, "
                           f"block_synced_at = now() WHERE id = %s {guard}", (t["id"], *old))
    else:
        start, end = _parse_when(ev["start"]), _parse_when(ev["end"])
        cur = conn.execute("UPDATE work.tasks SET planned_start = %s, planned_end = %s, planned_by = 'manual', "
                           f"planned_at = now(), block_synced_at = now() WHERE id = %s {guard}",
                           (start, end, t["id"], *old))
    return cur.rowcount == 1


def mark_synced(conn, t: dict) -> None:
    conn.execute("UPDATE work.tasks SET block_synced_at = now() WHERE id = %s "
                 "AND planned_start IS NOT DISTINCT FROM %s AND planned_end IS NOT DISTINCT FROM %s "
                 "AND (done_at IS NULL) = %s AND (dropped_at IS NULL) = %s",
                 (t["id"], t["planned_start"], t["planned_end"], t["done_at"] is None, t["dropped_at"] is None))


def _pull(api: GCal, conn, cal: str, st: dict, now: datetime, out: dict) -> None:
    """Изменения календаря «План» с прошлого прохода: ручные переносы и удаления.

    Есть syncToken: только изменения. Токена нет (первый запуск) или Google его
    отозвал (410): полный список без фильтров, как требует Google для нового токена;
    в «Плане» лежат только блоки, это одна страница. Google токен не выдал, но
    прошлый проход был: изменения с его времени по updatedMin."""
    started = now - timedelta(minutes=1)
    params = {"showDeleted": "true", "maxResults": 2500}
    items = token = None
    if st.get("sync_token"):
        try:
            items, token = api.list(cal, {**params, "syncToken": st["sync_token"]})
        except SyncTokenGone:
            st.pop("sync_token", None)
            st.pop("pulled_at", None)
    if items is None:
        if st.get("pulled_at"):
            since = max(base.parse_iso(st["pulled_at"]), now - timedelta(days=20))
            items, token = api.list(cal, {**params, "updatedMin": rfc(since)})
        else:
            items, token = api.list(cal, params)
    out["pulled"] = len(items)
    if items:
        ids = []
        for ev in items:
            tid = private(ev).get("task_id")
            if marker(ev) == "block" and tid and tid.isdigit():
                ids.append(int(tid))
        rows = conn.execute(
            f"""SELECT {TASK_COLS} FROM work.tasks
                WHERE id = ANY(%s) OR (block_synced_at IS NOT NULL AND planned_start IS NOT NULL
                                       AND planned_end > %s)""", (ids, now - timedelta(days=30))).fetchall()
        by_block = {block_id(t["id"]): t for t in rows}
        for ev in items:
            t = by_block.get(ev.get("id"))
            if t is None:
                continue
            if ev.get("status") != "cancelled" and (
                    marker(ev) != "block" or private(ev).get("task_id") != str(t["id"])):
                continue
            kind = manual_change(t, ev)
            if kind and accept_manual(conn, t, ev, kind):
                out[f"manual_{kind}"] += 1
    if token:
        st["sync_token"] = token
    st["pulled_at"] = rfc(started)


def blocks(conn=None, api: GCal | None = None, now: datetime | None = None, state: dict | None = None) -> dict:
    cal = config.env("GCAL_PLAN_CALENDAR")
    if not cal:
        return {"skipped": "GCAL_PLAN_CALENDAR не задан"}
    now = now or tu.now()
    today = tu.logical_date(now)
    win0 = tu.day_bounds(today - timedelta(days=BLOCK_DAYS_BACK))[0]
    win1 = tu.day_bounds(today + timedelta(days=BLOCK_DAYS_AHEAD))[1]
    own_conn = conn is None
    conn = conn or db.connect(autocommit=True)
    own_api = api is None
    api = api or GCal(google.client(TOKEN_ENV, transport=base.transport()))
    own_state = state is None
    state = load_state() if own_state else state
    st = state.setdefault("plan", {}).setdefault(cal, {})
    out = {"pulled": 0, "manual_moved": 0, "manual_deleted": 0, "inserted": 0, "updated": 0,
           "restored": 0, "unchanged": 0, "deleted": 0, "foreign": 0, "errors": []}
    try:
        # 1. сначала ручные действия из Google: они главнее
        _pull(api, conn, cal, st, now, out)
        if own_state:
            save_state(state)

        # 2. потом база -> календарь
        tasks = conn.execute(
            f"""SELECT {TASK_COLS} FROM work.tasks
                WHERE planned_start IS NOT NULL AND planned_end IS NOT NULL AND dropped_at IS NULL
                  AND planned_start < %s AND planned_end > %s
                ORDER BY planned_start, id""", (win1, win0)).fetchall()
        existing = {ev["id"]: ev for ev in api.list_marked(cal, "block", win0, win1)}
        for t in tasks:
            body = block_body(t)
            try:
                ev = existing.pop(body["id"], None)
                if ev is None:
                    # нет в окне: новый блок, блок за окном или удалённый (409)
                    verdict, ev = _insert_or_get(api, cal, body, "block")
                    if verdict == "foreign":
                        out["foreign"] += 1
                        continue
                    if verdict == "inserted":
                        out["inserted"] += 1
                        mark_synced(conn, t)
                        continue
                kind = manual_change(t, ev)
                if kind:
                    # руками перетащили или удалили после прошлого чтения изменений
                    if accept_manual(conn, t, ev, kind):
                        out[f"manual_{kind}"] += 1
                    continue
                verdict = _patch_if_changed(api, cal, ev, body)
                out[verdict] += 1
                if verdict != "unchanged" or t["block_synced_at"] is None or (
                        t["planned_at"] and t["planned_at"] > t["block_synced_at"]) or (
                        t["done_at"] and t["done_at"] > t["block_synced_at"]):
                    mark_synced(conn, t)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"задача {t['id']}: {e}"[:300])

        # 3. блоки в окне, которым больше не соответствует задача в окне
        leftovers = [ev for ev in existing.values() if marker(ev) == "block"]
        tids = [int(private(ev)["task_id"]) for ev in leftovers if str(private(ev).get("task_id", "")).isdigit()]
        rows = {t["id"]: t for t in conn.execute(
            f"SELECT {TASK_COLS} FROM work.tasks WHERE id = ANY(%s)", (tids,)).fetchall()} if tids else {}
        for ev in leftovers:
            tid = str(private(ev).get("task_id", ""))
            t = rows.get(int(tid)) if tid.isdigit() else None
            try:
                if t is not None and ev["id"] == block_id(t["id"]) and t["dropped_at"] is None \
                        and t["planned_start"] is not None and t["planned_end"] is not None:
                    kind = manual_change(t, ev)
                    if kind:
                        if accept_manual(conn, t, ev, kind):
                            out[f"manual_{kind}"] += 1
                        continue
                    # задача уехала за окно: блок едет за ней, а не исчезает
                    out[_patch_if_changed(api, cal, ev, block_body(t))] += 1
                    mark_synced(conn, t)
                    continue
                api.delete(cal, ev["id"])
                out["deleted"] += 1
                if t is not None:
                    mark_synced(conn, t)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"блок {ev['id']}: {e}"[:300])
    finally:
        if own_conn:
            conn.close()
        if own_api:
            api.http.close()
    return out


# ---------- точка входа ----------
def _alert(conn, state: dict, part: str, errors: list[str], now: datetime) -> None:
    """Сообщение в «Систему» о сбое, не чаще раза в шесть часов на часть."""
    alerts = state.setdefault("alerts", {})
    last = alerts.get(part)
    if last and now - base.parse_iso(last) < ALERT_EVERY:
        return
    title = {"mirror": "Зеркало календарей", "blocks": "Блоки плана в календаре"}[part]
    lines = "\n".join(f"· {base.esc(e[:200])}" for e in errors[:5])
    base.outbox(conn, "system", f"{title}: ошибки при синхронизации.\n{lines}")
    alerts[part] = rfc(now)


def run(date: str | None = None, dry_run: bool = False, parts=("mirror", "blocks"), **kw) -> dict:  # noqa: ARG001
    """Зеркало, затем блоки. Возвращает счётчики для лога."""
    now = tu.now()
    state = load_state()
    out: dict = {}
    conn = db.connect(autocommit=True)
    api = None
    try:
        api = GCal(google.client(TOKEN_ENV, transport=base.transport()))
        for part in parts:
            fn = {"mirror": mirror, "blocks": blocks}[part]
            try:
                res = fn(conn=conn, api=api, now=now, **({"state": state} if part == "blocks" else {}))
            except Exception as e:  # noqa: BLE001  часть упала целиком: другая всё равно работает
                log.exception("календарь: %s упал", part)
                res = {"errors": [str(e) or type(e).__name__]}
            out[part] = res
            if res.get("errors"):
                _alert(conn, state, part, res["errors"], now)
            elif part in state.get("alerts", {}):
                state["alerts"].pop(part)
    finally:
        save_state(state)
        if api is not None:
            api.http.close()
        conn.close()
    return out

"""П.4 · опыт из Garmin (workos-reader, каждый час).

Клетка сетки получает опыт сама, если у неё задан auto_source:
    garmin:activity             любая тренировка от 20 минут за день
    garmin:activity:strength    силовая (тип содержит 'strength') от 20 минут
    garmin:activity:running:40  бег от 40 минут
    garmin:activity:cycling|biking:60  велосипед любого вида (cycling, road_biking, mountain_biking) от 60 минут
    garmin:sleep:7.5            сон от 7,5 часа (ночь, после которой начался день)
    garmin:steps:10000          от 10 000 шагов за день
Начисление: rpg.xp source='garmin', source_ref='<клетка>:<дата>', ON CONFLICT DO NOTHING,
привычка +10% за каждый день серии до +50%, потолок 100 в день на характеристику.
Потолок срезает опыт, а не факт: упёрлись в него, запись пишется с нулём, и серия не рвётся.

Вход в Garmin: токены garth в GARMIN_TOKENS (по умолчанию ~/.garminconnect пользователя
workos-reader). Один раз: `workctl garmin-login` с GARMIN_EMAIL и GARMIN_PASSWORD в .env.reader.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from core import config, db, timeutil as tu

ACTIVITY_MIN_MINUTES = 20


class GarminUnavailable(RuntimeError):
    pass


class GarminClient:
    """Тонкая обёртка над garminconnect: импорт ленивый, библиотека может быть не поставлена."""

    def __init__(self, api=None):
        self.api = api or self._login()

    @staticmethod
    def tokenstore() -> str:
        return os.path.expanduser(config.env("GARMIN_TOKENS", "~/.garminconnect"))

    @classmethod
    def _login(cls, interactive: bool = False):
        try:
            from garminconnect import Garmin  # noqa: PLC0415
        except ImportError as e:
            raise GarminUnavailable("garminconnect не установлен: pip install garminconnect") from e
        store = cls.tokenstore()
        if not interactive and Path(store).exists():
            try:
                api = Garmin()
                api.login(store)
                return api
            except Exception:  # noqa: BLE001 — токены протухли, пробуем пароль
                pass
        email, password = config.env("GARMIN_EMAIL"), config.env("GARMIN_PASSWORD")
        if not (email and password):
            raise GarminUnavailable(f"нет токенов в {store} и не заданы GARMIN_EMAIL/GARMIN_PASSWORD")
        kw = {"prompt_mfa": lambda: input("Код MFA Garmin: ")} if interactive else {}
        try:
            api = Garmin(email, password, **kw)
        except TypeError:
            api = Garmin(email, password)
        api.login()
        try:
            Path(store).mkdir(parents=True, exist_ok=True)
            api.garth.dump(store)
        except Exception:  # noqa: BLE001
            pass
        return api

    # ---- данные за календарные дни ----
    def activities(self, start: date, end: date) -> list[dict]:
        """[{type, minutes, start_local: datetime}]"""
        raw = self.api.get_activities_by_date(start.isoformat(), end.isoformat()) or []
        out = []
        for a in raw:
            st = a.get("startTimeLocal") or a.get("startTimeGMT")
            try:
                loc = datetime.fromisoformat(st.replace("Z", ""))
            except (AttributeError, ValueError):
                continue
            if loc.tzinfo is None:
                loc = loc.replace(tzinfo=config.TZ)
            out.append({"type": str((a.get("activityType") or {}).get("typeKey") or ""),
                        "minutes": float(a.get("duration") or a.get("movingDuration") or 0) / 60,
                        "start_local": loc})
        return out

    def sleep_hours(self, d: date) -> float | None:
        data = self.api.get_sleep_data(d.isoformat()) or {}
        sec = (data.get("dailySleepDTO") or {}).get("sleepTimeSeconds")
        return round(sec / 3600, 2) if sec else None

    def steps(self, d: date) -> int | None:
        data = self.api.get_stats(d.isoformat()) or {}
        n = data.get("totalSteps")
        return int(n) if n is not None else None


def parse_source(src: str) -> dict | None:
    parts = (src or "").strip().split(":")
    if len(parts) < 2 or parts[0] != "garmin":
        return None
    kind = parts[1]
    try:
        if kind == "activity":
            return {"kind": "activity", "type": parts[2] if len(parts) > 2 and parts[2] else None,
                    "minutes": float(parts[3]) if len(parts) > 3 else ACTIVITY_MIN_MINUTES}
        if kind == "sleep":
            return {"kind": "sleep", "hours": float(parts[2].replace(",", ".")) if len(parts) > 2 else 7.0}
        if kind == "steps":
            return {"kind": "steps", "steps": int(parts[2]) if len(parts) > 2 else 10000}
    except ValueError:
        return None
    return None


def met(rule: dict, d: date, acts: dict[date, list[dict]], cache: dict, client) -> bool:
    if rule["kind"] == "activity":
        for a in acts.get(d, []):
            # тип можно задать вариантами через «|»: cycling|biking ловит road_biking и indoor_cycling
            if rule["type"] and not any(t in a["type"] for t in rule["type"].split("|") if t):
                continue
            if a["minutes"] >= rule["minutes"]:
                return True
        return False
    key = (rule["kind"], d)
    if key not in cache:
        cache[key] = client.sleep_hours(d) if rule["kind"] == "sleep" else client.steps(d)
    val = cache[key]
    if val is None:
        return False
    return val >= (rule["hours"] if rule["kind"] == "sleep" else rule["steps"])


def _stamp(d: date, now: datetime) -> datetime:
    """Момент начисления внутри логического дня d, не в будущем."""
    noon = tu.at_local(d, time(12))
    return min(noon, now) if tu.logical_date(now) == d else noon


def award(conn, cell: dict, d: date, now: datetime) -> int | None:
    """Засчитать клетке день d по правилам привычки. Сумма опыта (0, если день упёрся
    в потолок: отметка всё равно пишется) или None, если день уже был засчитан."""
    ref = f"{cell['id']}:{d.isoformat()}"
    if conn.execute("SELECT 1 FROM rpg.xp WHERE source = 'garmin' AND source_ref = %s AND cell_id = %s",
                    (ref, cell["id"])).fetchone():
        return None
    streak = 0
    if cell["kind"] == "habit":
        while streak < 5 and conn.execute(
                "SELECT 1 FROM rpg.xp WHERE cell_id = %s AND rpg.lday(earned_at) = %s LIMIT 1",
                (cell["id"], d - timedelta(days=streak + 1))).fetchone():
            streak += 1
    amount = (cell["xp_base"] * (10 + streak) + 5) // 10        # round half up, как round() в SQL
    if cell["kind"] != "milestone":
        left = config.XP_DAILY_CAP - conn.execute("SELECT rpg.day_xp(%s, %s) AS n",
                                                  (cell["attr"], d)).fetchone()["n"]
        amount = min(amount, left)
    amount = max(amount, 0)
    r = conn.execute("""INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
                        VALUES (%s, %s, 'garmin', %s, %s) ON CONFLICT DO NOTHING RETURNING amount""",
                     (cell["id"], amount, ref, _stamp(d, now))).fetchone()
    return r["amount"] if r else None


def run(date: str | None = None, dry_run: bool = False, days: int = 3, client=None, now=None, **kw) -> dict:
    now = now or tu.now()
    with db.connect() as conn:
        cells = conn.execute(
            """SELECT id, kind, xp_base, auto_source, rpg.attr_of(id) AS attr FROM rpg.cells
               WHERE active AND auto_source LIKE 'garmin:%%' AND kind IN ('habit','quality','milestone')
               ORDER BY id""").fetchall()
    rules = [(c, parse_source(c["auto_source"])) for c in cells]
    rules = [(c, r) for c, r in rules if r and c["attr"]]
    if not rules:
        return {"skipped": "нет клеток с garmin:*"}
    last = datetime.fromisoformat(date).date() if date else tu.logical_date(now)
    day_list = [last - timedelta(days=i) for i in range(max(days, 1) - 1, -1, -1)]   # по возрастанию: серия растёт
    try:
        client = client or GarminClient()
        acts: dict[date, list[dict]] = {}
        if any(r["kind"] == "activity" for _, r in rules):
            for a in client.activities(day_list[0], day_list[-1] + timedelta(days=1)):
                acts.setdefault(tu.logical_date(a["start_local"]), []).append(a)
        cache: dict = {}
        hits = [(c, d) for d in day_list for c, r in rules if met(r, d, acts, cache, client)]
    except GarminUnavailable as e:
        return {"skipped": str(e)}
    except Exception as e:  # noqa: BLE001 — ошибка видна heartbeat'у, если заведён источник garmin
        _mark(error=f"{type(e).__name__}: {e}")
        raise
    if dry_run:
        return {"dry_run": True, "hits": [f"{c['id']}:{d}" for c, d in hits]}
    awarded, capped = {}, []
    with db.connect() as conn, conn.transaction():
        for c, d in hits:
            n = award(conn, c, d, now)
            if n:
                awarded[f"{c['id']}:{d}"] = n
            elif n == 0:
                capped.append(f"{c['id']}:{d}")           # засчитано без опыта: потолок дня
    _mark()
    return {"days": [d.isoformat() for d in day_list], "hits": len(hits), "awarded": awarded, "capped": capped}


def _mark(error: str | None = None) -> None:
    """Если в work.sources заведена строка 'garmin', heartbeat видит и этот источник."""
    try:
        with db.connect(autocommit=True) as conn:
            if error:
                conn.execute("UPDATE work.sources SET last_error = %s WHERE id = 'garmin'", (error[:1500],))
            else:
                conn.execute("UPDATE work.sources SET last_ok_at = now(), last_error = NULL WHERE id = 'garmin'")
    except Exception:  # noqa: BLE001
        pass


def login() -> dict:
    """Разовый вход с паролем (и MFA, если включён): токены ложатся в GARMIN_TOKENS."""
    GarminClient._login(interactive=True)
    return {"ok": True, "tokens": GarminClient.tokenstore()}


if __name__ == "__main__":
    print(run())

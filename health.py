"""2.7 · heartbeat (workos-reader, каждые 15 минут).

По каждому включённому источнику: если последняя удачная синхронизация старше порога
(1 час для 15-минутных коллекторов и eas, 6 часов для А-Чата, свой порог можно задать
в sources.config.health_hours) или записана ошибка, в тему «Система» уходит один алерт
на инцидент (дедуп через work.notices 'health:<источник>'). Когда источник ожил,
отметка снимается и приходит «восстановился».

Плюс проверка API мини-приложения (APP_HEALTH_URL). Смерть самого бота отсюда не видна
(алерты идут через него): залежавшийся outbox только пишется в лог, а падение бота
ловит внешний сторож healthchecks.io, которого пингует бот.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from core import config, db, timeutil as tu
from core.tg import esc

log = logging.getLogger("workos.health")

THRESHOLD_HOURS = {"achat": 6.0, "eas": 1.0}
DEFAULT_HOURS = 1.0
STALE_OUTBOX_MIN = 30


def threshold(src: dict) -> float:
    cfg = src.get("config") or {}
    try:
        return float(cfg["health_hours"])
    except (KeyError, TypeError, ValueError):
        return THRESHOLD_HOURS.get(src["kind"], DEFAULT_HOURS)


def _ago(now: datetime, dt: datetime) -> str:
    mins = int((now - dt).total_seconds() // 60)
    if mins < 120:
        return f"{mins} мин"
    if mins < 48 * 60:
        return f"{mins // 60} ч"
    return f"{mins // 1440} дн"


def _when(dt: datetime) -> str:
    return tu.hm(dt) if tu.logical_date(dt) == tu.logical_today() else f"{tu.ru_date(tu.local(dt).date(), False)} {tu.hm(dt)}"


def problem(src: dict, now: datetime) -> str | None:
    """Текст проблемы или None, если источник здоров."""
    if src["last_error"]:
        first = src["last_error"].strip().splitlines()[0][:300]
        return f"ошибка: <code>{esc(first)}</code>"
    if src["last_ok_at"] is None:
        return "ещё ни разу не отработал"
    if now - src["last_ok_at"] > timedelta(hours=threshold(src)):
        return f"молчит {_ago(now, src['last_ok_at'])} (последняя синхронизация {_when(src['last_ok_at'])})"
    return None


def _incident(conn, key: str, name: str, issue: str | None) -> str | None:
    """Один алерт на инцидент и одно «восстановился». Возвращает 'alert' | 'recovered' | None."""
    open_ = conn.execute("SELECT 1 FROM work.notices WHERE key = %s", (key,)).fetchone()
    if issue and not open_:
        conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
        conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (f"⚠ <b>{esc(name)}</b>: {issue}",))
        return "alert"
    if not issue and open_:
        conn.execute("DELETE FROM work.notices WHERE key = %s", (key,))
        conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (f"✓ <b>{esc(name)}</b> восстановился",))
        return "recovered"
    return None


def check_app(url: str, transport=None) -> str | None:
    try:
        with httpx.Client(timeout=5.0, transport=transport) as c:
            r = c.get(url)
        if r.status_code == 200:
            return None
        return f"API отвечает {r.status_code}"
    except httpx.HTTPError as e:
        return f"API не отвечает: <code>{esc(type(e).__name__)}</code>"


def stale_outbox(conn, now: datetime) -> int:
    """Неотправленное дольше 30 минут вне тихих часов: бот лежит или застрял."""
    lag = timedelta(minutes=STALE_OUTBOX_MIN)
    if tu.is_quiet(now) or tu.is_quiet(now - lag):
        return 0
    return conn.execute("SELECT count(*) AS n FROM work.outbox WHERE sent_at IS NULL AND created_at < %s",
                        (now - lag,)).fetchone()["n"]


def run(date: str | None = None, dry_run: bool = False, now: datetime | None = None, transport=None, **kw) -> dict:
    now = now or tu.now()
    out = {"alerts": [], "recovered": [], "ok": 0}
    with db.connect() as conn:
        sources = conn.execute("SELECT id, kind, last_ok_at, last_error, config FROM work.sources "
                               "WHERE enabled AND kind <> 'telegram' ORDER BY id").fetchall()
        checks = [(f"health:{s['id']}", s["id"], problem(s, now)) for s in sources]
        url = config.env("APP_HEALTH_URL", "http://127.0.0.1:8787/api/health")
        if url and url.lower() not in ("off", "none", "0"):
            checks.append(("health:app", "Мини-приложение", check_app(url, transport)))
        stale = stale_outbox(conn, now)
        if stale:
            log.warning("outbox: %s сообщений не отправлены дольше %s мин: бот лежит?", stale, STALE_OUTBOX_MIN)
        out["stale_outbox"] = stale
        if dry_run:
            return {**out, "problems": {name: issue for _, name, issue in checks if issue}, "dry_run": True}
        with conn.transaction():
            for key, name, issue in checks:
                res = _incident(conn, key, name, issue)
                if res == "alert":
                    out["alerts"].append(name)
                elif res == "recovered":
                    out["recovered"].append(name)
                elif not issue:
                    out["ok"] += 1
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())

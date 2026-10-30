"""Трек доменов · разъём терминала для FoodBot и JobBot.

Терминал знает о домене только имя темы и префикс кнопок (config.DOMAINS).
Сообщение в теме домена -> событие с meta.domain и NOTIFY domain_<key> (payload = id события).
Кнопка с префиксом <key>: -> NOTIFY domain_<key> с JSON нажатия. Отвечает домен строкой
в work.outbox с topic = <key>. Своего Telegram-токена у домена нет: если задан
DOMAIN_FILES_DIR, бот сам скачивает фото и голос в <dir>/<key>/<event_id>.<ext>.
"""
from __future__ import annotations

import json
import logging
import os

from core import config

from bot import capture
from bot.topics import Ctx

log = logging.getLogger("workos.bot")


def is_domain(key: str) -> bool:
    return key in config.DOMAINS


def _save_file(ctx: Ctx, key: str, eid: int, msg: dict) -> str | None:
    base = config.env("DOMAIN_FILES_DIR")
    if not base:
        return None
    if msg.get("photo"):
        fid, ext = msg["photo"][-1]["file_id"], "jpg"
    elif msg.get("voice"):
        fid, ext = msg["voice"]["file_id"], "oga"
    elif msg.get("document"):
        fid, ext = msg["document"]["file_id"], (msg["document"].get("file_name") or "bin").rsplit(".", 1)[-1][:8]
    else:
        return None
    d = os.path.join(base, key)
    os.makedirs(d, mode=0o750, exist_ok=True)
    path = os.path.join(d, f"{eid}.{ext}")
    with open(path, "wb") as f:
        f.write(ctx.bot.download(fid))
    return path


def handle_message(ctx: Ctx, msg: dict, key: str) -> int | None:
    conn = ctx.conn
    eid = capture.store_event(ctx, msg, meta={"domain": key})
    if eid is None:
        return None
    try:
        path = _save_file(ctx, key, eid, msg)
        if path:
            capture.merge_meta(conn, eid, {"file_path": path})
    except Exception as e:
        log.warning("домен %s: файл не скачался: %s", key, e)
    conn.execute("SELECT pg_notify(%s, %s)", (f"domain_{key}", str(eid)))
    return eid


def on_callback(ctx: Ctx, cq: dict, key: str) -> str | None:
    msg = cq.get("message") or {}
    payload = json.dumps({"data": cq.get("data"), "message_id": msg.get("message_id"),
                          "callback_id": cq.get("id")}, ensure_ascii=False)
    ctx.conn.execute("SELECT pg_notify(%s, %s)", (f"domain_{key}", payload))
    return None

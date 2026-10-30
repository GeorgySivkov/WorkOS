"""Проверка initData мини-приложения Telegram в режиме третьих сторон.

Процесс workos-app не знает токена бота, поэтому проверяет не HMAC (поле hash),
а Ed25519-подпись Telegram (поле signature) по публичному ключу Telegram:

    data_check_string = f"{bot_id}:WebAppData\\n" + отсортированные "key=value"
                        всех полей, кроме hash и signature (значения раскодированы)
    signature         = base64url без «=» на конце

Дальше: user.id должен совпасть с TELEGRAM_OWNER_ID, auth_date не старше суток.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
from urllib.parse import parse_qsl

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core import config

PROD_PUBLIC_KEY = "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
TEST_PUBLIC_KEY = "40055058a4ee38156a06562e52eece92a771bcd8346a8c4615cb7376eddf72ec"
MAX_AGE = 24 * 3600
FUTURE_SKEW = 300          # часы клиента и сервера могут разойтись на пару минут


class InitDataError(Exception):
    """initData не прошла проверку; текст причины только для лога, наружу уходит 401."""


def public_key_hex(override: str | None = None) -> str:
    key = (override or config.env("TELEGRAM_PUBLIC_KEY") or PROD_PUBLIC_KEY).strip()
    if key.lower() == "test":
        return TEST_PUBLIC_KEY
    if key.lower() == "prod":
        return PROD_PUBLIC_KEY
    return key


def parse(init_data: str) -> list[tuple[str, str]]:
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as e:
        raise InitDataError("initData не разбирается") from e
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InitDataError("повтор ключа в initData")
    return pairs


def check_string(bot_id: int | str, pairs: list[tuple[str, str]]) -> str:
    fields = sorted((k, v) for k, v in pairs if k not in ("hash", "signature"))
    return f"{bot_id}:WebAppData\n" + "\n".join(f"{k}={v}" for k, v in fields)


def _b64url(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError) as e:
        raise InitDataError("signature не base64url") from e


def validate(init_data: str | None, *, bot_id: int | str | None = None, owner_id: int | str | None = None,
             public_key: str | None = None, max_age: int = MAX_AGE, now: float | None = None) -> dict:
    """Проверить initData. Возвращает {"user": {...}, "auth_date": int, "fields": {...}}.

    Бросает InitDataError, если подпись, владелец или свежесть не сходятся.
    """
    if not init_data:
        raise InitDataError("пустая initData")
    bot_id = bot_id or config.env("TELEGRAM_BOT_ID")
    owner_id = owner_id or config.env("TELEGRAM_OWNER_ID")
    if not bot_id or not owner_id:
        raise InitDataError("не заданы TELEGRAM_BOT_ID или TELEGRAM_OWNER_ID")
    pairs = parse(init_data)
    data = dict(pairs)
    sig = data.get("signature")
    if not sig:
        raise InitDataError("нет signature")
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex(public_key)))
    except ValueError as e:
        raise InitDataError("TELEGRAM_PUBLIC_KEY не похож на ключ Ed25519") from e
    try:
        key.verify(_b64url(sig), check_string(bot_id, pairs).encode("utf-8"))
    except InvalidSignature as e:
        raise InitDataError("подпись не сходится") from e

    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError as e:
        raise InitDataError("нет auth_date") from e
    now = time.time() if now is None else now
    if now - auth_date > max_age:
        raise InitDataError("initData старше суток")
    if auth_date - now > FUTURE_SKEW:
        raise InitDataError("auth_date из будущего")

    try:
        user = json.loads(data.get("user") or "")
        uid = int(user["id"])
    except (ValueError, TypeError, KeyError) as e:
        raise InitDataError("нет user.id") from e
    if uid != int(owner_id):
        raise InitDataError(f"чужой пользователь {uid}")
    return {"user": user, "auth_date": auth_date, "fields": data}

"""Тонкий клиент Telegram Bot API поверх httpx.

Без фреймворков: темы в личке (Bot API 9.3-9.4) появились недавно, и прямые
вызовы не зависят от того, догнала ли их библиотека.
"""
from __future__ import annotations

import json
import re

import httpx

from core import config


class TgError(RuntimeError):
    def __init__(self, method: str, desc: str, code: int | None = None):
        super().__init__(f"{method}: {desc}")
        self.method, self.desc, self.code = method, desc, code


class Bot:
    def __init__(self, token: str | None = None, base: str | None = None):
        self.token = token or config.env("TELEGRAM_BOT_TOKEN", required=True)
        self.base = (base or config.env("TELEGRAM_API_BASE", "https://api.telegram.org")).rstrip("/")
        self.http = httpx.Client(timeout=httpx.Timeout(70.0, connect=10.0))

    @property
    def bot_id(self) -> int:
        return int(self.token.split(":", 1)[0])

    def call(self, method: str, **params):
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in params.items() if v is not None}
        r = self.http.post(f"{self.base}/bot{self.token}/{method}", data=data)
        try:
            body = r.json()
        except ValueError as e:
            raise TgError(method, f"HTTP {r.status_code}") from e
        if not body.get("ok"):
            raise TgError(method, body.get("description", "?"), body.get("error_code"))
        return body["result"]

    def download(self, file_id: str) -> bytes:
        f = self.call("getFile", file_id=file_id)
        # в URL файла токен бота: ни raise_for_status, ни текст сетевой ошибки наружу не отдаём
        try:
            r = self.http.get(f"{self.base}/file/bot{self.token}/{f['file_path']}")
        except httpx.HTTPError as e:
            raise TgError("download", f"сеть: {type(e).__name__}: {scrub(str(e))}") from None
        if r.status_code != 200:
            raise TgError("download", f"HTTP {r.status_code}", r.status_code)
        return r.content

    def send(self, chat_id: int, text: str, thread_id: int | None = None, buttons=None,
             reply_to: int | None = None, parse_mode: str | None = "HTML"):
        params = dict(chat_id=chat_id, text=text[:4096], message_thread_id=thread_id,
                      parse_mode=parse_mode, link_preview_options={"is_disabled": True})
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str | None = None, buttons=None,
             parse_mode: str | None = "HTML"):
        markup = {"inline_keyboard": buttons or []}
        if text is None:
            return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                             reply_markup=markup)
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096],
                         parse_mode=parse_mode, reply_markup=markup,
                         link_preview_options={"is_disabled": True})

    def answer(self, callback_id: str, text: str | None = None):
        try:
            return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TgError:
            return None


RX_TOKEN = re.compile(r"bot\d+:[\w-]+|\b\d{6,}:[\w-]{30,}")


def scrub(text) -> str:
    """Убирает токен бота из текста ошибки перед записью в базу, outbox или журнал."""
    return RX_TOKEN.sub("bot<token>", str(text or ""))


def btn(text: str, data: str) -> dict:
    assert len(data.encode()) <= 64, f"callback_data длиннее 64 байт: {data}"
    return {"text": text, "callback_data": data}


def esc(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

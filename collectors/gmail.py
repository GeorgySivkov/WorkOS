"""Коллектор Gmail (шаг 3.1): History API, historyId как водяная метка, тела целиком.

Только gmail.readonly. Первый прогон (и откат, если historyId устарел и
history.list ответил 404): messages.list q=newer_than:Nd, где N = max(lookback, 2),
а historyId берётся из профиля ДО списка, чтобы не потерять письма,
пришедшие между двумя запросами. Дальше history.list с startHistoryId,
только messageAdded. Спам, корзина и черновики пропускаются.

Настройки источника (work.sources.config):
  {"refresh_env": "GMAIL_PERSONAL_REFRESH_TOKEN", "account": "personal"}
Секреты: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET и refresh-токен в .env.reader.
OAuth-приложение должно быть «In production», иначе токен умрёт через 7 дней.
"""
from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from html.parser import HTMLParser
from typing import Iterable

import httpx

from collectors.base import Collector, Event, run_enabled
from core import google, timeutil as tu

UTC = timezone.utc
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT"}


class GmailHistoryGone(Exception):
    """startHistoryId слишком старый: нужен полный перечит."""


def _b64(data: str | None) -> bytes:
    if not data:
        return b""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _hdr(v: str | None) -> str | None:
    if v is None:
        return None
    try:
        return str(make_header(decode_header(v)))
    except Exception:  # noqa: BLE001: кривой заголовок не повод ронять прогон
        return v


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    try:
        p.feed(s)
        p.close()
        text = "".join(p.parts)
    except Exception:  # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", s)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*(\n\s*)+", "\n\n", text)
    return text.strip()


def _charset(part: dict) -> str:
    for h in part.get("headers") or []:
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w\-]+)"?', h.get("value", ""), re.I)
            if m:
                return m.group(1)
    return "utf-8"


def _decode(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class GmailCollector(Collector):
    kind = "gmail"

    def __init__(self, source: dict, transport: httpx.BaseTransport | None = None):
        super().__init__(source)
        self.cfg = source.get("config") or {}
        self.transport = transport
        sid = self.source_id
        self.account = self.cfg.get("account") or (sid.split(":", 1)[1] if ":" in sid else sid)
        self.refresh_env = self.cfg.get("refresh_env") or f"GMAIL_{self.account.upper()}_REFRESH_TOKEN"
        self.http: httpx.Client | None = None

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(API + path, params={k: v for k, v in params.items() if v is not None})
        if r.status_code == 404:
            raise FileNotFoundError(path)
        if r.status_code >= 400:
            raise RuntimeError(f"Gmail {path.split('?')[0]}: {r.status_code} {r.text[:200]}")
        return r.json()

    # ---------- какие письма читать ----------
    def _initial_ids(self) -> tuple[list[str], str]:
        history_id = str(self._get("/profile")["historyId"])
        days = max(self.lookback.days, 2)
        ids, token = [], None
        while True:
            body = self._get("/messages", q=f"newer_than:{days}d", maxResults=500, pageToken=token)
            ids += [m["id"] for m in body.get("messages") or []]
            token = body.get("nextPageToken")
            if not token:
                break
        return ids, history_id

    def _history_ids(self, start: str) -> tuple[list[str], str]:
        ids, token, last = [], None, start
        while True:
            try:
                body = self._get("/history", startHistoryId=start, historyTypes="messageAdded",
                                 maxResults=500, pageToken=token)
            except FileNotFoundError as e:
                raise GmailHistoryGone(start) from e
            for h in body.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    msg = added.get("message") or {}
                    if msg.get("id") and not SKIP_LABELS & set(msg.get("labelIds") or []):
                        ids.append(msg["id"])
            last = str(body.get("historyId") or last)
            token = body.get("nextPageToken")
            if not token:
                break
        return ids, last

    # ---------- письмо ----------
    def _body(self, msg_id: str, payload: dict) -> str | None:
        plain: list[str] = []
        htmls: list[str] = []

        def walk(part: dict):
            mime = (part.get("mimeType") or "").lower()
            if part.get("parts"):
                for p in part["parts"]:
                    walk(p)
                return
            if part.get("filename"):            # вложения не тянем
                return
            if mime not in ("text/plain", "text/html"):
                return
            body = part.get("body") or {}
            data = body.get("data")
            if not data and body.get("attachmentId"):
                try:
                    data = self._get(f"/messages/{msg_id}/attachments/{body['attachmentId']}").get("data")
                except FileNotFoundError:
                    data = None
            text = _decode(_b64(data), _charset(part))
            (plain if mime == "text/plain" else htmls).append(text)

        walk(payload)
        if any(t.strip() for t in plain):
            return "\n\n".join(t.strip() for t in plain if t.strip())
        if htmls:
            return html_to_text("\n".join(htmls)) or None
        return None

    def _event(self, msg: dict) -> Event | None:
        labels = msg.get("labelIds") or []
        if SKIP_LABELS & set(labels):
            return None
        payload = msg.get("payload") or {}
        headers = {}
        for h in payload.get("headers") or []:
            headers.setdefault(h.get("name", "").lower(), h.get("value"))
        mid = (headers.get("message-id") or "").strip()
        occurred = datetime.fromtimestamp(int(msg.get("internalDate") or 0) / 1000, UTC) \
            if msg.get("internalDate") else tu.now()
        meta = {
            "gmail_id": msg["id"], "thread_id": msg.get("threadId"), "labels": labels,
            "from": _hdr(headers.get("from")), "to": _hdr(headers.get("to")), "cc": _hdr(headers.get("cc")),
            "date": headers.get("date"), "in_reply_to": headers.get("in-reply-to"),
            "references": headers.get("references"), "snippet": html.unescape(msg.get("snippet") or ""),
            "account": self.account,
        }
        return Event(external_id=mid or f"gmail:{msg['id']}", kind="email", occurred_at=occurred,
                     title=_hdr(headers.get("subject")), body=self._body(msg["id"], payload),
                     meta=meta, actor_raw=_hdr(headers.get("from")),
                     thread_key=f"gmail:{msg.get('threadId') or msg['id']}")

    def collect(self) -> Iterable[Event]:
        started = tu.now()
        self.http = google.client(self.refresh_env, transport=self.transport)
        start = self.state.get("history_id") if isinstance(self.state, dict) else None
        mode = "history"
        try:
            if not start:
                raise GmailHistoryGone("нет метки")
            ids, history_id = self._history_ids(str(start))
        except GmailHistoryGone:
            mode = "full"
            ids, history_id = self._initial_ids()
        events: dict[str, Event] = {}
        for mid in dict.fromkeys(ids):
            try:
                msg = self._get(f"/messages/{mid}", format="full")
            except FileNotFoundError:
                continue                      # письмо успели удалить
            ev = self._event(msg)
            if ev:
                events[ev.external_id] = ev
        self.state = {"cursor": started.isoformat(), "history_id": history_id, "mode": mode}
        return list(events.values())


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"gmail": run_enabled("gmail")}


if __name__ == "__main__":
    print(run())

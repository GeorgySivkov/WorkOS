"""Коллектор Slack (шаг 2.6): только свои каналы плюс треды с упоминанием George.

Пользовательский токен (xoxp) со скоупами только на чтение:
channels:history, groups:history, search:read, users:read.

Настройки источника (work.sources.config):
  {"token_env": "SLACK_TOKEN", "channels": ["C0123", "G0456"], "user_id": "U0789",
   "thread_days": 7, "initial_days": 7, "mentions": true, "resolve_names": true,
   "search_threads": true}

Водяная метка: ts последнего сообщения на канал (state.channels), каждый
прогон перекрывает lookback_days назад. Треды, в которых что-то было
за последние thread_days, перечитываются каждый прогон: ответы в старый
тред conversations.history не покажет. Тред, молчавший дольше thread_days и
вдруг оживший, находит поиск search.messages «in:<#C> after:<дата>» по своим
каналам (нужен search:read; без него этот шаг тихо пропускается). Упоминания
ищутся search.messages по «<@user_id>» во всех каналах, их треды подтягиваются
целиком.

Правка сообщения: body события не переписывается (правило коллекторов), поэтому
текущий текст отредактированного сообщения кладётся в meta.text.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import httpx

from collectors.base import Collector, Event, run_enabled
from core import config, timeutil as tu

UTC = timezone.utc
API = "https://slack.com/api/"
SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                 "channel_archive", "channel_unarchive", "group_join", "group_leave", "group_topic",
                 "group_purpose", "group_name", "message_deleted", "message_changed", "pinned_item",
                 "unpinned_item", "reminder_add", "bot_add", "bot_remove"}


class SlackError(RuntimeError):
    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method}: {error}")
        self.method, self.error = method, error


def ts_dt(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


def dt_ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


class SlackAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.http = httpx.Client(base_url=API, timeout=30, transport=transport,
                                 headers={"Authorization": f"Bearer {token}"})
        self.sleep = sleep
        self.calls = 0

    def call(self, method: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(6):
            self.calls += 1
            r = self.http.get(method, params=params)
            if r.status_code == 429:
                self.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 120))
                continue
            if r.status_code >= 400:
                raise SlackError(method, f"HTTP {r.status_code}")
            body = r.json()
            if not body.get("ok"):
                if body.get("error") == "ratelimited":
                    self.sleep(min(2 ** attempt, 60))
                    continue
                raise SlackError(method, body.get("error", "?"))
            return body
        raise SlackError(method, "ratelimited")

    def paged(self, method: str, key: str, **params) -> Iterable[dict]:
        cursor = None
        while True:
            body = self.call(method, cursor=cursor, limit=200, **params)
            yield from body.get(key) or []
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break


class SlackCollector(Collector):
    kind = "slack"

    def __init__(self, source: dict, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        super().__init__(source)
        self.cfg = source.get("config") or {}
        self.transport = transport
        self.sleep = sleep
        self._names: dict[str, str | None] = {}

    # ---------- вспомогательное ----------
    def _name(self, api: SlackAPI, user: str | None) -> str | None:
        if not user or not self.cfg.get("resolve_names", True):
            return None
        if user not in self._names:
            try:
                u = api.call("users.info", user=user)["user"]
                prof = u.get("profile") or {}
                self._names[user] = prof.get("real_name") or prof.get("display_name") or u.get("name")
            except SlackError:
                self._names[user] = None
        return self._names[user]

    def _event(self, api: SlackAPI, channel: str, m: dict, channel_name: str | None = None,
               permalink: str | None = None) -> Event | None:
        if m.get("subtype") in SKIP_SUBTYPES or not m.get("ts"):
            return None
        text = m.get("text") or ""
        if not text and not m.get("files"):
            return None
        if not text and m.get("files"):
            text = "[файлы: " + ", ".join(f.get("name") or f.get("title") or "?" for f in m["files"]) + "]"
        user = m.get("user") or m.get("bot_id") or m.get("username")
        me = self.cfg.get("user_id")
        thread_ts = m.get("thread_ts")
        meta = {
            "channel": channel, "channel_name": channel_name, "user": user,
            "user_name": self._name(api, m.get("user")) or m.get("username"),
            "thread_ts": thread_ts, "is_reply": bool(thread_ts and thread_ts != m["ts"]),
            "reply_count": m.get("reply_count") or 0,
            "mentions_me": bool(me and f"<@{me}>" in text), "from_me": bool(me and m.get("user") == me),
            "edited": bool(m.get("edited")),
        }
        if m.get("edited"):
            meta["text"] = text          # body остаётся первым вариантом, правка видна здесь
        if permalink:
            meta["permalink"] = permalink
        return Event(external_id=f"{channel}:{m['ts']}", kind="message", occurred_at=ts_dt(m["ts"]),
                     title=None, body=text, meta=meta, actor_raw=user,
                     thread_key=f"{channel}:{thread_ts or m['ts']}")

    @staticmethod
    def _search(api: SlackAPI, query: str, max_pages: int = 5) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            body = api.call("search.messages", query=query, sort="timestamp", sort_dir="desc",
                            count=100, page=page)
            msgs = body.get("messages") or {}
            out += msgs.get("matches") or []
            pages = (msgs.get("paging") or msgs.get("pagination") or {})
            total = pages.get("pages") or pages.get("page_count") or 1
            if page >= min(total, max_pages):
                return out
            page += 1

    def _replies(self, api: SlackAPI, channel: str, thread_ts: str, oldest: str | None) -> list[dict]:
        return list(api.paged("conversations.replies", "messages", channel=channel, ts=thread_ts,
                              oldest=oldest, inclusive="true"))

    # ---------- прогон ----------
    def collect(self) -> Iterable[Event]:
        started = tu.now()
        token = config.env(self.cfg.get("token_env") or "SLACK_TOKEN", required=True)
        api = SlackAPI(token, self.transport, self.sleep)
        st = self.state if isinstance(self.state, dict) else {}
        chan_marks: dict[str, str] = dict(st.get("channels") or {})
        threads: dict[str, str] = dict(st.get("threads") or {})      # "chan|thread_ts" -> ts последней активности
        initial = timedelta(days=int(self.cfg.get("initial_days", 7)))
        thread_keep = timedelta(days=int(self.cfg.get("thread_days", 7)))
        out: dict[str, Event] = {}
        fetched_threads: set[tuple[str, str]] = set()

        def add(ev: Event | None):
            if ev:
                out[ev.external_id] = ev

        def track(channel: str, thread_ts: str, last_ts: str):
            key = f"{channel}|{thread_ts}"
            if float(last_ts) > float(threads.get(key, "0")):
                threads[key] = last_ts

        def pull_thread(channel: str, thread_ts: str, oldest: str | None, name: str | None = None):
            if (channel, thread_ts) in fetched_threads:
                return
            fetched_threads.add((channel, thread_ts))
            msgs = self._replies(api, channel, thread_ts, oldest)
            for r in msgs:
                add(self._event(api, channel, r, name))
            if msgs:
                track(channel, thread_ts, max(r["ts"] for r in msgs))

        # 1. свои каналы: история с перекрытием, треды целиком
        for channel in self.cfg.get("channels") or []:
            mark = chan_marks.get(channel)
            oldest_dt = (ts_dt(mark) - self.lookback) if mark else (started - initial)
            oldest = dt_ts(oldest_dt)
            newest = mark
            for m in api.paged("conversations.history", "messages", channel=channel, oldest=oldest,
                               inclusive="true"):
                add(self._event(api, channel, m))
                if newest is None or float(m["ts"]) > float(newest):
                    newest = m["ts"]
                if m.get("reply_count") or (m.get("thread_ts") == m.get("ts") and m.get("latest_reply")):
                    pull_thread(channel, m["ts"], oldest)
            if newest:
                chan_marks[channel] = newest

        # 2. живые треды из прошлых прогонов: ответы в старые треды
        for key, last in list(threads.items()):
            channel, thread_ts = key.split("|", 1)
            if ts_dt(last) < started - thread_keep:
                threads.pop(key)
                continue
            if (channel, thread_ts) in fetched_threads:
                continue
            try:
                pull_thread(channel, thread_ts, dt_ts(ts_dt(last) - self.lookback))
            except SlackError as e:
                if e.error in ("thread_not_found", "channel_not_found", "not_in_channel", "missing_scope"):
                    threads.pop(key, None)
                else:
                    raise

        since = self.since(default_days=initial.days)
        after = (since - timedelta(days=1)).date().isoformat()        # after: исключает сам день

        # 2б. ожившие старые треды своих каналов: ответ в тред старше thread_days
        # не виден ни в истории канала, ни в списке живых тредов, его находит поиск
        if self.cfg.get("channels") and self.cfg.get("search_threads", True):
            for channel in self.cfg.get("channels") or []:
                try:
                    matches = self._search(api, f"in:<#{channel}> after:{after}")
                except SlackError as e:
                    if e.error in ("missing_scope", "not_allowed_token_type", "not_authed", "invalid_auth"):
                        break                       # нет search:read: живём без этого шага
                    raise
                for mt in matches:
                    if not mt.get("ts") or ts_dt(mt["ts"]) < since:
                        continue
                    q = parse_qs(urlparse(mt.get("permalink") or "").query)
                    thread_ts = (q.get("thread_ts") or [None])[0] or mt.get("thread_ts")
                    if not thread_ts or thread_ts == mt["ts"] or (channel, thread_ts) in fetched_threads:
                        continue
                    try:
                        pull_thread(channel, thread_ts, dt_ts(since - timedelta(days=1)))
                    except SlackError as e:
                        if e.error not in ("thread_not_found", "channel_not_found", "not_in_channel"):
                            raise

        # 3. упоминания в любых каналах
        me = self.cfg.get("user_id")
        if me and self.cfg.get("mentions", True):
            page = 1
            while True:
                body = api.call("search.messages", query=f"<@{me}> after:{after}", sort="timestamp",
                                sort_dir="desc", count=100, page=page)
                msgs = body.get("messages") or {}
                for mt in msgs.get("matches") or []:
                    ch = (mt.get("channel") or {})
                    channel, name = ch.get("id"), ch.get("name")
                    if not channel or not mt.get("ts") or ts_dt(mt["ts"]) < since:
                        continue
                    q = parse_qs(urlparse(mt.get("permalink") or "").query)
                    thread_ts = (q.get("thread_ts") or [None])[0] or mt.get("thread_ts") or mt["ts"]
                    try:
                        pull_thread(channel, thread_ts, None, name)
                    except SlackError:
                        # личка или канал без доступа по скоупам: сохраним хотя бы само упоминание
                        mt = dict(mt, thread_ts=thread_ts if thread_ts != mt["ts"] else None)
                        add(self._event(api, channel, mt, name, mt.get("permalink")))
                    else:
                        ev = out.get(f"{channel}:{mt['ts']}")
                        if ev and mt.get("permalink"):
                            ev.meta["permalink"] = mt["permalink"]
                pages = (msgs.get("paging") or msgs.get("pagination") or {})
                total_pages = pages.get("pages") or pages.get("page_count") or 1
                if page >= total_pages:
                    break
                page += 1

        self.state = {"cursor": started.isoformat(), "channels": chan_marks, "threads": threads}
        return list(out.values())


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"slack": run_enabled("slack")}


if __name__ == "__main__":
    print(run())

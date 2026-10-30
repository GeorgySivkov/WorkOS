"""Темы личного чата с ботом и общий контекст обработчиков.

С Bot API 9.3 у бота бывают темы в личке, с 9.4 он создаёт их сам
(createForumTopic с chat_id = user id). Ключ темы -> message_thread_id лежит
в work.bot_topics. Если темы не поддерживаются (не включён режим тем у бота),
всё работает без них: thread_id = NULL, сообщения идут в общий чат.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from core import config, db
from core.tg import TgError

log = logging.getLogger("workos.bot")

NOTICE_EVERY = 600          # не чаще раза в 10 минут на одинаковую ошибку в «Систему»


def owner_id() -> int:
    return int(config.env("TELEGRAM_OWNER_ID", required=True))


def all_topics() -> dict[str, str]:
    return {**config.TOPICS, **config.DOMAINS}


class Ctx:
    """Всё, что нужно обработчику: бот, база, владелец, темы.

    У каждого потока свой Ctx (своё соединение и свой httpx-клиент),
    общие только карта тем, замок карточек и состояние «Поправить».
    """

    def __init__(self, bot, owner: int | None = None, conn_factory=None,
                 topics: dict | None = None, lock=None, state: dict | None = None):
        self.bot = bot
        self.owner = owner if owner is not None else owner_id()
        self._factory = conn_factory or (lambda: db.connect(autocommit=True))
        self._conn = None
        self.topics: dict[str, int | None] = topics if topics is not None else {}
        self.lock = lock or threading.RLock()
        self.state = state if state is not None else {}
        self._notices: dict[str, float] = {}

    # ---------- база ----------
    @property
    def conn(self):
        c = self._conn
        if c is None or c.closed or c.broken:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            self._conn = self._factory()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def clone(self, bot) -> "Ctx":
        return Ctx(bot, self.owner, self._factory, self.topics, self.lock, self.state)

    # ---------- темы ----------
    def thread_of(self, key: str) -> int | None:
        return self.topics.get(key)

    def key_of(self, thread_id: int | None) -> str:
        if thread_id is None:
            return "inbox"
        for k, v in self.topics.items():
            if v == thread_id:
                return k
        return "inbox"

    # ---------- отправка ----------
    def send(self, key: str, text: str, buttons=None, reply_to: int | None = None) -> dict:
        thread = self.thread_of(key)
        try:
            return self.bot.send(self.owner, text, thread_id=thread, buttons=buttons, reply_to=reply_to)
        except TgError as e:
            if thread is not None and "thread" in (e.desc or "").lower():
                # тему удалили руками: шлём в общий чат, при следующем старте тема создастся заново
                log.warning("тема %s пропала: %s", key, e.desc)
                self.topics[key] = None
                try:
                    self.conn.execute("UPDATE work.bot_topics SET thread_id = NULL WHERE key = %s", (key,))
                except Exception:
                    pass
                return self.bot.send(self.owner, text, thread_id=None, buttons=buttons, reply_to=reply_to)
            if "parse" in (e.desc or "").lower():
                # битая разметка не должна терять сообщение
                return self.bot.send(self.owner, strip_tags(text), thread_id=thread, buttons=buttons,
                                     reply_to=reply_to, parse_mode=None)
            raise

    def reply(self, msg: dict, text: str, buttons=None) -> dict:
        """Ответ в той же теме, что и сообщение George."""
        return self.send(self.key_of(msg.get("message_thread_id")), text, buttons=buttons,
                         reply_to=msg.get("message_id"))

    def edit(self, message_id: int, text: str | None, buttons=None):
        try:
            return self.bot.edit(self.owner, message_id, text, buttons)
        except TgError as e:
            if "not modified" in (e.desc or ""):
                return None
            log.warning("edit %s: %s", message_id, e.desc)
            return None

    def action(self, msg_thread: int | None = None, kind: str = "typing") -> None:
        try:
            self.bot.call("sendChatAction", chat_id=self.owner, action=kind, message_thread_id=msg_thread)
        except Exception:
            pass

    def notify_system(self, text: str, key: str | None = None) -> None:
        """Короткое сообщение в «Систему», не чаще раза в 10 минут на ключ.

        Идёт через outbox (ждёт утра в тихие часы); если база лежит, пробует напрямую.
        """
        k = key or text[:60]
        nowt = time.monotonic()
        if nowt - self._notices.get(k, -1e9) < NOTICE_EVERY:
            return
        self._notices[k] = nowt
        try:
            self.conn.execute("INSERT INTO work.outbox (topic, text) VALUES ('system', %s)", (text,))
        except Exception:
            try:
                self.send("system", text)
            except Exception:
                log.exception("не смог сообщить в «Систему»")


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def load(ctx: Ctx) -> dict:
    rows = ctx.conn.execute("SELECT key, thread_id FROM work.bot_topics").fetchall()
    ctx.topics.update({r["key"]: r["thread_id"] for r in rows})
    return ctx.topics


def ensure_topics(ctx: Ctx) -> dict:
    """Создаёт недостающие темы (config.TOPICS + config.DOMAINS) и кладёт их в work.bot_topics.

    Если createForumTopic не поддержан, темы живут без thread_id, и об этом
    один раз (когда строк ещё не было) говорится в «Системе».
    """
    conn = ctx.conn
    have = {r["key"]: r for r in conn.execute("SELECT key, title, thread_id FROM work.bot_topics").fetchall()}
    supported = True
    fresh_fallback = False
    for key, title in all_topics().items():
        row = have.get(key)
        if row and row["thread_id"] is not None:
            ctx.topics[key] = row["thread_id"]
            continue
        thread = None
        if supported:
            try:
                res = ctx.bot.call("createForumTopic", chat_id=ctx.owner, name=title)
                thread = res.get("message_thread_id")
            except TgError as e:
                supported = False
                log.warning("createForumTopic не работает (%s): живём без тем", e.desc)
        if thread is None and row is None:
            fresh_fallback = True
        conn.execute("""INSERT INTO work.bot_topics (key, title, thread_id) VALUES (%s, %s, %s)
                        ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, thread_id = EXCLUDED.thread_id""",
                     (key, title, thread))
        ctx.topics[key] = thread
    if not supported and fresh_fallback:
        try:
            ctx.send("system", "Темы в личке не включились (createForumTopic не поддержан), "
                               "пишу всё в общий чат. Включи режим тем у бота в BotFather и перезапусти бота.")
        except Exception:
            log.exception("не смог сообщить о темах")
    return ctx.topics


def topic_of(ctx: Ctx, message: dict) -> str:
    return ctx.key_of(message.get("message_thread_id"))


def send_to(ctx: Ctx, topic_key: str, text: str, buttons=None) -> dict:
    return ctx.send(topic_key, text, buttons=buttons)


def web_app_button(text: str = "Открыть список") -> dict | None:
    url = config.env("APP_URL")
    return {"text": text, "web_app": {"url": url}} if url else None


# ---------- общие подписи ----------
RU_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
RU_WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def short_date(d) -> str:
    """пт 23 окт"""
    return f"{RU_WD_SHORT[d.weekday()]} {d.day} {RU_MONTHS_SHORT[d.month - 1]}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def contexts(conn) -> dict[str, str]:
    rows = conn.execute("SELECT id, title FROM work.contexts WHERE active ORDER BY id").fetchall()
    return {r["id"]: r["title"] for r in rows}

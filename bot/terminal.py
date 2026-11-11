"""Бот-терминал WorkOS: python -m bot.terminal (systemd workos-bot, WORKOS_ROLE=bot).

Основной поток: long polling getUpdates (timeout 50), залочен на TELEGRAM_OWNER_ID,
чужие апдейты молча игнорируются. Смещение живёт в памяти: после перезапуска
Telegram передоставит неподтверждённое, а захват идемпотентен по id сообщения.

Фоновый поток (своё соединение с базой и свой httpx-клиент): outbox раз в 2 с
(тихие часы 1:00-8:00: ничего не шлёт), раз в ~20 с карточки proposed-действий
и автономные одобрения, раз в минуту напоминания, раз в 5 минут пинг healthchecks.
Ошибка обработчика никогда не роняет цикл: журнал + короткое сообщение в «Систему».
"""
from __future__ import annotations

import logging
import re
import signal
import threading
import time
from datetime import timedelta

import httpx

from core import config, timeutil as tu
from core.tg import Bot, TgError, btn, esc, scrub

from bot import ask, candidates, capture, cards, domains, me, plancard, rating, topics, voice
from bot.topics import Ctx, short_date, web_app_button

log = logging.getLogger("workos.bot")

ALLOWED_UPDATES = ["message", "callback_query", "edited_message"]
APPROVAL_PREFIXES = ("act", "pb", "rb")
POLL_TIMEOUT = 50
TICK_EVERY = 20
OUTBOX_EVERY = 2
REMIND_EVERY = 60
HEALTH_EVERY = 300
CONTENT = ("text", "photo", "voice", "audio", "video_note", "document", "video", "location", "contact")
COMMANDS = [
    {"command": "me", "description": "Лист персонажа"},
    {"command": "plan", "description": "План на сегодня"},
    {"command": "day", "description": "Оценка дня от -10 до +10"},
    {"command": "manual", "description": "Все действия на подтверждение"},
    {"command": "auto", "description": "Снять блокировку автономии"},
    {"command": "help", "description": "Что где"},
]
HELP = (
    "<b>Как со мной работать</b>\n"
    "«Входящие»: кидай всё подряд, текст, голос, фото, пересланное. Задача с датой сразу уходит в список.\n"
    "«План»: вставь секцию «Завтра» из вечернего плана, соберу карточку. /me: лист персонажа, /plan: день.\n"
    "/day +3 заметка: оценка дня от -10 до +10, после 00:30 спрошу сам.\n"
    "«Решения»: карточки действий, одно нажатие на решение.\n"
    "«Спросить»: вопрос к памяти.\n"
    "/manual: всё на подтверждение и запереть автономию · /auto: снять блокировку."
)


class _Stop(Exception):
    pass


def outbox_batch() -> int:
    return config.env_int("WORKOS_OUTBOX_BATCH", 5)


# ---------- периодическая работа (функции от ctx: в тестах и в фоновом потоке) ----------
# Чужие строки outbox (читатели, исполнитель, домены): только кнопки, которые они и
# правда шлют. Кнопки одобрения (act:, pb:, rb:) рисует только сам бот, url-кнопок
# нет ни у кого, web_app только на мини-приложение.
FOREIGN_PREFIXES = ("idc", "tk")


def foreign_prefixes() -> set[str]:
    return set(FOREIGN_PREFIXES) | set(config.DOMAINS)


def _buttons(raw, trusted: bool = True) -> list | None:
    """Кнопки строки outbox в формат Telegram. trusted=False: строка не от бота, фильтр."""
    if not raw or not isinstance(raw, list):
        return None
    allowed = foreign_prefixes()
    app_url = config.env("APP_URL")
    rows = []
    for row in raw:
        if isinstance(row, dict):
            row = [row]
        ok = []
        for b in row if isinstance(row, list) else []:
            if not isinstance(b, dict) or not b.get("text"):
                continue
            if b.get("callback_data") and len(str(b["callback_data"]).encode()) <= 64:
                data = str(b["callback_data"])
                if trusted or data.split(":", 1)[0] in allowed and ":" in data:
                    ok.append({"text": str(b["text"]), "callback_data": data})
            elif b.get("url"):
                if trusted:
                    ok.append({"text": str(b["text"]), "url": str(b["url"])})
            elif b.get("web_app"):
                wa = b["web_app"] if isinstance(b["web_app"], dict) else {"url": str(b["web_app"])}
                if trusted or (app_url and str(wa.get("url") or "") == app_url):
                    ok.append({"text": str(b["text"]), "web_app": {"url": str(wa.get("url") or "")}})
        if ok:
            rows.append(ok)
    return rows or None


def _backoff(ctx: Ctx, e: TgError) -> None:
    s = re.search(r"retry after (\d+)", e.desc or "")
    ctx.state["outbox_backoff"] = time.monotonic() + (int(s.group(1)) if s else 30)


def flush_outbox(ctx: Ctx, now=None, limit: int | None = None) -> int:
    """Отправить неотправленное из work.outbox; в тихие часы ждёт утра."""
    now = now or tu.now()
    if tu.is_quiet(now):
        return 0
    if ctx.state.get("outbox_backoff", 0) > time.monotonic():
        return 0
    conn = ctx.conn
    rows = conn.execute("""SELECT id, topic, text, buttons, (created_by = current_user) AS own
                           FROM work.outbox WHERE sent_at IS NULL
                           ORDER BY created_at, id LIMIT %s""", (limit or outbox_batch(),)).fetchall()
    sent = 0
    for r in rows:
        try:
            m = ctx.send(r["topic"], r["text"], buttons=_buttons(r["buttons"], trusted=bool(r["own"])))
        except TgError as e:
            if e.code == 429:
                _backoff(ctx, e)
                break
            if e.code == 400:
                # кривое сообщение не должно застревать в очереди навсегда
                log.warning("outbox %s не отправить: %s", r["id"], scrub(e.desc))
                conn.execute("UPDATE work.outbox SET sent_at = %s WHERE id = %s", (now, r["id"]))
                ctx.notify_system(f"Сообщение outbox #{r['id']} отброшено: {esc(scrub(e.desc))}",
                                  key=f"ob400:{r['id']}")
                continue
            log.warning("outbox: %s", e)
            break
        except httpx.HTTPError as e:
            log.warning("outbox: сеть: %s", e)
            break
        conn.execute("UPDATE work.outbox SET sent_at = %s, tg_message_id = %s WHERE id = %s AND sent_at IS NULL",
                     (now, m.get("message_id"), r["id"]))
        sent += 1
    return sent


def reminders(ctx: Ctx, now=None) -> int:
    """Задачи с наступившим remind_at -> «⏰ ...» в «План»; тихие часы ждут утра.

    Одно кривое напоминание не держит очередь: 400 -> ещё раз без кнопки мини-приложения,
    не вышло -> отмечаем отправленным и пишем в «Систему»; 429 -> пауза, как у outbox."""
    now = now or tu.now()
    if tu.is_quiet(now):
        return 0
    if ctx.state.get("outbox_backoff", 0) > time.monotonic():
        return 0
    conn = ctx.conn
    rows = conn.execute("""SELECT id, title, remind_at FROM work.tasks
                           WHERE remind_at <= %s AND reminded_at IS NULL AND done_at IS NULL AND dropped_at IS NULL
                           ORDER BY remind_at LIMIT 20""", (now,)).fetchall()
    n = 0
    for r in rows:
        text = f"⏰ {esc(r['title'])}"
        if now - r["remind_at"] > timedelta(minutes=30):
            text += f"\n<i>напоминание на {short_date(tu.local(r['remind_at']).date())} {tu.hm(r['remind_at'])}</i>"
        base_rows = [[btn("Через час", f"tk:{r['id']}:1h"), btn("На завтра", f"tk:{r['id']}:tmr")]]
        wa = web_app_button()
        attempts = [[[wa]] + base_rows, base_rows] if wa else [base_rows]
        sent, stop, err = False, False, None
        for rows_b in attempts:
            try:
                ctx.send("plan", text, buttons=rows_b)
                sent = True
                break
            except TgError as e:
                if e.code == 429:
                    _backoff(ctx, e)
                    stop = True
                    break
                if e.code == 400:
                    err = e
                    continue                      # ещё раз без кнопки мини-приложения
                log.warning("напоминание %s: %s", r["id"], scrub(e))
                stop = True
                break
            except httpx.HTTPError as e:
                log.warning("напоминание %s: сеть: %s", r["id"], scrub(e))
                stop = True
                break
        if stop:
            break
        conn.execute("UPDATE work.tasks SET reminded_at = %s WHERE id = %s AND reminded_at IS NULL", (now, r["id"]))
        if sent:
            n += 1
        else:
            desc = scrub(err.desc if err else "?")
            log.warning("напоминание %s не отправить: %s", r["id"], desc)
            ctx.notify_system(f"Напоминание «{esc(r['title'])}» не ушло, отметил отправленным: {esc(desc)}",
                              key=f"rm400:{r['id']}")
    return n


def on_task_button(ctx: Ctx, cq: dict, parts: list[str]) -> str | None:
    """tk:<task_id>:1h|tmr|keep — кнопки напоминания и «не влезло» из живого дня
    (отметку «сделано» ставит мини-приложение)."""
    conn = ctx.conn
    try:
        tid, op = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        return "Не понял кнопку"
    row = conn.execute("""SELECT id, title, due, remind_at FROM work.tasks
                          WHERE id = %s AND done_at IS NULL AND dropped_at IS NULL""", (tid,)).fetchone()
    if not row:
        return "Задача уже закрыта"
    if op == "1h":
        new = tu.now() + timedelta(hours=1)
        conn.execute("UPDATE work.tasks SET remind_at = %s, reminded_at = NULL WHERE id = %s", (new, tid))
        note = f"напомню в {tu.hm(new)}"
    elif op == "tmr":
        d = tu.logical_today() + timedelta(days=1)
        new = shift_remind(row["remind_at"], row["due"], d)
        conn.execute("""UPDATE work.tasks SET due = %s, remind_at = %s, reminded_at = NULL,
                               planned_start = NULL, planned_end = NULL, planned_by = NULL, planned_at = now()
                        WHERE id = %s""", (d, new, tid))
        note = f"перенёс на {short_date(d)}" + (f" {tu.hm(new)}" if new else "")
    elif op == "keep":
        note = "оставил в списке на сегодня без времени"
    else:
        return "Не понял кнопку"
    msg_id = (cq.get("message") or {}).get("message_id")
    if msg_id:
        wa = web_app_button()
        ctx.edit(msg_id, f"⏰ {esc(row['title'])} · {note}", [[wa]] if wa else [])
    return note


def shift_remind(remind_at, old_due, new_due):
    """«На завтра»: напоминание едет вместе со сроком на столько же дней.

    Время 00:00-03:59 относится к следующей календарной дате после логического дня,
    поэтому дату не пересобираем из new_due, а сдвигаем на (new_due - old_due)."""
    if not remind_at:
        return None
    loc = tu.local(remind_at)
    base = old_due or tu.logical_date(remind_at)
    t = loc.time().replace(tzinfo=None)
    new = tu.at_local(loc.date() + timedelta(days=(new_due - base).days), t)
    if new <= tu.now():
        # срок был впереди напоминания: ставим то же время в новый логический день
        new = tu.at_local(new_due + timedelta(days=1) if t.hour < config.LOGICAL_DAY_END_HOUR else new_due, t)
    return new


def health_ping() -> None:
    url = config.env("HEALTHCHECKS_URL")
    if not url:
        return
    try:
        httpx.get(url, timeout=10)
    except Exception:
        pass


def plan_text(conn) -> str:
    d = tu.logical_today()
    rows = conn.execute("""SELECT title, planned_start, remind_at, done_at FROM work.tasks
                           WHERE due = %s AND dropped_at IS NULL
                           ORDER BY planned_start NULLS LAST, remind_at NULLS LAST, id""", (d,)).fetchall()
    done = sum(1 for r in rows if r["done_at"])
    out = [f"<b>{tu.ru_date(d)}</b> · сделано {done} из {len(rows)}"]
    for r in rows:
        t = r["planned_start"] or r["remind_at"]
        out.append(("✓ " if r["done_at"] else "▫️ ") + (f"{tu.hm(t)} " if t else "") + esc(r["title"]))
    week = conn.execute("""SELECT count(*) AS n FROM work.tasks WHERE week_of = %s AND due IS NULL
                           AND done_at IS NULL AND dropped_at IS NULL""", (tu.week_monday(d),)).fetchone()["n"]
    if week:
        out.append(f"\nНа неделе без дня: {week}")
    if not rows:
        out.append("Пусто. Вставь сюда секцию «Завтра» из вечернего плана, соберу карточку.")
    return "\n".join(out)


# ---------- терминал ----------
class Terminal:
    def __init__(self, bot=None, ctx: Ctx | None = None):
        self.bot = bot or (ctx.bot if ctx else Bot())
        self.ctx = ctx or Ctx(self.bot)
        self.offset: int | None = None
        self.stop = threading.Event()
        self._polling = False
        self._last: dict[str, float] = {}

    # --- старт ---
    def start(self) -> None:
        ctx = self.ctx
        topics.load(ctx)
        topics.ensure_topics(ctx)
        url = config.env("APP_URL")
        if url:
            try:
                ctx.bot.call("setChatMenuButton", chat_id=ctx.owner,
                             menu_button={"type": "web_app", "text": "Квесты", "web_app": {"url": url}})
            except TgError as e:
                log.warning("setChatMenuButton: %s", e.desc)
        try:
            ctx.bot.call("setMyCommands", commands=COMMANDS, scope={"type": "chat", "chat_id": ctx.owner})
        except TgError as e:
            log.warning("setMyCommands: %s", e.desc)

    # --- вход ---
    def _owner_msg(self, msg: dict) -> bool:
        return ((msg.get("from") or {}).get("id") == self.ctx.owner
                and (msg.get("chat") or {}).get("id") == self.ctx.owner)

    def handle_update(self, u: dict) -> None:
        try:
            if "callback_query" in u:
                self.on_callback(u["callback_query"])
            elif "message" in u:
                self.on_message(u["message"])
            elif "edited_message" in u:
                self.on_edited(u["edited_message"])
        except Exception as e:
            log.exception("апдейт %s", u.get("update_id"))
            self.ctx.notify_system(f"Ошибка обработки: {esc(type(e).__name__)}: {esc(scrub(e)[:200])}",
                                   key=f"err:{type(e).__name__}")

    def on_message(self, msg: dict) -> None:
        if not self._owner_msg(msg):
            return                                   # чужие: молча
        if not any(msg.get(k) for k in CONTENT):
            return                                   # служебные: тема создана, закреп и т.п.
        ctx = self.ctx
        key = topics.topic_of(ctx, msg)
        text = (msg.get("text") or "").strip()
        no_topics = ctx.thread_of("inbox") is None
        if text.startswith("/"):
            return self.command(msg, text)
        if domains.is_domain(key):
            domains.handle_message(ctx, msg, key)
            return
        if msg.get("voice") or msg.get("audio") or msg.get("video_note"):
            voice.handle(ctx, msg)
            return
        if key == "ask" or (no_topics and text.startswith("?")):
            ask.handle(ctx, msg)
            return
        if (key == "plan" or no_topics) and plancard.handle_paste(ctx, msg):
            return
        if msg.get("reply_to_message") or key == "decisions":
            if cards.on_fix_text(ctx, msg, allow_pending=(key == "decisions")):
                return
        capture.handle(ctx, msg)

    def on_edited(self, msg: dict) -> None:
        if not self._owner_msg(msg):
            return
        if domains.is_domain(topics.topic_of(self.ctx, msg)):
            return
        capture.on_edit(self.ctx, msg)

    def command(self, msg: dict, text: str) -> None:
        ctx = self.ctx
        cmd = text.split()[0][1:].split("@")[0].lower()
        wa = web_app_button()
        if cmd == "me":
            ctx.reply(msg, me.sheet(ctx.conn))
        elif cmd == "plan":
            ctx.reply(msg, plan_text(ctx.conn), [[wa]] if wa else None)
        elif cmd == "day":
            rating.handle_command(ctx, msg, text)
        elif cmd == "manual":
            ctx.reply(msg, cards.manual(ctx))
        elif cmd == "auto":
            ctx.reply(msg, cards.auto(ctx))
        elif cmd in ("help", "start"):
            ctx.reply(msg, HELP, [[wa]] if wa else None)
        else:
            ctx.reply(msg, "Такой команды нет. /help")

    def on_callback(self, cq: dict) -> None:
        ctx = self.ctx
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if (cq.get("from") or {}).get("id") != ctx.owner or (chat is not None and chat != ctx.owner):
            return                                   # одобрять может только George
        data = cq.get("data") or ""
        parts = data.split(":")
        prefix = parts[0]
        handlers = {
            "cap": capture.on_callback, "pb": plancard.on_callback, "act": cards.on_callback,
            "rb": cards.on_rollback, "idc": candidates.on_callback, "ask": ask.on_callback,
            "tk": on_task_button, "rt": rating.on_callback,
        }
        answer = None
        try:
            if prefix in APPROVAL_PREFIXES and not self._from_card(cq, parts):
                answer = "Кнопка не с карточки бота"
            elif prefix in handlers:
                answer = handlers[prefix](ctx, cq, parts)
            elif domains.is_domain(prefix):
                answer = domains.on_callback(ctx, cq, prefix)
            else:
                answer = "Кнопка устарела"
        except Exception:
            answer = "Ошибка, подробности в «Системе»"
            raise
        finally:
            ctx.bot.answer(cq.get("id"), answer)

    def _from_card(self, cq: dict, parts: list[str]) -> bool:
        """act:/pb:/rb: принимаются только с сообщения, чья строка outbox создана ботом
        и ссылается на то же действие (подделанную строку outbox бот не признает)."""
        try:
            aid = int(parts[1])
        except (IndexError, ValueError):
            return False
        return cards.is_card(self.ctx.conn, aid, (cq.get("message") or {}).get("message_id"))

    # --- периодика ---
    def _due(self, name: str, every: float, force: bool) -> bool:
        nowm = time.monotonic()
        if force or nowm - self._last.get(name, -1e9) >= every:
            self._last[name] = nowm
            return True
        return False

    def _safe(self, ctx: Ctx, name: str, fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            log.exception("периодика %s", name)
            ctx.notify_system(f"Сбой в фоне ({name}): {esc(type(e).__name__)}: {esc(scrub(e)[:200])}",
                              key=f"bg:{name}")
            return None

    def tick(self, ctx: Ctx | None = None, force: bool = False) -> dict:
        ctx = ctx or self.ctx
        out = {"cards": self._safe(ctx, "cards", cards.process_proposed, ctx)}
        if self._due("remind", REMIND_EVERY, force):
            out["reminders"] = self._safe(ctx, "reminders", reminders, ctx)
            out["rating_ask"] = self._safe(ctx, "rating", rating.ask, ctx)
        out["outbox"] = self._safe(ctx, "outbox", flush_outbox, ctx)
        if self._due("health", HEALTH_EVERY, force):
            health_ping()
        return out

    def _ticker(self) -> None:
        ctx = self.ctx.clone(Bot(self.bot.token, self.bot.base))
        last_full = -1e9
        while not self.stop.is_set():
            if time.monotonic() - last_full >= TICK_EVERY:
                self.tick(ctx)
                last_full = time.monotonic()
            else:
                self._safe(ctx, "outbox", flush_outbox, ctx)
            self.stop.wait(OUTBOX_EVERY)
        ctx.close()

    # --- цикл ---
    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        self._polling = True
        try:
            updates = self.bot.call("getUpdates", offset=self.offset, timeout=timeout,
                                    allowed_updates=ALLOWED_UPDATES)
        finally:
            self._polling = False
        for u in updates:
            self.offset = u["update_id"] + 1
            self.handle_update(u)
            if self.stop.is_set():
                break
        return len(updates)

    def _on_signal(self, signum, frame):
        log.info("сигнал %s: выхожу", signum)
        self.stop.set()
        if self._polling:
            raise _Stop()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        delay = 5
        while not self.stop.is_set():
            try:
                self.start()
                break
            except _Stop:
                return
            except Exception as e:
                log.warning("старт не удался: %s, повтор через %s с", e, delay)
                self.stop.wait(delay)
                delay = min(delay * 2, 300)
        ticker = threading.Thread(target=self._ticker, name="workos-ticker", daemon=True)
        ticker.start()
        log.info("бот-терминал запущен, владелец %s", self.ctx.owner)
        while not self.stop.is_set():
            try:
                self.poll_once()
            except _Stop:
                break
            except TgError as e:
                log.warning("getUpdates: %s", e)
                self.stop.wait(15 if e.code == 409 else 5)
            except httpx.HTTPError as e:
                log.warning("getUpdates: сеть: %s", e)
                self.stop.wait(5)
            except Exception:
                log.exception("цикл")
                self.stop.wait(5)
        # подтвердить обработанные апдейты, чтобы после рестарта они не пришли снова
        if self.offset is not None:
            try:
                self.bot.call("getUpdates", offset=self.offset, timeout=0, limit=1)
            except Exception:
                pass
        ticker.join(timeout=10)
        self.ctx.close()
        log.info("бот-терминал остановлен")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Terminal().run()


if __name__ == "__main__":
    main()

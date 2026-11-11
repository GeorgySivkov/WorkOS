"""Бот-терминал: фейковый Telegram через httpx.MockTransport, модель через llm.set_fake,
всё под ролью workos_bot (role_env), чтобы права роли проверялись на каждом запросе."""
from __future__ import annotations

import io
import itertools
import json
import os
import stat
import sys
import time
import types
import wave
from datetime import date, datetime, time as dtime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from core import config, llm, timeutil as tu
from core.db import payload_hash_sql
from core.tg import Bot

from bot import capture, terminal
from bot.terminal import Terminal
from bot.topics import Ctx

OWNER = 111
UTC = timezone.utc
JSON_KEYS = {"reply_markup", "menu_button", "commands", "scope", "allowed_updates",
             "link_preview_options", "reply_parameters"}
H = payload_hash_sql("payload")


class FakeTG:
    """Telegram Bot API на httpx.MockTransport: записывает вызовы, отвечает правдоподобно."""

    def __init__(self, topics: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.topics = topics
        self.ids = itertools.count(1000)
        self.threads = itertools.count(501)
        self.file_bytes = b"voice"
        self.updates: list[dict] = []
        self.fail = None          # fn(method, form) -> httpx.Response | None: подставить ошибку

    @staticmethod
    def error(code: int, desc: str) -> httpx.Response:
        return httpx.Response(code, json={"ok": False, "error_code": code, "description": desc})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/"):
            return httpx.Response(200, content=self.file_bytes)
        method = path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        for k in JSON_KEYS & form.keys():
            form[k] = json.loads(form[k])
        self.calls.append((method, form))
        if self.fail:
            bad = self.fail(method, form)
            if bad is not None:
                return bad
        if method == "createForumTopic":
            if not self.topics:
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                 "description": "Bad Request: the chat is not a forum"})
            return self.ok({"message_thread_id": next(self.threads), "name": form.get("name")})
        if method == "sendMessage":
            return self.ok({"message_id": next(self.ids), "chat": {"id": int(form["chat_id"])},
                            "text": form.get("text")})
        if method == "getFile":
            return self.ok({"file_id": form["file_id"], "file_path": "voice/f.oga"})
        if method == "getUpdates":
            got, self.updates = self.updates, []
            return self.ok(got)
        return self.ok(True)

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def last(self, method: str) -> dict:
        got = self.of(method)
        assert got, f"{method} не вызывался"
        return got[-1]

    def reset(self):
        self.calls.clear()


def buttons(params: dict) -> list[str]:
    kb = (params.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b.get("callback_data") for row in kb for b in row if b.get("callback_data")]


def btn_by_prefix(params: dict, prefix: str) -> str:
    for d in buttons(params):
        if d.startswith(prefix):
            return d
    raise AssertionError(f"нет кнопки {prefix}: {buttons(params)}")


class Clock:
    def __init__(self, monkeypatch):
        self.value = tu.at_local(date(2026, 10, 21), dtime(15, 0))      # среда, день
        monkeypatch.setattr(tu, "now", lambda: self.value)

    def set(self, d: date, hh: int, mm: int = 0):
        self.value = tu.at_local(d, dtime(hh, mm))

    def real(self):
        self.value = datetime.now(UTC)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def make_bot(fake: FakeTG) -> Bot:
    bot = Bot(token="123:TEST", base="http://tg.test")
    bot.http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return bot


@pytest.fixture
def tg():
    return FakeTG()


@pytest.fixture
def term(tg, role_env, monkeypatch, clock):
    role_env("workos_bot")
    monkeypatch.setenv("APP_URL", "https://app.test/")
    monkeypatch.setenv("HEALTHCHECKS_URL", "")
    bot = make_bot(tg)
    ctx = Ctx(bot, owner=OWNER)
    t = Terminal(bot=bot, ctx=ctx)
    t.start()
    tg.reset()
    yield t
    llm.set_fake(None)
    ctx.close()


_mid = itertools.count(1)


def message(term, text=None, topic="inbox", frm=OWNER, **extra) -> dict:
    m = {"message_id": next(_mid), "from": {"id": frm, "first_name": "George"},
         "chat": {"id": frm, "type": "private"}, "date": int(time.time())}
    if text is not None:
        m["text"] = text
    thread = term.ctx.thread_of(topic)
    if thread:
        m["message_thread_id"] = thread
        m["is_topic_message"] = True
    m.update(extra)
    return m


def send(term, msg) -> None:
    term.handle_update({"update_id": next(_mid), "message": msg})


def press(term, data: str, message_id: int, frm=OWNER) -> None:
    term.handle_update({"update_id": next(_mid), "callback_query": {
        "id": f"cb{next(_mid)}", "from": {"id": frm}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": OWNER}}}})


def fake_model(answers: dict):
    def fn(task, system, user):
        val = answers[task]
        return json.dumps(val(user) if callable(val) else val, ensure_ascii=False)
    llm.set_fake(fn)


# ---------------- 1.1: замок, темы, старт ----------------
def test_owner_lock_ignores_strangers(term, tg, admin):
    send(term, message(term, "привет", frm=222))
    press(term, "act:1:ok:deadbeef", 5, frm=222)
    assert tg.calls == []
    assert admin.execute("SELECT count(*) AS n FROM work.events").fetchone()["n"] == 0


def test_topics_created_and_stored(tg, role_env, admin, monkeypatch, clock):
    role_env("workos_bot")
    monkeypatch.setenv("APP_URL", "https://app.test/")
    bot = make_bot(tg)
    t = Terminal(bot=bot, ctx=Ctx(bot, owner=OWNER))
    t.start()
    made = tg.of("createForumTopic")
    assert [m["name"] for m in made] == list(config.TOPICS.values())
    assert all(int(m["chat_id"]) == OWNER for m in made)
    rows = {r["key"]: r["thread_id"] for r in admin.execute("SELECT key, thread_id FROM work.bot_topics")}
    assert set(rows) == set(config.TOPICS) and all(rows.values())
    menu = tg.last("setChatMenuButton")["menu_button"]
    assert menu["type"] == "web_app" and menu["text"] == "Квесты" and menu["web_app"]["url"] == "https://app.test/"
    assert {c["command"] for c in tg.last("setMyCommands")["commands"]} == {"me", "plan", "day", "manual", "auto", "help"}
    # второй старт ничего не создаёт заново
    tg.reset()
    t2 = Terminal(bot=bot, ctx=Ctx(bot, owner=OWNER))
    t2.start()
    assert tg.of("createForumTopic") == []
    assert t2.ctx.thread_of("plan") == rows["plan"]
    t.ctx.close()
    t2.ctx.close()


def test_topics_fallback_without_forum(role_env, admin, clock):
    role_env("workos_bot")
    fake = FakeTG(topics=False)
    bot = make_bot(fake)
    t = Terminal(bot=bot, ctx=Ctx(bot, owner=OWNER))
    t.start()
    rows = admin.execute("SELECT key, thread_id FROM work.bot_topics").fetchall()
    assert len(rows) == len(config.TOPICS) and all(r["thread_id"] is None for r in rows)
    notices = fake.of("sendMessage")
    assert len(notices) == 1 and "Темы" in notices[0]["text"] and "message_thread_id" not in notices[0]
    fake.reset()
    t.start()                                       # повторный старт: без второго сообщения
    assert fake.of("sendMessage") == []
    # без тем захват всё равно работает и отвечает в общий чат
    fake_model({"capture": {"kind": "idea", "context_id": "personal", "confidence": 0.9, "title": "x"}})
    send(t, message(t, "идея про бота"))
    assert "message_thread_id" not in fake.last("sendMessage")
    t.ctx.close()
    llm.set_fake(None)


# ---------------- 1.2: захват ----------------
TASK_ANSWER = {"kind": "task", "context_id": "infatica", "confidence": 0.9, "title": "Позвонить в банк",
               "due": "2026-10-22", "time": "09:00", "size": "S", "cell_id": None}


def test_capture_task_with_date(term, tg, admin):
    fake_model({"capture": TASK_ANSWER})
    m = message(term, "позвонить в банк в пт в 10")
    send(term, m)
    ev = admin.execute("SELECT * FROM work.events WHERE source_id='telegram:inbox'").fetchone()
    assert ev["external_id"] == str(m["message_id"]) and ev["kind"] == "note" and ev["context_id"] == "infatica"
    cls = ev["meta"]["class"]
    assert cls["kind"] == "task" and cls["due"] == "2026-10-23" and cls["time"] == "10:00"   # ruparse победил
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["source"] == "capture" and task["due"] == date(2026, 10, 23)
    assert task["remind_at"] == tu.at_local(date(2026, 10, 23), dtime(10, 0))
    assert task["context_id"] == "infatica" and ev["meta"]["task_id"] == task["id"]
    reply = tg.last("sendMessage")
    assert int(reply["message_thread_id"]) == term.ctx.thread_of("inbox")
    assert reply["reply_parameters"]["message_id"] == m["message_id"]
    assert "Задача" in reply["text"] and "пт 23 окт 10:00" in reply["text"] and "Infatica" in reply["text"]
    eid = ev["id"]
    assert {f"cap:{eid}:task", f"cap:{eid}:idea", f"cap:{eid}:fact"} <= set(buttons(reply))
    assert not any(":ctx:" in b for b in buttons(reply))            # уверена: без кнопок контекста
    # повторная доставка после рестарта: ничего не задваивается
    tg.reset()
    send(term, m)
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 1
    assert tg.of("sendMessage") == []


def test_capture_low_confidence_has_context_buttons_and_photo(term, tg, admin):
    fake_model({"capture": {"kind": "fact", "context_id": "alfa", "confidence": 0.4, "title": "SLA"}})
    send(term, message(term, None, caption="SLA поддержки 15 минут https://x.y", photo=[{"file_id": "p1"}]))
    ev = admin.execute("SELECT * FROM work.events").fetchone()
    assert ev["meta"]["has_photo"] and ev["meta"]["has_link"] and ev["body"].startswith("SLA")
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 0
    assert f"cap:{ev['id']}:ctx:infatica" in buttons(tg.last("sendMessage"))


def test_correction_to_idea_drops_task(term, tg, admin):
    fake_model({"capture": TASK_ANSWER})
    send(term, message(term, "позвонить в банк завтра в 10"))
    ev = admin.execute("SELECT id, meta FROM work.events").fetchone()
    reply_id = ev["meta"]["reply_id"]
    press(term, f"cap:{ev['id']}:idea", reply_id)
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["dropped_at"] is not None
    meta = admin.execute("SELECT meta FROM work.events WHERE id=%s", (ev["id"],)).fetchone()["meta"]
    assert meta["class_before"]["kind"] == "task" and meta["class_after"]["kind"] == "idea"
    assert meta["class"]["kind"] == "idea" and meta["corrections"][0]["op"] == "idea"
    edited = tg.last("editMessageText")
    assert int(edited["message_id"]) == reply_id and "Идея" in edited["text"]
    assert tg.last("answerCallbackQuery")["text"] == "Поправил"
    # обратно в задачу: та же задача возвращается; контекст правится кнопкой
    press(term, f"cap:{ev['id']}:task", reply_id)
    press(term, f"cap:{ev['id']}:ctx:alfa", reply_id)
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["dropped_at"] is None and task["context_id"] == "alfa"
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 1
    ev2 = admin.execute("SELECT context_id, meta FROM work.events").fetchone()
    assert ev2["context_id"] == "alfa" and ev2["meta"]["class_before"]["kind"] == "task"


def test_capture_cell_choice(term, tg, admin):
    admin.execute("INSERT INTO rpg.cells (id, parent, title, kind) VALUES ('C', NULL, 'Квест', 'quest'), "
                  "('B', 'C', 'Карьера', 'attribute'), ('B1', 'B', 'Резюме', 'milestone')")
    fake_model({"capture": dict(TASK_ANSWER, cell_id="B")})
    send(term, message(term, "обновить резюме завтра"))
    ev = admin.execute("SELECT id, meta FROM work.events").fetchone()
    assert "клетка «Карьера»" in tg.last("sendMessage")["text"]
    press(term, f"cap:{ev['id']}:cell", ev["meta"]["reply_id"])
    assert f"cap:{ev['id']}:c:B1" in buttons(tg.last("editMessageReplyMarkup"))
    press(term, f"cap:{ev['id']}:c:B1", ev["meta"]["reply_id"])
    assert admin.execute("SELECT cell_id FROM work.tasks").fetchone()["cell_id"] == "B1"


def test_forwarded_message_actor_raw(term, admin):
    fake_model({"capture": {"kind": "fact", "context_id": "infatica", "confidence": 0.9, "title": "релиз"}})
    send(term, message(term, "релиз SDK в четверг", forward_origin={
        "type": "user", "date": 1761000000, "sender_user": {"id": 5, "first_name": "Ян", "last_name": "Петров"}}))
    send(term, message(term, "что-то ещё", forward_origin={"type": "hidden_user", "date": 1,
                                                           "sender_user_name": "Денис"}))
    rows = admin.execute("SELECT actor_raw, meta FROM work.events ORDER BY id").fetchall()
    assert [r["actor_raw"] for r in rows] == ["Ян Петров", "Денис"]
    assert all(r["meta"]["forwarded"] for r in rows)


def test_capture_survives_model_failure(term, tg, admin):
    def boom(task, system, user):
        raise llm.LLMError("нет сети")
    llm.set_fake(boom)
    send(term, message(term, "купить корм завтра"))
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["due"] == date(2026, 10, 22) and task["title"] == "купить корм"
    assert "модель недоступна" in tg.last("sendMessage")["text"]


def test_voice_not_configured(term, tg, admin, monkeypatch):
    monkeypatch.setenv("WHISPER_BIN", "/nonexistent/whisper-cli")
    send(term, message(term, None, voice={"file_id": "v1", "duration": 3}))
    ev = admin.execute("SELECT meta FROM work.events").fetchone()
    assert ev["meta"]["voice"] is True
    assert "расшифровка не настроена" in tg.last("sendMessage")["text"]


@pytest.mark.skipif(not os.path.exists("/usr/bin/ffmpeg") and not os.environ.get("FFMPEG_BIN"),
                    reason="нет ffmpeg")
def test_voice_transcribed_to_task(term, tg, admin, monkeypatch, tmp_path):
    script = tmp_path / "whisper-cli"
    script.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = "-of" ] && out="$2"; shift; done\n'
                      'printf "позвонить маме завтра в 10" > "$out.txt"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    monkeypatch.setenv("WHISPER_BIN", str(script))
    monkeypatch.setenv("WHISPER_MODEL", str(model))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    tg.file_bytes = buf.getvalue()
    fake_model({"capture": dict(TASK_ANSWER, title="Позвонить маме", context_id="personal")})
    send(term, message(term, None, voice={"file_id": "v1", "duration": 1}))
    ev = admin.execute("SELECT body, meta FROM work.events").fetchone()
    assert ev["body"] == "позвонить маме завтра в 10" and ev["meta"]["voice"] is True
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["due"] == date(2026, 10, 22) and task["remind_at"] == tu.at_local(date(2026, 10, 22), dtime(10))


# ---------------- 1.4: мост плана ----------------
PLAN = """Завтра
- [ ] Отчёт по SDK (M)
- [ ] 10:00 Созвон с Яном ~1 ч
- [x] уже сделано
- [ ] Купить корм
Эта неделя
- [ ] Разобрать почту
"""


def _paste_plan(term, tg, clock):
    clock.set(date(2026, 10, 22), 21)               # четверг вечером
    send(term, message(term, PLAN, topic="plan"))
    return tg.last("sendMessage")


def test_plan_bridge_select_subset(term, tg, admin, clock):
    card = _paste_plan(term, tg, clock)
    act = admin.execute(f"SELECT *, left({H}, 8) AS h8 FROM work.actions").fetchone()
    p = act["payload"]
    assert act["type"] == "plan_batch" and act["status"] == "proposed" and p["for_date"] == "2026-10-23"
    assert [i["title"] for i in p["items"]] == ["Отчёт по SDK", "Созвон с Яном", "Купить корм"]
    assert p["items"][0]["size"] == "M" and p["items"][1]["size"] == "M"
    assert p["items"][1]["start"] == tu.at_local(date(2026, 10, 23), dtime(10)).isoformat()
    assert p["items"][1]["fixed"] is True and "fixed" not in p["items"][0]
    assert [i["title"] for i in p["week_items"]] == ["Разобрать почту"]
    assert int(card["message_thread_id"]) == term.ctx.thread_of("plan")
    assert "3 задачи на пт, 23 октября" in card["text"]
    h8 = act["h8"]
    assert f"pb:{act['id']}:ok:{h8}" in buttons(card)
    ob = admin.execute("SELECT * FROM work.outbox WHERE ref_action=%s", (act["id"],)).fetchone()
    assert ob["sent_at"] is not None and ob["tg_message_id"] is not None and ob["created_by"] == "workos_bot"
    card_id = ob["tg_message_id"]
    # «Выбрать» -> тумблеры, убираем «Купить корм» (индекс 2) -> «Готово»
    press(term, btn_by_prefix(card, f"pb:{act['id']}:sel:"), card_id)
    sel = tg.last("editMessageText")
    press(term, btn_by_prefix(sel, f"pb:{act['id']}:t:2:"), card_id)
    tog = tg.last("editMessageText")
    assert "<s>" in tog["text"]
    press(term, btn_by_prefix(tog, f"pb:{act['id']}:go:"), card_id)
    tasks = admin.execute("SELECT * FROM work.tasks ORDER BY id").fetchall()
    assert [t["title"] for t in tasks] == ["Отчёт по SDK", "Созвон с Яном", "Разобрать почту"]
    assert all(t["source"] == "plan" and t["action_id"] == act["id"] for t in tasks)
    assert tasks[0]["due"] == date(2026, 10, 23) and tasks[0]["planned_start"] is None
    assert tasks[1]["planned_start"] == tu.at_local(date(2026, 10, 23), dtime(10))
    assert tasks[1]["planned_by"] == "manual"          # время George написал сам: replan его не двигает
    assert tasks[2]["due"] is None and tasks[2]["week_of"] == date(2026, 10, 19)
    a2 = admin.execute("SELECT status, approved_hash, result, " + H + " AS h FROM work.actions").fetchone()
    assert a2["status"] == "executed" and a2["approved_hash"] == a2["h"]
    assert a2["result"]["task_ids"] == [t["id"] for t in tasks]
    oc = admin.execute("SELECT * FROM work.outcomes").fetchone()
    assert oc["verdict"] == "edited" and "- Купить корм" in oc["diff"]
    assert "Убрал: Купить корм" in tg.last("editMessageText")["text"]
    # повторное нажатие: ничего не задваивается
    press(term, btn_by_prefix(tog, f"pb:{act['id']}:go:"), card_id)
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 3


def test_plan_accept_all_and_reject(term, tg, admin, clock):
    card = _paste_plan(term, tg, clock)
    act = admin.execute("SELECT id FROM work.actions").fetchone()
    card_id = admin.execute("SELECT tg_message_id FROM work.outbox").fetchone()["tg_message_id"]
    press(term, btn_by_prefix(card, f"pb:{act['id']}:ok:"), card_id)
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 4
    assert admin.execute("SELECT verdict FROM work.outcomes").fetchone()["verdict"] == "accepted"
    # второй план: «Не надо»
    send(term, message(term, PLAN.replace("Купить корм", "Купить кофе"), topic="plan"))
    card2 = tg.last("sendMessage")
    a2 = admin.execute("SELECT id FROM work.actions ORDER BY id DESC").fetchone()["id"]
    cid2 = admin.execute("SELECT tg_message_id FROM work.outbox WHERE ref_action=%s", (a2,)).fetchone()["tg_message_id"]
    press(term, btn_by_prefix(card2, f"pb:{a2}:no:"), cid2)
    assert admin.execute("SELECT status FROM work.actions WHERE id=%s", (a2,)).fetchone()["status"] == "rejected"
    assert admin.execute("SELECT verdict FROM work.outcomes WHERE action_id=%s", (a2,)).fetchone()["verdict"] == "rejected"
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 4


def test_plan_hash_mismatch_refused(term, tg, admin, clock):
    card = _paste_plan(term, tg, clock)
    act = admin.execute("SELECT id FROM work.actions").fetchone()
    card_id = admin.execute("SELECT tg_message_id FROM work.outbox").fetchone()["tg_message_id"]
    # после показа карточки payload поменялся (например, планировщик пересчитал)
    admin.execute("""UPDATE work.actions SET payload = jsonb_set(payload, '{items,0,title}', '"Удалить прод"')""")
    tg.reset()
    press(term, btn_by_prefix(card, f"pb:{act['id']}:ok:"), card_id)
    row = admin.execute("SELECT status, approved_at FROM work.actions").fetchone()
    assert row["status"] == "proposed" and row["approved_at"] is None
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 0
    assert "одобрение не принято" in tg.last("answerCallbackQuery")["text"]
    new_card = tg.last("sendMessage")                 # новая карточка с новым текстом и хешем
    assert "Удалить прод" in new_card["text"]
    r = admin.execute(f"SELECT left({H}, 8) AS h, shown_hash, approved_hash, {H} AS full FROM work.actions").fetchone()
    assert f"pb:{act['id']}:ok:{r['h']}" in buttons(new_card) and r["shown_hash"] == r["full"]
    assert r["approved_hash"] is None                 # показ карточки не одобряет
    # старая кнопка с тем же h8, что у нового текста, не пройдёт без совпадения полного хеша
    admin.execute("UPDATE work.actions SET shown_hash = 'x' || shown_hash")
    new_id = admin.execute("SELECT tg_message_id FROM work.outbox ORDER BY id DESC").fetchone()["tg_message_id"]
    press(term, f"pb:{act['id']}:ok:{r['h']}", new_id)
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "proposed"


# ---------------- 5.1 / 5.4: карточки действий ----------------
EMAIL = {"account": "personal", "to": ["yan@infatica.io"], "cc": [], "subject": "Re: SDK",
         "body": "Привет, Ян!\n\nОтчёт пришлю завтра.\n\nGeorge"}


def _propose(admin, atype="email_reply", payload=None, autonomy="approval", ctx_id="infatica"):
    return admin.execute("""INSERT INTO work.actions (context_id, type, payload, rationale, autonomy)
                            VALUES (%s, %s, %s, 'Ян спросил про отчёт', %s) RETURNING id""",
                         (ctx_id, atype, json.dumps(payload or EMAIL), autonomy)).fetchone()["id"]


def _card_of(admin, aid):
    return admin.execute("SELECT * FROM work.outbox WHERE ref_action=%s ORDER BY id DESC", (aid,)).fetchone()


def test_action_card_ok_sets_sql_hash(term, tg, admin):
    aid = _propose(admin)
    out = term.tick(force=True)
    assert out["cards"]["cards"] == 1 and out["outbox"] == 1
    card = tg.last("sendMessage")
    assert int(card["message_thread_id"]) == term.ctx.thread_of("decisions")
    assert "Re: SDK" in card["text"] and "Отчёт пришлю завтра." in card["text"] and "yan@infatica.io" in card["text"]
    h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions").fetchone()["h"]
    assert {f"act:{aid}:ok:{h8}", f"act:{aid}:dr:{h8}", f"act:{aid}:fix:{h8}", f"act:{aid}:no:{h8}"} == set(buttons(card))
    assert term.tick(force=True)["cards"]["cards"] == 0             # второй раз карточку не рисует
    card_id = _card_of(admin, aid)["tg_message_id"]
    # нажатие на чужом сообщении (не карточка бота) не одобряет
    press(term, f"act:{aid}:ok:{h8}", card_id + 77)
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "proposed"
    press(term, f"act:{aid}:ok:{h8}", card_id)
    row = admin.execute(f"SELECT status, approved_hash, approval_nonce, approved_at, {H} AS h FROM work.actions").fetchone()
    assert row["status"] == "approved" and row["approved_hash"] == row["h"]
    assert row["approval_nonce"] and row["approved_at"]
    assert admin.execute("SELECT verdict FROM work.outcomes").fetchone()["verdict"] == "accepted"
    queue = admin.execute(f"""SELECT id FROM work.actions WHERE status IN ('approved','drafted')
                               AND executed_at IS NULL AND approved_hash = {H}""").fetchall()
    assert [r["id"] for r in queue] == [aid]
    assert "Одобрено" in tg.last("editMessageText")["text"]


def test_action_draft_and_reject(term, tg, admin):
    a1, a2 = _propose(admin), _propose(admin, "slack_post", {"channel": "#sdk", "text": "Релиз в четверг"})
    term.tick(force=True)
    h = {r["id"]: r["h"] for r in admin.execute(f"SELECT id, left({H}, 8) AS h FROM work.actions")}
    press(term, f"act:{a1}:dr:{h[a1]}", _card_of(admin, a1)["tg_message_id"])
    press(term, f"act:{a2}:no:{h[a2]}", _card_of(admin, a2)["tg_message_id"])
    st = {r["id"]: r for r in admin.execute(f"SELECT id, status, approved_hash, {H} AS h FROM work.actions")}
    assert st[a1]["status"] == "drafted" and st[a1]["approved_hash"] == st[a1]["h"]
    assert st[a2]["status"] == "rejected"
    verdicts = {r["action_id"]: r["verdict"] for r in admin.execute("SELECT action_id, verdict FROM work.outcomes")}
    assert verdicts == {a1: "drafted", a2: "rejected"}


def test_action_hash_mismatch_refused(term, tg, admin):
    aid = _propose(admin)
    term.tick(force=True)
    h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions").fetchone()["h"]
    admin.execute("""UPDATE work.actions SET payload = jsonb_set(payload, '{to}', '["evil@x.io"]')""")
    press(term, f"act:{aid}:ok:{h8}", _card_of(admin, aid)["tg_message_id"])
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "proposed"
    assert "evil@x.io" in tg.last("sendMessage")["text"]          # новая карточка с тем, что лежит сейчас


def test_fix_creates_new_action(term, tg, admin):
    aid = _propose(admin)
    term.tick(force=True)
    h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions").fetchone()["h"]
    card_id = _card_of(admin, aid)["tg_message_id"]
    press(term, f"act:{aid}:fix:{h8}", card_id)
    assert f"act:{aid}:fx:short" in buttons(tg.last("editMessageText"))
    seen = {}

    def rewrite(user):
        seen["user"] = user
        return {"subject": "Re: SDK", "body": "Ян, отчёт будет завтра."}
    fake_model({"rewrite": rewrite})
    press(term, f"act:{aid}:fx:short", card_id)
    assert "Отчёт пришлю завтра." in seen["user"] and "yan@infatica.io" not in seen["user"]
    rows = admin.execute("SELECT id, status, replaces, payload FROM work.actions ORDER BY id").fetchall()
    assert rows[0]["status"] == "edited" and rows[0]["payload"] == EMAIL
    assert rows[1]["status"] == "proposed" and rows[1]["replaces"] == aid
    assert rows[1]["payload"]["body"] == "Ян, отчёт будет завтра." and rows[1]["payload"]["to"] == EMAIL["to"]
    oc = admin.execute("SELECT verdict, diff FROM work.outcomes").fetchone()
    assert oc["verdict"] == "edited" and "-body: Отчёт пришлю завтра." in oc["diff"]
    assert "+body: Ян, отчёт будет завтра." in oc["diff"]
    new_card = tg.last("sendMessage")
    assert "Ян, отчёт будет завтра." in new_card["text"] and f"act:{rows[1]['id']}:ok:" in " ".join(buttons(new_card))
    # «Убрать последний абзац» без модели и свободный текст ответом на карточку
    new_id = rows[1]["id"]
    new_msg = _card_of(admin, new_id)["tg_message_id"]
    fake_model({"rewrite": {"body": "Ян, привет. Отчёт завтра к обеду."}})
    send(term, message(term, "добавь, что к обеду", topic="decisions",
                       reply_to_message={"message_id": new_msg}))
    last = admin.execute("SELECT id, payload, replaces FROM work.actions ORDER BY id DESC").fetchone()
    assert last["replaces"] == new_id and "к обеду" in last["payload"]["body"]


def test_fix_cut_and_tomorrow(term, tg, admin, clock):
    aid = _propose(admin)
    term.tick(force=True)
    press(term, f"act:{aid}:fx:cut", _card_of(admin, aid)["tg_message_id"])
    new = admin.execute("SELECT id, payload FROM work.actions WHERE replaces=%s", (aid,)).fetchone()
    assert new["payload"]["body"] == "Привет, Ян!\n\nОтчёт пришлю завтра."
    t = _propose(admin, "task_add", {"title": "Отчёт", "due": "2026-10-21", "size": "M"}, ctx_id="personal")
    term.tick(force=True)
    press(term, f"act:{t}:fx:tomorrow", _card_of(admin, t)["tg_message_id"])
    moved = admin.execute("SELECT id, payload FROM work.actions WHERE replaces=%s", (t,)).fetchone()
    assert moved["payload"]["due"] == "2026-10-22"
    # task_add одобряет сам бот: задача в списке, действие executed
    h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions WHERE id=%s", (moved["id"],)).fetchone()["h"]
    press(term, f"act:{moved['id']}:ok:{h8}", _card_of(admin, moved["id"])["tg_message_id"])
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["title"] == "Отчёт" and task["due"] == date(2026, 10, 22) and task["action_id"] == moved["id"]
    assert admin.execute("SELECT status FROM work.actions WHERE id=%s", (moved["id"],)).fetchone()["status"] == "executed"


# ---------------- 6.2: автономия ----------------
AUTO_PLAN = {"for_date": "2026-10-22", "items": [{"title": "Тренировка", "size": "L"},
                                                 {"title": "Письмо Денису", "size": "S"}], "week_items": []}


def test_auto_action_notice_and_rollback(term, tg, admin):
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('plan_batch', '*', 'auto')")
    aid = _propose(admin, "plan_batch", AUTO_PLAN, autonomy="auto", ctx_id="personal")
    out = term.tick(force=True)
    assert out["cards"]["auto"] == 1
    row = admin.execute(f"SELECT status, approved_hash, {H} AS h FROM work.actions").fetchone()
    assert row["status"] == "executed" and row["approved_hash"] == row["h"]
    assert admin.execute("SELECT count(*) AS n FROM work.tasks WHERE dropped_at IS NULL").fetchone()["n"] == 2
    assert admin.execute("SELECT count(*) AS n FROM work.outcomes").fetchone()["n"] == 0
    notice = tg.last("sendMessage")
    assert "Сам добавил" in notice["text"] and buttons(notice) == [f"rb:{aid}"]
    assert int(notice["message_thread_id"]) == term.ctx.thread_of("plan")
    press(term, f"rb:{aid}", _card_of(admin, aid)["tg_message_id"])
    assert admin.execute("SELECT count(*) AS n FROM work.tasks WHERE dropped_at IS NULL").fetchone()["n"] == 0
    assert admin.execute("SELECT verdict FROM work.outcomes").fetchone()["verdict"] == "rejected"
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "rejected"
    press(term, f"rb:{aid}", 1)
    assert admin.execute("SELECT count(*) AS n FROM work.outcomes").fetchone()["n"] == 1


def test_manual_switch_sends_auto_to_cards(term, tg, admin):
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('plan_batch', '*', 'auto')")
    send(term, message(term, "/manual", topic="system"))
    a = admin.execute("SELECT mode, locked FROM work.autonomy WHERE action_type = 'plan_batch'").fetchone()
    assert a["mode"] == "approval" and a["locked"] is True
    g = admin.execute("SELECT locked FROM work.autonomy WHERE action_type = '*' AND context_id = '*'").fetchone()
    assert g["locked"] is True                             # общий рубильник заведён и заперт
    assert "Ручной режим" in tg.last("sendMessage")["text"]
    aid = _propose(admin, "plan_batch", AUTO_PLAN, autonomy="auto", ctx_id="personal")
    out = term.tick(force=True)
    assert out["cards"] == {"cards": 1, "auto": 0}
    assert admin.execute("SELECT status FROM work.actions WHERE id=%s", (aid,)).fetchone()["status"] == "proposed"
    send(term, message(term, "/auto", topic="system"))
    assert [r["locked"] for r in admin.execute("SELECT locked FROM work.autonomy")] == [False, False]


def test_external_auto_never_auto(term, admin):
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('email_reply', '*', 'auto')")
    _propose(admin, autonomy="auto")
    assert term.tick(force=True)["cards"] == {"cards": 1, "auto": 0}
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "proposed"


def test_auto_sampling_every_fifth_is_a_card(term, admin):
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('plan_batch', '*', 'auto')")
    for i in range(5):
        _propose(admin, "plan_batch", {**AUTO_PLAN, "for_date": f"2026-10-2{i}"}, ctx_id="personal")
    assert term.tick(force=True)["cards"] == {"cards": 1, "auto": 4}
    assert admin.execute("SELECT auto_count FROM work.autonomy").fetchone()["auto_count"] == 5
    st = [r["status"] for r in admin.execute("SELECT status FROM work.actions ORDER BY id")]
    assert st == ["executed"] * 4 + ["proposed"]


def test_task_add_night_time_is_next_calendar_day(term, tg, admin):
    a1 = _propose(admin, "task_add", {"title": "Таблетка", "due": "2026-10-22", "time": "02:30", "size": "S"},
                  ctx_id="personal")
    a2 = _propose(admin, "task_add", {"title": "Созвон", "due": "2026-10-22", "time": "10:00", "size": "S"},
                  ctx_id="personal")
    term.tick(force=True)
    for aid in (a1, a2):
        h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions WHERE id=%s", (aid,)).fetchone()["h"]
        press(term, f"act:{aid}:ok:{h8}", _card_of(admin, aid)["tg_message_id"])
    t = {r["title"]: r for r in admin.execute("SELECT * FROM work.tasks")}
    assert t["Таблетка"]["due"] == date(2026, 10, 22)
    assert t["Таблетка"]["remind_at"] == tu.at_local(date(2026, 10, 23), dtime(2, 30))
    assert t["Созвон"]["remind_at"] == tu.at_local(date(2026, 10, 22), dtime(10))


def test_truncated_card_offers_only_fix_and_reject(term, tg, admin):
    long = {"list_id": "L1", "name": "Отчёт", "desc": "Очень длинно. " * 400, "labels": []}
    aid = _propose(admin, "trello_card", long)
    term.tick(force=True)
    card = tg.last("sendMessage")
    h8 = admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions").fetchone()["h"]
    assert set(buttons(card)) == {f"act:{aid}:fix:{h8}", f"act:{aid}:no:{h8}"}
    assert "показан не целиком" in card["text"] and "—" not in card["text"]
    # кнопку черновика с той же карточки подделать нельзя: обрезанное не одобряется
    press(term, f"act:{aid}:dr:{h8}", _card_of(admin, aid)["tg_message_id"])
    assert admin.execute("SELECT status FROM work.actions").fetchone()["status"] == "proposed"
    assert "не целиком" in tg.last("answerCallbackQuery")["text"]
    # «Назад» из меню правки тоже без «Отправить» и «черновика»
    press(term, f"act:{aid}:bk:{h8}", _card_of(admin, aid)["tg_message_id"])
    assert set(buttons(tg.last("editMessageText"))) == {f"act:{aid}:fix:{h8}", f"act:{aid}:no:{h8}"}


def test_cards_show_trello_list_and_gmail_thread(term, tg, admin):
    admin.execute("""INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, meta)
                     VALUES ('infatica', 'telegram:inbox', 'trello-x', 'card', now(), 'x',
                             '{"list_id": "L1", "list": "Backlog", "board": "SDK"}')""")
    _propose(admin, "trello_card", {"list_id": "L1", "name": "Отчёт", "desc": "кратко", "labels": []})
    _propose(admin, "trello_card", {"list_id": "L2", "name": "Другое", "desc": "кратко", "labels": []})
    _propose(admin, payload={**EMAIL, "thread_id": "18c2f00dbeef"})
    _propose(admin, payload=EMAIL)
    term.tick(force=True)
    texts = [p["text"] for p in tg.of("sendMessage")]
    assert any("Список: SDK / Backlog" in t and "L1" in t for t in texts)
    assert any("Список: <code>L2</code>" in t for t in texts)
    assert any("Ветка Gmail: <code>18c2f00dbeef</code>" in t for t in texts)
    assert any("Ветка Gmail: новое письмо" in t for t in texts)


def test_plan_task_id_moves_existing_and_skips_closed(term, tg, admin, clock):
    s0 = tu.at_local(date(2026, 10, 22), dtime(11))
    open_id = admin.execute("""INSERT INTO work.tasks (title, due, source, planned_start, planned_end, planned_by,
                                                       planned_at)
                               VALUES ('Старое', '2026-10-21', 'plan', %s, %s, 'auto', '2026-10-01')
                               RETURNING id""", (s0, s0 + (tu.at_local(date(2026, 10, 22), dtime(12)) - s0))).fetchone()["id"]
    done_id = admin.execute("""INSERT INTO work.tasks (title, due, source, done_at)
                               VALUES ('Сделано', '2026-10-21', 'plan', now()) RETURNING id""").fetchone()["id"]
    payload = {"for_date": "2026-10-22", "week_items": [], "items": [
        {"title": "Старое", "size": "M", "task_id": open_id, "start": s0.isoformat(),
         "end": tu.at_local(date(2026, 10, 22), dtime(12)).isoformat()},
        {"title": "Сделано", "size": "S", "task_id": done_id},
        {"title": "Пропавшая", "size": "S", "task_id": 999999},
        {"title": "Новая", "size": "S"}]}
    aid = _propose(admin, "plan_batch", payload, ctx_id="personal")
    term.tick(force=True)
    card = tg.last("sendMessage")
    assert card["text"].count("перенос") == 3 and "Новая · S\n" not in card["text"].replace("перенос", "")
    press(term, btn_by_prefix(card, f"pb:{aid}:ok:"), _card_of(admin, aid)["tg_message_id"])
    tasks = {r["title"]: r for r in admin.execute("SELECT * FROM work.tasks")}
    assert set(tasks) == {"Старое", "Сделано", "Пропавшая", "Новая"}       # дубля «Сделано» нет
    assert tasks["Старое"]["due"] == date(2026, 10, 22) and tasks["Старое"]["planned_start"] == s0
    assert tasks["Старое"]["planned_at"].date() == date(2026, 10, 1)       # время не менялось
    assert tasks["Сделано"]["done_at"] is not None
    res = admin.execute("SELECT result FROM work.actions").fetchone()["result"]
    assert open_id in res["task_ids"] and done_id not in res["task_ids"] and len(res["task_ids"]) == 3


def test_reminder_400_retries_without_web_app_and_never_blocks(term, tg, admin, clock):
    clock.set(date(2026, 10, 22), 9)
    for title, m in (("сломанное", 0), ("с кнопкой", 1), ("обычное", 2)):
        admin.execute("INSERT INTO work.tasks (title, due, remind_at, source) VALUES (%s, '2026-10-22', %s, 'capture')",
                      (title, tu.at_local(date(2026, 10, 22), dtime(8, m))))

    def fail(method, form):
        if method != "sendMessage" or "⏰" not in (form.get("text") or ""):
            return None
        if "сломанное" in form["text"]:
            return FakeTG.error(400, "Bad Request: something is broken")
        kb = (form.get("reply_markup") or {}).get("inline_keyboard") or []
        if "с кнопкой" in form["text"] and any("web_app" in b for row in kb for b in row):
            return FakeTG.error(400, "Bad Request: BUTTON_TYPE_INVALID")
        return None
    tg.fail = fail
    assert terminal.reminders(term.ctx) == 2
    rows = {r["title"]: r for r in admin.execute("SELECT title, reminded_at FROM work.tasks")}
    assert all(r["reminded_at"] is not None for r in rows.values())     # кривое не держит очередь
    sys_msgs = [r["text"] for r in admin.execute("SELECT text FROM work.outbox WHERE topic = 'system'")]
    assert any("сломанное" in t and "не ушло" in t for t in sys_msgs)
    tries = {}
    for p in tg.of("sendMessage"):
        if "⏰" in p["text"]:
            kb = p["reply_markup"]["inline_keyboard"]
            tries.setdefault(p["text"].split("\n")[0], []).append(any("web_app" in b for r in kb for b in r))
    # сломанное: с кнопкой и без, обе мимо; с кнопкой: второй раз без web_app; обычное: с первого раза
    assert tries == {"⏰ сломанное": [True, False], "⏰ с кнопкой": [True, False], "⏰ обычное": [True]}

    # 429: пауза, ничего не помечено отправленным
    admin.execute("UPDATE work.tasks SET reminded_at = NULL WHERE title = 'обычное'")
    tg.fail = lambda method, form: (FakeTG.error(429, "Too Many Requests: retry after 7")
                                    if method == "sendMessage" else None)
    assert terminal.reminders(term.ctx) == 0
    assert admin.execute("SELECT reminded_at FROM work.tasks WHERE title = 'обычное'").fetchone()["reminded_at"] is None
    assert term.ctx.state["outbox_backoff"] > time.monotonic() + 5
    tg.fail = None
    assert terminal.reminders(term.ctx) == 0                          # ждёт паузу
    term.ctx.state["outbox_backoff"] = 0
    assert terminal.reminders(term.ctx) == 1


def test_tomorrow_shifts_reminder_by_days(term, tg, admin, clock):
    clock.set(date(2026, 10, 22), 23, 30)
    tid = admin.execute("""INSERT INTO work.tasks (title, due, remind_at, source)
                           VALUES ('вечернее', '2026-10-22', %s, 'capture') RETURNING id""",
                        (tu.at_local(date(2026, 10, 22), dtime(23)),)).fetchone()["id"]
    press(term, f"tk:{tid}:tmr", 4243)
    t = admin.execute("SELECT due, remind_at FROM work.tasks").fetchone()
    assert t["due"] == date(2026, 10, 23) and t["remind_at"] == tu.at_local(date(2026, 10, 23), dtime(23))


# ---------------- 1.4: напоминания и outbox ----------------
def test_reminders_respect_quiet_hours(term, tg, admin, clock):
    tid = admin.execute("""INSERT INTO work.tasks (title, due, remind_at, source)
                           VALUES ('выпить таблетку', '2026-10-22', %s, 'capture') RETURNING id""",
                        (tu.at_local(date(2026, 10, 22), dtime(2, 30)),)).fetchone()["id"]
    clock.set(date(2026, 10, 22), 2, 45)
    assert terminal.reminders(term.ctx) == 0
    assert admin.execute("SELECT reminded_at FROM work.tasks").fetchone()["reminded_at"] is None
    clock.set(date(2026, 10, 22), 8, 5)
    assert terminal.reminders(term.ctx) == 1
    msg = tg.last("sendMessage")
    assert msg["text"].startswith("⏰ выпить таблетку") and int(msg["message_thread_id"]) == term.ctx.thread_of("plan")
    kb = msg["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["web_app"]["url"] == "https://app.test/"
    assert admin.execute("SELECT reminded_at FROM work.tasks").fetchone()["reminded_at"] is not None
    assert terminal.reminders(term.ctx) == 0                     # второй раз не шлёт
    press(term, f"tk:{tid}:tmr", 4242)
    t = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert t["due"] == date(2026, 10, 23) and t["reminded_at"] is None
    assert t["remind_at"] == tu.at_local(date(2026, 10, 23), dtime(2, 30))


def test_outbox_waits_for_morning(term, tg, admin, clock):
    admin.execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('system', 'диск 90%%', %s)",
                  (json.dumps([[{"text": "Ок", "callback_data": "food:ok"}]]),))
    clock.set(date(2026, 10, 22), 3)
    assert terminal.flush_outbox(term.ctx) == 0
    assert admin.execute("SELECT sent_at FROM work.outbox").fetchone()["sent_at"] is None
    clock.set(date(2026, 10, 22), 9)
    assert terminal.flush_outbox(term.ctx) == 1
    row = admin.execute("SELECT sent_at, tg_message_id FROM work.outbox").fetchone()
    assert row["sent_at"] is not None and row["tg_message_id"] is not None
    msg = tg.last("sendMessage")
    assert msg["text"] == "диск 90%" and int(msg["message_thread_id"]) == term.ctx.thread_of("system")
    assert terminal.flush_outbox(term.ctx) == 0


# ---------------- П.2: /me ----------------
def test_me_sheet(term, tg, admin, clock):
    clock.real()
    admin.execute("""INSERT INTO rpg.cells (id, parent, title, kind) VALUES ('C', NULL, 'Квест', 'quest'),
                     ('A', 'C', 'Здоровье', 'attribute'), ('B', 'C', 'Карьера', 'attribute'),
                     ('A1', 'A', 'Сон', 'habit')""")
    admin.execute("""INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at) VALUES
                     ('A1', 60, 'manual', 'a', now()), ('A1', 60, 'manual', 'b', now() - interval '1 day'),
                     ('A', 30, 'task', '1', now())""")
    t0 = time.monotonic()
    send(term, message(term, "/me", topic="plan"))
    assert time.monotonic() - t0 < 1.0
    text = tg.last("sendMessage")["text"]
    a = admin.execute("SELECT level, xp_into_level, level_span FROM rpg.attributes WHERE id='A'").fetchone()
    assert f"Здоровье · ур. {a['level']}" in text and f"{a['xp_into_level']}/{a['level_span']}" in text
    assert "▰" in text and "Отстаёт: Карьера" in text and "Заржавели" in text and "Сон 2 дня" in text
    assert "Уровень 1" in text


# ---------------- 3.2: кандидаты ----------------
def test_candidates_callbacks(term, tg, admin):
    pid = admin.execute("INSERT INTO work.people (display_name) VALUES ('Ян Петров') RETURNING id").fetchone()["id"]
    c = [admin.execute("""INSERT INTO work.identity_candidates (raw, kind, context_id)
                          VALUES (%s, 'display_name', 'infatica') RETURNING id""", (raw,)).fetchone()["id"]
         for raw in ("Ian", "Яна", "бот")]
    press(term, f"idc:{c[0]}:{pid}", 10)
    press(term, f"idc:{c[1]}:new", 11)
    press(term, f"idc:{c[2]}:skip", 12)
    st = {r["id"]: r["status"] for r in admin.execute("SELECT id, status FROM work.identity_candidates")}
    assert st == {c[0]: "resolved", c[1]: "resolved", c[2]: "ignored"}
    ids = admin.execute("SELECT person_id, kind, value, context_id, confidence FROM work.identities ORDER BY id").fetchall()
    assert ids[0]["person_id"] == pid and ids[0]["value"] == "Ian" and ids[0]["context_id"] == "infatica"
    assert ids[0]["confidence"] == 1.0 and ids[0]["kind"] == "display_name"
    yana = admin.execute("SELECT id FROM work.people WHERE display_name='Яна'").fetchone()["id"]
    assert ids[1]["person_id"] == yana
    assert admin.execute("SELECT count(*) AS n FROM work.person_contexts WHERE context_id='infatica'").fetchone()["n"] == 2
    assert "это Ян Петров" in [p["text"] for p in tg.of("editMessageText")][0]
    press(term, f"idc:{c[0]}:{pid}", 10)
    assert tg.last("answerCallbackQuery")["text"] == "Уже разобран"


# ---------------- 3.4: «Спросить» ----------------
def test_ask_flow_with_choices(term, tg, admin, monkeypatch):
    calls = {}
    stub = types.ModuleType("retrieve")

    def prepare(conn, question):
        calls["prepare"] = question
        return {"choices": [{"raw": "Ян", "candidates": [
            {"person_id": 7, "name": "Ян", "contexts": ["infatica"]},
            {"person_id": 8, "name": "Ян", "contexts": ["alfa"]}]}]}

    def answer(conn, question, person_ids):
        calls["answer"] = (question, person_ids)
        return {"text": "Ян (Альфа) на встрече 14 августа сказал: <b>SLA 15 минут</b>.", "refs": []}
    stub.prepare, stub.answer = prepare, answer
    monkeypatch.setitem(sys.modules, "retrieve", stub)
    send(term, message(term, "что Ян говорил про SLA?", topic="ask"))
    ev = admin.execute("SELECT id, kind FROM work.events").fetchone()
    assert ev["kind"] == "question" and calls["prepare"] == "что Ян говорил про SLA?"
    q = tg.last("sendMessage")
    assert set(buttons(q)) == {f"ask:{ev['id']}:7", f"ask:{ev['id']}:8"}
    texts = [b["text"] for row in q["reply_markup"]["inline_keyboard"] for b in row]
    assert texts == ["Ян (Infatica)", "Ян (Альфа-Банк)"]
    assert int(q["message_thread_id"]) == term.ctx.thread_of("ask")
    press(term, f"ask:{ev['id']}:8", 77)
    assert calls["answer"] == ("что Ян говорил про SLA?", [8])
    assert "SLA 15 минут" in tg.last("sendMessage")["text"]
    press(term, f"ask:{ev['id']}:7", 77)                        # второй ответ не нужен
    assert calls["answer"][1] == [8]


def test_ask_without_retrieve(term, tg, monkeypatch):
    monkeypatch.setitem(sys.modules, "retrieve", types.ModuleType("retrieve"))
    send(term, message(term, "что было в среду?", topic="ask"))
    assert "не настроен" in tg.last("sendMessage")["text"]


# ---------------- домены и устойчивость ----------------
def test_domain_topic_notifies(tg, role_env, admin, monkeypatch, clock):
    role_env("workos_bot")
    monkeypatch.setattr(config, "DOMAINS", {"food": "Еда"})
    bot = make_bot(tg)
    t = Terminal(bot=bot, ctx=Ctx(bot, owner=OWNER))
    t.start()
    assert "Еда" in [m["name"] for m in tg.of("createForumTopic")]
    admin.execute("LISTEN domain_food")
    send(t, message(t, "овсянка 300 г", topic="food"))
    ev = admin.execute("SELECT id, meta FROM work.events").fetchone()
    assert ev["meta"]["domain"] == "food"
    got = [n.payload for n in admin.notifies(timeout=2, stop_after=1)]
    assert got == [str(ev["id"])]
    press(t, "food:like:3", 55)
    got = [json.loads(n.payload) for n in admin.notifies(timeout=2, stop_after=1)]
    assert got[0]["data"] == "food:like:3"
    admin.execute("INSERT INTO work.outbox (topic, text) VALUES ('food', '420 ккал')")
    terminal.flush_outbox(t.ctx)
    assert int(tg.last("sendMessage")["message_thread_id"]) == t.ctx.thread_of("food")
    admin.execute("UNLISTEN *")
    t.ctx.close()


def test_handler_error_does_not_crash(term, tg, admin, monkeypatch):
    def broken(ctx, msg, **kw):
        raise RuntimeError("сломалось")
    monkeypatch.setattr(capture, "handle", broken)
    send(term, message(term, "что угодно"))
    send(term, message(term, "ещё раз"))
    rows = admin.execute("SELECT topic, text FROM work.outbox").fetchall()
    assert len(rows) == 1 and rows[0]["topic"] == "system" and "сломалось" in rows[0]["text"]


def test_plan_command(term, tg, admin, clock):
    admin.execute("""INSERT INTO work.tasks (title, due, source, done_at) VALUES
                     ('Отчёт', '2026-10-21', 'plan', now()), ('Созвон', '2026-10-21', 'plan', NULL)""")
    send(term, message(term, "/plan", topic="plan"))
    text = tg.last("sendMessage")["text"]
    assert "сделано 1 из 2" in text and "✓ Отчёт" in text


def test_edited_message_updates_task(term, tg, admin):
    fake_model({"capture": TASK_ANSWER})
    m = message(term, "позвонить в банк в пт в 10")
    send(term, m)
    reply_id = admin.execute("SELECT meta FROM work.events").fetchone()["meta"]["reply_id"]
    term.handle_update({"update_id": next(_mid), "edited_message": dict(m, text="позвонить в банк в субботу в 11")})
    task = admin.execute("SELECT * FROM work.tasks").fetchone()
    assert task["due"] == date(2026, 10, 24) and task["remind_at"] == tu.at_local(date(2026, 10, 24), dtime(11))
    assert task["title"] == "позвонить в банк"
    meta = admin.execute("SELECT meta FROM work.events").fetchone()["meta"]
    assert meta["edited_text"] == "позвонить в банк в субботу в 11" and meta["class"]["due"] == "2026-10-24"
    ed = tg.last("editMessageText")
    assert int(ed["message_id"]) == reply_id and "сб 24 окт 11:00" in ed["text"]


def test_poll_once_tracks_offset(term, tg):
    tg.updates = [{"update_id": 41, "message": message(term, "/help")},
                  {"update_id": 42, "message": message(term, "чужое", frm=999)}]
    assert term.poll_once(timeout=0) == 2 and term.offset == 43
    first = tg.of("getUpdates")[-1]
    assert first["allowed_updates"] == ["message", "callback_query", "edited_message"]
    assert len(tg.of("sendMessage")) == 1                        # ответ только владельцу
    term.poll_once(timeout=0)
    assert tg.of("getUpdates")[-1]["offset"] == "43"

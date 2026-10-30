"""Безопасность (миграция 009 и правки бота): каждая находка ревью доказана настоящими
ролями базы (as_role / role_env), а не админом.

1. читатель не пишет work.autonomy и не может сам себя сделать автономным;
2. без нажатия бывают только внутренние типы, решает выборка бота;
3. строку outbox нельзя подделать под карточку бота, чужие кнопки фильтруются;
4. исполнитель не может одобрить, бот не может «исполнить» внешнее;
5. /manual запирает общий рубильник;
6. модель получает промпт через stdin и минимальное окружение;
7. токен бота не попадает в базу и outbox;
8. приёмник А-Чата принимает только message и белый список meta.
"""
from __future__ import annotations

import json
import subprocess

import httpx
import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from agents import autonomy
from collectors import ingest_server
from core import config, db, llm
from core.tg import Bot, TgError, scrub
from executors import base as exec_base

from bot import capture, cards, terminal, voice
# фикстуры term, tg, clock берём из тестов бота (фейковый Telegram, роль workos_bot)
from tests.test_bot import EMAIL, _card_of, _propose, clock, message, press, send, term, tg  # noqa: F401

H = db.payload_hash_sql("payload")
PLAN = {"for_date": "2026-10-22", "items": [{"title": "Тренировка", "size": "L"}], "week_items": []}
SLACK = {"channel": "C1", "text": "Релиз в четверг"}


def _h8(admin, aid):
    return admin.execute(f"SELECT left({H}, 8) AS h FROM work.actions WHERE id = %s", (aid,)).fetchone()["h"]


def _status(admin, aid):
    return admin.execute("SELECT status FROM work.actions WHERE id = %s", (aid,)).fetchone()["status"]


def _sent_markup(tg, text):
    got = [p for p in tg.of("sendMessage") if p.get("text") == text]
    assert got, f"не отправлено: {text}"
    return (got[-1].get("reply_markup") or {}).get("inline_keyboard") or []


# ---------- 1. автономию пишет не читатель ----------
def test_reader_cannot_write_autonomy_or_outcomes(admin, as_role):
    r = as_role("workos_reader")
    with pytest.raises(InsufficientPrivilege):
        r.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('trello_card', '*', 'auto')")
    admin.execute("INSERT INTO work.autonomy (action_type, context_id) VALUES ('task_add', 'personal')")
    for sql in ("UPDATE work.autonomy SET mode = 'auto'", "UPDATE work.autonomy SET locked = false",
                "UPDATE work.autonomy SET auto_count = 1", "DELETE FROM work.autonomy"):
        with pytest.raises(InsufficientPrivilege):
            r.execute(sql)
    # статистику, по которой двигаются режимы, тоже не подделать
    aid = admin.execute("INSERT INTO work.actions (context_id, type, payload) VALUES ('personal', 'task_add', '{}') "
                        "RETURNING id").fetchone()["id"]
    with pytest.raises(InsufficientPrivilege):
        r.execute("INSERT INTO work.outcomes (action_id, action_type, context_id, verdict) "
                  "VALUES (%s, 'task_add', 'personal', 'accepted')", (aid,))
    # пересчёт только через функцию, пороги в ней зашиты: подсунуть свои нельзя
    assert r.execute("SELECT * FROM work.autonomy_recompute(false)").fetchall() == []
    with pytest.raises(psycopg.errors.UndefinedFunction):
        r.execute("SELECT * FROM work.autonomy_recompute(false, 0.0)")
    assert admin.execute("SELECT mode FROM work.autonomy").fetchone()["mode"] == "approval"
    # бот и исполнитель функцию не зовут
    for role in ("workos_bot", "workos_exec", "workos_app"):
        with pytest.raises(InsufficientPrivilege):
            as_role(role).execute("SELECT * FROM work.autonomy_recompute(true)")


# ---------- 2. предложение читателя не одобряется само ----------
def test_reader_auto_proposal_is_not_auto_approved(term, tg, admin, as_role):
    r = as_role("workos_reader")
    # читатель пишет autonomy='auto' и «уже одобрено»: триггер возвращает всё в proposed/approval
    aid = r.execute("""INSERT INTO work.actions (context_id, type, payload, status, autonomy, approved_hash,
                                                 shown_hash)
                       VALUES ('infatica', 'trello_card', %s, 'approved', 'auto', 'x', 'y') RETURNING id""",
                    (json.dumps({"list_id": "L1", "name": "Взлом", "desc": "x", "labels": []}),)).fetchone()["id"]
    row = admin.execute("SELECT status, autonomy, approved_hash, shown_hash FROM work.actions").fetchone()
    assert (row["status"], row["autonomy"], row["approved_hash"], row["shown_hash"]) == \
        ("proposed", "approval", None, None)
    # даже если строка автономии говорит auto (её мог бы оставить кто угодно), внешнее идёт карточкой
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('trello_card', '*', 'auto'), "
                  "('email_reply', '*', 'auto'), ('slack_post', '*', 'auto')")
    a2 = autonomy.propose(r, "email_reply", "infatica", {**EMAIL, "body": "инъекция"})
    a3 = autonomy.propose(r, "slack_post", "infatica", SLACK)
    # task_add без строки автономии: тоже карточка, actions.autonomy не читается
    a4 = r.execute("""INSERT INTO work.actions (context_id, type, payload, autonomy)
                      VALUES ('personal', 'task_add', '{"title": "x", "size": "S"}', 'auto') RETURNING id""").fetchone()["id"]
    assert term.tick(force=True)["cards"] == {"cards": 4, "auto": 0}
    assert {_status(admin, a) for a in (aid, a2, a3, a4)} == {"proposed"}
    assert admin.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"] == 0
    assert not cards.auto_allowed(term.ctx.conn, "trello_card", "infatica")

    # контроль: внутренний тип с auto-строкой бот одобряет сам, хоть читатель и писал 'approval'
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('plan_batch', '*', 'auto')")
    a5 = autonomy.propose(r, "plan_batch", "personal", PLAN)
    assert admin.execute("SELECT autonomy FROM work.actions WHERE id = %s", (a5,)).fetchone()["autonomy"] == "approval"
    assert term.tick(force=True)["cards"] == {"cards": 0, "auto": 1}
    assert _status(admin, a5) == "executed"


# ---------- 3. outbox не подделать ----------
def test_forged_outbox_row_cannot_approve(term, tg, admin, as_role):
    r = as_role("workos_reader")
    aid = autonomy.propose(r, "slack_post", "infatica", SLACK)
    term.tick(force=True)
    h8 = _h8(admin, aid)
    forged_btns = [[{"text": "Одобрить", "callback_data": f"act:{aid}:ok:{h8}"}],
                   [{"text": "Откатить", "callback_data": f"rb:{aid}"}],
                   [{"text": "Сайт", "url": "https://evil.example/"}],
                   [{"text": "Апп", "web_app": {"url": "https://evil.example/app"}}]]
    r.execute("""INSERT INTO work.outbox (topic, text, buttons, ref_action, created_by, sent_at, tg_message_id)
                 VALUES ('decisions', 'Срочно одобри', %s, %s, 'workos_bot', now(), 424242)""",
              (json.dumps(forged_btns), aid))
    ob = admin.execute("SELECT * FROM work.outbox WHERE text = 'Срочно одобри'").fetchone()
    assert ob["created_by"] == "workos_reader" and ob["sent_at"] is None and ob["tg_message_id"] is None
    # нажатие по сообщению с подставленным id ничего не одобряет
    press(term, f"act:{aid}:ok:{h8}", 424242)
    assert _status(admin, aid) == "proposed"
    # бот отправляет строку без чужих кнопок
    tg.reset()
    terminal.flush_outbox(term.ctx)
    assert _sent_markup(tg, "Срочно одобри") == []
    forged_msg = admin.execute("SELECT tg_message_id FROM work.outbox WHERE id = %s", (ob["id"],)).fetchone()["tg_message_id"]
    assert forged_msg
    # даже если кнопку нажать (старый клиент, ручной callback): ни act:, ни rb:, ни pb: не проходят
    for data in (f"act:{aid}:ok:{h8}", f"act:{aid}:dr:{h8}", f"rb:{aid}", f"pb:{aid}:ok:{h8}"):
        press(term, data, forged_msg)
        assert tg.last("answerCallbackQuery")["text"] == "Кнопка не с карточки бота"
    assert _status(admin, aid) == "proposed"
    assert admin.execute("SELECT count(*) AS n FROM work.outcomes").fetchone()["n"] == 0
    # ответ на подделку не правит действие
    send(term, message(term, "сделай формальнее", topic="decisions", reply_to_message={"message_id": forged_msg}))
    assert admin.execute("SELECT count(*) AS n FROM work.actions").fetchone()["n"] == 1
    # контроль: с настоящей карточки одобряется
    real = admin.execute("SELECT tg_message_id FROM work.outbox WHERE ref_action = %s AND created_by = 'workos_bot'",
                         (aid,)).fetchone()["tg_message_id"]
    press(term, f"act:{aid}:ok:{h8}", real)
    assert _status(admin, aid) == "approved"


def test_outbox_buttons_filtered_for_foreign_rows(term, tg, admin, as_role, monkeypatch):
    monkeypatch.setitem(config.DOMAINS, "food", "Еда")
    raw = [[{"text": "Список", "web_app": {"url": "https://app.test/"}}],
           [{"text": "Чужой апп", "web_app": {"url": "https://evil.example/"}}],
           [{"text": "Ссылка", "url": "https://evil.example/"}],
           [{"text": "a", "callback_data": "act:1:ok:deadbeef"}, {"text": "p", "callback_data": "pb:1:ok:deadbeef"}],
           [{"text": "r", "callback_data": "rb:1"}, {"text": "c", "callback_data": "cap:1:task"}],
           [{"text": "На завтра", "callback_data": "tk:5:tmr"}, {"text": "Кто", "callback_data": "idc:3:new"}],
           [{"text": "Съел", "callback_data": "food:ok"}, {"text": "j", "callback_data": "jobs:x"}],
           [{"text": "без двоеточия", "callback_data": "tk"}]]
    as_role("workos_reader").execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('plan', 'от читателя', %s)",
                                     (json.dumps(raw),))
    as_role("workos_exec").execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('decisions', 'от исполнителя', %s)",
                                   (json.dumps(raw),))
    # строка бота (его карточка): кнопки как есть
    as_role("workos_bot").execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('decisions', 'от бота', %s)",
                                  (json.dumps(raw[3:5]),))
    terminal.flush_outbox(term.ctx, limit=10)
    expected = [[{"text": "Список", "web_app": {"url": "https://app.test/"}}],
                [{"text": "На завтра", "callback_data": "tk:5:tmr"}, {"text": "Кто", "callback_data": "idc:3:new"}],
                [{"text": "Съел", "callback_data": "food:ok"}]]
    assert _sent_markup(tg, "от читателя") == expected
    assert _sent_markup(tg, "от исполнителя") == expected
    bot_cb = [b["callback_data"] for row in _sent_markup(tg, "от бота") for b in row]
    assert bot_cb == ["act:1:ok:deadbeef", "pb:1:ok:deadbeef", "rb:1", "cap:1:task"]
    owners = {r["text"]: r["created_by"] for r in admin.execute("SELECT text, created_by FROM work.outbox")}
    assert owners == {"от читателя": "workos_reader", "от исполнителя": "workos_exec", "от бота": "workos_bot"}


# ---------- 4. исполнитель не одобряет, бот не исполняет внешнее ----------
def test_exec_cannot_approve(term, tg, admin, as_role):
    aid = _propose(admin, "slack_post", SLACK)
    term.tick(force=True)
    row = admin.execute(f"SELECT shown_hash, approved_hash, {H} AS h FROM work.actions").fetchone()
    assert row["shown_hash"] == row["h"] and row["approved_hash"] is None     # показ не одобряет
    ex = as_role("workos_exec")
    with pytest.raises(InsufficientPrivilege):
        ex.execute("UPDATE work.actions SET status = 'approved', approved_at = now() WHERE id = %s", (aid,))
    for col in ("approved_hash", "shown_hash", "payload"):
        with pytest.raises(InsufficientPrivilege):
            ex.execute(f"UPDATE work.actions SET {col} = NULL WHERE id = %s", (aid,))
    with ex.transaction():
        assert ex.execute(exec_base.QUEUE_SQL, {"types": ["slack_post"]}).fetchone() is None
    assert _status(admin, aid) == "proposed"
    # черновик нельзя «повысить» до отправки, исполненное нельзя перезапустить
    admin.execute(f"UPDATE work.actions SET status = 'drafted', approved_hash = {H} WHERE id = %s", (aid,))
    with pytest.raises(InsufficientPrivilege):
        ex.execute("UPDATE work.actions SET status = 'approved' WHERE id = %s", (aid,))
    ex.execute("UPDATE work.actions SET status = 'drafted', executed_at = now() WHERE id = %s", (aid,))
    admin.execute("UPDATE work.actions SET status = 'executed' WHERE id = %s", (aid,))
    with pytest.raises(InsufficientPrivilege):
        ex.execute("UPDATE work.actions SET status = 'approved', executed_at = NULL WHERE id = %s", (aid,))
    # контроль: approved -> executed исполнитель может
    admin.execute("UPDATE work.actions SET status = 'approved', executed_at = NULL WHERE id = %s", (aid,))
    ex.execute("UPDATE work.actions SET status = 'executed', executed_at = now() WHERE id = %s", (aid,))
    # бот внешнее не «исполняет» (отметкой executed можно было бы спрятать действие от исполнителя)
    a2 = _propose(admin, "email_reply")
    admin.execute(f"UPDATE work.actions SET status = 'approved', approved_hash = {H} WHERE id = %s", (a2,))
    with pytest.raises(InsufficientPrivilege):
        as_role("workos_bot").execute("UPDATE work.actions SET status = 'executed' WHERE id = %s", (a2,))
    assert _status(admin, a2) == "approved"


def test_approve_checks_shown_hash_not_approved_hash(term, tg, admin):
    aid = _propose(admin, "slack_post", SLACK)
    term.tick(force=True)
    h8 = _h8(admin, aid)
    card = _card_of(admin, aid)["tg_message_id"]
    # кто-то подложил approved_hash текущего payload: это ничего не даёт, сверяется shown_hash
    admin.execute(f"UPDATE work.actions SET approved_hash = {H}, shown_hash = 'x' || shown_hash")
    press(term, f"act:{aid}:ok:{h8}", card)
    assert _status(admin, aid) == "proposed"
    new_card = _card_of(admin, aid)["tg_message_id"]          # «карточка устарела», новая ниже
    assert new_card != card
    press(term, f"act:{aid}:ok:{h8}", new_card)
    row = admin.execute(f"SELECT status, approved_hash, shown_hash, {H} AS h FROM work.actions").fetchone()
    assert row["status"] == "approved" and row["approved_hash"] == row["h"] == row["shown_hash"]


# ---------- 5. /manual: общий рубильник ----------
def test_manual_global_lock(term, tg, admin):
    send(term, message(term, "/manual", topic="system"))
    assert admin.execute("SELECT locked FROM work.autonomy WHERE action_type = '*'").fetchone()["locked"] is True
    # строка типа появилась после /manual (её не было, когда запирали): всё равно карточкой
    admin.execute("INSERT INTO work.autonomy (action_type, context_id, mode) VALUES ('plan_batch', '*', 'auto')")
    a1 = _propose(admin, "plan_batch", PLAN, ctx_id="personal")
    assert term.tick(force=True)["cards"] == {"cards": 1, "auto": 0}
    assert _status(admin, a1) == "proposed"
    send(term, message(term, "/auto", topic="system"))
    assert "общий рубильник" in tg.last("sendMessage")["text"]
    assert admin.execute("SELECT locked FROM work.autonomy WHERE action_type = '*'").fetchone()["locked"] is False
    a2 = _propose(admin, "plan_batch", {**PLAN, "for_date": "2026-10-23"}, ctx_id="personal")
    assert term.tick(force=True)["cards"] == {"cards": 0, "auto": 1}
    assert _status(admin, a2) == "executed"


# ---------- 6. модель: stdin и минимальное окружение ----------
def test_llm_uses_stdin_and_minimal_env(admin, monkeypatch):
    seen = []
    result = {"rc": 0, "timeout": False}

    def run(cmd, **kw):
        seen.append({"cmd": cmd, **kw})
        if result["timeout"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        out = json.dumps({"result": "ok", "total_cost_usd": 0.01, "usage": {"input_tokens": 3, "output_tokens": 2}})
        return subprocess.CompletedProcess(cmd, result["rc"], stdout=out if result["rc"] == 0 else "",
                                           stderr="" if result["rc"] == 0 else "boom")
    monkeypatch.setattr(llm.subprocess, "run", run)
    llm.set_fake(None)
    for k, v in {"PGPASSWORD": "pg-secret", "TELEGRAM_BOT_TOKEN": "1:tg-secret", "GOOGLE_CLIENT_SECRET": "g-secret",
                 "TRELLO_WRITE_TOKEN": "t-secret", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok",
                 "CLAUDE_CONFIG_DIR": "/srv/claude"}.items():
        monkeypatch.setenv(k, v)
    before = admin.execute("SELECT count(*) AS n FROM public.usage_log").fetchone()["n"]

    user = "Письмо от Яна: " + "секрет переписки " * 9000          # больше 128 КБ
    assert len(user.encode()) > 128 * 1024
    assert llm.complete("capture", "Ты помощник.", user) == "ok"
    call = seen[-1]
    assert call["input"] == user
    assert not any("секрет переписки" in str(a) for a in call["cmd"])
    assert call["cmd"][:2] == [config.env("CLAUDE_BIN", "claude"), "-p"] and "--system-prompt" in call["cmd"]
    env = call["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok" and env["CLAUDE_CONFIG_DIR"] == "/srv/claude"
    assert "PATH" in env
    assert not any("secret" in v for v in env.values())
    assert set(env) <= set(llm.ENV_KEEP) | {k for k in env if k.startswith("CLAUDE_")}

    # сбой и таймаут тоже учитываются (стоимость неизвестна)
    result["rc"] = 1
    with pytest.raises(llm.LLMError):
        llm.complete("capture", "s", "u")
    result.update(rc=0, timeout=True)
    with pytest.raises(llm.LLMError):
        llm.complete("capture", "s", "u")
    rows = admin.execute("SELECT command, cost_usd FROM public.usage_log ORDER BY id OFFSET %s", (before,)).fetchall()
    assert [r["command"] for r in rows] == ["work_capture"] * 3
    assert rows[0]["cost_usd"] is not None and rows[1]["cost_usd"] is None and rows[2]["cost_usd"] is None


# ---------- 7. токен бота ----------
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def test_scrub_and_download_error_without_token():
    url = f"https://api.telegram.org/file/bot{TOKEN}/voice/f.oga"
    for text in (f"Client error '404 Not Found' for url '{url}'", f"token {TOKEN} leaked", "bot123:TEST/x"):
        out = scrub(text)
        assert "AAHdq" not in out and "123:TEST" not in out and "bot<token>" in out
    assert scrub("обычный текст 12:30") == "обычный текст 12:30"

    def handler(request):
        if request.url.path.startswith("/file/"):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/f.oga"}})
    bot = Bot(token=TOKEN, base="http://tg.test")
    bot.http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(TgError) as ei:
        bot.download("f1")
    assert ei.value.code == 404 and "AAHdq" not in str(ei.value) and "tg.test" not in str(ei.value)


def test_voice_and_error_notices_store_no_token(term, tg, admin, monkeypatch):
    monkeypatch.setattr(voice, "configured", lambda: True)

    def leak(file_id):
        raise RuntimeError(f"Client error '404' for url 'http://tg.test/file/bot{TOKEN}/voice/f.oga'")
    monkeypatch.setattr(term.ctx.bot, "download", leak)
    send(term, message(term, None, voice={"file_id": "v1", "duration": 1}))
    meta = admin.execute("SELECT meta FROM work.events").fetchone()["meta"]
    assert meta["transcribed"] is False and "bot<token>" in meta["error"] and "AAHdq" not in meta["error"]

    def boom(*a, **kw):
        raise RuntimeError(f"сеть: https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(capture, "handle", boom)
    send(term, message(term, "что-то"))
    texts = " ".join(r["text"] for r in admin.execute("SELECT text FROM work.outbox"))
    assert "Ошибка обработки" in texts and "AAHdq" not in texts


# ---------- 8. приёмник А-Чата ----------
def test_ingest_only_messages_and_whitelisted_meta(admin, role_env):
    role_env("workos_reader")
    admin.execute("INSERT INTO work.sources (id, context_id, kind, enabled) VALUES ('achat:sec', 'alfa', 'achat', true) "
                  "ON CONFLICT (id) DO UPDATE SET enabled = true")

    def batch(**over):
        ev = {"external_id": "m1", "occurred_at": "2026-10-29T09:00:00+00:00", "title": "Чат", "body": "привет",
              "meta": {"chat": "Чат", "author": "Ян", "mode": "sqlite", "chat_id": 17}}
        ev.update(over)
        return {"events": [ev]}

    for bad in (batch(kind="meeting"), batch(kind="email"),
                batch(meta={"chat": "Чат", "plan_paste": True}), batch(meta={"domain": "food"}),
                batch(meta={"chat": {"nested": 1}}), batch(meta={"chat": ["a"]}), batch(meta={"author": True}),
                batch(meta="строка")):
        code, body = ingest_server.ingest("achat:sec", bad)
        assert code == 400, (bad, body)
    assert admin.execute("SELECT count(*) AS n FROM work.events WHERE source_id = 'achat:sec'").fetchone()["n"] == 0
    code, body = ingest_server.ingest("achat:sec", batch())
    assert code == 200 and body["inserted"] == 1
    ev = admin.execute("SELECT kind, meta FROM work.events WHERE source_id = 'achat:sec'").fetchone()
    assert ev["kind"] == "message" and ev["meta"]["chat"] == "Чат" and ev["meta"]["chat_id"] == 17
    assert set(ev["meta"]) <= ingest_server.META_KEYS

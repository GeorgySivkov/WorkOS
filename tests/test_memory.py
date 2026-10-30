"""Память: люди (3.2), эмбеддинги и бэкфилл (3.3), вопрос к памяти (3.4).

Эмбеддинги фейковые (WORKOS_EMBED_FAKE=1), модель через core.llm.set_fake, сети нет.
В тестовой базе нет pgvector: колонки embedding там real[], поиск идёт полнотекстом.
"""
from __future__ import annotations

import itertools
import math

import pytest
from psycopg.types.json import Jsonb

from agents import embed_backfill
from core import embed, llm
from extract import people
import retrieve

_ext = itertools.count(1)

ATTENDEES = [
    {"email": "yan@infatica.io", "name": "Ян Иванов"},
    {"email": "a.petrov@infatica.io", "name": "Александр Петров"},
    {"email": "a.kozlov@infatica.io", "name": "Aleksandr Kozlov"},
    {"email": "george@infatica.io", "name": "George"},
]


def ev(admin, source, ctx, kind, title=None, body=None, meta=None, days_ago=1, actor_raw=None):
    return admin.execute(
        "INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title, body, meta, actor_raw) "
        "VALUES (%s, %s, %s, %s, now() - make_interval(days => %s), %s, %s, %s, %s) RETURNING id",
        (ctx, source, f"t{next(_ext)}", kind, days_ago, title, body, Jsonb(meta or {}), actor_raw)).fetchone()["id"]


def pid_by_email(admin, addr):
    return admin.execute("SELECT person_id FROM work.identities WHERE kind = 'email' AND value = %s "
                         "AND context_id IS NULL", (addr,)).fetchone()["person_id"]


def n(admin, sql, args=()):
    return admin.execute(sql, args).fetchone()["n"]


@pytest.fixture
def reader(role_env, monkeypatch):
    role_env("workos_reader")
    monkeypatch.setenv("WORKOS_SELF_EMAILS", "george@infatica.io,george@gmail.com")


@pytest.fixture
def fake_embed(monkeypatch):
    monkeypatch.setenv("WORKOS_EMBED_FAKE", "1")


@pytest.fixture
def fake_llm():
    calls = []

    def install(reply):
        def fn(task, system, user):
            calls.append({"task": task, "system": system, "user": user})
            return reply(task, system, user) if callable(reply) else reply
        llm.set_fake(fn)
        return calls
    yield install
    llm.set_fake(None)


# ---------------------------------------------------------------------------
# 3.2 · люди
# ---------------------------------------------------------------------------
def test_email_dedup_one_yan_two_contexts_other_yan_separate(admin, reader):
    e1 = ev(admin, "gmail:infatica", "infatica", "email", "SDK", "текст",
            {"from": "Ян Иванов <Yan@Infatica.io>", "to": ["George <george@infatica.io>"]})
    e2 = ev(admin, "gmail:personal", "personal", "email", "Пиво в пятницу", "текст",
            {"from": {"email": "yan@infatica.io", "name": "Yan"}, "to": "george@gmail.com"})
    e3 = ev(admin, "eas:alfa", "alfa", "email", "Методология", "текст",
            {"from": "Сидоров Ян Петрович <Yan.Sidorov@alfabank.ru>", "cc": ["noreply@alfabank.ru"]})
    r = people.run()
    assert r["errors"] == 0 and r["events"] == 3

    ivanov = pid_by_email(admin, "yan@infatica.io")
    sidorov = pid_by_email(admin, "yan.sidorov@alfabank.ru")
    assert ivanov != sidorov
    # Ян из Инфатики, написавший на личную почту: одна строка в people и две в person_contexts
    ctx = {r["context_id"] for r in admin.execute("SELECT context_id FROM work.person_contexts WHERE person_id = %s",
                                                  (ivanov,))}
    assert ctx == {"infatica", "personal"}
    assert {r["context_id"] for r in admin.execute(
        "SELECT context_id FROM work.person_contexts WHERE person_id = %s", (sidorov,))} == {"alfa"}
    assert admin.execute("SELECT display_name FROM work.people WHERE id = %s", (ivanov,)).fetchone()["display_name"] \
        == "Ян Иванов"
    # George и noreply людьми не становятся
    assert n(admin, "SELECT count(*) AS n FROM work.people") == 2
    assert n(admin, "SELECT count(*) AS n FROM work.identities WHERE kind = 'email' AND value = 'yan@infatica.io'") == 1
    for e in (e1, e2):
        row = admin.execute("SELECT actor_person_id, meta FROM work.events WHERE id = %s", (e,)).fetchone()
        assert row["actor_person_id"] == ivanov and row["meta"]["participant_ids"] == [ivanov]
    assert admin.execute("SELECT actor_person_id FROM work.events WHERE id = %s", (e3,)).fetchone()[
        "actor_person_id"] == sidorov

    again = people.run()
    assert again["events"] == 0 and again["people_created"] == 0


def test_slack_and_trello_handles(admin, reader):
    ev(admin, "gmail:infatica", "infatica", "email", "x", "x", {"from": "Ян Иванов <yan@infatica.io>"})
    ev(admin, "slack:infatica", "infatica", "message", None, "привет",
       {"user": "U01YAN", "user_real_name": "Ян Иванов", "user_email": "yan@infatica.io"})
    ev(admin, "trello:george", "infatica", "card", "SDK", None,
       {"members": [{"id": "5f00abc", "fullName": "Мария Смирнова"}]})
    assert people.run()["errors"] == 0
    ivanov = pid_by_email(admin, "yan@infatica.io")
    assert admin.execute("SELECT person_id FROM work.identities WHERE kind = 'slack' AND value = 'U01YAN'"
                         ).fetchone()["person_id"] == ivanov
    maria = admin.execute("SELECT p.display_name FROM work.identities i JOIN work.people p ON p.id = i.person_id "
                          "WHERE i.kind = 'trello' AND i.value = '5f00abc'").fetchone()
    assert maria["display_name"] == "Мария Смирнова"


def _meeting(admin, body, attendees=None, title="SDK синк"):
    cal = ev(admin, "ical:infatica", "infatica", "calendar", title, None, {"attendees": attendees or ATTENDEES})
    m = ev(admin, "plaud", "infatica", "meeting", title, body, {"calendar_event_id": cal})
    return cal, m


def test_transcript_names_resolve_among_participants_only(admin, reader):
    # другой Ян живёт в Альфе: его в этой встрече нет, и он не должен появиться нигде
    ev(admin, "eas:alfa", "alfa", "email", "x", "x", {"from": "Ян Сидоров <yan.sidorov@alfabank.ru>"})
    _, m = _meeting(admin, "Саша Петров рассказал про SDK-дистрибуцию. Ян согласился с планом. "
                           "Потом Саша сказал, что релиз сдвигается. Маша пришлёт договор. Жора всё запишет.")
    r = people.run()
    assert r["errors"] == 0 and r["meetings"] == 1
    ivanov, petrov, kozlov = (pid_by_email(admin, a) for a in
                              ("yan@infatica.io", "a.petrov@infatica.io", "a.kozlov@infatica.io"))
    names = {(r["value"], r["context_id"]): (r["person_id"], r["confidence"]) for r in admin.execute(
        "SELECT person_id, value, context_id, confidence FROM work.identities WHERE kind = 'display_name'")}
    # длинная форма бьёт короткую: «Саша Петров» однозначно Петров
    assert names[("Саша Петров", "infatica")][0] == petrov
    assert names[("Ян", "infatica")][0] == ivanov
    assert names[("Ян", "infatica")][1] == pytest.approx(0.8)
    assert ("Саша", "infatica") not in names          # голое имя двух Саш обличьем не становится
    meta = admin.execute("SELECT meta FROM work.events WHERE id = %s", (m,)).fetchone()["meta"]
    assert meta["participant_ids"] == sorted([ivanov, petrov, kozlov])

    cands = {c["raw"]: c for c in admin.execute("SELECT * FROM work.identity_candidates ORDER BY id")}
    assert set(cands) == {"Саша", "Маша"}               # «Жора» это сам George
    sasha = cands["Саша"]
    assert sasha["status"] == "open" and sasha["source_event"] == m and sasha["context_id"] == "infatica"
    assert sorted(s["person_id"] for s in sasha["suggestions"]) == sorted([petrov, kozlov])
    assert cands["Маша"]["suggestions"] == []

    cards = admin.execute("SELECT * FROM work.outbox ORDER BY id").fetchall()
    assert len(cards) == 2 and {c["topic"] for c in cards} == {"inbox"}
    card = next(c for c in cards if "„Саша“" in c["text"])
    assert card["text"] == "На встрече «SDK синк» (Infatica) упомянут „Саша“. Кто это?"
    cid = sasha["id"]
    data = [[b["callback_data"] for b in row] for row in card["buttons"]]
    assert data == [[f"idc:{cid}:{petrov}"], [f"idc:{cid}:{kozlov}"], [f"idc:{cid}:new", f"idc:{cid}:skip"]]
    assert card["buttons"][0][0]["text"] == "Александр Петров (Infatica)"
    assert [b["text"] for b in card["buttons"][2]] == ["Новый человек", "Пропустить"]
    assert n(admin, "SELECT count(*) AS n FROM work.notices WHERE key LIKE 'idc:%%'") == 2

    again = people.run()
    assert again["events"] == 0 and again["cards"] == 0
    assert n(admin, "SELECT count(*) AS n FROM work.identity_candidates") == 2

    # Следующая встреча: из двух Саш пришёл только Петров. На встрече «Саша» однозначен,
    # но в контексте Инфатики Саш двое, поэтому голое «Саша» обличьем не пишется
    _, m2 = _meeting(admin, "Саша показал демо, Ян доволен.", attendees=ATTENDEES[:2], title="Демо")
    assert people.run()["candidates"] == 0
    meta2 = admin.execute("SELECT meta FROM work.events WHERE id = %s", (m2,)).fetchone()["meta"]
    assert meta2["participant_ids"] == sorted([ivanov, petrov])
    assert n(admin, "SELECT count(*) AS n FROM work.identities WHERE kind = 'display_name' AND value = 'Саша'") == 0


def test_longest_form_wins_and_declension(admin, reader):
    _, m = _meeting(admin, "Сашей Петровым и Александр Козлов обсудили релиз. Козлов против.")
    r = people.run()
    assert r["candidates"] == 0 and r["cards"] == 0
    petrov, kozlov = pid_by_email(admin, "a.petrov@infatica.io"), pid_by_email(admin, "a.kozlov@infatica.io")
    vals = {r["value"]: r["person_id"] for r in admin.execute(
        "SELECT value, person_id FROM work.identities WHERE kind = 'display_name'")}
    assert vals == {"Саша Петров": petrov, "Александр Козлов": kozlov, "Козлов": kozlov}


def test_same_display_name_in_meeting_is_never_auto_resolved(admin, reader):
    two_yans = [{"email": "yan@infatica.io", "name": "Ян Иванов"}, {"email": "yan.k@infatica.io", "name": "Ян Кузнецов"}]
    _meeting(admin, "Ян Иванов открыл встречу. Ян потом добавил про сроки.", attendees=two_yans)
    people.run()
    cands = admin.execute("SELECT raw, suggestions FROM work.identity_candidates").fetchall()
    assert [c["raw"] for c in cands] == ["Ян"] and len(cands[0]["suggestions"]) == 2
    assert n(admin, "SELECT count(*) AS n FROM work.identities WHERE kind = 'display_name' AND value = 'Ян'") == 0


def test_people_dry_run_writes_nothing(admin, reader):
    _meeting(admin, "Саша сказал, что Маша пришлёт договор.")
    r = people.run(dry_run=True)
    assert r["dry_run"] and r["candidates"] == 2 and r["cards"] == 2
    for t in ("people", "identities", "identity_candidates", "outbox", "notices"):
        assert n(admin, f"SELECT count(*) AS n FROM work.{t}") == 0


def test_unlinked_meeting_waits_for_calendar(admin, reader):
    ev(admin, "plaud", "infatica", "meeting", "Без календаря", "Саша и Маша спорили.", {})
    r = people.run()
    assert r["meetings_waiting"] == 1 and r["candidates"] == 0
    assert n(admin, "SELECT count(*) AS n FROM work.outbox") == 0


# ---------------------------------------------------------------------------
# 3.3 · эмбеддинги и бэкфилл
# ---------------------------------------------------------------------------
def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_fake_embed_is_deterministic_unit_768(fake_embed):
    a, b, c = embed.embed(["SDK дистрибуция через партнёров", "партнёров дистрибуция SDK", "пиво в пятницу"])
    assert len(a) == embed.DIM and math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    assert a == embed.embed(["SDK дистрибуция через партнёров"])[0]
    assert _cos(a, b) > 0.8 > 0.2 > _cos(a, c)
    assert embed.model_name() == embed.FAKE_MODEL_NAME
    assert embed.embed([], kind="query") == []
    with pytest.raises(ValueError):
        embed.embed(["x"], kind="doc")


def test_real_embed_without_model_files_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKOS_EMBED_FAKE", raising=False)
    monkeypatch.setenv("EMBED_MODEL_DIR", str(tmp_path))
    assert embed.model_name() == embed.MODEL_NAME
    with pytest.raises(FileNotFoundError, match="fetch_embed_model"):
        embed.embed(["проверка"])


def test_onnx_path_pooling_padding_and_batches(monkeypatch):
    """Путь ONNX без модели: заглушка сессии отдаёт one-hot токенов, проверяем пулинг, маску и порядок."""
    import numpy as np
    from tokenizers import Tokenizer, models, pre_tokenizers

    tok = Tokenizer(models.WordLevel({"<pad>": 0, "<unk>": 1, "query:": 2, "passage:": 3, "a": 4, "b": 5},
                                     unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()

    class Sess:
        shapes = []

        def run(self, _out, feeds):
            ids = feeds["input_ids"]
            assert set(feeds) == {"input_ids", "attention_mask"}
            self.shapes.append(ids.shape)
            h = np.zeros(ids.shape + (embed.DIM,), dtype=np.float32)
            for i in range(ids.shape[0]):
                for j in range(ids.shape[1]):
                    h[i, j, ids[i, j]] = 1.0          # паддинг (id 0) тоже «светится»: его должна убрать маска
            return [h]

    sess = Sess()
    monkeypatch.delenv("WORKOS_EMBED_FAKE", raising=False)
    monkeypatch.setattr(embed, "_state", (sess, tok, {"input_ids", "attention_mask"}, 0))
    monkeypatch.setenv("EMBED_MAX_BATCH_TOKENS", "9")
    long_v, short_v, _, ab = embed.embed(["a a b", "b", "b", "a b"], kind="passage")
    assert sess.shapes == [(3, 3), (1, 4)]               # бюджет токенов разрезал батч, короткие с паддингом
    r2 = 2 ** -0.5
    assert short_v[3] == pytest.approx(r2) and short_v[5] == pytest.approx(r2) and short_v[0] == 0
    assert ab[3] == ab[4] == ab[5] == pytest.approx(3 ** -0.5)
    assert long_v[4] == pytest.approx(2 / 6 ** 0.5) and long_v[5] == pytest.approx(6 ** -0.5)
    q = embed.embed(["b"], kind="query")[0]
    assert q[2] == pytest.approx(r2) and q[3] == 0           # префикс query:, а не passage:


def test_backfill_fills_embedding_and_model(admin, reader, fake_embed):
    a = ev(admin, "plaud", "infatica", "meeting", "SDK синк", "длинный транскрипт " * 500, days_ago=2)
    b = ev(admin, "gmail:personal", "personal", "email", "Письмо", "текст", days_ago=10)
    old = ev(admin, "gmail:personal", "personal", "email", "Старое", "текст", days_ago=120)
    empty = ev(admin, "gmail:personal", "personal", "email", None, None, days_ago=1)
    d = admin.execute("INSERT INTO work.decisions (context_id, decided_at, statement) "
                      "VALUES ('infatica', now() - interval '3 days', 'Запускаем SDK через партнёров') "
                      "RETURNING id").fetchone()["id"]
    assert embed_backfill.run(dry_run=True) == {"model": "fake-hash-768", "days": 90, "events_todo": 2,
                                                "decisions_todo": 1, "dry_run": True}
    before = n(admin, "SELECT count(*) AS n FROM public.usage_log WHERE command = 'work_embed'")
    r = embed_backfill.run(batch=1)
    assert r["events"] == 2 and r["decisions"] == 1 and r["seconds"] >= 0
    rows = {r["id"]: r for r in admin.execute("SELECT id, embedding, embedding_model FROM work.events")}
    for i in (a, b):
        assert rows[i]["embedding_model"] == "fake-hash-768" and len(rows[i]["embedding"]) == 768
    assert rows[old]["embedding"] is None and rows[empty]["embedding"] is None
    dec = admin.execute("SELECT embedding, embedding_model FROM work.decisions WHERE id = %s", (d,)).fetchone()
    assert dec["embedding_model"] == "fake-hash-768" and len(dec["embedding"]) == 768
    log = admin.execute("SELECT cost_usd, tokens FROM public.usage_log WHERE command = 'work_embed' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    assert n(admin, "SELECT count(*) AS n FROM public.usage_log WHERE command = 'work_embed'") == before + 1
    assert log["tokens"] == 3 and log["cost_usd"] == 0
    assert embed_backfill.run()["events"] == 0
    # смена модели: старые векторы несравнимы, строка считается заново
    admin.execute("UPDATE work.events SET embedding_model = 'old-model' WHERE id = %s", (b,))
    assert embed_backfill.run()["events"] == 1


def test_backfill_text_is_trimmed():
    assert embed_backfill.fragment("Заголовок", "x" * 5000) == ("Заголовок\n\n" + "x" * 5000)[:2000]
    assert embed_backfill.fragment("", "тело") == "тело"


# ---------------------------------------------------------------------------
# 3.4 · вопрос к памяти
# ---------------------------------------------------------------------------
def _person(admin, name, email, *contexts):
    pid = admin.execute("INSERT INTO work.people (display_name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    admin.execute("INSERT INTO work.identities (person_id, kind, value) VALUES (%s, 'email', %s)", (pid, email))
    for c in contexts:
        admin.execute("INSERT INTO work.person_contexts (person_id, context_id) VALUES (%s, %s)", (pid, c))
    return pid


@pytest.fixture
def memory(admin):
    ivanov = _person(admin, "Ян Иванов", "yan@infatica.io", "infatica", "personal")
    sidorov = _person(admin, "Ян Сидоров", "yan.sidorov@alfabank.ru", "alfa")
    m1 = ev(admin, "plaud", "infatica", "meeting", "SDK синк",
            "Решили запускать SDK-дистрибуцию через партнёров. Ян тормозил с договором.",
            {"participant_ids": [ivanov]}, days_ago=3)
    m2 = ev(admin, "eas:alfa", "alfa", "meeting", "Методология",
            "Обсуждали SDK для банка и часы поддержки.", {"participant_ids": [sidorov]}, days_ago=2)
    mail = ev(admin, "gmail:personal", "personal", "email", "Re: договор", "Пришлю договор по SDK завтра.",
              {}, days_ago=1, actor_raw="Ян Иванов <yan@infatica.io>")
    c = admin.execute("INSERT INTO work.commitments (context_id, direction, person_id, what, source_event) "
                      "VALUES ('infatica', 'theirs', %s, 'прислать договор', %s) RETURNING id",
                      (ivanov, m1)).fetchone()["id"]
    return {"ivanov": ivanov, "sidorov": sidorov, "m1": m1, "m2": m2, "mail": mail, "commitment": c}


def test_resolve_names_two_yans_needs_choice(as_role, memory):
    bot = as_role("workos_bot")
    names = retrieve.resolve_names(bot, "Что говорил Ян про SDK?")
    assert len(names) == 1 and names[0]["raw"] == "Ян"
    by_id = {c["person_id"]: c for c in names[0]["candidates"]}
    assert by_id[memory["ivanov"]]["contexts"] == ["Infatica", "Личное"]
    assert by_id[memory["sidorov"]]["contexts"] == ["Альфа-Банк"]

    p = retrieve.prepare(bot, "что говорил ян про sdk")       # строчными тоже
    assert "choices" in p and len(p["choices"][0]["candidates"]) == 2
    assert retrieve.prepare(bot, "Что обещал Ян Иванов?") == {"person_ids": [memory["ivanov"]]}
    assert retrieve.prepare(bot, "Что Яну Сидорову нужно от меня") == {"person_ids": [memory["sidorov"]]}
    assert retrieve.prepare(bot, "Что говорил Вася про SDK?")["unknown"] == ["Вася"]
    # явное «из Альфы» сразу после имени это не догадка, а ответ на вопрос «который Ян»
    assert retrieve.prepare(bot, "Что говорил Ян из Альфы про SDK?") == {"person_ids": [memory["sidorov"]]}
    assert retrieve.prepare(bot, "что ян в инфатике обещал") == {"person_ids": [memory["ivanov"]]}
    assert "choices" in retrieve.prepare(bot, "Что Ян говорил про Альфу?")
    assert retrieve.prepare(bot, "Какие данные по теме SDK?") == {"person_ids": []}


def test_search_person_filter_excludes_other_yan(as_role, memory):
    bot = as_role("workos_bot")
    everyone = retrieve.search(bot, "SDK")
    assert {h["event_id"] for h in everyone if h["kind"] == "meeting"} == {memory["m1"], memory["m2"]}

    hits = retrieve.search(bot, "SDK", person_ids=[memory["ivanov"]])
    kinds = {(h["kind"], h["id"]) for h in hits}
    assert ("meeting", memory["m1"]) in kinds and ("email", memory["mail"]) in kinds   # автор по почте
    assert ("commitment", memory["commitment"]) in kinds
    assert all(h["event_id"] != memory["m2"] for h in hits)
    assert retrieve.search(bot, "SDK", person_ids=[memory["ivanov"]], context_id="alfa") == []
    for h in hits:
        assert set(h) >= {"event_id", "kind", "title", "occurred_at", "context_id", "snippet", "score"}


def test_answer_cites_refs_and_names_person(as_role, memory, fake_llm):
    bot = as_role("workos_bot")
    calls = fake_llm("Ян (Infatica) на встрече сказал, что SDK пойдёт через партнёров [1]. Договор обещан [2].")
    r = retrieve.answer(bot, "Что решили по SDK?", person_ids=[memory["ivanov"]])
    assert len(calls) == 1 and calls[0]["task"] == "ask"
    assert "[1]" in calls[0]["system"] and "Ян (Infatica)" in calls[0]["system"]
    assert "часы поддержки" not in calls[0]["user"]                # чужой Ян в промпт не попал
    assert "Ян Иванов (Infatica, Личное)" in calls[0]["user"]
    assert r["refs"] and r["refs"][0] in (memory["m1"], memory["mail"])
    assert "\n\n[1] " in r["text"] and "[2] " in r["text"] and " · " in r["text"]
    assert "—" not in r["text"]


def test_answer_without_hits_does_not_call_model(as_role, memory, fake_llm):
    calls = fake_llm("не должно вызываться")
    r = retrieve.answer(as_role("workos_bot"), "квантовая хромодинамика")
    assert r == {"text": "В памяти ничего не нашлось по этому вопросу.", "refs": []} and calls == []


def test_period_from_question():
    from datetime import date, datetime, timezone
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    s, u = retrieve.period("что мы решили по SDK в июне", now)
    assert retrieve.tu.local(s).date() == date(2026, 6, 1) and retrieve.tu.local(u).date() == date(2026, 7, 1)
    s, _ = retrieve.period("что было в декабре", now)
    assert retrieve.tu.local(s).date() == date(2025, 12, 1)
    assert retrieve.period("что говорил Ян", now) == (None, None)

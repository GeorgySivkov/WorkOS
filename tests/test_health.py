"""2.7 heartbeat и workctl."""
import httpx
import pytest

import workctl


@pytest.fixture(autouse=True)
def sources_off(admin):
    """conftest не сбрасывает enabled: включённые здесь источники выключаются обратно."""
    on = [r["id"] for r in admin.execute("SELECT id FROM work.sources WHERE enabled")]
    yield
    admin.execute("UPDATE work.sources SET enabled = (id = ANY(%s))", (on,))


def ok_app(status=200):
    return httpx.MockTransport(lambda req: httpx.Response(status, json={"ok": status == 200}))


def outbox(admin):
    return [r["text"] for r in admin.execute("SELECT text FROM work.outbox WHERE topic = 'system' ORDER BY id")]


def test_health_alerts_once_and_recovers(admin, role_env):
    import health
    admin.execute("UPDATE work.sources SET enabled = true, last_ok_at = now() - interval '2 hours' "
                  "WHERE id = 'trello:george'")
    admin.execute("UPDATE work.sources SET enabled = true, last_ok_at = now() - interval '3 hours' "
                  "WHERE id = 'achat:alfa'")                       # порог А-Чата 6 часов
    role_env("workos_reader")
    r = health.run(transport=ok_app())
    assert r["alerts"] == ["trello:george"] and r["ok"] == 2      # А-Чат и мини-приложение в порядке
    assert len(outbox(admin)) == 1 and "trello:george" in outbox(admin)[0] and "молчит 2 ч" in outbox(admin)[0]
    assert health.run(transport=ok_app())["alerts"] == []
    assert len(outbox(admin)) == 1

    admin.execute("UPDATE work.sources SET last_ok_at = now() WHERE id = 'trello:george'")
    r = health.run(transport=ok_app())
    assert r["recovered"] == ["trello:george"]
    assert "восстановился" in outbox(admin)[-1]
    assert admin.execute("SELECT count(*) AS n FROM work.notices").fetchone()["n"] == 0
    assert health.run(transport=ok_app())["recovered"] == []

    admin.execute("UPDATE work.sources SET last_error = 'HTTPError: 401 <Unauthorized>\ntrace' "
                  "WHERE id = 'trello:george'")
    health.run(transport=ok_app())
    assert "ошибка: <code>HTTPError: 401 &lt;Unauthorized&gt;</code>" in outbox(admin)[-1]


def test_health_app_down_once(admin, role_env):
    import health
    role_env("workos_reader")
    assert health.run(transport=ok_app(503))["alerts"] == ["Мини-приложение"]
    assert health.run(transport=ok_app(503))["alerts"] == []
    assert len(outbox(admin)) == 1 and "503" in outbox(admin)[0]
    assert health.run(transport=ok_app())["recovered"] == ["Мини-приложение"]


def test_health_backup_alert_only_when_backups_enabled(admin, role_env):
    import health
    role_env("workos_reader")
    r = health.run(transport=ok_app())
    assert "Бэкап базы" not in r["alerts"] and r["ok"] == 1          # отметок нет: бэкап не включён, молчим
    admin.execute("INSERT INTO work.notices (key, sent_at) VALUES ('backup:2026-10-01', now() - interval '30 hours')")
    r = health.run(transport=ok_app())
    assert r["alerts"] == ["Бэкап базы"]
    text = outbox(admin)[-1]
    assert "последний удачный бэкап 30 ч назад" in text and "workos-backup" in text and "—" not in text
    assert health.run(transport=ok_app())["alerts"] == []             # один алерт на инцидент
    admin.execute("INSERT INTO work.notices (key) VALUES ('backup:2026-10-02')")      # свежий бэкап
    assert health.run(transport=ok_app())["recovered"] == ["Бэкап базы"]
    assert len(outbox(admin)) == 2 and "восстановился" in outbox(admin)[-1]


def test_workctl_llm_lock_serializes_and_skips(tmp_path, monkeypatch, capsys):
    import fcntl
    import os
    lock = tmp_path / "llm.lock"
    with workctl.llm_lock("extract", str(lock), wait=0) as held:
        assert held
        with pytest.raises(workctl.Busy):
            with workctl.llm_lock("extract", str(lock), wait=0.1):
                pass
    with workctl.llm_lock("extract", str(tmp_path / "нет" / "llm.lock"), wait=0) as held:
        assert held is False                                        # нет каталога: без замка
    # команда с моделью при занятом замке пропускает прогон с кодом 0 и модуль не зовёт
    monkeypatch.setenv("WORKOS_LLM_LOCK", str(lock))
    monkeypatch.setitem(workctl.LLM_LOCK, "extract", 0.1)
    called = []
    monkeypatch.setattr(workctl, "resolve", lambda target: lambda **kw: called.append(kw))
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert workctl.main(["extract"]) == 0
    finally:
        os.close(fd)
    assert not called and "пропускаю прогон" in capsys.readouterr().err
    assert workctl.main(["extract"]) == 0 and called == [{}]
    assert workctl.main(["embed", "--max-minutes", "5"]) == 0 and called[-1] == {"max_minutes": 5.0}


def test_workctl_status_and_missing_module(admin, role_env, capsys):
    role_env("workos_reader")
    assert workctl.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Источники" in out and "trello:george" in out and "не отправлено: 0" in out and "нет данных" not in out
    with pytest.raises(workctl.Missing):
        workctl.resolve("agents.no_such_module_xyz:run")
    assert workctl.call(lambda date=None: date, {"date": "2026-10-23", "force": True}) == "2026-10-23"


@pytest.mark.parametrize("role", ["workos_reader", "workos_bot", "workos_exec", "workos_app"])
def test_workctl_sql_check(role, role_env, capsys):
    role_env(role)
    code = workctl.main(["sql-check"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "✗" not in out and "✓" in out


def test_workctl_seed_needs_admin(monkeypatch, capsys):
    monkeypatch.setenv("WORKOS_ROLE", "reader")
    assert workctl.main(["seed-grid", "x.json"]) == 2
    assert "WORKOS_ROLE=admin" in capsys.readouterr().err

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

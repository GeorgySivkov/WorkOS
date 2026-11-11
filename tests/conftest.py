"""Тестовая база: свежая БД на локальном Postgres, все миграции, роли без паролей (trust).

Нужен локальный сервер: PGTEST_HOST (каталог сокета или хост) и PGTEST_PORT.
pgvector не обязателен: без него колонки embedding становятся real[].
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOST = os.environ.get("PGTEST_HOST", "/home/claude/.pgtest")
PORT = os.environ.get("PGTEST_PORT", "5433")
DBNAME = f"workos_test_{os.getpid()}"

os.environ.setdefault("WORKOS_ENV_FILE", "/nonexistent")
os.environ.update(PGHOST=HOST, PGPORT=PORT, PGDATABASE=DBNAME, PGUSER="postgres", PGSSLMODE="disable")
os.environ.setdefault("TELEGRAM_OWNER_ID", "111")


def _admin(dbname="postgres"):
    return psycopg.connect(host=HOST, port=PORT, user="postgres", dbname=dbname, autocommit=True)


@pytest.fixture(scope="session", autouse=True)
def database():
    with _admin() as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DBNAME}"')
        c.execute(f'CREATE DATABASE "{DBNAME}"')
    with _admin(DBNAME) as c:
        c.execute("CREATE TABLE public.usage_log (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                  "ts timestamptz NOT NULL DEFAULT now(), command text, cost_usd numeric(10,5), tokens int)")
        has_vector = c.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'").fetchone()
        if has_vector:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    env = dict(os.environ, WORKOS_ROLE="admin", PGUSER="postgres",
               WORKOS_READER_PASSWORD="x", WORKOS_EXEC_PASSWORD="x",
               WORKOS_BOT_PASSWORD="x", WORKOS_APP_PASSWORD="x")
    if not has_vector:
        env["WORKOS_TEST_NO_VECTOR"] = "1"
    subprocess.run([sys.executable, str(ROOT / "migrate.py")], env=env, check=True, capture_output=True)
    yield DBNAME
    with _admin() as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DBNAME}" WITH (FORCE)')


@pytest.fixture
def admin():
    with psycopg.connect(host=HOST, port=PORT, user="postgres", dbname=DBNAME,
                         autocommit=True, row_factory=dict_row) as c:
        yield c


@pytest.fixture
def as_role():
    """as_role('workos_bot') -> соединение под ролью; закрывается после теста."""
    opened = []

    def make(role: str):
        c = psycopg.connect(host=HOST, port=PORT, user=role, dbname=DBNAME,
                            autocommit=True, row_factory=dict_row)
        opened.append(c)
        return c
    yield make
    for c in opened:
        c.close()


@pytest.fixture
def role_env(monkeypatch):
    """role_env('workos_bot'): модули, зовущие core.db.connect(), работают под этой ролью."""
    def setrole(role: str):
        monkeypatch.setenv("PGUSER", role)
    return setrole


@pytest.fixture(autouse=True)
def clean(admin):
    yield
    admin.execute("""TRUNCATE work.outbox, work.outcomes, work.tasks, work.actions, work.events,
                     work.identity_candidates, work.decisions, work.commitments, work.identities,
                     work.person_contexts, work.people, work.notices, work.autonomy, work.bot_topics,
                     rpg.xp, rpg.level_marks, rpg.achievements, rpg.rewards, rpg.day_ratings RESTART IDENTITY CASCADE""")
    admin.execute("DELETE FROM rpg.cells WHERE parent IS NOT NULL")
    admin.execute("DELETE FROM rpg.cells")
    admin.execute("UPDATE work.sources SET watermark = NULL, last_ok_at = NULL, last_error = NULL")

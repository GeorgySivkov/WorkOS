"""Деплой: юниты systemd, install.sh, бэкап и проверка восстановления, google_auth без httpx.

Бэкап и восстановление гоняются по-настоящему против тестовой базы, если в системе есть
pg_dump не старше сервера и age; иначе эти тесты пропускаются.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import DBNAME, HOST, PORT

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = ["install.sh", "phase.sh", "backup.sh", "restore_check.sh"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_shell_syntax(name):
    r = subprocess.run(["bash", "-n", str(DEPLOY / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _unit(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(p.name for p in (DEPLOY / "systemd").glob("*.service")))
def test_units_hide_other_processes(name):
    text = _unit(name)
    assert "ProtectProc=invisible" in re.findall(r"^ProtectProc=\S+", text, re.M)
    cli = name in ("workos-bot.service", "workos@.service")
    # claude CLI (node), onnxruntime и whisper читают /proc/meminfo: им ProcSubset=pid нельзя
    assert bool(re.search(r"^ProcSubset=pid", text, re.M)) == (not cli and name != "workos-backup.service")
    if cli:
        assert "DISABLE_AUTOUPDATER=1" in text and "ReadWritePaths=/var/lib/workos-" in text


def test_embed_dropin_limits_memory_and_time():
    text = (DEPLOY / "systemd" / "workos@embed.service.d" / "limits.conf").read_text(encoding="utf-8")
    assert re.search(r"^MemoryMax=\d+M$", text, re.M) and re.search(r"^TimeoutStartSec=\d+min$", text, re.M)
    t = int(re.search(r"^TimeoutStartSec=(\d+)min", _unit("workos@.service"), re.M).group(1))
    assert t >= 30                      # замок модели ждут до 15 минут


def test_install_closes_secrets_before_group_chmod_and_adds_swap():
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    tighten = text.index("secret_top -exec chmod go-rwx")
    group = text.index("chmod u+rwX,g+rX,g-w,o-rwx")
    assert tighten < group
    # рекурсивный chmod группы пропускает .env*, json-ключи в корне и certs/
    find = text[text.rfind("find", 0, group):group]
    for pat in ("'.env*'", "'*.json'", '"$APP/certs"', "-prune"):
        assert pat in find, pat
    assert "chmod -R u+rwX,g+rX" not in text
    assert "postgresql-client-$pg_major" in text and "apt.postgresql.org" in text
    assert "/swapfile" in text and "swapon --noheadings --show=NAME" in text
    # plan и brief догоняют пропущенный запуск
    for t in ("plan", "brief"):
        assert re.search(rf"^timer {t}\s+'[^']+' true$", text, re.M), t


# ---------- бэкап и восстановление против тестовой базы ----------

def _major(cmd: str) -> int:
    out = subprocess.run([cmd, "--version"], capture_output=True, text=True).stdout
    return int(re.search(r"(\d+)", out).group(1))


def _server_major() -> int:
    out = subprocess.run(["psql", "-h", HOST, "-p", PORT, "-U", "postgres", "-XAtqc", "SHOW server_version_num"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip()) // 10000


needs_tools = pytest.mark.skipif(
    not all(shutil.which(x) for x in ("pg_dump", "pg_restore", "psql", "age", "age-keygen")),
    reason="нет pg_dump/age")


@pytest.fixture
def backup_env(tmp_path):
    key = tmp_path / "age.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True,
                               text=True).stdout.strip()
    env_file = tmp_path / "env.admin"
    env_file.write_text(f"PGHOST={HOST}\nPGPORT={PORT}\nPGDATABASE={DBNAME}\nPGUSER=postgres\n"
                        f"PGSSLMODE=disable\nBACKUP_AGE_RECIPIENT={recipient}\nBACKUP_DIR={tmp_path / 'bk'}\n")
    env = dict(os.environ, WORKOS_ENV_ADMIN=str(env_file))
    return env, tmp_path / "bk", key


@needs_tools
def test_backup_marks_success_and_restore_check_passes(admin, backup_env):
    if _major("pg_dump") < _server_major():
        pytest.skip("pg_dump старше сервера")
    env, bk, key = backup_env
    admin.execute("INSERT INTO work.events (context_id, source_id, external_id, kind, occurred_at, title) "
                  "VALUES ('personal', 'plaud', 'bk1', 'meeting', now(), 'для бэкапа')")
    r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    files = sorted(bk.iterdir())
    assert len(files) == 1 and files[0].name.endswith(".dump.age") and files[0].stat().st_size > 0
    today = datetime.now(timezone.utc).date().isoformat()
    assert admin.execute("SELECT 1 FROM work.notices WHERE key = %s", (f"backup:{today}",)).fetchone()

    db = f"restore_check_{os.getpid()}"
    env2 = dict(env, RESTORE_DB=db, RESTORE_EXTENSIONS="")     # в тестовой базе нет pgvector
    r = subprocess.run(["bash", str(DEPLOY / "restore_check.sh"), str(files[0]), str(key)],
                       env=env2, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "восстановление проверено" in r.stdout and re.search(r"events\s*\|\s*1\b", r.stdout)
    assert not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone()


@needs_tools
def test_failed_pg_dump_leaves_no_file_and_no_mark(admin, backup_env, tmp_path):
    env, bk, _ = backup_env
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "pg_dump"
    fake.write_text('#!/bin/sh\n[ "$1" = "--version" ] && { echo "pg_dump (PostgreSQL) 99.0"; exit 0; }\n'
                    'echo "обрезанный дамп"; echo "pg_dump: error: server version mismatch" >&2; exit 1\n')
    fake.chmod(0o755)
    env = dict(env, PATH=f"{fake_bin}:{env['PATH']}")
    r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert not bk.exists() or list(bk.iterdir()) == []        # ни .age, ни .tmp
    assert not admin.execute("SELECT 1 FROM work.notices WHERE key LIKE 'backup:%'").fetchone()


@needs_tools
def test_backup_refuses_old_client(admin, backup_env, tmp_path):
    env, bk, _ = backup_env
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "pg_dump").write_text('#!/bin/sh\necho "pg_dump (PostgreSQL) 9.6"\n')
    (fake_bin / "pg_dump").chmod(0o755)
    r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=dict(env, PATH=f"{fake_bin}:{env['PATH']}"),
                       capture_output=True, text=True)
    assert r.returncode != 0 and "postgresql-client-" in r.stderr
    assert not bk.exists() or list(bk.iterdir()) == []


# ---------- google_auth: только stdlib ----------

def test_google_auth_is_stdlib_only():
    import importlib.util
    import io
    import json

    src = (ROOT / "tools" / "google_auth.py").read_text(encoding="utf-8")
    assert "import httpx" not in src and "requests" not in src
    spec = importlib.util.spec_from_file_location("google_auth", ROOT / "tools" / "google_auth.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen["url"], seen["body"] = req.full_url, req.data.decode()
        return Resp(json.dumps({"refresh_token": "1//abc", "access_token": "x"}).encode())
    tok = mod.exchange({"client_id": "cid", "client_secret": "sec"}, "CODE", "http://127.0.0.1:8765/", opener)
    assert tok["refresh_token"] == "1//abc" and seen["url"] == "https://oauth2.googleapis.com/token"
    assert "code=CODE" in seen["body"] and "grant_type=authorization_code" in seen["body"]


def test_install_group_chmod_does_not_follow_symlinks(tmp_path):
    """Раздача прав на код не идёт по ссылкам наружу: .venv/bin/python ведёт в /usr/bin/python3.12,
    и прежний find делал системный Python root:workos 750 (Unrote падал с 203/EXEC)."""
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    start = text.index('say "Права на код')
    block = text[text.index("\n", start) + 1:text.index("\nsay ", start)]
    app, outside = tmp_path / "app", tmp_path / "usr_bin_python"
    (app / ".venv" / "bin").mkdir(parents=True)
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    (app / ".venv" / "bin" / "python").symlink_to(outside)
    (app / "main.py").write_text("x = 1\n")
    me = f"{os.getuid()}:{os.getgid()}"
    script = f'APP="{app}"\n' + block.replace("root:workos", me)
    subprocess.run(["bash", "-euc", script], check=True)
    assert outside.stat().st_mode & 0o777 == 0o755
    assert (app / "main.py").stat().st_mode & 0o007 == 0          # свои файлы закрыты от остальных

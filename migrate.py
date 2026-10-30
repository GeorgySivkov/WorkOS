#!/usr/bin/env python3
"""Миграции базы WorkOS.

Запуск на дроплете под админской ролью базы:
    WORKOS_ROLE=admin .venv/bin/python migrate.py            # применить новые
    WORKOS_ROLE=admin .venv/bin/python migrate.py --status   # что применено

.env.admin: PG* админа (doadmin) и пароли ролей WORKOS_*_PASSWORD.
Версия 001 — это schema.sql из шага 0.3. Если схема уже стоит, она
помечается применённой и не запускается повторно.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from psycopg import sql

from core import config, db

ROOT = Path(__file__).resolve().parent
MIG_DIR = ROOT / "migrations"
ROLES = {
    "workos_reader": "WORKOS_READER_PASSWORD",
    "workos_exec": "WORKOS_EXEC_PASSWORD",
    "workos_bot": "WORKOS_BOT_PASSWORD",
    "workos_app": "WORKOS_APP_PASSWORD",
}
NO_VECTOR = os.environ.get("WORKOS_TEST_NO_VECTOR") == "1"   # только для локальных тестов без pgvector


def ensure_roles(conn) -> None:
    for role, var in ROLES.items():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists:
            continue
        pw = os.environ.get(var)
        if not pw:
            sys.exit(f"роли {role} нет в базе, а пароль {var} не задан в .env.admin")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(pw)))
        print(f"  создана роль {role}")


def migrations() -> list[tuple[str, Path]]:
    out = [("001", ROOT / "schema.sql")]
    for p in sorted(MIG_DIR.glob("*.sql")):
        m = re.match(r"(\d{3})_", p.name)
        if m and m.group(1) != "001":
            out.append((m.group(1), p))
    return out


def prepare(version: str, text: str) -> str:
    if version == "001":
        # роли заводит ensure_roles с настоящими паролями
        text = re.sub(r"^CREATE ROLE[^;]*;", "", text, flags=re.M)
    if NO_VECTOR:
        text = text.replace("halfvec(768)", "real[]")
    return text


def main() -> None:
    if config.ROLE != "admin":
        sys.exit("запускай с WORKOS_ROLE=admin")
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    with db.connect(autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS work")
        conn.execute("""CREATE TABLE IF NOT EXISTS work.schema_migrations (
                          version text PRIMARY KEY, name text, applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {r["version"] for r in conn.execute("SELECT version FROM work.schema_migrations")}
        if args.status:
            for v, p in migrations():
                print(("✓ " if v in applied else "· ") + f"{v} {p.name}")
            return
        ensure_roles(conn)
        if "001" not in applied and conn.execute("SELECT to_regclass('work.contexts') AS t").fetchone()["t"]:
            conn.execute("INSERT INTO work.schema_migrations (version, name) VALUES ('001', 'schema.sql (уже стояла)')")
            applied.add("001")
            print("001 schema.sql уже применена в 0.3, помечаю")
        for v, p in migrations():
            if v in applied:
                continue
            print(f"→ {v} {p.name}")
            with conn.transaction():
                conn.execute(prepare(v, p.read_text(encoding="utf-8")))
                conn.execute("INSERT INTO work.schema_migrations (version, name) VALUES (%s, %s)", (v, p.name))
        print("готово")


if __name__ == "__main__":
    main()

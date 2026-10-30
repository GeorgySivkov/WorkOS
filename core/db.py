"""Подключение к Postgres.

Параметры приходят из PG* переменных окружения (libpq), которые подгружает
core.config из .env своей роли. Роль базы у каждого процесса своя.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core import config  # noqa: F401  (подгружает .env роли)


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(row_factory=dict_row, autocommit=autocommit)


@contextmanager
def tx():
    """Транзакция: commit на выходе, rollback на исключении."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def jb(value) -> Jsonb:
    return Jsonb(value)


def payload_hash_sql(column: str = "payload") -> str:
    """Тот же самый хеш, что проверяет исполнитель (шаг 5.1).

    Считается только в SQL: jsonb выводится в текст детерминированно,
    а Python-сериализация могла бы разойтись в мелочах.
    """
    return f"encode(sha256(convert_to({column}::text, 'UTF8')), 'hex')"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)

"""Эмбеддинги и бэкфилл (шаг 3.3): проход по строкам без вектора, батчами, через core.embed.

Берёт work.events и work.decisions за последние N дней (по умолчанию 90, глубже отдача
падает быстрее, чем растёт объём), у которых embedding IS NULL или посчитан другой моделью
(векторы разных моделей несравнимы). Текст: заголовок + тело, обрезанные до ~2000 символов.
Длинная встреча так даёт только первый кусок транскрипта: для поиска «о чём была встреча»
этого хватает, конкретные фразы модель достаёт из тела уже при ответе (retrieve.py).

Индекс по вектору не строится (решение 0.3): только по факту замедления.
Работает под workos_reader: у него UPDATE на events и decisions.
В public.usage_log: command 'work_embed', cost 0, tokens = число фрагментов, одна строка
на пачку (прогон, убитый по памяти или таймауту, всё равно оставляет учёт);
секунды видны в сводке запуска (отдельной колонки в usage_log нет).

Прогон ограничен по времени (max_minutes, по умолчанию 40, `workctl embed --max-minutes N`):
первый бэкфилл за 90 дней на дроплете может идти часами, поэтому он идёт частями по ночам,
свежие строки первыми. В сводке partial=true, если время вышло раньше, чем работа.
"""
from __future__ import annotations

import logging
import time

from core import db, embed

log = logging.getLogger("workos.embed")

MAX_CHARS = 2000
MAX_MINUTES = 40

# таблица -> (заголовок, текст, время, доп. условие)
TABLES = {
    "events": ("coalesce(title, '')", "coalesce(body, '')", "occurred_at",
               "AND (coalesce(title, '') <> '' OR coalesce(body, '') <> '')"),
    "decisions": ("statement", "coalesce(rationale, '')", "decided_at", ""),
}


def _where(table: str) -> str:
    _, _, ts, extra = TABLES[table]
    return (f"FROM work.{table} "
            f"WHERE (embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s) "
            f"AND {ts} > now() - make_interval(days => %(days)s) {extra} "
            f"AND id <> ALL(%(skip)s::bigint[])")


def _select(table: str) -> str:
    head, text, ts, _ = TABLES[table]
    return (f"SELECT id, {head} AS head, left({text}, %(chars)s) AS text {_where(table)} "
            f"ORDER BY {ts} DESC LIMIT %(n)s")


def fragment(head: str, text: str) -> str:
    head, text = (head or "").strip(), (text or "").strip()
    joined = f"{head}\n\n{text}" if head and text else (head or text)
    return joined[:MAX_CHARS]


def _count(conn, table: str, model: str, days: int) -> int:
    return conn.execute(f"SELECT count(*) AS n {_where(table)}",
                        {"model": model, "days": days, "skip": []}).fetchone()["n"]


def _log_usage(conn, n: int) -> None:
    try:
        conn.execute("INSERT INTO public.usage_log (command, cost_usd, tokens) VALUES ('work_embed', 0, %s)", (n,))
    except Exception as e:  # noqa: BLE001 — учёт не роняет бэкфилл
        log.warning("usage_log: %s", e)


def _backfill(conn, table: str, model: str, days: int, batch: int, deadline: float | None = None) -> tuple[int, bool]:
    """-> (сколько посчитано, кончилось ли время раньше работы)."""
    vtype = embed.vector_type(conn, f"work.{table}")
    if vtype:
        upd = f"UPDATE work.{table} SET embedding = %s::{vtype}, embedding_model = %s WHERE id = %s"
    else:   # тестовая база без pgvector: колонка real[]
        upd = f"UPDATE work.{table} SET embedding = %s::real[], embedding_model = %s WHERE id = %s"
    done = 0
    seen: list[int] = []
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return done, True
        rows = conn.execute(_select(table), {"model": model, "days": days, "skip": seen,
                                            "n": batch, "chars": MAX_CHARS}).fetchall()
        if not rows:
            break
        texts = [fragment(r["head"], r["text"]) for r in rows]
        vecs = embed.embed(texts, kind="passage")
        params = [(embed.literal(v) if vtype else v, model, r["id"]) for r, v in zip(rows, vecs)]
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(upd, params)
        _log_usage(conn, len(rows))
        seen.extend(r["id"] for r in rows)      # страховка от вечного цикла, если строка не обновилась
        done += len(rows)
        log.info("embed %s: +%d (всего %d)", table, len(rows), done)
    return done, False


def run(date: str | None = None, dry_run: bool = False, days: int = 90, batch: int = 32,
        max_minutes: float | None = MAX_MINUTES, **kw) -> dict:
    model = embed.model_name()
    t0 = time.time()
    deadline = time.monotonic() + float(max_minutes) * 60 if max_minutes else None
    out: dict = {"model": model, "days": days}
    with db.connect(autocommit=True) as conn:
        if dry_run:
            for table in ("events", "decisions"):
                out[f"{table}_todo"] = _count(conn, table, model, days)
            out["dry_run"] = True
            return out
        partial = False
        for table in ("events", "decisions"):
            out[table], cut = _backfill(conn, table, model, days, batch, deadline)
            partial = partial or cut
        out["partial"] = partial
    total = out["events"] + out["decisions"]
    out["seconds"] = secs = round(time.time() - t0, 1)
    log.info("embed: %d фрагментов за %.1f с%s", total, secs, ", время вышло, остальное в следующий раз" if partial else "")
    return out


if __name__ == "__main__":
    print(run())

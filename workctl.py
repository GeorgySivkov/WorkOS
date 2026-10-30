#!/usr/bin/env python3
"""WorkOS control CLI: одна точка входа для systemd-таймеров и ручных запусков.

    WORKOS_ROLE=reader python workctl.py plan --dry-run
    WORKOS_ROLE=reader python workctl.py collect --kind gmail
    WORKOS_ROLE=admin  python workctl.py seed-grid mandarat.json --auto A1=garmin:sleep:7.5
    python workctl.py status | sql-check | ping

.env выбирается по WORKOS_ROLE (это делает core.config). Модули импортируются лениво:
команда, чей модуль ещё не написан, честно говорит об этом и не роняет остальные.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config  # noqa: E402  (подгружает .env роли)

# команда -> (модуль:функция, роль)
COMMANDS = {
    "collect": ("collectors.base:run_enabled", "reader"),
    "eas-watch": ("collectors.eas:watch", "reader"),
    "ingest": ("collectors.ingest_server:serve", "reader"),
    "people": ("extract.people:run", "reader"),
    "embed": ("agents.embed_backfill:run", "reader"),
    "extract": ("extract.decisions:run", "reader"),
    "triage": ("extract.triage:run", "reader"),
    "draft": ("agents.drafter:run", "reader"),
    "brief": ("agents.brief:run_brief", "reader"),
    "cards": ("agents.brief:run_meeting_cards", "reader"),
    "review": ("agents.review:run", "reader"),
    "autonomy": ("agents.autonomy:run", "reader"),
    "audit": ("agents.audit:run", "reader"),
    "report": ("agents.reports:run", "reader"),
    "plan": ("agents.planner:run", "reader"),
    "dayplan": ("agents.dayplan:run", "reader"),
    "classify": ("agents.dayplan:classify_tasks", "reader"),
    "award": ("rpg.award:run", "reader"),
    "garmin": ("rpg.garmin:run", "reader"),
    "garmin-login": ("rpg.garmin:login", "reader"),
    "weekly": ("rpg.weekly:run", "reader"),
    "seed-grid": ("rpg.seed:run", "admin"),
    "health": ("health:run", "reader"),
}

HELP = {
    "collect": "коллекторы: все включённые, по виду (--kind) или один источник (--source)",
    "seed-grid": "засеять сетку из JSON страницы «Мандарат»",
    "report": "отчёт из базы (--since YYYY-MM-DD)",
    "garmin-login": "разовый вход в Garmin, токены в GARMIN_TOKENS",
}


class Missing(RuntimeError):
    pass


def resolve(target: str):
    mod_name, func_name = target.split(":")
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as e:
        if e.name and (mod_name == e.name or mod_name.startswith(e.name + ".")):
            raise Missing(f"модуль {mod_name} ещё не написан") from e
        raise
    fn = getattr(mod, func_name, None)
    if fn is None:
        raise Missing(f"в {mod_name} нет функции {func_name}")
    return fn


def call(fn, kwargs: dict):
    """Передать только те аргументы, которые функция принимает."""
    sig = inspect.signature(fn)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(**kwargs)
    dropped = [k for k in kwargs if k not in sig.parameters]
    if dropped:
        print(f"(игнорирую {', '.join('--' + k.replace('_', '-') for k in dropped)}: команда их не принимает)",
              file=sys.stderr)
    return fn(**{k: v for k, v in kwargs.items() if k in sig.parameters})


def show(result) -> None:
    if result is None:
        return
    if isinstance(result, (dict, list)):
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    else:
        print(result)


def cmd_task(args) -> int:
    target, role = COMMANDS[args.cmd]
    if role == "admin" and os.environ.get("WORKOS_ROLE") != "admin" and not args.dry_run:
        print(f"{args.cmd} запускается под админом: WORKOS_ROLE=admin python workctl.py {args.cmd} ...",
              file=sys.stderr)
        return 2
    kwargs: dict = {}
    if args.date:
        kwargs["date"] = args.date
    if args.dry_run:
        kwargs["dry_run"] = True
    if args.force:
        kwargs["force"] = True
    if args.cmd == "collect":
        if args.source or (args.arg and ":" in args.arg):
            target, kwargs = "collectors.base:run_source", {"source_id": args.source or args.arg}
        else:
            kind = args.kind or args.arg
            kwargs = {"kind": kind} if kind else {}
    elif args.cmd == "seed-grid":
        if not args.arg:
            print("нужен файл: workctl seed-grid mandarat.json", file=sys.stderr)
            return 2
        kwargs.update(file=args.arg, auto=args.auto, reward=args.reward)
        if args.season:
            kwargs["season"] = args.season
    elif args.cmd == "report":
        if args.since:
            kwargs["since"] = args.since
        if args.kind:
            kwargs["kind"] = args.kind
    elif args.cmd in ("garmin", "embed") and args.days:
        kwargs["days"] = args.days
    try:
        fn = resolve(target)
    except Missing as e:
        print(f"{args.cmd}: {e}", file=sys.stderr)
        return 3
    show(call(fn, kwargs))
    return 0


# ---------- встроенные команды ----------
def cmd_ping(_args) -> int:
    from core import db
    with db.connect() as conn:
        r = conn.execute("SELECT version() AS v, current_user AS u, current_database() AS d").fetchone()
    print(f"role : {config.ROLE}")
    print(f"user : {r['u']}@{r['d']}")
    print(r["v"])
    return 0


def _section(title: str, fn) -> None:
    print(f"\n{title}")
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — у роли может не быть прав на часть таблиц
        print(f"  нет данных: {type(e).__name__}: {str(e).splitlines()[0][:150]}")


def cmd_status(_args) -> int:
    from core import db, timeutil as tu
    with db.connect(autocommit=True) as conn:
        u = conn.execute("SELECT current_user AS u").fetchone()["u"]
        print(f"WorkOS · роль {config.ROLE} ({u}) · {tu.local().strftime('%d.%m %H:%M')} {config.TZ.key}")

        def sources():
            for s in conn.execute("SELECT id, enabled, last_ok_at, last_error FROM work.sources ORDER BY id"):
                ok = tu.local(s["last_ok_at"]).strftime("%d.%m %H:%M") if s["last_ok_at"] else "никогда"
                err = f" · ошибка: {s['last_error'].strip().splitlines()[0][:80]}" if s["last_error"] else ""
                print(f"  {'●' if s['enabled'] else '○'} {s['id']:<18} {ok}{err}")

        def outbox():
            r = conn.execute("SELECT count(*) AS n, min(created_at) AS oldest FROM work.outbox "
                             "WHERE sent_at IS NULL").fetchone()
            oldest = f", самое старое {tu.local(r['oldest']).strftime('%d.%m %H:%M')}" if r["oldest"] else ""
            print(f"  не отправлено: {r['n']}{oldest}")

        def actions():
            rows = conn.execute("SELECT status, count(*) AS n FROM work.actions GROUP BY status ORDER BY status").fetchall()
            print("  " + (", ".join(f"{r['status']} {r['n']}" for r in rows) or "пусто"))

        def tasks():
            d = tu.logical_today()
            d0, d1 = tu.day_bounds(d)
            r = conn.execute(
                """SELECT count(*) FILTER (WHERE due = %(d)s OR planned_start >= %(d0)s AND planned_start < %(d1)s) AS planned,
                          count(*) FILTER (WHERE done_at >= %(d0)s AND done_at < %(d1)s) AS done,
                          count(*) FILTER (WHERE done_at IS NULL AND dropped_at IS NULL AND due < %(d)s) AS overdue
                   FROM work.tasks""", {"d": d, "d0": d0, "d1": d1}).fetchone()
            print(f"  {tu.ru_date(d)}: на сегодня {r['planned']}, сделано {r['done']}, просрочено {r['overdue']}")

        _section("Источники", sources)
        _section("Outbox", outbox)
        _section("Действия", actions)
        _section("Задачи", tasks)
    return 0


# Ожидаемые права ролей (docs/CODE.md, migrations/00*.sql): (вид, объект, право, ждём)
T, C, F = "table", "column", "function"
EXPECT = {
    "workos_reader": [
        (T, "work.events", "SELECT", True), (T, "work.events", "INSERT", True),
        (T, "work.actions", "INSERT", True), (C, "work.actions.status", "UPDATE", False),
        (C, "work.actions.payload", "UPDATE", False),
        (T, "work.outbox", "INSERT", True), (C, "work.outbox.sent_at", "UPDATE", False),
        (T, "work.notices", "INSERT", True), (T, "work.notices", "DELETE", True),
        (T, "work.tasks", "SELECT", True), (T, "work.tasks", "INSERT", False),
        (C, "work.tasks.cell_id", "UPDATE", True), (C, "work.tasks.planned_start", "UPDATE", True),
        (C, "work.tasks.done_at", "UPDATE", False),
        (T, "work.calendar_feed", "SELECT", True),
        (T, "rpg.xp", "INSERT", True), (T, "rpg.cells", "INSERT", False),
        (T, "rpg.level_marks", "UPDATE", True), (T, "rpg.achievements", "INSERT", True),
        (F, "rpg.award_task(bigint)", "EXECUTE", True), (F, "rpg.log_manual(text)", "EXECUTE", False),
    ],
    "workos_bot": [
        (T, "work.events", "SELECT", True), (T, "work.actions", "INSERT", True),
        (C, "work.actions.status", "UPDATE", True), (C, "work.actions.approved_hash", "UPDATE", True),
        (C, "work.actions.payload", "UPDATE", False),
        (T, "work.outbox", "INSERT", True), (C, "work.outbox.sent_at", "UPDATE", True),
        (T, "work.tasks", "INSERT", True), (C, "work.tasks.due", "UPDATE", True),
        (C, "work.tasks.planned_start", "UPDATE", True),
        (T, "rpg.xp", "INSERT", False), (T, "work.calendar_feed", "SELECT", True),
    ],
    "workos_exec": [
        (T, "work.events", "SELECT", False), (T, "work.calendar_feed", "SELECT", True),
        (T, "work.actions", "SELECT", True), (C, "work.actions.status", "UPDATE", True),
        (C, "work.actions.approved_hash", "UPDATE", False), (C, "work.actions.payload", "UPDATE", False),
        (T, "work.outbox", "INSERT", True), (C, "work.tasks.planned_start", "UPDATE", True),
        (C, "work.tasks.block_synced_at", "UPDATE", True), (C, "work.tasks.title", "UPDATE", False),
        (T, "work.outcomes", "INSERT", False),
    ],
    "workos_app": [
        (T, "work.tasks", "SELECT", True), (T, "work.tasks", "INSERT", True),
        (C, "work.tasks.done_at", "UPDATE", True), (C, "work.tasks.planned_start", "UPDATE", True),
        (T, "work.calendar_feed", "SELECT", True),
        (T, "work.events", "SELECT", False), (T, "work.actions", "SELECT", False),
        (T, "work.outbox", "SELECT", False), (T, "rpg.xp", "INSERT", False),
        (F, "rpg.log_manual(text)", "EXECUTE", True), (F, "rpg.award_task(bigint)", "EXECUTE", True),
    ],
}


def cmd_sql_check(_args) -> int:
    from core import db
    with db.connect(autocommit=True) as conn:
        user = conn.execute("SELECT current_user AS u").fetchone()["u"]
        checks = EXPECT.get(user)
        if not checks:
            print(f"роль {user}: проверок нет (админ или неизвестная роль)")
            return 0
        bad = 0
        print(f"роль {user}:")
        for kind, obj, priv, want in checks:
            if kind == C:
                rel, col = obj.rsplit(".", 1)
                q, params = "SELECT has_column_privilege(current_user, %s, %s, %s) AS ok", (rel, col, priv)
            elif kind == F:
                q, params = "SELECT has_function_privilege(current_user, %s, %s) AS ok", (obj, priv)
            else:
                q, params = "SELECT has_table_privilege(current_user, %s, %s) AS ok", (obj, priv)
            try:
                got = conn.execute(q, params).fetchone()["ok"]
            except Exception as e:  # noqa: BLE001 — объекта нет: миграция не применена
                got = None
                note = f" (нет объекта: {str(e).splitlines()[0][:80]})"
            else:
                note = ""
            ok = got == want
            bad += not ok
            print(f"  {'✓' if ok else '✗'} {priv} {obj}: {'можно' if want else 'нельзя'}"
                  + ("" if ok else f", а на деле {'можно' if got else 'нельзя'}") + note)
        print("всё как задумано" if not bad else f"расхождений: {bad}")
        return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="workctl", description="WorkOS: запуск задач и проверки")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping", help="проверить подключение к базе").set_defaults(func=cmd_ping)
    sub.add_parser("status", help="источники, outbox, действия, задачи").set_defaults(func=cmd_status)
    sub.add_parser("sql-check", help="права текущей роли базы").set_defaults(func=cmd_sql_check)
    for name, (target, role) in COMMANDS.items():
        sp = sub.add_parser(name, help=HELP.get(name, f"{target} ({role})"))
        sp.add_argument("arg", nargs="?", help="collect: id или вид источника; seed-grid: файл JSON")
        sp.add_argument("--date", help="YYYY-MM-DD")
        sp.add_argument("--dry-run", action="store_true", help="показать, ничего не записывать")
        sp.add_argument("--force", action="store_true", help="повторить, даже если уже было")
        if name in ("collect", "report"):
            sp.add_argument("--kind", help="collect: вид источника; report: вид отчёта")
        if name == "collect":
            sp.add_argument("--source")
        if name == "seed-grid":
            sp.add_argument("--season", type=int)
            sp.add_argument("--auto", action="append", default=[], metavar="A1=garmin:sleep:7.5")
            sp.add_argument("--reward", action="append", default=[], metavar='character:8="..."')
        if name == "report":
            sp.add_argument("--since", help="YYYY-MM-DD")
        if name in ("garmin", "embed"):
            sp.add_argument("--days", type=int)
        sp.set_defaults(func=cmd_task)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

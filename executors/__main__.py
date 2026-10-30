"""Точка входа процесса workos-exec.

    python -m executors queue [--once] [--types trello_card,slack_post] [--sleep 10]
    python -m executors calendar [--loop 900] [--only mirror|blocks]

Без WORKOS_ROLE процесс считает себя исполнителем и читает .env.executor.
"""
from __future__ import annotations

import os

os.environ.setdefault("WORKOS_ROLE", "executor")

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m executors")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="очередь одобренных действий")
    q.add_argument("--once", action="store_true", help="разобрать очередь и выйти")
    q.add_argument("--types", default="", help="через запятую, по умолчанию все типы исполнителей")
    q.add_argument("--sleep", type=float, default=10, help="пауза опроса, секунды")
    c = sub.add_parser("calendar", help="зеркало календарей и блоки плана")
    c.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 = один проход)")
    c.add_argument("--only", choices=["mirror", "blocks"], help="только одна часть")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.environ.get("WORKOS_LOG", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from executors import base

    if args.cmd == "queue":
        types = [t.strip() for t in args.types.split(",") if t.strip()] or None
        print(json.dumps(base.run_queue(once=args.once, types=types, sleep=args.sleep), ensure_ascii=False))
        return 0

    from executors import calendar
    parts = (args.only,) if args.only else ("mirror", "blocks")
    if args.loop:
        base.install_signal_handlers()
    while True:
        try:
            res = calendar.run(parts=parts)
            print(json.dumps(res, ensure_ascii=False, default=str), flush=True)
        except Exception:  # noqa: BLE001  нет сети или базы: следующий проход
            logging.getLogger("executors").exception("календарь: проход упал")
            if not args.loop:
                return 1
        if not args.loop or base.wait(args.loop):
            return 0


if __name__ == "__main__":
    sys.exit(main())

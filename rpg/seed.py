"""П.0 -> П.1: засев сетки целей (Мандарат 9×9) в rpg.cells.

Источник: выгрузка страницы «Мандарат» в JSON
    {"v": 1, "center": "главный квест",
     "dirs": [{"t": "Здоровье", "acts": [{"t": "Сон 7,5 ч", "k": "habit"}, ...8]}, ...8]}
id: C центр, A..H направления, A1..H8 практики. Пустые клетки пропускаются.

Засев идемпотентный: названия и типы обновляются, клетки, исчезнувшие из сетки,
становятся active=false и не удаляются (на них ссылается журнал опыта).

Запуск под админской ролью:
    WORKOS_ROLE=admin python workctl.py seed-grid mandarat.json \
        --auto A1=garmin:sleep:7.5 --reward character:8="Наушники"
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core import db

LETTERS = "ABCDEFGH"
XP_BASE = {"habit": 10, "quality": 20, "milestone": 150}
KIND_ALIASES = {
    "habit": "habit", "привычка": "habit", "h": "habit",
    "quality": "quality", "качество": "quality", "q": "quality",
    "milestone": "milestone", "этап": "milestone", "m": "milestone", "boss": "milestone", "босс": "milestone",
}


def _kind(raw) -> str:
    return KIND_ALIASES.get(str(raw or "").strip().lower(), "habit")


def _title(raw) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def parse_grid(data: dict) -> list[dict]:
    """JSON страницы -> список клеток {id, parent, title, kind, xp_base?, auto_source?}."""
    if isinstance(data.get("grid"), dict):          # на случай обёртки
        data = data["grid"]
    cells = [{"id": "C", "parent": None, "title": _title(data.get("center")) or "Главный квест",
              "kind": "quest", "xp_base": 0}]
    for i, d in enumerate((data.get("dirs") or [])[:8]):
        d = d or {}
        title = _title(d.get("t") or d.get("title"))
        if not title:
            continue
        aid = LETTERS[i]
        cells.append({"id": aid, "parent": "C", "title": title, "kind": "attribute", "xp_base": 0})
        for j, a in enumerate((d.get("acts") or [])[:8]):
            a = a if isinstance(a, dict) else {"t": a}
            at = _title(a.get("t") or a.get("title"))
            if not at:
                continue
            kind = _kind(a.get("k") or a.get("kind"))
            cell = {"id": f"{aid}{j + 1}", "parent": aid, "title": at, "kind": kind}
            if a.get("xp"):
                cell["xp_base"] = int(a["xp"])
            if a.get("auto") is not None:
                cell["auto_source"] = a["auto"] or None
            cells.append(cell)
    return cells


def parse_auto(pairs) -> dict[str, str | None]:
    """['A1=garmin:sleep:7.5', 'B3=-'] -> {'A1': 'garmin:sleep:7.5', 'B3': None}."""
    out = {}
    for p in pairs or []:
        cell, _, src = p.partition("=")
        if not cell or not _:
            raise ValueError(f"--auto ждёт КЛЕТКА=источник, получено: {p}")
        src = src.strip()
        out[cell.strip().upper()] = None if src in ("", "-", "none") else src
    return out


def parse_rewards(items) -> list[dict]:
    """['character:8=Наушники', 'A:5="Кроссовки"'] -> [{scope, level, title}]."""
    out = []
    for it in items or []:
        m = re.match(r"^\s*([\w-]+):(\d+)\s*=\s*(.+?)\s*$", it, re.S)
        if not m:
            raise ValueError(f"--reward ждёт scope:уровень=\"название\", получено: {it}")
        scope, level, title = m.group(1), int(m.group(2)), m.group(3).strip().strip('"«»').strip()
        out.append({"scope": scope if scope == "character" else scope.upper(), "level": level, "title": title})
    return out


def seed(conn, cells: list[dict], season: int | None = None, auto: dict | None = None,
         rewards: list[dict] | None = None) -> dict:
    existing = {r["id"]: r for r in conn.execute("SELECT id, kind, xp_base, active FROM rpg.cells")}
    ins = upd = 0
    for c in cells:                                   # родители идут раньше детей
        old = existing.get(c["id"])
        if "xp_base" in c:
            base = c["xp_base"]
        elif old and old["kind"] == c["kind"]:
            base = old["xp_base"]                     # ручная правка стоимости этапа переживает засев
        else:
            base = XP_BASE.get(c["kind"], 10)
        row = conn.execute(
            """INSERT INTO rpg.cells (id, parent, title, kind, xp_base, season, active)
               VALUES (%(id)s, %(parent)s, %(title)s, %(kind)s, %(xp)s, COALESCE(%(season)s, 1), true)
               ON CONFLICT (id) DO UPDATE SET
                 parent = EXCLUDED.parent, title = EXCLUDED.title, kind = EXCLUDED.kind,
                 xp_base = EXCLUDED.xp_base, active = true,
                 season = COALESCE(%(season)s, rpg.cells.season)
               RETURNING (xmax = 0) AS inserted""",
            {"id": c["id"], "parent": c["parent"], "title": c["title"], "kind": c["kind"],
             "xp": base, "season": season}).fetchone()
        if row["inserted"]:
            ins += 1
        else:
            upd += 1
        if "auto_source" in c:
            conn.execute("UPDATE rpg.cells SET auto_source = %s WHERE id = %s", (c["auto_source"], c["id"]))
    ids = [c["id"] for c in cells]
    gone = conn.execute("UPDATE rpg.cells SET active = false WHERE active AND NOT (id = ANY(%s)) RETURNING id",
                        (ids,)).fetchall()
    for cell, src in (auto or {}).items():
        r = conn.execute("UPDATE rpg.cells SET auto_source = %s WHERE id = %s RETURNING id", (src, cell)).fetchone()
        if not r:
            raise ValueError(f"--auto: клетки {cell} нет в сетке")
    for rw in rewards or []:
        conn.execute("""INSERT INTO rpg.rewards (scope, level, title) VALUES (%(scope)s, %(level)s, %(title)s)
                        ON CONFLICT (scope, level) DO UPDATE SET title = EXCLUDED.title""", rw)
    return {"cells": len(cells), "inserted": ins, "updated": upd,
            "deactivated": [r["id"] for r in gone], "auto": len(auto or {}), "rewards": len(rewards or [])}


def run(file: str | None = None, season: int | None = None, auto=None, reward=None,
        dry_run: bool = False, **kw) -> dict:
    if not file:
        raise ValueError("нужен путь к JSON страницы «Мандарат»")
    data = json.loads(Path(file).read_text(encoding="utf-8"))
    cells = parse_grid(data)
    auto_map = parse_auto(auto) if isinstance(auto, (list, tuple)) else (auto or {})
    rewards = parse_rewards(reward) if isinstance(reward, (list, tuple)) else []
    for rw in data.get("rewards") or []:             # награды можно положить и в сам JSON
        rewards.append({"scope": rw.get("scope", "character"), "level": int(rw["level"]), "title": rw["title"]})
    if dry_run:
        return {"dry_run": True, "cells": len(cells),
                "attributes": [c["title"] for c in cells if c["kind"] == "attribute"],
                "auto": auto_map, "rewards": rewards}
    with db.connect() as conn, conn.transaction():
        return seed(conn, cells, season=season, auto=auto_map, rewards=rewards)


def main(argv=None) -> None:
    from core import config
    ap = argparse.ArgumentParser(prog="rpg.seed")
    ap.add_argument("file")
    ap.add_argument("--season", type=int)
    ap.add_argument("--auto", action="append", default=[])
    ap.add_argument("--reward", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if config.ROLE != "admin" and not a.dry_run:
        raise SystemExit("засев сетки идёт под админом: WORKOS_ROLE=admin")
    print(run(a.file, season=a.season, auto=a.auto, reward=a.reward, dry_run=a.dry_run))


if __name__ == "__main__":
    main()

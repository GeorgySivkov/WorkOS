"""Коллектор Trello (шаг 2.2).

Карточки доски (по умолчанию George_Sivkov_Board), изменённые с прошлого
прогона минус lookback_days. Зеркальные карточки (cardRole='mirror',
поле mirrorSourceId) разрешаются в исходную: имя, описание, срок и метки
берутся у неё, оба id остаются в meta. body = описание при первой встрече с
карточкой, текущие имя и описание всегда в meta.name и meta.desc.

Настройки источника (work.sources.config):
  {"board_id": "6a19...", "board_ids": [...], "workspace_id": "...",
   "key_env": "TRELLO_KEY", "token_env": "TRELLO_TOKEN", "card_filter": "all"}
Секреты: TRELLO_KEY (ключ приложения) и токен с scope=read.

Проверить, один ли это аккаунт у личного Trello и Trello Инфатики:
    python -m collectors.trello whoami                 # токен из TRELLO_TOKEN
    python -m collectors.trello whoami TRELLO_INFATICA_TOKEN
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from typing import Iterable

import httpx

from collectors.base import Collector, Event, run_enabled
from core import config, timeutil as tu

API = "https://api.trello.com/1"
DEFAULT_BOARD = "6a197760b7795b2a91550ec0"     # George_Sivkov_Board
CARD_URL_RX = re.compile(r"trello\.com/c/([A-Za-z0-9]+)")
SOURCE_FIELDS = "name,desc,due,dueComplete,idLabels,labels,idList,idBoard,shortUrl,url,closed,dateLastActivity,idMembers"


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class TrelloAPI:
    """Тонкий клиент Trello REST с ключом и токеном в заголовке (не в URL, чтобы не светить в логах)."""

    def __init__(self, key: str, token: str, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.http = httpx.Client(base_url=API, timeout=30, transport=transport, headers={
            "Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"',
            "Accept": "application/json",
        })
        self.sleep = sleep

    def get(self, path: str, **params):
        for attempt in range(5):
            r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
            if r.status_code == 429:
                self.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Trello {path}: {r.status_code} {r.text[:200]}")
            return r.json()
        raise RuntimeError(f"Trello {path}: лимит запросов не отпустил")


def api_from_config(cfg: dict, transport=None, sleep=time.sleep) -> TrelloAPI:
    key = config.env(cfg.get("key_env") or "TRELLO_KEY", required=True)
    token = config.env(cfg.get("token_env") or "TRELLO_TOKEN", required=True)
    return TrelloAPI(key, token, transport=transport, sleep=sleep)


class TrelloCollector(Collector):
    kind = "trello"

    def __init__(self, source: dict, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        super().__init__(source)
        self.cfg = source.get("config") or {}
        self.transport = transport
        self.sleep = sleep

    # ---------- доски ----------
    def _boards(self, api: TrelloAPI) -> list[str]:
        ids = list(self.cfg.get("board_ids") or [])
        if self.cfg.get("board_id"):
            ids.append(self.cfg["board_id"])
        ws = self.cfg.get("workspace_id")
        if ws:
            ids += [b["id"] for b in api.get(f"/organizations/{ws}/boards", filter="open", fields="id")]
        if not ids:
            ids = [DEFAULT_BOARD]
        return list(dict.fromkeys(ids))

    # ---------- зеркала ----------
    @staticmethod
    def mirror_source_id(card: dict) -> str | None:
        """id исходной карточки для зеркала или None. У зеркала имя = ссылка на источник."""
        src = card.get("mirrorSourceId")
        if src:
            return src
        if card.get("cardRole") == "mirror":
            m = CARD_URL_RX.search(card.get("name") or "")
            if m:
                return m.group(1)          # shortLink тоже годится как id в /cards/{id}
        return None

    def collect(self) -> Iterable[Event]:
        started = tu.now()
        since = self.since(default_days=max(self.lookback.days, 7))
        api = api_from_config(self.cfg, self.transport, self.sleep)
        source_cache: dict[str, dict | None] = {}

        def source_card(sid: str) -> dict | None:
            if sid not in source_cache:
                try:
                    source_cache[sid] = api.get(f"/cards/{sid}", fields=SOURCE_FIELDS,
                                                board="true", board_fields="name", list="true",
                                                list_fields="name", members="true",
                                                member_fields="fullName,username")
                except RuntimeError:
                    source_cache[sid] = None    # нет доступа к исходной доске: оставим зеркало как есть
            return source_cache[sid]

        for board_id in self._boards(api):
            board = api.get(f"/boards/{board_id}", fields="name,url")
            lists = {x["id"]: x["name"] for x in api.get(f"/boards/{board_id}/lists", filter="all", fields="name")}
            labels = {x["id"]: (x.get("name") or x.get("color") or "")
                      for x in api.get(f"/boards/{board_id}/labels", fields="name,color", limit=1000)}
            members = {x["id"]: (x.get("fullName") or x.get("username") or x["id"])
                       for x in api.get(f"/boards/{board_id}/members", fields="fullName,username")}
            cards = api.get(f"/boards/{board_id}/cards/{self.cfg.get('card_filter') or 'all'}")
            for card in cards:
                yield from self._card_event(card, board, lists, labels, members, since, source_card)
        self.state = {"cursor": started.isoformat()}

    def _card_event(self, card, board, lists, labels, members, since, source_card) -> Iterable[Event]:
        last = _parse_ts(card.get("dateLastActivity")) or tu.now()
        src_id = self.mirror_source_id(card)
        src = source_card(src_id) if src_id else None
        src_last = _parse_ts(src.get("dateLastActivity")) if src else None
        # зеркало само не меняется, когда правят исходную карточку: смотрим на обе даты
        changed = max(last, src_last) if src_last else last
        if changed < since:
            return
        name, desc, due = card.get("name"), card.get("desc"), card.get("due")
        card_labels = [labels.get(i, i) for i in card.get("idLabels") or []]
        card_members = [members.get(i, i) for i in card.get("idMembers") or []]
        meta = {
            "board": board.get("name"), "board_id": card.get("idBoard"),
            "list": lists.get(card.get("idList"), card.get("idList")), "list_id": card.get("idList"),
            "url": card.get("shortUrl") or card.get("url"), "closed": bool(card.get("closed")),
            "due_complete": bool(card.get("dueComplete")), "mirror_of": None,
        }
        if src:
            name, desc, due = src.get("name"), src.get("desc"), src.get("due")
            card_labels = [x.get("name") or x.get("color") or "" for x in src.get("labels") or []]
            if src.get("members"):
                card_members = [m.get("fullName") or m.get("username") for m in src["members"]]
            meta.update({
                "mirror_of": src.get("id") or src_id,
                "mirror_card_id": card["id"],
                "source_url": src.get("shortUrl") or src.get("url"),
                "source_board": (src.get("board") or {}).get("name"),
                "source_list": (src.get("list") or {}).get("name"),
                "source_closed": bool(src.get("closed")),
                "due_complete": bool(src.get("dueComplete")),
            })
        elif src_id:
            meta["mirror_of"] = src_id          # источник недоступен, но связь сохраняем
        # body события не переписывается (правило коллекторов): текущее описание
        # карточки держим в meta.desc, как текущее имя в meta.name
        meta.update({"name": name, "desc": desc or "", "labels": card_labels, "members": card_members,
                     "due": due})
        yield Event(external_id=card["id"], kind="card", occurred_at=changed, title=name,
                    body=desc or None, meta=meta, thread_key=f"trello:{src_id or card['id']}")


def whoami(token_env: str = "TRELLO_TOKEN", key_env: str = "TRELLO_KEY", transport=None) -> dict:
    """Какие доски и воркспейсы видит токен: так видно, один ли аккаунт у двух Trello."""
    api = api_from_config({"token_env": token_env, "key_env": key_env}, transport)
    me = api.get("/members/me", fields="id,username,fullName")
    orgs = api.get("/members/me/organizations", fields="id,name,displayName")
    boards = api.get("/members/me/boards", filter="open", fields="id,name,idOrganization,url")
    return {"me": me, "organizations": orgs, "boards": boards}


def run(date: str | None = None, dry_run: bool = False, **kw) -> dict:
    return {"trello": run_enabled("trello")}


def _print_whoami(token_env: str) -> None:
    info = whoami(token_env)
    me = info["me"]
    print(f"Аккаунт: {me.get('fullName')} (@{me.get('username')}, id {me.get('id')})")
    orgs = {o["id"]: o.get("displayName") or o.get("name") for o in info["organizations"]}
    print("Воркспейсы:")
    for oid, name in orgs.items():
        print(f"  {oid}  {name}")
    print("Доски:")
    for b in info["boards"]:
        print(f"  {b['id']}  {b['name']}  [{orgs.get(b.get('idOrganization'), 'без воркспейса')}]")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "whoami":
        _print_whoami(sys.argv[2] if len(sys.argv) > 2 else "TRELLO_TOKEN")
    else:
        print(run())

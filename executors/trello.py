"""Исполнитель trello_card (шаг 5.2): карточка в формате George.

.env.executor: TRELLO_KEY, TRELLO_WRITE_TOKEN (токен с правом write),
TRELLO_DRAFT_LIST_ID (необязательно): куда класть «Оставить черновиком».
Ключ и токен уходят заголовком Authorization, а не в URL, чтобы не попасть в логи.
"""
from __future__ import annotations

import httpx

from core import config
from executors import base

API = "https://api.trello.com/1"
DRAFT_PREFIX = "[черновик] "


def _client() -> httpx.Client:
    key = config.env("TRELLO_KEY", required=True)
    token = config.env("TRELLO_WRITE_TOKEN", required=True)
    return base.http_client(headers={
        "Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"',
        "Accept": "application/json",
    })


def _board_labels(http: httpx.Client, list_id: str) -> dict[str, str]:
    """Имя метки (без регистра) -> id метки на доске, где лежит список."""
    r = base.check(base.request(http, "GET", f"{API}/lists/{list_id}/board", params={"fields": "id"}),
                   "Trello: доска списка")
    board_id = r.json()["id"]
    r = base.check(base.request(http, "GET", f"{API}/boards/{board_id}/labels",
                                params={"fields": "name,color", "limit": 1000}), "Trello: метки доски")
    out: dict[str, str] = {}
    for lab in r.json():
        name = (lab.get("name") or "").strip().casefold()
        if name and name not in out:
            out[name] = lab["id"]
    return out


def execute(action: dict, notify) -> dict:
    p = action["payload"]
    list_id, name = p["list_id"], p["name"]
    drafted = action["status"] == "drafted"
    if drafted:
        draft_list = config.env("TRELLO_DRAFT_LIST_ID")
        if draft_list:
            list_id = draft_list
        else:
            name = DRAFT_PREFIX + name

    with _client() as http:
        label_ids, unknown = [], []
        if p["labels"]:
            board = _board_labels(http, list_id)
            for lab in p["labels"]:
                lid = board.get(lab.strip().casefold())
                if lid and lid not in label_ids:
                    label_ids.append(lid)
                elif not lid:
                    unknown.append(lab)

        card = {"idList": list_id, "name": name, "desc": p["desc"]}
        if p.get("due"):
            card["due"] = p["due"]
        if label_ids:
            card["idLabels"] = ",".join(label_ids)
        r = base.check(base.request(http, "POST", f"{API}/cards", json=card), "Trello: создать карточку")
        c = r.json()
        result = {"card_id": c["id"], "url": c.get("shortUrl") or c.get("url"), "list_id": list_id,
                  "labels": label_ids}
        if unknown:
            result["unknown_labels"] = unknown

        cl = p.get("checklist")
        if cl:
            # карточка уже создана: ошибка чеклиста не делает действие failed,
            # иначе повторное одобрение плодило бы дубли карточек
            try:
                r = base.check(base.request(http, "POST", f"{API}/cards/{c['id']}/checklists",
                                            json={"name": cl["name"]}), "Trello: чеклист")
                checklist_id = r.json()["id"]
                for item in cl["items"]:
                    base.check(base.request(http, "POST", f"{API}/checklists/{checklist_id}/checkItems",
                                            json={"name": item, "pos": "bottom"}), "Trello: пункт чеклиста")
                result["checklist_id"] = checklist_id
            except (base.ExecError, httpx.HTTPError) as e:
                result["checklist_error"] = str(e)[:500]
                notify(f"Карточка создана, но чеклист не добавился: {base.esc(str(e)[:300])}\n"
                       f"{base.esc(result['url'] or '')}")
    if unknown:
        notify(f"Карточка «{base.esc(name)}» создана, таких меток на доске нет: "
               f"{base.esc(', '.join(unknown))}")
    return result

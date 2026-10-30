"""Исполнитель slack_post: «Отправить» публикует, «Оставить черновиком» ничего не постит.

.env.executor: SLACK_WRITE_TOKEN (бот xoxb- со скоупом chat:write или
пользовательский xoxp- с chat:write, тогда сообщение уходит от имени George).
Черновик: Slack API не умеет создавать черновики, поэтому текст приходит в
«Решения» готовым к копированию, а результат действия = {draft: true}.
"""
from __future__ import annotations

from core import config
from executors import base

API = "https://slack.com/api"


def execute(action: dict, notify) -> dict:
    p = action["payload"]
    if action["status"] == "drafted":
        where = f"канал {p['channel']}" + (", в тред" if p.get("thread_ts") else "")
        notify(f"Черновик для Slack ({base.esc(where)}), скопируй и отправь сам:\n"
               f"<pre>{base.esc(p['text'])}</pre>")
        return {"draft": True, "channel": p["channel"]}

    token = config.env("SLACK_WRITE_TOKEN", required=True)
    body = {"channel": p["channel"], "text": p["text"]}
    if p.get("thread_ts"):
        body["thread_ts"] = p["thread_ts"]
    with base.http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        r = base.check(base.request(http, "POST", f"{API}/chat.postMessage", json=body), "Slack")
    data = r.json()
    if not data.get("ok"):
        raise base.ExecError(f"Slack: {data.get('error', 'unknown_error')}")
    return {"channel": data.get("channel"), "ts": data.get("ts")}

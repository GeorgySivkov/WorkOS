"""Исполнитель email_reply (шаг 5.3): только ЧЕРНОВИК в Gmail, никогда не отправка.

И «Отправить», и «Оставить черновиком» создают черновик: двойная защита,
апрув в боте плюс рука George в почте. Эндпоинтов отправки здесь нет вообще.

.env.executor: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_WRITE_REFRESH_TOKEN
(скоупы calendar.events + gmail.compose). Другой ящик: GMAIL_WRITE_TOKENS =
JSON {"personal": "GOOGLE_WRITE_REFRESH_TOKEN", "infatica": "GMAIL_INFATICA_WRITE_REFRESH_TOKEN"}.
"""
from __future__ import annotations

import base64
import json
from email import policy
from email.message import EmailMessage

from core import config, google
from executors import base

API = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_TOKENS = {"personal": "GOOGLE_WRITE_REFRESH_TOKEN"}


def token_env(account: str) -> str:
    """Имя переменной с refresh-токеном для ящика из payload.account."""
    mapping = dict(DEFAULT_TOKENS)
    raw = config.env("GMAIL_WRITE_TOKENS")
    if raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as e:
            raise base.ExecError(f"GMAIL_WRITE_TOKENS: не JSON ({e})") from None
        if not isinstance(extra, dict):
            raise base.ExecError("GMAIL_WRITE_TOKENS: ожидался объект {ящик: имя переменной}")
        mapping.update({str(k): str(v) for k, v in extra.items()})
    key = account.strip()
    if key.startswith("gmail:"):
        key = key[len("gmail:"):]
    if key not in mapping:
        raise base.ExecError(f"нет токена записи для ящика {account}: добавь его в GMAIL_WRITE_TOKENS")
    return mapping[key]


def _msgid(s: str) -> str:
    s = s.strip()
    return s if s.startswith("<") else f"<{s}>"


def build_mime(p: dict) -> bytes:
    """RFC 2822 в UTF-8: кириллица в заголовках как encoded-words, тело quoted-printable."""
    msg = EmailMessage(policy=policy.SMTP)
    msg["To"] = ", ".join(p["to"])
    if p["cc"]:
        msg["Cc"] = ", ".join(p["cc"])
    msg["Subject"] = p["subject"]
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = _msgid(p["in_reply_to"])
    refs = p.get("references")
    if isinstance(refs, list):
        refs = " ".join(_msgid(r) for r in refs if r.strip())
    elif refs:
        refs = " ".join(_msgid(r) for r in refs.split())
    if not refs and p.get("in_reply_to"):
        refs = _msgid(p["in_reply_to"])
    if refs:
        msg["References"] = refs
    msg.set_content(p["body"], charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()


def manual_accounts() -> set[str]:
    """Ящики без API черновиков (Альфа: Exchange по ActiveSync). Для них черновик
    приходит текстом в «Решения», отправляет George сам из своего клиента."""
    return {x.strip() for x in (config.env("MANUAL_DRAFT_ACCOUNTS", "alfa,eas:alfa") or "").split(",") if x.strip()}


def execute(action: dict, notify) -> dict:
    p = action["payload"]
    if p["account"].strip() in manual_accounts():
        head = f"Кому: {base.esc(', '.join(p['to']))}"
        if p["cc"]:
            head += f"\nКопия: {base.esc(', '.join(p['cc']))}"
        notify(f"Черновик ответа ({base.esc(p['account'])}), скопируй в почту:\n{head}\n"
               f"Тема: {base.esc(p['subject'])}\n<pre>{base.esc(p['body'])}</pre>")
        return {"manual": True, "account": p["account"]}
    env_name = token_env(p["account"])
    raw = base64.urlsafe_b64encode(build_mime(p)).decode().rstrip("=")
    message: dict = {"raw": raw}
    if p.get("thread_id"):
        message["threadId"] = p["thread_id"]
    with google.client(env_name, transport=base.transport()) as http:
        r = base.check(base.request(http, "POST", f"{API}/drafts", json={"message": message}),
                       "Gmail: создать черновик")
    d = r.json()
    m = d.get("message") or {}
    return {"draft_id": d.get("id"), "message_id": m.get("id"), "thread_id": m.get("threadId"),
            "account": p["account"]}

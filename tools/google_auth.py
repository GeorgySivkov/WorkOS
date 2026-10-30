#!/usr/bin/env python3
"""Получить refresh-токен Google один раз, на Маке (нужен браузер).

  python3 tools/google_auth.py --client client_secret.json --scopes read-gmail
  python3 tools/google_auth.py --client client_secret.json --scopes write

Наборы прав:
  read-gmail  gmail.readonly                      -> .env.reader  (GMAIL_PERSONAL_REFRESH_TOKEN)
  write       calendar.events + gmail.compose     -> .env.executor (GOOGLE_WRITE_REFRESH_TOKEN)

Скрипт поднимает локальный порт, открывает браузер, ловит код и печатает
refresh_token. Ничего не сохраняет на диск. Без зависимостей, кроме httpx.
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import urllib.parse
import webbrowser

import httpx

SCOPES = {
    "read-gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    "write": ["https://www.googleapis.com/auth/calendar.events",
              "https://www.googleapis.com/auth/gmail.compose"],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, help="client_secret_*.json из Google Cloud (Desktop app)")
    ap.add_argument("--scopes", required=True, choices=SCOPES)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    cfg = json.load(open(args.client))
    cfg = cfg.get("installed") or cfg.get("web") or cfg
    redirect = f"http://127.0.0.1:{args.port}/"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES[args.scopes]), "access_type": "offline", "prompt": "consent",
        "state": state,
    })
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово, можно закрыть вкладку.".encode())

        def log_message(self, *a):
            pass

    print("Открываю браузер. Если не открылся, перейди по ссылке:\n" + url)
    webbrowser.open(url)
    srv = http.server.HTTPServer(("127.0.0.1", args.port), H)
    while "code" not in got and "error" not in got:
        srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"не получилось: {got}")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": got["code"], "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    r.raise_for_status()
    tok = r.json()
    print("\nrefresh_token (положи в .env, никому не показывай):\n" + tok["refresh_token"])


if __name__ == "__main__":
    main()

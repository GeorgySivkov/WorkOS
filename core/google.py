"""Google OAuth: refresh-токен -> access-токен, общий клиент для Gmail и Calendar.

Один OAuth-клиент (Desktop app) в Google Cloud, в режиме «In production»
(в режиме Testing refresh-токен живёт семь дней). Токены разные по правам:
  .env.reader:   GMAIL_PERSONAL_REFRESH_TOKEN (gmail.readonly) и т.п.
  .env.executor: GOOGLE_WRITE_REFRESH_TOKEN (calendar.events + gmail.compose)
Получить токен: python tools/google_auth.py --scopes ... на Маке.
"""
from __future__ import annotations

import time

import httpx

from core import config

TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleAuth:
    def __init__(self, refresh_env: str, http: httpx.Client | None = None):
        self.refresh_env = refresh_env
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={
            "client_id": config.env("GOOGLE_CLIENT_ID", required=True),
            "client_secret": config.env("GOOGLE_CLIENT_SECRET", required=True),
            "refresh_token": config.env(self.refresh_env, required=True),
            "grant_type": "refresh_token",
        })
        if r.status_code != 200:
            raise RuntimeError(f"Google token refresh ({self.refresh_env}): {r.status_code} {r.text[:300]}")
        body = r.json()
        self._token, self._exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token


def client(refresh_env: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """httpx-клиент, который сам подставляет свежий Bearer."""
    auth = GoogleAuth(refresh_env, httpx.Client(timeout=30, transport=transport) if transport else None)

    def add_auth(request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {auth.token()}"

    return httpx.Client(timeout=60, transport=transport, event_hooks={"request": [add_auth]})

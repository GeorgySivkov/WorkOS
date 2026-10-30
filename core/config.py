"""Конфигурация WorkOS.

Каждый процесс живёт под своей ролью (WORKOS_ROLE) и читает только свой файл
окружения: .env.reader, .env.bot, .env.executor, .env.app или .env.admin.
Секретов в коде нет, всё остальное (ритм дня, роутинг моделей) лежит здесь.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ROLE = os.environ.get("WORKOS_ROLE", "reader")
ENV_FILE = Path(os.environ.get("WORKOS_ENV_FILE", ROOT / f".env.{ROLE}"))
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"{name} не задан в {ENV_FILE.name}")
    return val


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


# ---------- время и ритм дня (шаг 1.4) ----------
TZ = ZoneInfo(env("WORKOS_TZ", "Europe/Madrid"))
WAKE = env("WORKOS_WAKE", "08:00")            # подъём
SLEEP = env("WORKOS_SLEEP", "01:00")          # отбой (следующих суток)
QUIET_START = env("WORKOS_QUIET_START", "01:00")
QUIET_END = env("WORKOS_QUIET_END", "08:00")
LOGICAL_DAY_END_HOUR = env_int("WORKOS_DAY_END_HOUR", 4)   # день кончается в 4:00, а не в полночь
TRAINING_WINDOW = (env("WORKOS_TRAIN_FROM", "21:00"), env("WORKOS_TRAIN_TO", "00:00"))
TRAINING_DEFAULT = env("WORKOS_TRAIN_DEFAULT", "21:30")
TRAINING_MINUTES = env_int("WORKOS_TRAIN_MINUTES", 90)
TRAINING_WEEKDAYS = [int(x) for x in env("WORKOS_TRAIN_WEEKDAYS", "0,1,2,3,4,5,6").split(",") if x != ""]

# ---------- план дня (шаг 2.9) ----------
SIZE_MINUTES = {"S": 30, "M": 60, "L": 120}
BUFFER_MINUTES = env_int("WORKOS_BUFFER_MIN", 10)
FILL_RATIO = float(env("WORKOS_FILL_RATIO", "0.7"))

# ---------- персонаж (П.1) ----------
SIZE_XP = {"S": 10, "M": 25, "L": 60}
XP_DAILY_CAP = 100
RUST_DAYS = 14

# ---------- роутинг моделей (шаг 4.2): задача -> модель ----------
# Дёшево: разбор, классификация, дедуп. Фронтир: черновики и синтез.
MODELS = {
    "capture": "haiku",
    "classify_cell": "haiku",
    "triage": "haiku",
    "people": "haiku",
    "brief": "haiku",
    "meeting_card": "haiku",
    "weekly": "haiku",
    "rewrite": "sonnet",
    "extract": "sonnet",
    "planner": "sonnet",
    "drafter": "sonnet",
    "ask": "sonnet",
    "review": "sonnet",
}


def model_for(task: str) -> str:
    return env(f"WORKOS_MODEL_{task.upper()}", MODELS.get(task, "haiku"))


# ---------- темы бота ----------
TOPICS = {
    "inbox": "Входящие",
    "decisions": "Решения",
    "plan": "План",
    "system": "Система",
    "ask": "Спросить",
}
# домены подключаются к терминалу темой (трек доменов): ключ = префикс кнопок
DOMAINS: dict[str, str] = {}
if env("WORKOS_DOMAIN_FOOD") == "1":
    DOMAINS["food"] = "Еда"
if env("WORKOS_DOMAIN_JOBS") == "1":
    DOMAINS["jobs"] = "Вакансии"

# ---------- автономия (шаг 6.2) ----------
AUTONOMY_PROMOTE_RATE = 0.85
AUTONOMY_PROMOTE_MIN_N = 20
AUTONOMY_DEMOTE_RATE = 0.7
AUTONOMY_SAMPLE_EVERY = 5
# типы с внешней видимостью не продвигаются никогда
NEVER_AUTO = {"email_reply", "email_send", "slack_post", "trello_comment"}

"""Очередь исполнителя (контракт шага 5.1) и общие куски для всех исполнителей.

Процесс workos-exec, роль базы workos_exec. Модель не вызывается никогда:
ни один модуль executors не импортирует core.llm (это проверяет тест).

На входе только строка work.actions со статусом approved или drafted, у которой
approved_hash равен хешу ТЕКУЩЕГО payload, посчитанному в SQL. Payload проверяется
по строгой схеме своего типа; лишнее поле или не тот тип = failed с понятной ошибкой
и сообщение в «Решения». plan_batch и task_add исполняет бот, сюда они не попадают.

Внешний вызов идёт вне транзакции: сначала строка помечается executed_at и это
коммитится, потом вызов, потом итог (см. claim / execute_action / sweep_interrupted).
Так убитый посреди действия процесс не повторит его после перезапуска.
"""
from __future__ import annotations

import html
import importlib
import logging
import random
import signal
import threading
import time
from datetime import datetime
from typing import Any, Callable

import httpx

from core import db
from core import timeutil as tu

log = logging.getLogger("executors")

# ---------- строгие схемы payload (docs/CODE.md, таблица типов) ----------
# Вид поля: "str" любая строка · "text" непустая строка · "line" непустая строка без
# переводов строки (уходит в заголовки и id) · "email" line с @ · "iso" дата или
# дата-время ISO 8601 · [вид] список · {ключ: (вид, обязательное)} вложенный объект ·
# ("any", вид1, вид2) одно из.
Spec = Any

SCHEMAS: dict[str, dict[str, tuple[Spec, bool]]] = {
    "trello_card": {
        "list_id": ("line", True),
        "name": ("line", True),
        "desc": ("str", True),
        "labels": (["line"], True),
        "checklist": ({"name": ("line", True), "items": (["line"], True)}, False),
        "due": ("iso", False),
    },
    "email_reply": {
        "account": ("line", True),
        "to": (["email"], True),
        "cc": (["email"], True),
        "subject": ("line", True),
        "body": ("text", True),
        "thread_id": ("line", False),
        "in_reply_to": ("line", False),
        "references": (("any", "line", ["line"]), False),
    },
    "slack_post": {
        "channel": ("line", True),
        "text": ("text", True),
        "thread_ts": ("line", False),
    },
}

# какой модуль исполняет тип: executors.<модуль>.execute(action, notify) -> dict
HANDLERS = {"trello_card": "trello", "email_reply": "gmail", "slack_post": "slack"}

TYPE_RU = {
    "trello_card": "карточка Trello",
    "email_reply": "черновик письма",
    "slack_post": "сообщение в Slack",
}

_KIND_RU = {
    "str": "строка", "text": "непустая строка", "line": "строка без переводов строки",
    "email": "адрес почты", "iso": "дата ISO 8601",
}


class SchemaError(ValueError):
    pass


class ExecError(RuntimeError):
    """Внешний сервис ответил ошибкой: текст идёт George как есть."""


def _describe(spec: Spec) -> str:
    if isinstance(spec, str):
        return _KIND_RU[spec]
    if isinstance(spec, list):
        return f"список ({_describe(spec[0])})"
    if isinstance(spec, dict):
        return "объект"
    return " или ".join(_describe(s) for s in spec[1:])


def parse_iso(s: str) -> datetime:
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _check(spec: Spec, value: Any, path: str) -> None:
    if isinstance(spec, tuple):                       # ("any", ...)
        for alt in spec[1:]:
            try:
                _check(alt, value, path)
                return
            except SchemaError:
                pass
        raise SchemaError(f"поле {path}: ожидалась {_describe(spec)}")
    if isinstance(spec, list):
        if not isinstance(value, list):
            raise SchemaError(f"поле {path}: ожидался {_describe(spec)}")
        for i, item in enumerate(value):
            _check(spec[0], item, f"{path}[{i}]")
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise SchemaError(f"поле {path}: ожидался объект")
        _check_object(spec, value, path + ".")
        return
    if not isinstance(value, str):
        raise SchemaError(f"поле {path}: ожидалась {_KIND_RU[spec]}, пришло {type(value).__name__}")
    if spec == "str":
        return
    if not value.strip():
        raise SchemaError(f"поле {path}: пустая строка")
    if spec in ("line", "email", "iso") and ("\r" in value or "\n" in value):
        raise SchemaError(f"поле {path}: перевод строки недопустим")
    if spec == "email" and "@" not in value:
        raise SchemaError(f"поле {path}: не похоже на адрес почты")
    if spec == "iso":
        try:
            parse_iso(value)
        except ValueError:
            raise SchemaError(f"поле {path}: не дата ISO 8601") from None


def _check_object(schema: dict, obj: dict, prefix: str = "") -> None:
    extra = sorted(set(obj) - set(schema))
    if extra:
        raise SchemaError(f"лишнее поле {prefix}{extra[0]}")
    for key, (spec, required) in schema.items():
        value = obj.get(key)
        if value is None:                             # null у необязательного = поля нет
            if required:
                raise SchemaError(f"нет обязательного поля {prefix}{key}")
            continue
        _check(spec, value, prefix + key)


def validate(action_type: str, payload: Any) -> dict:
    """Строгая проверка payload по схеме типа. Возвращает payload или бросает SchemaError."""
    schema = SCHEMAS.get(action_type)
    if schema is None:
        raise SchemaError(f"неизвестный тип действия {action_type}")
    if not isinstance(payload, dict):
        raise SchemaError("payload должен быть объектом")
    _check_object(schema, payload)
    if action_type == "email_reply" and not payload["to"]:
        raise SchemaError("поле to: нужен хотя бы один адрес")
    return payload


# ---------- HTTP наружу: общий транспорт для тестов и бэкофф на лимитах ----------
_transport: httpx.BaseTransport | None = None
sleep: Callable[[float], None] = time.sleep          # тесты подменяют

RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
IDEMPOTENT = {"GET", "PUT", "PATCH", "DELETE", "HEAD"}


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Тесты подсовывают httpx.MockTransport: все клиенты исполнителей идут через него."""
    global _transport
    _transport = transport


def transport() -> httpx.BaseTransport | None:
    return _transport


def http_client(**kw) -> httpx.Client:
    return httpx.Client(timeout=60, transport=_transport, **kw)


def _rate_limited(r: httpx.Response) -> bool:
    if r.status_code == 429:
        return True
    if r.status_code == 403:
        return any(reason in r.text for reason in RATE_REASONS)
    return False


def request(client: httpx.Client, method: str, url: str, *, tries: int = 5, **kw) -> httpx.Response:
    """Запрос с повтором на 429, 403-rateLimit, 503 и обрывах соединения.

    POST повторяется только там, где сервер точно ничего не создал (лимит, 503,
    соединение не установлено), остальные методы ещё и на 500/502/504.
    """
    method = method.upper()
    for attempt in range(tries):
        last = attempt == tries - 1
        try:
            r = client.request(method, url, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if last:
                raise
            sleep(_backoff(attempt, None))
            continue
        except httpx.TransportError:
            if last or method not in IDEMPOTENT:
                raise
            sleep(_backoff(attempt, None))
            continue
        retry = _rate_limited(r) or r.status_code == 503 or (
            method in IDEMPOTENT and r.status_code in (500, 502, 504))
        if not retry or last:
            return r
        sleep(_backoff(attempt, r.headers.get("Retry-After")))
    raise AssertionError("unreachable")


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(2 ** attempt, 32) + random.random()


def fail_text(r: httpx.Response, what: str) -> str:
    body = r.text.strip().replace("\n", " ")
    return f"{what}: HTTP {r.status_code} {body[:300]}"


def check(r: httpx.Response, what: str, ok: tuple[int, ...] = ()) -> httpx.Response:
    if r.is_success or r.status_code in ok:
        return r
    raise ExecError(fail_text(r, what))


# ---------- очередь ----------
QUEUE_SQL = """
SELECT id, type, payload, status, context_id FROM work.actions
WHERE status IN ('approved','drafted') AND executed_at IS NULL
  AND approved_hash = encode(sha256(convert_to(payload::text, 'UTF8')), 'hex')
  AND type = ANY(%(types)s)
ORDER BY approved_at
LIMIT 1
FOR UPDATE SKIP LOCKED
"""


def claim_one(conn, types=None) -> dict | None:
    """Берёт одну строку очереди под блокировку (звать внутри транзакции)."""
    types = [t for t in (types or SCHEMAS) if t in SCHEMAS]
    if not types:
        return None
    return conn.execute(QUEUE_SQL, {"types": types}).fetchone()


def outbox(conn, topic: str, text: str, ref_action: int | None = None) -> None:
    # без RETURNING: SELECT на outbox у исполнителя нет
    conn.execute("INSERT INTO work.outbox (topic, text, ref_action) VALUES (%s, %s, %s)",
                 (topic, text, ref_action))


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def _handler(action_type: str):
    mod = importlib.import_module(f"executors.{HANDLERS[action_type]}")
    return mod.execute


def _write_failed(conn, action: dict, err: str) -> dict:
    conn.execute("UPDATE work.actions SET status = 'failed', error = %s WHERE id = %s",
                 (err[:2000], action["id"]))
    what = TYPE_RU.get(action["type"], action["type"])
    outbox(conn, "decisions", f"Не получилось: {esc(what)} (#{action['id']}). {esc(err[:600])}", action["id"])
    return {"id": action["id"], "status": "failed", "error": err}


# ---------- исполнение в два шага ----------
# Шаг 1 (короткая транзакция): взять строку, проверить схему, записать executed_at и
# закоммитить. С этого момента очередь (executed_at IS NULL) строку больше не отдаёт.
# Шаг 2: внешний вызов БЕЗ открытой транзакции. Шаг 3 (вторая транзакция): статус и
# результат. Если процесс убили между шагами 1 и 3, строка остаётся approved/drafted
# с executed_at и без result: повторять её нельзя (карточка или пост могли уже уйти),
# поэтому sweep_interrupted() один раз сообщает George в «Систему» и помечает error.
# Статусы меняются только approved|drafted -> executed|drafted|failed.

def claim(types=None) -> tuple[dict | None, dict | None]:
    """Шаг 1. Возвращает (действие, None) для исполнения или (действие, итог), если оно
    уже закрыто без внешнего вызова (схема не сошлась). (None, None) = очередь пуста."""
    conn = db.connect()
    try:
        with conn.transaction():
            action = claim_one(conn, types)
            if action is None:
                return None, None
            try:
                validate(action["type"], action["payload"])
            except SchemaError as e:
                return action, _write_failed(conn, action, str(e))
            conn.execute("UPDATE work.actions SET executed_at = now() WHERE id = %s AND executed_at IS NULL",
                         (action["id"],))
        return action, None
    finally:
        conn.close()


def execute_action(action: dict) -> dict:
    """Шаги 2 и 3: внешний вызов вне транзакции, затем итог отдельной транзакцией."""
    notes: list[str] = []
    err = None
    result: dict = {}
    try:
        result = _handler(action["type"])(action, notes.append) or {}
    except Exception as e:  # noqa: BLE001  любая ошибка = failed и сообщение George
        err = str(e) or type(e).__name__
        if not isinstance(e, (SchemaError, ExecError)):
            log.exception("action %s (%s) упало", action["id"], action["type"])
    conn = db.connect()
    try:
        with conn.transaction():
            if err is not None:
                return _write_failed(conn, action, err)
            status = "executed" if action["status"] == "approved" else "drafted"
            cur = conn.execute("UPDATE work.actions SET status = %s, executed_at = now(), error = NULL, "
                               "result = %s WHERE id = %s AND status IN ('approved','drafted')",
                               (status, db.jb(result), action["id"]))
            if cur.rowcount != 1:
                log.warning("action %s: статус успели поменять, итог %s не записан", action["id"], status)
            for text in notes:
                outbox(conn, "decisions", text, action["id"])
            return {"id": action["id"], "status": status, "result": result}
    finally:
        conn.close()


def process_one(types=None) -> dict | None:
    """Одна строка очереди: взять (шаг 1), исполнить и записать итог (шаги 2-3)."""
    action, done = claim(types)
    if action is None:
        return None
    if done is not None:
        return done
    return execute_action(action)


# ---------- прерванные действия ----------
INTERRUPTED_AFTER_MIN = 15          # дольше любого внешнего вызова с повторами
INTERRUPTED_ERROR = "исполнитель прервался посреди действия: результат неизвестен, повтора не будет"
INTERRUPTED_SQL = """
SELECT id, type, status, executed_at FROM work.actions
WHERE status IN ('approved','drafted') AND executed_at IS NOT NULL
  AND executed_at < now() - make_interval(mins => %(mins)s)
  AND result IS NULL AND error IS NULL
ORDER BY executed_at
FOR UPDATE SKIP LOCKED
"""


def sweep_interrupted(minutes: int = INTERRUPTED_AFTER_MIN) -> int:
    """Строки, взятые в работу и брошенные (SIGKILL, падение базы на шаге 3): один раз
    сказать George в «Систему» и пометить error, чтобы не сказать второй раз."""
    conn = db.connect()
    try:
        with conn.transaction():
            rows = conn.execute(INTERRUPTED_SQL, {"mins": minutes}).fetchall()
            for r in rows:
                conn.execute("UPDATE work.actions SET error = %s WHERE id = %s", (INTERRUPTED_ERROR, r["id"]))
                what = TYPE_RU.get(r["type"], r["type"])
                when = tu.local(r["executed_at"]).strftime("%d.%m %H:%M")
                outbox(conn, "system",
                       f"Исполнитель прервался посреди действия #{r['id']} ({esc(what)}), начатого {when}. "
                       "Не знаю, ушло ли оно наружу: проверь руками. Повторять сам не буду, "
                       "чтобы не задвоить.", r["id"])
        return len(rows)
    finally:
        conn.close()


# ---------- цикл с SIGTERM ----------
_stop = threading.Event()


def _on_signal(signum, frame):  # noqa: ARG001
    log.info("сигнал %s: дорабатываю текущее действие и выхожу", signum)
    _stop.set()


def install_signal_handlers() -> None:
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)


def stopping() -> bool:
    return _stop.is_set()


def wait(seconds: float) -> bool:
    """Пауза, которую прерывает SIGTERM. True = пора выходить."""
    return _stop.wait(seconds)


def _sweep_safely() -> None:
    try:
        n = sweep_interrupted()
        if n:
            log.warning("прерванных действий: %s, George предупреждён", n)
    except Exception:  # noqa: BLE001  проверка вспомогательная, очередь важнее
        log.exception("проверка прерванных действий упала")


def run_queue(once: bool = False, types=None, sleep: float = 10) -> dict:
    """Разбирает очередь. once=True: до пустой очереди и выход (для таймера и тестов)."""
    counts = {"executed": 0, "drafted": 0, "failed": 0}
    if not once:
        install_signal_handlers()
    _sweep_safely()
    last_sweep = time.monotonic()
    while not _stop.is_set():
        if time.monotonic() - last_sweep >= 60:
            _sweep_safely()
            last_sweep = time.monotonic()
        try:
            res = process_one(types)
        except Exception:  # noqa: BLE001  база недоступна и т.п.: подождать и снова
            log.exception("очередь исполнителя: ошибка")
            if once:
                raise
            if wait(sleep):
                break
            continue
        if res is not None:
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            log.info("action %s -> %s", res["id"], res["status"])
            continue
        if once or wait(sleep):
            break
    return counts

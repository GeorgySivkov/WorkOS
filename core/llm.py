"""Вызов модели через claude CLI в headless-режиме (подписка Max, как у JobBot).

Правило 04 плана: каждый вызов пишется в public.usage_log с префиксом work_.
Модель выбирается по таблице задача -> модель из config.MODELS (шаг 4.2).
Исполнители этот модуль не импортируют никогда.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from core import config, db

DENY = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,MultiEdit"


class LLMError(RuntimeError):
    pass


# Тесты подменяют этот хук: fn(task, system, user) -> str
_fake = None


def set_fake(fn) -> None:
    global _fake
    _fake = fn


def _log_usage(task: str, cost, tokens) -> None:
    try:
        with db.connect(autocommit=True) as conn:
            conn.execute("INSERT INTO public.usage_log (command, cost_usd, tokens) VALUES (%s, %s, %s)",
                         (f"work_{task}", cost, tokens))
    except Exception:
        pass  # учёт расходов никогда не роняет пайплайн


# что из окружения нужно claude CLI; PGPASSWORD, токены Telegram и Google туда не идут
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "CLAUDE_CODE_OAUTH_TOKEN")


def child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_KEEP if os.environ.get(k)}
    env.update({k: v for k, v in os.environ.items() if k.startswith("CLAUDE_") and v})
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def complete(task: str, system: str, user: str, timeout: int = 240) -> str:
    if config.ROLE == "executor":
        raise LLMError("исполнитель не вызывает модель по контракту (шаг 5.1)")
    if _fake is not None:
        return _fake(task, system, user)
    model = config.model_for(task)
    # промпт (тела писем, расшифровки) идёт через stdin: в argv его видно в /proc/*/cmdline,
    # и там предел ~128 КБ на аргумент; системный промпт фиксированный, ему можно в argv
    cmd = [config.env("CLAUDE_BIN", "claude"), "-p",
           "--system-prompt", system + " Не используй инструменты и не пиши файлы, верни только ответ.",
           "--output-format", "json", "--max-turns", "2", "--model", model,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", DENY]
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout,
                              env=child_env())
    except FileNotFoundError as e:
        _log_usage(task, None, None)
        raise LLMError("claude CLI не найден: поставь Claude Code и сделай claude setup-token") from e
    except subprocess.TimeoutExpired as e:
        _log_usage(task, None, None)
        raise LLMError(f"модель не ответила за {timeout} с") from e
    if proc.returncode != 0:
        _log_usage(task, None, None)
        raise LLMError(f"claude -p exit {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _log_usage(task, None, None)
        return proc.stdout
    if not isinstance(payload, dict):
        _log_usage(task, None, None)
        return proc.stdout
    u = payload.get("usage") or {}
    toks = sum(int(u.get(k, 0) or 0) for k in ("input_tokens", "output_tokens",
                                                 "cache_read_input_tokens", "cache_creation_input_tokens"))
    _log_usage(task, payload.get("total_cost_usd"), toks)
    if payload.get("is_error"):
        raise LLMError(str(payload.get("result"))[:500])
    return payload.get("result", "") or ""


def extract_json(text: str):
    """Достаёт первый JSON-объект или массив из ответа модели."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        if i == -1:
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"в ответе модели нет JSON: {text[:200]}")


def complete_json(task: str, system: str, user: str, timeout: int = 240):
    return extract_json(complete(task, system + " Ответ строго JSON без пояснений.", user, timeout))

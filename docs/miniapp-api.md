# API мини-приложения (процесс workos-app)

Сервер: `python -m app.server`, слушает `127.0.0.1:${APP_PORT:-8787}`, наружу через Tailscale Funnel.
Статика: `GET /` -> `app/static/index.html`, `GET /static/<file>`.

Каждый `/api/*`, кроме `/api/health`, требует заголовок `Authorization: tma <initData>`
(сырой `Telegram.WebApp.initData`). Проверка в режиме третьих сторон: Ed25519-подпись
Telegram по публичному ключу, строка `"{bot_id}:WebAppData\n" + отсортированные key=value
без hash и signature`, `user.id == TELEGRAM_OWNER_ID`, `auth_date` не старше 24 часов.
Токена бота у процесса нет. Без подписи или с чужим id: `401 {"error": "unauthorized"}`.
Env `.env.app`: PG* роли workos_app, `TELEGRAM_BOT_ID`, `TELEGRAM_OWNER_ID`, `APP_PORT`,
`TELEGRAM_PUBLIC_KEY` (по умолчанию продовый ключ), `APP_DEV_TOKEN` (опционально: разрешает
`Authorization: dev <token>` для локального просмотра, на проде пусто).

Все даты — по Europe/Madrid, «сегодня» = логический день (кончается в 4:00).
Цвета контекстов: infatica #4FB6C2, alfa #F08FB0, consulting #E3C26B, personal #A3B0FF.

## Объект задачи
```json
{"id": 12, "title": "Отчёт по SDK", "context_id": "infatica", "context_title": "Инфатика",
 "color": "#4FB6C2", "size": "M", "xp": 25, "cell_id": "B3", "cell_title": "Карьера",
 "due": "2026-10-22", "week_of": null, "time": "11:00", "start": "ISO", "end": "ISO",
 "done": false, "overdue": false, "remind_at": "ISO|null"}
```

## Эндпоинты
- `GET /api/health` -> `{"ok": true}` без авторизации (его дёргает heartbeat).
- `GET /api/today` ->
```json
{"date": "2026-10-22", "date_label": "Четверг, 22 октября", "week": 43, "now": "ISO",
 "character": {"name": "Георгий", "level": 7, "xp": 1440, "level_floor": 1400, "next_level_at": 1600},
 "summary": {"done": 1, "total": 5, "xp_today": 10},
 "habits": [{"cell_id": "A1", "title": "Сон 7,5 ч", "dir": "Здоровье", "note": "из Garmin", "done": true, "auto": true,
             "via": "garmin|rating|auto|null", "streak": 12}],
 "rating": {"day": "2026-10-22", "score": 3, "note": "устал, но доволен", "month_avg": 2.4, "month_days": 12},
 "timeline": [
   {"type": "task", "...": "объект задачи"},
   {"type": "meeting", "title": "Daily Infatica", "context_id": "infatica", "color": "#4FB6C2",
    "time": "10:00", "start": "ISO", "end": "ISO", "meta": "10:00–10:30 · Инфатика"},
   {"type": "gap", "time": "12:00", "title": "Привал · свободно 1 ч"},
   {"type": "training", "time": "21:30", "title": "Тренировка", "meta": "21:30–23:00 · Здоровье"},
   {"type": "now", "time": "11:20"}],
 "unscheduled": ["объекты задач на сегодня и просроченные без времени"]}
```
- `GET /api/list?view=week|later` -> `{"groups": [{"title": "Пт, 23 октября", "date": "2026-10-23", "items": [задачи]}]}`.
  week: дни от завтра до воскресенья + группа «На неделе, без дня» (week_of = этот понедельник).
  later: задачи после этой недели по датам + группа «Без даты».
- `POST /api/tasks` `{"text": "позвонить в банк в пт в 10", "bucket": "today|week|later"?}` -> задача.
  Дата и время из текста через `core.ruparse`; bucket используется, если в тексте даты нет.
  source='app', клетку и размер позже ставит читатель.
- `POST /api/tasks/{id}/done` -> `{"task": {...}, "xp": 25, "level_up": false, "character": {...}}`
  (done_at = now, `rpg.award_task`). `level_up` true только при первом достижении уровня (`rpg.app_level_up`).
- `POST /api/tasks/{id}/undone` -> `{"task": {...}, "character": {...}}` (`rpg.revoke_task`).
- `POST /api/tasks/{id}/tomorrow` -> задача (due = завтра, время снято, напоминание сдвинуто на сутки).
- `POST /api/tasks/{id}/drop` -> `{"ok": true}` (dropped_at = now).
- `POST /api/habits/{cell_id}/toggle` -> `{"done": true, "xp": 10, "character": {...}}`. Если привычка уже засчитана
  не вручную (задача на этой клетке, Garmin), ответ `{"done": true, "xp": 0, "locked": true}` и ничего не пишется.
  При достигнутом потолке дня отметка сохраняется с 0 опыта, серия не рвётся.
- `POST /api/rating` `{"score": 3}` -> `{"rating": {...как в /api/today}, "xp": 10, "milestone": null,
  "week_avg": 2.9, "level_up": false, "character": {...}}`. Ползунок «Оценка дня» на «Сегодня».
  Пишет `rpg.log_rating` (та же функция, что `/day` в боте): оценка за логический день по часам базы,
  повторная оценка того же дня перезаписывает число без нового опыта, заметка из `/day` сохраняется.
  score: целое от -10 до +10, note: строка до 500 символов или нет. Иначе 400.
  Привычка с `via: "rating"` руками не переключается (`/toggle` отвечает 400 auto), её отмечает оценка.
- `POST /api/replan` -> то же, что `/api/today`, после пересборки несделанного с текущей минуты.
- `GET /api/character` ->
```json
{"name": "Георгий", "level": 7, "xp": 1440, "level_floor": 1400, "next_level_at": 1600,
 "season": "Сезон 1", "main_quest": "текст центра сетки или null",
 "attributes": [{"id": "A", "title": "Здоровье", "level": 6, "xp": 2100, "pct": 62,
                 "rusty": false, "idle_days": 2}],
 "next_reward": {"level": 8, "title": "..."} , "achievements": [{"code": "...", "title": "...", "earned_at": "ISO"}],
 "streaks": [{"cell_id": "A1", "title": "Сон 7,5 ч", "days": 12}]}
```
- `GET /api/grid` -> `{"center": {"id": "C", "title": "..."}, "dirs": [{"id": "A", "title": "Здоровье",
  "level": 6, "filled": 7, "rusty": false, "cells": [{"id": "A1", "title": "Сон 7,5 ч", "kind": "habit",
  "stat": "серия 12"}]}]}` (8 направлений по порядку A..H).

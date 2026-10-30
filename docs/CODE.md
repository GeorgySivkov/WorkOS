# WorkOS: устройство кода и контракты модулей

Python 3.10+ (на дроплете 3.10/3.12, тесты гоняются на 3.13). Без фреймворков там,
где можно обойтись: httpx, psycopg 3, stdlib. Комментарии и тексты для George — по-русски,
без длинных тире в текстах, которые видит George (используй двоеточие, запятую или «·»).

## Процессы и права (правило 05 плана)

| Процесс (systemd)   | ОС-пользователь | Роль БД         | .env           | Модель | Внешние токены |
|---------------------|-----------------|-----------------|----------------|--------|----------------|
| workos-bot          | workos-bot      | workos_bot      | .env.bot       | да     | только Telegram |
| читатели (таймеры)  | workos-reader   | workos_reader   | .env.reader    | да     | read-only скоупы |
| workos-exec         | workos-exec     | workos_exec     | .env.executor  | НЕТ    | запись наружу |
| workos-app          | workos-app      | workos_app      | .env.app       | нет    | нет |
| миграции            | root            | doadmin         | .env.admin     | нет    | нет |

Что каждая роль может в базе — `migrations/00*.sql`. Коротко:
- reader: SELECT почти всего; INSERT/UPDATE events, people, identities, decisions, commitments,
  sources, identity_candidates; INSERT actions (только предложить), outbox, rpg.xp, notices;
  UPDATE tasks(cell_id, size, planned_*); autonomy. НЕ может approve.
- bot: SELECT contexts, people, identities, person_contexts, events, actions, identity_candidates,
  outbox, sources, decisions, commitments, tasks, rpg.*, calendar_feed, autonomy, outcomes;
  INSERT events, actions, outcomes, identities, outbox, tasks, bot_topics;
  UPDATE events(meta, context_id), actions(status, approved_at, approval_nonce, approved_hash,
  decided_at, result), tasks(reminded_at, dropped_at, due, week_of, remind_at, cell_id, size,
  context_id, title, planned_*), outbox(sent_at, tg_message_id), identity_candidates(status),
  autonomy(mode, locked, changed_at, reason).
- exec: SELECT actions, contexts, tasks, calendar_feed; UPDATE actions(status, executed_at, error,
  result), tasks(planned_*, block_synced_at); INSERT outbox. НЕ видит work.events. НЕ зовёт модель.
- app: SELECT contexts, tasks, calendar_feed, rpg.cells/attributes/character/xp/rewards/achievements;
  INSERT tasks; UPDATE tasks(title, due, week_of, remind_at, reminded_at, done_at, dropped_at, size,
  context_id, planned_*); EXECUTE rpg.award_task, revoke_task, log_manual, revoke_manual.

Код процесса не должен пытаться делать то, на что у роли нет прав: тест `as_role(...)` это ловит.

## Ядро (`core/`)

- `core.config` — `ROLE`, `env(name, default, required)`, `TZ` (Europe/Madrid), ритм дня
  (`WAKE`, `SLEEP`, `QUIET_*`, `LOGICAL_DAY_END_HOUR=4`, `TRAINING_*`), `SIZE_MINUTES`
  {S:30,M:60,L:120}, `SIZE_XP` {S:10,M:25,L:60}, `BUFFER_MINUTES=10`, `FILL_RATIO=0.7`,
  `MODELS` + `model_for(task)`, `TOPICS` {inbox, decisions, plan, system, ask}, `DOMAINS`,
  `NEVER_AUTO`, пороги автономии.
- `core.db` — `connect(autocommit=False)` (dict_row, PG* из env), `tx()` контекст-менеджер,
  `jb(obj)` -> Jsonb, `payload_hash_sql(col)` -> SQL-выражение sha256(payload::text).
- `core.timeutil` (`tu`) — `now()` UTC, `local(dt)`, `logical_date(dt)`, `logical_today()`,
  `week_monday(d)`, `at_local(date, time)` -> UTC, `day_bounds(d)`, `waking_window(d)`,
  `is_quiet(dt)`, `next_quiet_end(dt)`, `ru_date(d)`, `hm(dt)`, `RU_WEEKDAYS`, `RU_MONTHS_GEN`.
- `core.ruparse.parse(text, now=None) -> Parsed(title, due, week_of, at, remind_at, explicit_later, bucket)`.
- `core.llm` — `complete(task, system, user) -> str`, `complete_json(...)`, `extract_json(text)`,
  `set_fake(fn)` для тестов (fn(task, system, user) -> str). Пишет usage_log `work_<task>`.
  В роли executor бросает исключение.
- `core.tg` — `Bot(token)`: `.call(method, **params)`, `.send(chat_id, text, thread_id, buttons, reply_to)`,
  `.edit(chat_id, message_id, text|None, buttons)`, `.answer(callback_id, text)`, `.download(file_id)`,
  `.bot_id`; `btn(text, data)` (data ≤ 64 байт), `esc(s)` для HTML.
- `core.google` — `GoogleAuth(refresh_env).token()`, `client(refresh_env, transport=None)` -> httpx.Client
  с Bearer. Секреты: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `<NAME>_REFRESH_TOKEN`.

## Таблицы, которые важно понимать

- `work.events` — всё входящее; уникально `(source_id, external_id)`. Календарные события:
  `kind='calendar'`, `occurred_at` = начало, `meta`: `ends_at` (ISO), `uid`, `recurrence_id`
  (ISO начала вхождения или ''), `all_day`, `location`, `status` (confirmed|cancelled), `busy`,
  `updated`, `attendees` [{email, name}]. Встречи с записью: `kind='meeting'` (Plaud),
  `meta.participant_ids` [person_id], `meta.calendar_event_id`.
- `work.calendar_feed` — вид поверх календарных событий без тел (см. 005).
- `work.outbox(topic, text, buttons jsonb, ref_action)` — всё, что система хочет сказать.
  `text` в HTML-разметке Telegram, `buttons` = список рядов `[[{text, callback_data}], ...]`.
  Отправляет только бот; в тихие часы (1:00-8:00) ждёт утра.
- `work.actions(type, payload, status, autonomy, approved_hash, replaces, result)`.
- `work.tasks` — дела; `planned_start/end`, `planned_by` auto|manual, `block_synced_at`.
- `rpg.*` — сетка, опыт, виды `rpg.attributes`, `rpg.character`.

## Типы действий и строгие схемы payload

Предлагает читатель (INSERT в actions со status='proposed'), одобряет бот (кнопка с user id George
+ approved_hash = sha256(payload::text), посчитанный SQL-выражением в момент показа карточки).

| type          | Кто исполняет | payload |
|---------------|---------------|---------|
| plan_batch    | бот (внутреннее) | `{for_date: 'YYYY-MM-DD', items: [{title, size, context_id?, cell_id?, start?, end?}], week_items: [{title, size?, context_id?}]}` |
| task_add      | бот (внутреннее) | `{title, due?: 'YYYY-MM-DD', time?: 'HH:MM', size: 'S'|'M'|'L', context_id?, cell_id?, commitment_id?}` |
| trello_card   | executors/trello | `{list_id, name, desc, labels: [str], checklist?: {name, items: [str]}, due?: ISO}` |
| email_reply   | executors/gmail (только черновик) | `{account, to: [str], cc: [str], subject, body, thread_id?, in_reply_to?, references?}` |
| slack_post    | executors/slack | `{channel, text, thread_ts?}` |

Статусы: proposed -> approved -> executed|failed; proposed -> drafted (оставить черновиком:
исполнитель создаёт объект и не отправляет) | rejected | edited (заменено новой строкой с
`replaces`) | expired. Исходы пишет бот в `work.outcomes` (accepted, drafted, edited, rejected).

Очередь исполнителя (контракт шага 5.1, не нарушать):
```sql
SELECT id, type, payload, status FROM work.actions
WHERE status IN ('approved','drafted') AND executed_at IS NULL
  AND approved_hash = encode(sha256(convert_to(payload::text, 'UTF8')), 'hex')
ORDER BY approved_at FOR UPDATE SKIP LOCKED;
```

## Префиксы callback_data бота

`cap:` захват · `pb:` карточка плана · `act:` карточка действия · `idc:` кандидат в людях ·
`ask:` уточнение в «Спросить» · `rb:` откат автономного действия · `<домен>:` кнопки домена.

## Коллекторы

`collectors.base`: `Event(external_id, kind, occurred_at, title, body, meta, actor_raw, thread_key,
context_id)`, класс `Collector(source_row)` с `collect() -> Iterable[Event]`, `self.state` (dict,
сериализуется в sources.watermark), `watermark()`, `finalize(conn, seen)`, `since(default_days)`.
`run_source(source_id)` пишет события идемпотентно и сдвигает метку. Реестр видов —
`collectors.base.REGISTRY`. Настройки источника берутся из env (секреты) и `work.contexts.config`.

## Расстановщик дня (`agents/scheduler.py`)

`place(day, tasks, busy, now=None) -> dict[id, (start, end)]` — чистая функция без базы:
`tasks` = [{id, size, title, planned_by?, planned_start?, planned_end?}], `busy` = [(start, end)].
Окно — `tu.waking_window(day)`, буфер 10 минут вокруг встреч, тренировка ставится первой
(21:00-00:00, по умолчанию 21:30, 90 минут) и считается занятым, крупное раньше, не больше 70%
свободного времени, `planned_by='manual'` не трогается. Что не влезло — без времени.

## Точки входа

Каждый модуль-задача экспортирует `run(date: str | None = None, dry_run: bool = False, **kw) -> dict`
(короткая сводка для лога) и `if __name__ == "__main__": print(run())`. `workctl.py` вызывает их
по таблице, systemd-таймеры зовут `workctl.py <команда>`.

| workctl          | модуль:функция                    | роль    | когда |
|------------------|-----------------------------------|---------|-------|
| collect [id\|kind] | collectors.base:run_enabled / run_source | reader | каждые 15 мин |
| eas-watch        | collectors.eas:watch              | reader  | сервис (Ping) |
| ingest           | collectors.ingest_server:serve    | reader  | сервис (tailnet) |
| people           | extract.people:run                | reader  | каждые 15 мин |
| embed            | agents.embed_backfill:run         | reader  | ночью |
| extract          | extract.decisions:run             | reader  | каждые 15 мин |
| triage           | extract.triage:run                | reader  | каждые 15 мин |
| draft            | agents.drafter:run                | reader  | каждые 15 мин |
| brief            | agents.brief:run_brief            | reader  | 08:00 |
| cards            | agents.brief:run_meeting_cards    | reader  | каждые 5 мин |
| review           | agents.review:run                 | reader  | вс 19:00 |
| autonomy         | agents.autonomy:run               | reader  | ежечасно |
| audit            | agents.audit:run                  | reader  | вс 19:30 |
| report           | agents.reports:run                | reader  | вручную |
| plan             | agents.planner:run                | reader  | 00:00 |
| dayplan          | agents.dayplan:run                | reader  | каждые 15 мин |
| classify         | agents.dayplan:classify_tasks     | reader  | каждые 5 мин |
| award            | rpg.award:run                     | reader  | каждые 5 мин |
| garmin           | rpg.garmin:run                    | reader  | каждый час |
| weekly           | rpg.weekly:run                    | reader  | вс 20:00 |
| seed-grid FILE   | rpg.seed:run                      | admin   | вручную |
| health           | health:run                        | reader  | каждые 15 мин |

Отдельные сервисы: `python -m bot.terminal` (bot), `python -m app.server` (app),
`python -m executors queue` и `python -m executors calendar` (exec).

Предложить действие из любого читателя: `agents.autonomy.propose(conn, type, context_id, payload,
rationale=None, source_event=None) -> action_id` — сама решает approval|auto и семплинг.
Карточки по предложенным действиям рисует бот (опрашивает `status='proposed'`), outbox для этого не нужен.

## Тесты

`pytest -q` с локальным Postgres (`tests/conftest.py`): фикстуры `admin`, `as_role(role)`,
`role_env(role)` (модули под нужной ролью). Внешние API — через `httpx.MockTransport`;
модель — через `core.llm.set_fake`.

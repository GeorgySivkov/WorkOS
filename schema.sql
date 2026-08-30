CREATE SCHEMA IF NOT EXISTS work;

CREATE TABLE work.contexts (
  id          text PRIMARY KEY,              -- infatica | alfa | consulting | personal
  title       text NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  config      jsonb  NOT NULL DEFAULT '{}',  -- доска, воркспейс, ящик, тон, права агента
  created_at  timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE work.sources (
  id            text PRIMARY KEY,            -- 'trello:george', 'gmail:personal', 'plaud'
  context_id    text REFERENCES work.contexts(id) ON DELETE CASCADE,
  kind          text NOT NULL,
  watermark     text,                        -- курсор ПРИБЫТИЯ, не события:
                                             -- historyId, SyncKey, updated_at записи
  lookback_days int NOT NULL DEFAULT 7,      -- перекрытие для дозревающих записей
  last_ok_at    timestamptz,
  last_error    text,
  enabled       boolean NOT NULL DEFAULT true
);

-- ЛЮДИ ГЛОБАЛЬНЫ. Ян в Инфатике и Ян в личном - один человек.
CREATE TABLE work.people (
  id           bigserial PRIMARY KEY,
  display_name text NOT NULL,
  notes        text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- все обличья одного человека: почта, слак, трелло, имя в транскрипте
CREATE TABLE work.identities (
  id         bigserial PRIMARY KEY,
  person_id  bigint NOT NULL REFERENCES work.people(id) ON DELETE CASCADE,
  kind       text NOT NULL,                  -- email|slack|trello|jira|telegram|display_name
  value      text NOT NULL,
  context_id text REFERENCES work.contexts(id),   -- NULL = обличье общее
  confidence real NOT NULL DEFAULT 1.0,      -- 1.0 детерминированно, ниже - разрешено моделью
  UNIQUE NULLS NOT DISTINCT (kind, value, context_id)
);

-- в каких контекстах человек присутствует и кем
CREATE TABLE work.person_contexts (
  person_id  bigint NOT NULL REFERENCES work.people(id) ON DELETE CASCADE,
  context_id text   NOT NULL REFERENCES work.contexts(id) ON DELETE CASCADE,
  role       text,
  PRIMARY KEY (person_id, context_id)
);

CREATE TABLE work.events (
  id              bigserial PRIMARY KEY,
  context_id      text NOT NULL REFERENCES work.contexts(id) ON DELETE CASCADE,
  source_id       text NOT NULL REFERENCES work.sources(id),
  external_id     text NOT NULL,             -- Message-ID, id записи Plaud, Slack ts, iCal UID
  kind            text NOT NULL,             -- email|meeting|message|card|calendar|note
  occurred_at     timestamptz NOT NULL,      -- когда произошло
  ingested_at     timestamptz NOT NULL DEFAULT now(),  -- когда МЫ это увидели
  actor_person_id bigint REFERENCES work.people(id),
  actor_raw       text,                      -- как автор назван в источнике
  title           text,
  body            text,
  meta            jsonb NOT NULL DEFAULT '{}',
  thread_key      text,
  embedding       halfvec(768),              -- см. заметку про размерность ниже
  UNIQUE (source_id, external_id)            -- одно письмо в двух ящиках = две строки,
);                                           -- общий external_id их связывает
CREATE INDEX ON work.events (context_id, occurred_at DESC);
CREATE INDEX ON work.events (ingested_at DESC);
CREATE INDEX ON work.events (thread_key);

-- неразрешённые упоминания -> очередь на подтверждение в боте
CREATE TABLE work.identity_candidates (
  id           bigserial PRIMARY KEY,
  raw          text NOT NULL,                -- "Ian", "Яна", "@yan"
  kind         text NOT NULL,
  source_event bigint REFERENCES work.events(id) ON DELETE CASCADE,
  context_id   text REFERENCES work.contexts(id),
  suggestions  jsonb NOT NULL DEFAULT '[]',
  status       text NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','resolved','ignored')),
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE work.decisions (
  id           bigserial PRIMARY KEY,
  context_id   text NOT NULL REFERENCES work.contexts(id) ON DELETE CASCADE,
  decided_at   timestamptz NOT NULL,
  statement    text NOT NULL,
  rationale    text,
  participants jsonb DEFAULT '[]',           -- person_id, не строки
  source_event bigint REFERENCES work.events(id) ON DELETE SET NULL,
  supersedes   bigint REFERENCES work.decisions(id),
  embedding    halfvec(768)
);

CREATE TABLE work.commitments (
  id           bigserial PRIMARY KEY,
  context_id   text NOT NULL REFERENCES work.contexts(id) ON DELETE CASCADE,
  direction    text NOT NULL CHECK (direction IN ('mine','theirs')),
  person_id    bigint REFERENCES work.people(id),   -- глобальный человек
  what         text NOT NULL,
  due          date,
  status       text NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','done','dropped')),
  source_event bigint REFERENCES work.events(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  closed_at    timestamptz
);

CREATE TABLE work.actions (
  id             bigserial PRIMARY KEY,
  context_id     text NOT NULL REFERENCES work.contexts(id) ON DELETE CASCADE,
  type           text NOT NULL,              -- email_reply|trello_card|slack_post|calendar_block
  payload        jsonb NOT NULL,             -- строгая схема на тип, исполнитель валидирует
  rationale      text,
  source_event   bigint REFERENCES work.events(id) ON DELETE SET NULL,
  status         text NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed','pending','approved','drafted','edited',
                                   'rejected','executed','failed','expired')),
  autonomy       text NOT NULL DEFAULT 'approval'
                 CHECK (autonomy IN ('approval','auto')),
  approved_at    timestamptz,
  approval_nonce text,                       -- ставит бот, проверяет исполнитель
  created_at     timestamptz NOT NULL DEFAULT now(),
  decided_at     timestamptz,
  executed_at    timestamptz,
  error          text
);

CREATE TABLE work.outcomes (
  id          bigserial PRIMARY KEY,
  action_id   bigint NOT NULL REFERENCES work.actions(id) ON DELETE CASCADE,
  action_type text NOT NULL,
  context_id  text NOT NULL,
  verdict     text NOT NULL CHECK (verdict IN ('accepted','drafted','edited','rejected')),
  diff        text,
  decided_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON work.outcomes (action_type, context_id, decided_at DESC);

CREATE VIEW work.acceptance AS
SELECT action_type, context_id, count(*) AS n,
       round(count(*) FILTER (WHERE verdict = 'accepted')::numeric
             / NULLIF(count(*),0), 2) AS accept_rate,   -- продвижение считается только по ней
       round(count(*) FILTER (WHERE verdict = 'drafted')::numeric
             / NULLIF(count(*),0), 2) AS draft_rate,    -- «оставил черновиком» не равно «принято»
       max(decided_at) AS last_at
FROM work.outcomes
WHERE decided_at > now() - interval '60 days'
GROUP BY 1,2;

-- ========== РОЛИ И ПРАВА ==========
-- Пароли сгенерировать заранее: openssl rand -base64 24
CREATE ROLE workos_reader LOGIN PASSWORD '<СЮДА-ПАРОЛЬ-READER>';
CREATE ROLE workos_exec   LOGIN PASSWORD '<СЮДА-ПАРОЛЬ-EXEC>';

GRANT USAGE ON SCHEMA work TO workos_reader, workos_exec;

-- Читатель: видит всё, пишет данные, но НЕ МОЖЕТ approve.
GRANT SELECT ON ALL TABLES IN SCHEMA work TO workos_reader;
GRANT INSERT, UPDATE, DELETE ON
      work.events, work.decisions, work.commitments,
      work.people, work.identities, work.person_contexts,
      work.identity_candidates, work.sources
   TO workos_reader;
GRANT INSERT ON work.actions TO workos_reader;   -- только предложить
GRANT USAGE ON ALL SEQUENCES IN SCHEMA work TO workos_reader;

-- Исполнитель и бот: видят очередь, меняют её статус, пишут исходы.
-- Ни строчки из events не читают - им нечего там делать.
GRANT SELECT, UPDATE ON work.actions   TO workos_exec;
GRANT SELECT           ON work.contexts TO workos_exec;
GRANT INSERT           ON work.outcomes TO workos_exec;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA work TO workos_exec;

-- Чтобы права не пришлось раздавать заново после каждой новой таблицы
ALTER DEFAULT PRIVILEGES IN SCHEMA work
  GRANT SELECT ON TABLES TO workos_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA work
  GRANT USAGE ON SEQUENCES TO workos_reader, workos_exec;

-- ========== ЗАСЕВ ==========
INSERT INTO work.contexts (id, title) VALUES
  ('infatica',   'Infatica'),
  ('alfa',       'Альфа-Банк'),
  ('consulting', 'AI-консалтинг'),
  ('personal',   'Личное');
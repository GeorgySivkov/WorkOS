-- 2.x-3.x · источники, эмбеддинги, служебные отметки
ALTER TABLE work.events    ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE work.decisions ADD COLUMN IF NOT EXISTS embedding_model text;
-- фильтр «что говорил Ян» по участникам встречи: participant_ids в meta под GIN
CREATE INDEX IF NOT EXISTS events_meta_gin ON work.events USING gin (meta jsonb_path_ops);
CREATE INDEX IF NOT EXISTS events_kind_time ON work.events (kind, occurred_at DESC);

-- дедуп разовых уведомлений: карточки к встречам, брифы, алерты heartbeat
CREATE TABLE IF NOT EXISTS work.notices (
  key     text PRIMARY KEY,
  sent_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, DELETE ON work.notices TO workos_reader;

-- несекретные настройки экземпляра источника: доска, каналы, имя env с секретом
ALTER TABLE work.sources ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}';

-- бот заводит нового человека из карточки «Это [новый человек]» (3.2)
GRANT INSERT ON work.people, work.person_contexts TO workos_bot;
GRANT USAGE ON SEQUENCE work.people_id_seq TO workos_bot;

-- заготовки всех источников, выключены; включаются по фазам
INSERT INTO work.sources (id, context_id, kind, enabled, lookback_days) VALUES
  ('trello:george',   'infatica', 'trello', false, 7),
  ('plaud',           NULL,       'plaud',  false, 3),
  ('ical:personal',   'personal', 'ical',   false, 0),
  ('ical:infatica',   'infatica', 'ical',   false, 0),
  ('eas:alfa',        'alfa',     'eas',    false, 0),
  ('slack:infatica',  'infatica', 'slack',  false, 2),
  ('gmail:personal',  'personal', 'gmail',  false, 2),
  ('gmail:infatica',  'infatica', 'gmail',  false, 2),
  ('achat:alfa',      'alfa',     'achat',  false, 1)
ON CONFLICT (id) DO NOTHING;

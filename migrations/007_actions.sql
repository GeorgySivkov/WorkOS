-- 5.x-6.x · карточки действий, исходы, автономия
-- «Поправить» не меняет строку, а создаёт новую со ссылкой на старую
ALTER TABLE work.actions ADD COLUMN IF NOT EXISTS replaces bigint REFERENCES work.actions(id);
-- что сделал исполнитель: id карточки, черновика, блока
ALTER TABLE work.actions ADD COLUMN IF NOT EXISTS result jsonb;
GRANT UPDATE (result) ON work.actions TO workos_exec, workos_bot;
CREATE INDEX IF NOT EXISTS actions_queue ON work.actions (status, approved_at);

-- автономия по типу и контексту (6.2): продвигает читатель по статистике,
-- /manual в боте возвращает всё в подтверждение и запирает
CREATE TABLE IF NOT EXISTS work.autonomy (
  action_type text NOT NULL,
  context_id  text NOT NULL DEFAULT '*',
  mode        text NOT NULL DEFAULT 'approval' CHECK (mode IN ('approval','auto')),
  locked      boolean NOT NULL DEFAULT false,
  n           int,
  accept_rate numeric,
  auto_count  int NOT NULL DEFAULT 0,       -- для семплинга: каждое пятое всё равно на подтверждение
  changed_at  timestamptz NOT NULL DEFAULT now(),
  reason      text,
  PRIMARY KEY (action_type, context_id)
);
GRANT SELECT, INSERT, UPDATE ON work.autonomy TO workos_reader;
GRANT SELECT ON work.autonomy TO workos_bot;
GRANT UPDATE (mode, locked, changed_at, reason) ON work.autonomy TO workos_bot;
GRANT SELECT ON work.outcomes, work.acceptance TO workos_bot, workos_reader;

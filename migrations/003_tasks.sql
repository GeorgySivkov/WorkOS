-- 1.3 · дела живут в базе, мини-приложение их показывает
CREATE TABLE IF NOT EXISTS work.tasks (
  id          bigserial PRIMARY KEY,
  context_id  text REFERENCES work.contexts(id),
  title       text NOT NULL,
  due         date,                         -- день; без дня и без недели это «Потом»
  week_of     date,                         -- понедельник недели, если дня нет
  remind_at   timestamptz,
  reminded_at timestamptz,
  cell_id     text,                         -- клетка сетки, внешний ключ появляется в 004
  size        text NOT NULL DEFAULT 'S' CHECK (size IN ('S','M','L')),
  source      text NOT NULL,                -- capture | plan | app | commitment
  action_id   bigint REFERENCES work.actions(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  done_at     timestamptz,
  dropped_at  timestamptz                   -- «удалить» не стирает: плану против факта нужна история
);
CREATE INDEX IF NOT EXISTS tasks_open_due ON work.tasks (due) WHERE done_at IS NULL AND dropped_at IS NULL;
CREATE INDEX IF NOT EXISTS tasks_remind ON work.tasks (remind_at) WHERE reminded_at IS NULL AND done_at IS NULL AND dropped_at IS NULL;

-- процесс мини-приложения видит только дела
GRANT USAGE ON SCHEMA work TO workos_app;
GRANT SELECT ON work.contexts TO workos_app;
GRANT SELECT, INSERT ON work.tasks TO workos_app;
GRANT UPDATE (title, due, week_of, remind_at, reminded_at, done_at, dropped_at, size, context_id)
   ON work.tasks TO workos_app;
GRANT USAGE ON SEQUENCE work.tasks_id_seq TO workos_app;

-- бот кладёт задачи из захвата и из карточки плана, отмечает напоминания, правит захват
GRANT SELECT, INSERT ON work.tasks TO workos_bot;
GRANT UPDATE (reminded_at, dropped_at, due, week_of, remind_at, cell_id, size, context_id, title)
   ON work.tasks TO workos_bot;
GRANT USAGE ON SEQUENCE work.tasks_id_seq TO workos_bot;

-- читатели видят дела (план, бриф) и ставят клетку и размер задачам из мини-приложения
GRANT SELECT ON work.tasks TO workos_reader;
GRANT UPDATE (cell_id, size) ON work.tasks TO workos_reader;

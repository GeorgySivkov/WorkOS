-- 2.5 + 2.9 · у задачи появляется время, у блока в календаре — сверка с базой
ALTER TABLE work.tasks
  ADD COLUMN IF NOT EXISTS planned_start   timestamptz,
  ADD COLUMN IF NOT EXISTS planned_end     timestamptz,
  ADD COLUMN IF NOT EXISTS planned_at      timestamptz,      -- когда время задачи менялось в базе
  ADD COLUMN IF NOT EXISTS planned_by      text CHECK (planned_by IN ('auto','manual')),
  ADD COLUMN IF NOT EXISTS block_synced_at timestamptz;      -- когда блок в календаре сверен с базой

-- расстановщик (код без модели) работает под читателями; приложение пересобирает день
GRANT UPDATE (planned_start, planned_end, planned_at, planned_by) ON work.tasks
   TO workos_reader, workos_app, workos_bot;
-- писатель календаря пишет блоки и принимает их ручные переносы
GRANT SELECT ON work.tasks TO workos_exec;
GRANT UPDATE (planned_start, planned_end, planned_at, planned_by, block_synced_at) ON work.tasks TO workos_exec;

-- Встречи без тел: всё, что нужно зеркалу, расстановщику и ленте «Сегодня».
-- Вид работает с правами владельца, поэтому исполнителю и приложению
-- не нужен доступ к work.events, и писем они не видят.
CREATE OR REPLACE VIEW work.calendar_feed AS
SELECT e.id, e.context_id, e.source_id, e.external_id,
       COALESCE(e.meta->>'uid', e.external_id)            AS uid,
       COALESCE(e.meta->>'recurrence_id', '')             AS recurrence_id,
       e.title,
       e.occurred_at                                      AS starts_at,
       COALESCE((e.meta->>'ends_at')::timestamptz, e.occurred_at + interval '30 minutes') AS ends_at,
       COALESCE((e.meta->>'all_day')::boolean, false)     AS all_day,
       e.meta->>'location'                                AS location,
       COALESCE(e.meta->>'status', 'confirmed')           AS status,
       COALESCE((e.meta->>'busy')::boolean, true)         AS busy,
       e.meta->>'updated'                                 AS updated
FROM work.events e
WHERE e.kind = 'calendar';

GRANT SELECT ON work.calendar_feed TO workos_exec, workos_app, workos_bot, workos_reader;

-- Защита одобрения на уровне базы: новая строка actions всегда «предложена».
-- У читателя есть INSERT на actions. Без этого триггера он мог бы вставить
-- строку сразу со status='approved' и правильным хешем, и исполнитель
-- выполнил бы её в обход бота. Одобряет только бот, и только UPDATE-ом.
CREATE OR REPLACE FUNCTION work.actions_force_proposed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  -- только сервисные роли; миграции и ручная починка под админом не ограничены
  IF current_user NOT IN ('workos_reader', 'workos_bot', 'workos_exec', 'workos_app') THEN
    RETURN NEW;
  END IF;
  NEW.status         := 'proposed';
  NEW.approved_at    := NULL;
  NEW.approved_hash  := NULL;
  NEW.approval_nonce := NULL;
  NEW.decided_at     := NULL;
  NEW.executed_at    := NULL;
  NEW.result         := NULL;
  NEW.error          := NULL;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS actions_force_proposed ON work.actions;
CREATE TRIGGER actions_force_proposed BEFORE INSERT ON work.actions
FOR EACH ROW EXECUTE FUNCTION work.actions_force_proposed();

-- полнотекстовый поиск «Спросить»: выражение ровно как в retrieve.py
CREATE INDEX IF NOT EXISTS events_fts ON work.events
  USING gin (to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(body, '')));

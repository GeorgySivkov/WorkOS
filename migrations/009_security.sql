-- Безопасность по итогам ревью: автономию решает только бот, outbox нельзя подделать,
-- исполнитель не может одобрить, общий рубильник /manual.

-- ---------- 1. автономия: читатель не пишет work.autonomy ----------
-- Раньше у читателя были INSERT/UPDATE: он мог сам перевести свой тип в auto и
-- вставить действие с autonomy='auto'. Теперь режимы двигает только функция ниже
-- (по статистике исходов, которые пишет бот), а auto решает бот своей выборкой.
REVOKE INSERT, UPDATE, DELETE ON work.autonomy FROM workos_reader;

-- семплинг (каждое пятое автономное на подтверждение) теперь считает бот
GRANT UPDATE (auto_count) ON work.autonomy TO workos_bot;
-- /manual запирает общий рубильник, даже если его строки ещё нет
GRANT INSERT (action_type, context_id, mode, locked, changed_at, reason) ON work.autonomy TO workos_bot;

-- общий рубильник ('*', '*'): locked = всё на подтверждение
INSERT INTO work.autonomy (action_type, context_id, mode, locked, reason)
VALUES ('*', '*', 'approval', false, 'общий рубильник /manual')
ON CONFLICT (action_type, context_id) DO NOTHING;

-- Продвижение и деградация режимов (раз в час, agents.autonomy:run под читателем).
-- Только SQL: пороги зашиты здесь, читатель передаёт лишь dry_run.
-- Вверх: принято/всего > 0.85 (без округления) и n >= 20, только внутренние типы.
-- Вниз: < 0.7, а внешний тип в auto возвращается на подтверждение всегда.
-- Запертые строки и общий замок не трогаются. Возвращает изменения режимов.
CREATE OR REPLACE FUNCTION work.autonomy_recompute(p_dry_run boolean DEFAULT false)
RETURNS TABLE (action_type text, context_id text, old_mode text, new_mode text,
               n int, accepted int, accept_rate numeric, reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = work, pg_temp AS $$
#variable_conflict use_column
DECLARE
  c_allowed CONSTANT text[] := ARRAY['plan_batch', 'task_add'];
  c_window  CONSTANT int := 30;
  c_promote CONSTANT numeric := 0.85;
  c_min_n   CONSTANT int := 20;
  c_demote  CONSTANT numeric := 0.7;
  st record;
  own record;
  star record;
  has_own boolean;
  has_star boolean;
  cur text;
  nm text;
  why text;
  rate numeric;
BEGIN
  IF NOT p_dry_run THEN
    LOCK TABLE work.autonomy IN SHARE ROW EXCLUSIVE MODE;   -- два прогона подряд не мешают друг другу
  END IF;
  IF EXISTS (SELECT 1 FROM work.autonomy g WHERE g.action_type = '*' AND g.context_id = '*' AND g.locked) THEN
    RETURN;                                                -- /manual: ничего не двигаем
  END IF;

  FOR st IN
    SELECT o.action_type AS t, o.context_id AS c, count(*)::int AS cnt,
           (count(*) FILTER (WHERE o.verdict = 'accepted'))::int AS acc
    FROM work.outcomes o
    WHERE o.decided_at > now() - make_interval(days => c_window)
      AND o.action_type <> '*' AND o.context_id <> '*'
    GROUP BY 1, 2
    ORDER BY 1, 2
  LOOP
    rate := st.acc::numeric / st.cnt;
    SELECT * INTO own FROM work.autonomy a WHERE a.action_type = st.t AND a.context_id = st.c;
    has_own := FOUND;
    SELECT * INTO star FROM work.autonomy a WHERE a.action_type = st.t AND a.context_id = '*';
    has_star := FOUND;
    CONTINUE WHEN (has_own AND own.locked) OR (has_star AND star.locked);
    cur := CASE WHEN has_own THEN own.mode WHEN has_star THEN star.mode ELSE 'approval' END;
    nm := NULL;
    why := NULL;
    IF cur = 'auto' AND rate < c_demote THEN
      nm := 'approval';
      why := format('принято %s из %s за %s дней (%s%%), ниже порога %s%%',
                    st.acc, st.cnt, c_window, round(rate * 100), round(c_demote * 100));
    ELSIF cur = 'auto' AND NOT (st.t = ANY (c_allowed)) THEN
      nm := 'approval';
      why := 'без подтверждения бывают только внутренние типы (план и задачи)';
    ELSIF cur <> 'auto' AND st.t = ANY (c_allowed) AND rate > c_promote AND st.cnt >= c_min_n THEN
      nm := 'auto';
      why := format('принято %s из %s за %s дней (%s%%), порог %s%% при n от %s',
                    st.acc, st.cnt, c_window, round(rate * 100), round(c_promote * 100), c_min_n);
    END IF;
    IF NOT p_dry_run THEN
      IF NOT has_own THEN          -- своя строка перекрывает '*': без изменения наследует её режим
        INSERT INTO work.autonomy (action_type, context_id, mode, n, accept_rate, reason)
        VALUES (st.t, st.c, coalesce(nm, cur), st.cnt, round(rate, 4), why);
      ELSIF nm IS NOT NULL THEN
        UPDATE work.autonomy a SET mode = nm, n = st.cnt, accept_rate = round(rate, 4),
                                   reason = why, changed_at = now()
         WHERE a.action_type = st.t AND a.context_id = st.c;
      ELSE
        UPDATE work.autonomy a SET n = st.cnt, accept_rate = round(rate, 4)
         WHERE a.action_type = st.t AND a.context_id = st.c;
      END IF;
    END IF;
    IF nm IS NOT NULL THEN
      action_type := st.t; context_id := st.c; old_mode := cur; new_mode := nm;
      n := st.cnt; accepted := st.acc; accept_rate := round(rate, 4); reason := why;
      RETURN NEXT;
    END IF;
  END LOOP;

  -- внешний тип в auto без свежей статистики (в том числе строка (тип, '*')): вниз
  FOR own IN
    SELECT a.action_type AS t, a.context_id AS c, a.n AS cnt, a.accept_rate AS ar
    FROM work.autonomy a
    WHERE a.mode = 'auto' AND NOT a.locked AND a.action_type <> '*'
      AND NOT (a.action_type = ANY (c_allowed))
    ORDER BY 1, 2
  LOOP
    why := 'без подтверждения бывают только внутренние типы (план и задачи)';
    IF NOT p_dry_run THEN
      UPDATE work.autonomy a SET mode = 'approval', reason = why, changed_at = now()
       WHERE a.action_type = own.t AND a.context_id = own.c;
    END IF;
    action_type := own.t; context_id := own.c; old_mode := 'auto'; new_mode := 'approval';
    n := own.cnt; accepted := NULL; accept_rate := own.ar; reason := why;
    RETURN NEXT;
  END LOOP;
END $$;

REVOKE ALL ON FUNCTION work.autonomy_recompute(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION work.autonomy_recompute(boolean) TO workos_reader;

-- ---------- 4. одобрение: показанный хеш отдельно от одобренного ----------
-- Раньше бот писал полный хеш в approved_hash ещё при показе карточки, и
-- исполнитель (UPDATE status) мог сам перевести строку в approved.
-- Теперь при показе пишется shown_hash, approved_hash ставит только нажатие.
ALTER TABLE work.actions ADD COLUMN IF NOT EXISTS shown_hash text;
GRANT UPDATE (shown_hash) ON work.actions TO workos_bot;

-- новая строка actions от сервисной роли: всегда proposed и на подтверждение
CREATE OR REPLACE FUNCTION work.actions_force_proposed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  -- только сервисные роли; миграции и ручная починка под админом не ограничены
  IF current_user NOT IN ('workos_reader', 'workos_bot', 'workos_exec', 'workos_app') THEN
    RETURN NEW;
  END IF;
  NEW.status         := 'proposed';
  NEW.autonomy       := 'approval';   -- auto решает бот своей выборкой из work.autonomy
  NEW.approved_at    := NULL;
  NEW.approved_hash  := NULL;
  NEW.shown_hash     := NULL;
  NEW.approval_nonce := NULL;
  NEW.decided_at     := NULL;
  NEW.executed_at    := NULL;
  NEW.result         := NULL;
  NEW.error          := NULL;
  RETURN NEW;                        -- replaces не трогаем: им «Поправить» связывает версии
END $$;

-- переходы статусов по ролям
CREATE OR REPLACE FUNCTION work.actions_guard_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user = 'workos_exec' THEN
    -- исполнитель только ведёт и закрывает одобренное: approved -> approved (метка взятия,
    -- ошибка) | executed | failed, drafted -> drafted | failed. Одобрить (proposed -> approved)
    -- или поднять черновик до отправки (drafted -> approved) он не может.
    IF NOT ((OLD.status = 'approved' AND NEW.status IN ('approved', 'executed', 'failed'))
            OR (OLD.status = 'drafted' AND NEW.status IN ('drafted', 'failed'))) THEN
      RAISE EXCEPTION 'workos_exec: переход статуса % -> % запрещён', OLD.status, NEW.status
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  ELSIF current_user = 'workos_bot' THEN
    -- бот сам исполняет только внутренние типы; внешнее исполняет только исполнитель
    IF NEW.status = 'executed' AND OLD.status IS DISTINCT FROM 'executed'
       AND NEW.type NOT IN ('plan_batch', 'task_add') THEN
      RAISE EXCEPTION 'workos_bot: % исполняет только исполнитель', NEW.type
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS actions_guard_update ON work.actions;
CREATE TRIGGER actions_guard_update BEFORE UPDATE ON work.actions
FOR EACH ROW EXECUTE FUNCTION work.actions_guard_update();

-- ---------- 3. outbox: автора подделать нельзя ----------
-- created_by = текущая роль всегда; читатель и исполнитель не могут вставить
-- строку «уже отправленной» с чужим tg_message_id (так её приняли бы за карточку).
CREATE OR REPLACE FUNCTION work.outbox_stamp() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.created_by := current_user;
  IF current_user IN ('workos_reader', 'workos_exec', 'workos_app') THEN
    NEW.sent_at := NULL;
    NEW.tg_message_id := NULL;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS outbox_stamp ON work.outbox;
CREATE TRIGGER outbox_stamp BEFORE INSERT ON work.outbox
FOR EACH ROW EXECUTE FUNCTION work.outbox_stamp();

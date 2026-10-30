-- Поправки опыта после ревью логики.
-- 1. Потолок 100 в день не наказывает: отметка привычки или задачи пишется и после
--    упора в потолок (с нулём опыта), поэтому привычка засчитана, серия не рвётся,
--    а Garmin не пропускает день.
-- 2. Этапы (боссы), отмеченные руками или из Garmin, не съедают дневной потолок
--    привычек и задач.
-- 3. Опыт персонажа считается по всему журналу: пересев сетки нового сезона
--    (неактивные клетки) его не обнуляет.
-- 4. «Уровень повышен» в мини-приложении приходит один раз: сравнение с наибольшим
--    уже показанным уровнем, а не с уровнем до нажатия.

-- ---------- 1. нулевые записи в журнале ----------
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'rpg.xp'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) ~ 'amount > 0'
  LOOP
    EXECUTE format('ALTER TABLE rpg.xp DROP CONSTRAINT %I', r.conname);
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'rpg.xp'::regclass AND conname = 'xp_amount_nonneg') THEN
    ALTER TABLE rpg.xp ADD CONSTRAINT xp_amount_nonneg CHECK (amount >= 0);
  END IF;
END $$;

-- ---------- 2. потолок дня без этапов ----------
-- опыт этапа (ручная отметка или Garmin) в потолок не входит; задача на клетке этапа входит
CREATE OR REPLACE FUNCTION rpg.day_xp(p_attr text, p_day date) RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(x.amount), 0)::int
  FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
  WHERE (CASE WHEN c.kind = 'attribute' THEN c.id ELSE c.parent END) = p_attr
    AND rpg.lday(x.earned_at) = p_day
    AND NOT (c.kind = 'milestone' AND x.source <> 'task')
$$;

-- задача: S 10, M 25, L 60, потолок 100 в день на характеристику;
-- упёрлась в потолок: запись с нулём, задача всё равно засчитана клетке
CREATE OR REPLACE FUNCTION rpg.award_task(p_task bigint) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = rpg, work, pg_temp AS $$
DECLARE
  t work.tasks%ROWTYPE; attr text; base int; amt int; d date; n int;
BEGIN
  SELECT * INTO t FROM work.tasks WHERE id = p_task;
  IF NOT FOUND OR t.done_at IS NULL OR t.dropped_at IS NOT NULL OR t.cell_id IS NULL THEN RETURN 0; END IF;
  IF EXISTS (SELECT 1 FROM rpg.xp WHERE source = 'task' AND source_ref = t.id::text) THEN RETURN 0; END IF;
  attr := rpg.attr_of(t.cell_id);
  IF attr IS NULL THEN RETURN 0; END IF;
  base := CASE t.size WHEN 'S' THEN 10 WHEN 'M' THEN 25 ELSE 60 END;
  d := rpg.lday(t.done_at);
  amt := GREATEST(LEAST(base, 100 - rpg.day_xp(attr, d)), 0);
  INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
  VALUES (t.cell_id, amt, 'task', t.id::text, t.done_at)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN CASE WHEN n > 0 THEN amt ELSE 0 END;
END $$;

-- ручная отметка: привычка раз в день (+10% за каждый день серии, до +50%),
-- качество раз в неделю, этап один раз; потолок 100 в день на характеристику
-- (кроме этапа). Потолок срезает опыт, но не отметку: запись пишется и с нулём.
CREATE OR REPLACE FUNCTION rpg.log_manual(p_cell text) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = rpg, pg_temp AS $$
DECLARE
  c rpg.cells%ROWTYPE; ref text; d date := rpg.lday(now()); streak int := 0; amt int; attr text; n int;
BEGIN
  SELECT * INTO c FROM rpg.cells WHERE id = p_cell AND active AND kind IN ('habit','quality','milestone');
  IF NOT FOUND THEN RETURN 0; END IF;
  ref := c.id || ':' || CASE c.kind
           WHEN 'quality' THEN to_char(d, 'IYYY-"W"IW')
           WHEN 'milestone' THEN 'once'
           ELSE to_char(d, 'YYYY-MM-DD') END;
  IF c.kind = 'habit' THEN
    WHILE streak < 5 AND EXISTS (
      SELECT 1 FROM rpg.xp WHERE cell_id = c.id AND rpg.lday(earned_at) = d - (streak + 1)) LOOP
      streak := streak + 1;
    END LOOP;
  END IF;
  amt := round(c.xp_base * (1 + 0.1 * streak));
  attr := rpg.attr_of(c.id);
  IF c.kind <> 'milestone' THEN
    amt := LEAST(amt, 100 - rpg.day_xp(attr, d));
  END IF;
  amt := GREATEST(amt, 0);
  INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
  VALUES (c.id, amt, 'manual', ref, now())
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN CASE WHEN n > 0 THEN amt ELSE 0 END;
END $$;

-- ---------- 3. персонаж по всему журналу ----------
CREATE OR REPLACE VIEW rpg.character AS
SELECT t.xp,
       rpg.level_of(t.xp) AS level,
       50 * rpg.level_of(t.xp) * (rpg.level_of(t.xp) + 1) AS level_floor,
       50 * (rpg.level_of(t.xp) + 1) * (rpg.level_of(t.xp) + 2) AS next_level_at
FROM (SELECT COALESCE(sum(amount), 0)::bigint AS xp FROM rpg.xp) t;

-- ---------- 4. повышение уровня в приложении один раз ----------
-- Своя отметка 'app:character' в rpg.level_marks: начислятор (award.check_levels)
-- смотрит только на свои шкалы и её не трогает. Порог = наибольшее из уровня до
-- нажатия, уже показанного в приложении и уже объявленного начислятором в Telegram.
CREATE OR REPLACE FUNCTION rpg.app_level_up(p_before int, p_after int) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = rpg, pg_temp AS $$
DECLARE seen int;
BEGIN
  INSERT INTO rpg.level_marks (scope, level) VALUES ('app:character', p_before)
  ON CONFLICT (scope) DO NOTHING;
  SELECT GREATEST(m.level, p_before, (SELECT level FROM rpg.level_marks WHERE scope = 'character'))
    INTO seen
    FROM rpg.level_marks m WHERE m.scope = 'app:character' FOR UPDATE;
  IF p_after > seen THEN
    UPDATE rpg.level_marks SET level = p_after, updated_at = now() WHERE scope = 'app:character';
    RETURN true;
  END IF;
  RETURN false;
END $$;

REVOKE EXECUTE ON FUNCTION rpg.app_level_up(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rpg.app_level_up(int, int) TO workos_app;

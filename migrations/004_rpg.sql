-- П.1 + П.3 · домен rpg: сетка, опыт, уровни, награды
CREATE SCHEMA IF NOT EXISTS rpg;

CREATE TABLE IF NOT EXISTS rpg.cells (
  id          text PRIMARY KEY,             -- 'C' центр, 'A'..'H' направления, 'A1'..'H8' практики
  parent      text REFERENCES rpg.cells(id),
  title       text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('quest','attribute','habit','milestone','quality')),
  xp_base     int  NOT NULL DEFAULT 10,
  season      int  NOT NULL DEFAULT 1,      -- пересмотр сетки раз в квартал
  active      boolean NOT NULL DEFAULT true,
  auto_source text                          -- откуда опыт приходит сам: garmin:activity, garmin:sleep:7.5 ...
);

-- журнал опыта: только факты, начисление идемпотентно
CREATE TABLE IF NOT EXISTS rpg.xp (
  id         bigserial PRIMARY KEY,
  cell_id    text NOT NULL REFERENCES rpg.cells(id),
  amount     int  NOT NULL CHECK (amount > 0),
  source     text NOT NULL,                 -- task | garmin | food | jobs | work | manual
  source_ref text NOT NULL,                 -- id события-основания
  earned_at  timestamptz NOT NULL,
  UNIQUE (source, source_ref, cell_id)
);
CREATE INDEX IF NOT EXISTS xp_cell_time ON rpg.xp (cell_id, earned_at DESC);

-- награды из списка хотелок на порогах уровней; scope = 'character' или id характеристики
CREATE TABLE IF NOT EXISTS rpg.rewards (
  id         bigserial PRIMARY KEY,
  scope      text NOT NULL DEFAULT 'character',
  level      int  NOT NULL,
  title      text NOT NULL,
  claimed_at timestamptz,
  UNIQUE (scope, level)
);

-- ачивки за разовые события
CREATE TABLE IF NOT EXISTS rpg.achievements (
  code      text PRIMARY KEY,
  title     text NOT NULL,
  detail    text,
  earned_at timestamptz NOT NULL DEFAULT now()
);

-- последний уровень, о котором уже сообщили (чтобы повышение пришло ровно один раз)
CREATE TABLE IF NOT EXISTS rpg.level_marks (
  scope      text PRIMARY KEY,
  level      int NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- логический день: по Мадриду, заканчивается в 4:00
CREATE OR REPLACE FUNCTION rpg.lday(ts timestamptz) RETURNS date
LANGUAGE sql IMMUTABLE AS $$ SELECT ((ts AT TIME ZONE 'Europe/Madrid') - interval '4 hours')::date $$;

-- уровень по обратной к 50·L·(L+1)
CREATE OR REPLACE FUNCTION rpg.level_of(xp bigint) RETURNS int
LANGUAGE sql IMMUTABLE AS $$ SELECT floor((sqrt(1 + 0.08 * xp) - 1) / 2)::int $$;

CREATE OR REPLACE FUNCTION rpg.attr_of(p_cell text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN kind = 'attribute' THEN id WHEN kind IN ('habit','milestone','quality') THEN parent END
  FROM rpg.cells WHERE id = p_cell
$$;

-- опыт характеристики за логический день (для потолка 100 в день)
CREATE OR REPLACE FUNCTION rpg.day_xp(p_attr text, p_day date) RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(x.amount), 0)::int
  FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
  WHERE (CASE WHEN c.kind = 'attribute' THEN c.id ELSE c.parent END) = p_attr
    AND rpg.lday(x.earned_at) = p_day
$$;

CREATE OR REPLACE VIEW rpg.attributes AS
WITH per_attr AS (
  SELECT CASE WHEN c.kind = 'attribute' THEN c.id ELSE c.parent END AS attr_id, x.amount, x.earned_at
  FROM rpg.xp x JOIN rpg.cells c ON c.id = x.cell_id
), agg AS (
  SELECT a.id, a.title, COALESCE(sum(p.amount), 0)::bigint AS xp, max(p.earned_at) AS last_xp_at,
         COALESCE(sum(p.amount) FILTER (WHERE p.earned_at > now() - interval '7 days'), 0)::bigint AS xp_week
  FROM rpg.cells a LEFT JOIN per_attr p ON p.attr_id = a.id
  WHERE a.kind = 'attribute' AND a.active
  GROUP BY a.id, a.title
)
SELECT id, title, xp, rpg.level_of(xp) AS level,
       xp - 50 * rpg.level_of(xp) * (rpg.level_of(xp) + 1) AS xp_into_level,
       100 * (rpg.level_of(xp) + 1) AS level_span,
       last_xp_at, xp_week,
       (last_xp_at IS NULL OR last_xp_at < now() - interval '14 days') AS rusty
FROM agg;

CREATE OR REPLACE VIEW rpg.character AS
SELECT COALESCE(sum(xp), 0)::bigint AS xp,
       rpg.level_of(COALESCE(sum(xp), 0)::bigint) AS level,
       50 * rpg.level_of(COALESCE(sum(xp), 0)::bigint) * (rpg.level_of(COALESCE(sum(xp), 0)::bigint) + 1) AS level_floor,
       50 * (rpg.level_of(COALESCE(sum(xp), 0)::bigint) + 1) * (rpg.level_of(COALESCE(sum(xp), 0)::bigint) + 2) AS next_level_at
FROM rpg.attributes;

-- задачи получают ссылку на клетку
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_cell_fk') THEN
    ALTER TABLE work.tasks ADD CONSTRAINT tasks_cell_fk FOREIGN KEY (cell_id) REFERENCES rpg.cells(id);
  END IF;
END $$;

-- ---------- начисление опыта: одни правила для мини-приложения и начислятора ----------
-- задача: S 10, M 25, L 60, потолок 100 в день на характеристику
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
  amt := LEAST(base, 100 - rpg.day_xp(attr, d));
  IF amt <= 0 THEN RETURN 0; END IF;
  INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
  VALUES (t.cell_id, amt, 'task', t.id::text, t.done_at)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN CASE WHEN n > 0 THEN amt ELSE 0 END;
END $$;

-- снятая отметка забирает опыт задачи обратно
CREATE OR REPLACE FUNCTION rpg.revoke_task(p_task bigint) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = rpg, work, pg_temp AS $$
  DELETE FROM rpg.xp x USING work.tasks t
  WHERE x.source = 'task' AND x.source_ref = p_task::text
    AND t.id = p_task AND t.done_at IS NULL;
$$;

-- ручная отметка: привычка раз в день (+10% за каждый день серии, до +50%),
-- качество раз в неделю, этап один раз; потолок 100 в день на характеристику
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
  IF amt <= 0 THEN RETURN 0; END IF;
  INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
  VALUES (c.id, amt, 'manual', ref, now())
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN CASE WHEN n > 0 THEN amt ELSE 0 END;
END $$;

-- снять ручную отметку привычки за сегодня
CREATE OR REPLACE FUNCTION rpg.revoke_manual(p_cell text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = rpg, pg_temp AS $$
  DELETE FROM rpg.xp
  WHERE source = 'manual' AND cell_id = p_cell
    AND source_ref = p_cell || ':' || to_char(rpg.lday(now()), 'YYYY-MM-DD');
$$;

REVOKE EXECUTE ON FUNCTION rpg.award_task(bigint), rpg.revoke_task(bigint),
                           rpg.log_manual(text), rpg.revoke_manual(text) FROM PUBLIC;

-- опыт начисляет процесс читателей, и пишет он только в журнал и отметки уровней
GRANT USAGE ON SCHEMA rpg TO workos_reader, workos_bot, workos_app;
GRANT SELECT ON ALL TABLES IN SCHEMA rpg TO workos_reader, workos_bot;
GRANT INSERT ON rpg.xp TO workos_reader;
GRANT USAGE ON SEQUENCE rpg.xp_id_seq TO workos_reader;
GRANT INSERT, UPDATE ON rpg.level_marks TO workos_reader;
GRANT INSERT ON rpg.achievements TO workos_reader;
GRANT UPDATE (claimed_at) ON rpg.rewards TO workos_reader, workos_bot;
GRANT EXECUTE ON FUNCTION rpg.award_task(bigint) TO workos_reader, workos_app, workos_bot;

-- мини-приложение читает персонажа и пишет только через функции
GRANT SELECT ON rpg.cells, rpg.attributes, rpg.character, rpg.xp, rpg.rewards, rpg.achievements TO workos_app;
GRANT EXECUTE ON FUNCTION rpg.revoke_task(bigint), rpg.log_manual(text), rpg.revoke_manual(text) TO workos_app;

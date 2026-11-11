-- Оценка дня (команда /day в боте): число от -10 до +10 за логический день.
-- Клетки сетки получают опыт сами по auto_source:
--   rating:day      привычка «оценил день»: раз в день, серия, потолок 100 на характеристику
--   rating:month:N  этап «месяц со средней оценкой не ниже N»: разово, по закрытому месяцу,
--                   если в нём оценено хотя бы 20 дней
CREATE TABLE IF NOT EXISTS rpg.day_ratings (
  day        date PRIMARY KEY,                -- логический день (кончается в 4:00 по Мадриду)
  score      int  NOT NULL CHECK (score BETWEEN -10 AND 10),
  note       text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rpg.log_rating(p_score int, p_note text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = rpg, pg_temp AS $$
DECLARE
  d          date := rpg.lday(now());
  m_start    date := date_trunc('month', rpg.lday(now()))::date;
  prev_start date := (date_trunc('month', rpg.lday(now())) - interval '1 month')::date;
  c rpg.cells%ROWTYPE;
  streak int; amt int; n int; got int := 0;
  boss jsonb := NULL; p_avg numeric; p_days int; thr numeric;
BEGIN
  IF p_score IS NULL OR p_score < -10 OR p_score > 10 THEN
    RAISE EXCEPTION 'оценка дня от -10 до +10';
  END IF;
  INSERT INTO rpg.day_ratings (day, score, note) VALUES (d, p_score, NULLIF(btrim(p_note), ''))
  ON CONFLICT (day) DO UPDATE
    SET score = EXCLUDED.score, note = COALESCE(EXCLUDED.note, rpg.day_ratings.note), updated_at = now();

  -- привычка: опыт за то, что день оценён (оценка может быть и минусовой)
  FOR c IN SELECT * FROM rpg.cells WHERE active AND kind = 'habit' AND auto_source = 'rating:day' LOOP
    streak := 0;
    WHILE streak < 5 AND EXISTS (
      SELECT 1 FROM rpg.xp WHERE cell_id = c.id AND rpg.lday(earned_at) = d - (streak + 1)) LOOP
      streak := streak + 1;
    END LOOP;
    amt := GREATEST(LEAST(round(c.xp_base * (1 + 0.1 * streak))::int,
                          100 - rpg.day_xp(rpg.attr_of(c.id), d)), 0);
    INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
    VALUES (c.id, amt, 'rating', c.id || ':' || to_char(d, 'YYYY-MM-DD'), now())
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN got := got + amt; END IF;
  END LOOP;

  -- этап: прошлый месяц закрыт со средней не ниже порога
  SELECT avg(score), count(*) INTO p_avg, p_days
  FROM rpg.day_ratings WHERE day >= prev_start AND day < m_start;
  FOR c IN SELECT * FROM rpg.cells WHERE active AND kind = 'milestone' AND auto_source LIKE 'rating:month:%' LOOP
    thr := NULLIF(split_part(c.auto_source, ':', 3), '')::numeric;
    IF thr IS NOT NULL AND p_days >= 20 AND p_avg >= thr THEN
      INSERT INTO rpg.xp (cell_id, amount, source, source_ref, earned_at)
      VALUES (c.id, c.xp_base, 'rating', c.id || ':once', now())
      ON CONFLICT DO NOTHING;
      GET DIAGNOSTICS n = ROW_COUNT;
      IF n > 0 THEN
        got := got + c.xp_base;
        boss := jsonb_build_object('title', c.title, 'xp', c.xp_base,
                                   'month', to_char(prev_start, 'YYYY-MM'), 'avg', round(p_avg, 1));
      END IF;
    END IF;
  END LOOP;

  RETURN jsonb_build_object(
    'day', d, 'score', p_score, 'xp', got, 'milestone', boss,
    'month_avg',  (SELECT round(avg(score), 1) FROM rpg.day_ratings WHERE day >= m_start AND day <= d),
    'month_days', (SELECT count(*) FROM rpg.day_ratings WHERE day >= m_start AND day <= d),
    'week_avg',   (SELECT round(avg(score), 1) FROM rpg.day_ratings WHERE day > d - 7 AND day <= d));
END $$;

REVOKE EXECUTE ON FUNCTION rpg.log_rating(int, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rpg.log_rating(int, text) TO workos_bot;
GRANT SELECT ON rpg.day_ratings TO workos_bot, workos_reader, workos_app;

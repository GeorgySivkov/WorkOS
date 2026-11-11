-- Ползунок «Оценка дня» на вкладке «Сегодня» в Mini App.
-- Приложение пишет оценку той же функцией, что и /day в боте: таблицу напрямую не трогает,
-- опыт начисляет только rpg.log_rating. SELECT на rpg.day_ratings у приложения уже есть (011).
GRANT EXECUTE ON FUNCTION rpg.log_rating(int, text) TO workos_app;

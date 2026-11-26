-- Голосовые заметки с часов: Siri (fenix 8 -> айфон) -> команда «Заметка» -> приёмник (collectors/ingest_server.py).
-- Формат notes разбирает collectors/ios.py: {"now", "text"}; бот подхватывает событие как захват из «Входящих».
-- health_hours = 0: заметки нерегулярные, молчание источника не тревога. Включается руками после проверки.
INSERT INTO work.sources (id, context_id, kind, enabled, lookback_days, config) VALUES
  ('ios:notes', 'personal', 'ios', false, 0, '{"format": "notes", "health_hours": 0}'::jsonb)
ON CONFLICT (id) DO NOTHING;

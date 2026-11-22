-- Альфа через айфон: «Команды» iOS шлют встречи и письма на приёмник (collectors/ios.py).
-- Замена eas:alfa, новое устройство в Exchange не нужно. Включается руками после проверки.
INSERT INTO work.sources (id, context_id, kind, enabled, lookback_days, config) VALUES
  ('ios:alfa', 'alfa', 'ios', false, 0, '{"account": "Alfabank", "window_days": 60}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 1.1 · бот-терминал: своя роль, outbox, одобрение привязано к тексту
GRANT USAGE ON SCHEMA work TO workos_bot;

-- выход: всё, что система хочет сказать. Пишут все, отправляет только бот
CREATE TABLE IF NOT EXISTS work.outbox (
  id            bigserial PRIMARY KEY,
  topic         text NOT NULL,              -- inbox | decisions | plan | system | ask | домен
  text          text NOT NULL,
  buttons       jsonb NOT NULL DEFAULT '[]',
  ref_action    bigint REFERENCES work.actions(id) ON DELETE SET NULL,
  created_by    text NOT NULL DEFAULT current_user,
  created_at    timestamptz NOT NULL DEFAULT now(),
  sent_at       timestamptz,
  tg_message_id bigint
);
CREATE INDEX IF NOT EXISTS outbox_unsent ON work.outbox (created_at) WHERE sent_at IS NULL;

-- одобрение привязано к тексту, который был на карточке
ALTER TABLE work.actions ADD COLUMN IF NOT EXISTS approved_hash text;

-- темы личного чата: ключ -> message_thread_id
CREATE TABLE IF NOT EXISTS work.bot_topics (
  key        text PRIMARY KEY,
  title      text NOT NULL,
  thread_id  bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO work.sources (id, context_id, kind, enabled)
VALUES ('telegram:inbox', NULL, 'telegram', true)
ON CONFLICT (id) DO NOTHING;

-- бот: видит то, что показывает; пишет вход; одобряет, но payload не трогает
GRANT SELECT ON work.contexts, work.people, work.identities, work.person_contexts,
                work.events, work.actions, work.identity_candidates, work.outbox,
                work.sources, work.decisions, work.commitments, work.bot_topics
   TO workos_bot;
GRANT INSERT ON work.events, work.actions, work.outcomes, work.identities, work.outbox TO workos_bot;
GRANT INSERT, UPDATE ON work.bot_topics TO workos_bot;
GRANT UPDATE (meta, context_id) ON work.events TO workos_bot;     -- поправка класса и контекста захвата
GRANT UPDATE (status, approved_at, approval_nonce, approved_hash, decided_at)
   ON work.actions TO workos_bot;
GRANT UPDATE (status) ON work.identity_candidates TO workos_bot;
GRANT UPDATE (sent_at, tg_message_id) ON work.outbox TO workos_bot;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA work TO workos_bot;

-- читатели и исполнители кладут сообщения в outbox, но отправить не могут
GRANT INSERT ON work.outbox TO workos_reader, workos_exec;
GRANT USAGE ON SEQUENCE work.outbox_id_seq TO workos_reader, workos_exec;

-- исполнитель: только колонки исполнения, одобрить не может
REVOKE UPDATE ON work.actions FROM workos_exec;
GRANT UPDATE (status, executed_at, error) ON work.actions TO workos_exec;
REVOKE INSERT ON work.outcomes FROM workos_exec;

-- учёт вызовов модели (правило 04): таблица общая с доменами
DO $$
BEGIN
  IF to_regclass('public.usage_log') IS NOT NULL THEN
    GRANT INSERT ON public.usage_log TO workos_reader, workos_bot;
  END IF;
END $$;

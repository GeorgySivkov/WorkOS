#!/usr/bin/env bash
# Проверка, что бэкап разворачивается (готово, когда «развернул бэкап в чистую базу и он открылся»).
#   bash deploy/restore_check.sh /var/backups/workos/workos-....dump.age ~/age-key.txt
# Ключ age держи у себя и передавай на время проверки. Создаёт базу workos_restore_check
# и удаляет её при любом исходе. Любая ошибка pg_restore валит проверку.
set -euo pipefail
set -a; . "${WORKOS_ENV_ADMIN:-/opt/workos/.env.admin}"; set +a
dump="${1:?путь к .dump.age}"; key="${2:?путь к приватному ключу age}"
DB="${RESTORE_DB:-workos_restore_check}"
tmp=$(mktemp -d)
cleanup() {
  rm -rf "$tmp"
  psql -XAtq -c "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1 || echo "не смог удалить базу $DB, удали руками" >&2
}
trap cleanup EXIT

age -d -i "$key" "$dump" > "$tmp/db.dump"
psql -XAtq -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
# дамп по схемам не содержит CREATE EXTENSION, а halfvec-колонки без pgvector не создать
for ext in ${RESTORE_EXTENSIONS-vector}; do
  PGDATABASE="$DB" psql -XAtq -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS $ext"
done
# схема public в чистой базе уже есть: её запись из оглавления выкидываем, остальное восстанавливаем
pg_restore -l "$tmp/db.dump" | grep -vE ' SCHEMA - public |COMMENT - SCHEMA public ' > "$tmp/toc.list"
PGDATABASE="$DB" pg_restore --no-owner --no-privileges --exit-on-error -L "$tmp/toc.list" -d "$DB" "$tmp/db.dump"
PGDATABASE="$DB" psql -X -v ON_ERROR_STOP=1 -c "SELECT 'events' t, count(*) FROM work.events
  UNION ALL SELECT 'tasks', count(*) FROM work.tasks UNION ALL SELECT 'xp', count(*) FROM rpg.xp"
echo "восстановление проверено"

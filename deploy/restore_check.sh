#!/usr/bin/env bash
# Проверка, что бэкап разворачивается (готово, когда «развернул бэкап в чистую базу и он открылся»).
#   bash deploy/restore_check.sh /var/backups/workos/workos-....dump.age ~/age-key.txt
# Ключ age держи у себя и передавай на время проверки. Создаёт базу workos_restore_check и удаляет её.
set -euo pipefail
set -a; . /opt/workos/.env.admin; set +a
dump="${1:?путь к .dump.age}"; key="${2:?путь к приватному ключу age}"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
age -d -i "$key" "$dump" > "$tmp/db.dump"
psql -qc "DROP DATABASE IF EXISTS workos_restore_check" -c "CREATE DATABASE workos_restore_check"
PGDATABASE=workos_restore_check pg_restore --no-owner --no-privileges -d workos_restore_check "$tmp/db.dump" || true
PGDATABASE=workos_restore_check psql -c "SELECT 'events' t, count(*) FROM work.events UNION ALL SELECT 'tasks', count(*) FROM work.tasks UNION ALL SELECT 'xp', count(*) FROM rpg.xp"
psql -qc "DROP DATABASE workos_restore_check"
echo "восстановление проверено"

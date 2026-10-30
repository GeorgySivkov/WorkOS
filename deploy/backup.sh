#!/usr/bin/env bash
# Шифрованный бэкап базы (шаг 8.3): pg_dump под админом -> age -> /var/backups/workos.
# Приватный ключ age хранится НЕ на дроплете. Проверка восстановления: deploy/restore_check.sh
set -euo pipefail
set -a; . /opt/workos/.env.admin; set +a
: "${BACKUP_AGE_RECIPIENT:?нет BACKUP_AGE_RECIPIENT в .env.admin}"
DIR="${BACKUP_DIR:-/var/backups/workos}"
install -d -m 700 "$DIR"
f="$DIR/workos-$(date -u +%Y%m%d-%H%M).dump.age"
pg_dump --format=custom --no-owner --schema=work --schema=rpg --schema=public | age -r "$BACKUP_AGE_RECIPIENT" > "$f"
chmod 600 "$f"
find "$DIR" -name 'workos-*.dump.age' -mtime +14 -delete
echo "бэкап: $f ($(du -h "$f" | cut -f1))"

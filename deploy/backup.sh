#!/usr/bin/env bash
# Шифрованный бэкап базы (шаг 8.3): pg_dump под админом -> age -> /var/backups/workos.
# Приватный ключ age хранится НЕ на дроплете. Проверка восстановления: deploy/restore_check.sh
#
# Пишет во временный файл и переименовывает только после удачи всей цепочки (pipefail):
# упавший pg_dump не оставляет обрезанный .age, который выглядит как бэкап.
# Удача отмечается в базе: work.notices 'backup:<дата>'; health.py алертит в «Систему»,
# если свежей отметки нет дольше 26 часов.
set -euo pipefail
umask 077
set -a; . "${WORKOS_ENV_ADMIN:-/opt/workos/.env.admin}"; set +a
: "${BACKUP_AGE_RECIPIENT:?нет BACKUP_AGE_RECIPIENT в .env.admin}"
DIR="${BACKUP_DIR:-/var/backups/workos}"
install -d -m 700 "$DIR"

# pg_dump не снимает дамп с сервера новее себя: ставь postgresql-client-<версия сервера> из PGDG
server=$(psql -XAtqc "SHOW server_version_num")
client=$(pg_dump --version | grep -oE '[0-9]+' | head -n1)
if [ "$client" -lt $((server / 10000)) ]; then
  echo "pg_dump $client старше сервера $((server / 10000)): apt-get install postgresql-client-$((server / 10000)) (см. SETUP.md)" >&2
  exit 1
fi

f="$DIR/workos-$(date -u +%Y%m%d-%H%M).dump.age"
tmp="$f.tmp"
trap 'rm -f "$tmp"' EXIT
pg_dump --format=custom --no-owner --schema=work --schema=rpg --schema=public \
  | age -r "$BACKUP_AGE_RECIPIENT" > "$tmp"
[ -s "$tmp" ] || { echo "бэкап пустой, не сохраняю" >&2; exit 1; }
mv "$tmp" "$f"
chmod 600 "$f"

psql -XAtq -v ON_ERROR_STOP=1 \
  -c "INSERT INTO work.notices (key) VALUES ('backup:$(date -u +%F)') ON CONFLICT (key) DO UPDATE SET sent_at = now()" \
  -c "DELETE FROM work.notices WHERE key LIKE 'backup:%' AND sent_at < now() - interval '30 days'"
find "$DIR" -name 'workos-*.dump.age' -mtime +14 -delete
find "$DIR" -name 'workos-*.dump.age.tmp' -mmin +180 -delete     # остатки прогона, убитого SIGKILL
echo "бэкап: $f ($(du -h "$f" | cut -f1))"

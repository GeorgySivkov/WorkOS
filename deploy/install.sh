#!/usr/bin/env bash
# Установка и обновление WorkOS на дроплете. Идемпотентно, можно гонять после каждого git pull.
#   sudo bash deploy/install.sh
# Ничего не включает само: сервисы и таймеры включаются по фазам через deploy/phase.sh.
set -euo pipefail

APP=/opt/workos
cd "$APP"

say() { printf '\n== %s\n' "$*"; }

say "Пользователи ОС"
mkuser() {  # имя, домашний каталог
  if ! id "$1" >/dev/null 2>&1; then
    useradd --system --home-dir "$2" --shell /usr/sbin/nologin "$1"
    echo "  создан $1"
  fi
  install -d -m 700 -o "$1" -g "$1" "$2"
}
mkuser workos-reader /var/lib/workos-reader
mkuser workos-exec   /var/lib/workos-exec
mkuser workos-bot    /var/lib/workos-bot
mkuser workos-app    /var/lib/workos-app
groupadd -f workos
for u in workos-reader workos-exec workos-bot workos-app; do usermod -aG workos "$u"; done

say "Права на код: root пишет, группа workos читает, остальные никто"
chown -R root:workos "$APP"
chmod -R u+rwX,g+rX,g-w,o-rwx "$APP"

say "Файлы окружения: каждый читает только свой пользователь"
for pair in reader:workos-reader bot:workos-bot executor:workos-exec app:workos-app; do
  f="$APP/.env.${pair%%:*}"; u="${pair##*:}"
  if [ -f "$f" ]; then chown "$u:$u" "$f"; chmod 600 "$f"; echo "  $f -> $u 600"; else echo "  нет $f (заполни из deploy/env/)"; fi
done
if [ -f "$APP/.env.admin" ]; then chown root:root "$APP/.env.admin"; chmod 600 "$APP/.env.admin"; fi

say "Python-окружение"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ -f requirements-optional.txt ] && [ "${WITH_GARMIN:-0}" = "1" ]; then .venv/bin/pip install -q -r requirements-optional.txt; fi
chown -R root:workos .venv && chmod -R g+rX,o-rwx .venv

say "Каталоги моделей и состояния"
install -d -m 750 -o root -g workos "$APP/models"
install -d -m 750 -o root -g workos "$APP/certs"

say "systemd: сервисы"
install -m 644 deploy/systemd/*.service /etc/systemd/system/

say "systemd: таймеры читателей (время по Мадриду)"
timer() {  # команда workctl, расписание, догонять ли пропущенный запуск
  cat > "/etc/systemd/system/workos-$1.timer" <<EOF
[Unit]
Description=WorkOS: $1

[Timer]
OnCalendar=$2
Persistent=${3:-false}
RandomizedDelaySec=20
Unit=workos@$1.service

[Install]
WantedBy=timers.target
EOF
}
timer collect   '*:0/15'
timer people    '*:4/15'
timer extract   '*:6/15'
timer triage    '*:8/15'
timer draft     '*:10/15'
timer dayplan   '*:2/15'
timer health    '*:12/15'
timer cards     '*:0/5'
timer classify  '*:1/5'
timer award     '*:3/5'
timer autonomy  'hourly'
timer garmin    '*:20'
timer plan      '*-*-* 00:00:00 Europe/Madrid' true
timer brief     '*-*-* 08:00:00 Europe/Madrid' true
timer embed     '*-*-* 03:30:00 Europe/Madrid' true
timer review    'Sun *-*-* 19:00:00 Europe/Madrid' true
timer audit     'Sun *-*-* 19:30:00 Europe/Madrid' true
timer weekly    'Sun *-*-* 20:00:00 Europe/Madrid' true
cat > /etc/systemd/system/workos-backup.timer <<'EOF'
[Unit]
Description=WorkOS: шифрованный бэкап базы

[Timer]
OnCalendar=*-*-* 04:30:00 Europe/Madrid
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
say "Готово. Включай по фазам: sudo bash deploy/phase.sh 1"

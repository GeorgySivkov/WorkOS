#!/usr/bin/env bash
# Установка и обновление WorkOS на дроплете. Идемпотентно, можно гонять после каждого git pull.
#   sudo bash deploy/install.sh
# Ничего не включает само: сервисы и таймеры включаются по фазам через deploy/phase.sh.
# Переменные: PG_MAJOR=18 (версия клиента PostgreSQL, иначе спросим сервер), NO_SWAP=1 (не трогать своп),
# WITH_GARMIN=1 (поставить garminconnect).
set -euo pipefail

APP=/opt/workos
cd "$APP"

say() { printf '\n== %s\n' "$*"; }

say "Секреты: закрыть до раздачи прав группе"
# .env*, json-ключи и сертификаты в корне (client_secret.json, .env.bot.save, .env.reader.bak ...)
# закрываются первыми и не попадают в рекурсивный chmod ниже: окна, когда их читает группа, нет.
secret_top() {
  find "$APP" -maxdepth 1 -type f \( -name '.env*' -o -name '*.json' -o -name '*.key' -o -name '*.pem' \
       -o -name '*.p12' -o -name '*.save' -o -name '*.bak' -o -name '*.swp' -o -name '*~' \) "$@"
}
secret_top -exec chmod go-rwx {} +        # владельца не меняем: .env.<роль> остаётся читаемым своему сервису

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
# claude CLI пишет конфиг и кеш в $HOME: под ProtectSystem=strict это только /var/lib/<user>
for u in workos-reader workos-bot; do
  for d in .claude .cache .config; do install -d -m 700 -o "$u" -g "$u" "/var/lib/$u/$d"; done
done

say "Права на код: root пишет, группа workos читает, остальные никто (секреты и certs/ не трогаем)"
find "$APP" \( -path "$APP/certs" -o \( -path "$APP/*" ! -path "$APP/*/*" -type f \( -name '.env*' \
       -o -name '*.json' -o -name '*.key' -o -name '*.pem' -o -name '*.p12' -o -name '*.save' -o -name '*.bak' \
       -o -name '*.swp' -o -name '*~' \) \) \) -prune \
     -o -exec chown root:workos {} + -exec chmod u+rwX,g+rX,g-w,o-rwx {} +

say "Файлы окружения: каждый читает только свой пользователь"
for pair in reader:workos-reader bot:workos-bot executor:workos-exec app:workos-app; do
  f="$APP/.env.${pair%%:*}"; u="${pair##*:}"
  if [ -f "$f" ]; then chown "$u:$u" "$f"; chmod 600 "$f"; echo "  $f -> $u 600"; else echo "  нет $f (заполни из deploy/env/)"; fi
done
if [ -f "$APP/.env.admin" ]; then chown root:root "$APP/.env.admin"; chmod 600 "$APP/.env.admin"; fi

say "Сертификаты: каталог читает группа, приватные ключи только root"
install -d -m 750 -o root -g workos "$APP/certs"
find "$APP/certs" -type f | while read -r c; do
  if grep -q "PRIVATE KEY" "$c" 2>/dev/null; then chown root:root "$c"; chmod 600 "$c"
  else chown root:workos "$c"; chmod 640 "$c"; fi
done

say "Системные пакеты: ffmpeg, age, клиент PostgreSQL под версию сервера"
command -v ffmpeg >/dev/null && command -v age >/dev/null || { apt-get update -q && apt-get install -y -q ffmpeg age; }
pg_major="${PG_MAJOR:-}"
if [ -z "$pg_major" ] && [ -f "$APP/.env.admin" ] && command -v psql >/dev/null; then
  v=$( (set -a; . "$APP/.env.admin"; set +a; psql -XAtqc "SHOW server_version_num") 2>/dev/null || true)
  if [[ "$v" =~ ^[0-9]+$ ]]; then pg_major=$((v / 10000)); fi
fi
pg_major="${pg_major:-18}"     # управляемый Postgres у DO сейчас 15-18; клиент новее сервера можно
have=$(command -v pg_dump >/dev/null && pg_dump --version | grep -oE '[0-9]+' | head -n1 || echo 0)
if [ "$have" -lt "$pg_major" ]; then
  # клиент Ubuntu старше сервера: pg_dump откажется снимать дамп. Ставим из apt.postgresql.org (PGDG)
  if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
    apt-get install -y -q curl ca-certificates
    install -d /usr/share/postgresql-common/pgdg
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list
    apt-get update -q
  fi
  apt-get install -y -q "postgresql-client-$pg_major"
fi
echo "  $(pg_dump --version)"

say "Своп 2 ГБ: на дроплете 1 ГБ без него OOM убивает бота, когда работают модель и эмбеддинги"
if [ "${NO_SWAP:-0}" != "1" ] && [ -z "$(swapon --noheadings --show=NAME 2>/dev/null)" ]; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/60-workos-swap.conf
  sysctl -q -p /etc/sysctl.d/60-workos-swap.conf
  echo "  своп включён"
else
  echo "  своп уже есть (или NO_SWAP=1): $(swapon --noheadings --show=NAME,SIZE 2>/dev/null | tr '\n' ' ')"
fi

say "Python-окружение"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ -f requirements-optional.txt ] && [ "${WITH_GARMIN:-0}" = "1" ]; then .venv/bin/pip install -q -r requirements-optional.txt; fi
chown -R root:workos .venv && chmod -R g+rX,g-w,o-rwx .venv

say "Каталоги моделей"
install -d -m 750 -o root -g workos "$APP/models"

say "systemd: сервисы"
install -m 644 deploy/systemd/*.service /etc/systemd/system/
for d in deploy/systemd/*.service.d; do
  [ -d "$d" ] || continue
  install -d -m 755 "/etc/systemd/system/$(basename "$d")"
  install -m 644 "$d"/*.conf "/etc/systemd/system/$(basename "$d")/"
done

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
# plan и brief догоняют пропуск (Persistent): дроплет перезагрузился в 00:00, план всё равно будет
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

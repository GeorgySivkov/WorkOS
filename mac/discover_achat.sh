#!/bin/bash
# Поиск хранилища А-Чата на Маке (шаг 7.2). Ничего не меняет, только печатает кандидатов.
#
#   bash discover_achat.sh                 # имя приложения ищется по подстроке «чат»
#   bash discover_achat.sh "A-Chat"        # своя подстрока имени
#   MINUTES=5 bash discover_achat.sh       # окно «изменилось за N минут» (по умолчанию 2)
#
# Порядок: открой А-Чат, отправь себе сообщение и сразу запусти скрипт.
# Найденные пути запиши в contexts.config для alfa и в ~/.workos/achat.json.
set -u
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

PATTERN="${1:-чат}"
MINUTES="${MINUTES:-2}"

hr() { printf '\n== %s ==\n' "$1"; }

hr "1. Файлы в ~/Library, изменённые за последние ${MINUTES} мин (без кешей и логов)"
find "$HOME/Library" -type f -mmin "-${MINUTES}" 2>/dev/null \
  | grep -viE '/caches?/|/logs?/|\.log$|/Cookies|/WebKit/|/Saved Application State/|com\.apple\.' \
  | head -200

hr "2. Приложение и bundle id (в Dock имя может отличаться)"
APPS=()
while IFS= read -r app; do APPS+=("$app"); done < <(
  find /Applications "$HOME/Applications" -maxdepth 2 -iname "*${PATTERN}*.app" 2>/dev/null)
if [ "${#APPS[@]}" -eq 0 ]; then
  echo "не нашёл *${PATTERN}*.app, попробуй другую подстроку: bash $0 'Alfa'"
fi
BUNDLES=()
for app in "${APPS[@]+"${APPS[@]}"}"; do
  plist="$app/Contents/Info.plist"
  exe=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$plist" 2>/dev/null)
  bid=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist" 2>/dev/null)
  echo "приложение: $app"
  echo "  исполняемый файл: ${exe:-?}"
  echo "  bundle id:        ${bid:-?}"
  if [ -d "$app/Contents/Frameworks/Electron Framework.framework" ]; then
    echo "  это Electron: данные скорее в IndexedDB/leveldb, а не в SQLite"
  fi
  [ -n "$bid" ] && BUNDLES+=("$bid")
done

hr "3. Открытые приложением файлы баз (по PID, А-Чат должен быть запущен)"
PIDS=$(pgrep -if "$PATTERN" 2>/dev/null || true)
if [ -z "$PIDS" ]; then
  echo "процесс по подстроке «${PATTERN}» не найден"
else
  for pid in $PIDS; do
    echo "PID $pid: $(ps -p "$pid" -o comm= 2>/dev/null)"
    lsof -p "$pid" 2>/dev/null | grep -Ei '\.db|\.sqlite|leveldb|IndexedDB|\.realm' | awk '{ $1=$2=$3=$4=$5=$6=$7=$8=""; print "  " $0 }' | sort -u
  done
fi

hr "4. Куда смотреть руками"
for bid in "${BUNDLES[@]+"${BUNDLES[@]}"}"; do
  for d in "$HOME/Library/Containers/$bid/Data" "$HOME/Library/Application Support/$bid"; do
    [ -d "$d" ] && echo "есть: $d"
  done
done
ls -d "$HOME/Library/Application Support/"*"${PATTERN}"* 2>/dev/null
ls -d "$HOME/Library/Group Containers/"* 2>/dev/null | grep -iE "${PATTERN}|alfa|achat" || true

hr "5. SQLite-кандидаты (только чтение) и их таблицы"
for bid in "${BUNDLES[@]+"${BUNDLES[@]}"}"; do
  for root in "$HOME/Library/Containers/$bid" "$HOME/Library/Application Support/$bid"; do
    [ -d "$root" ] || continue
    find "$root" -type f \( -iname '*.sqlite' -o -iname '*.db' -o -iname '*.sqlite3' -o -iname '*.store' \) 2>/dev/null \
      | while IFS= read -r f; do
          echo "$f"
          sqlite3 "file:${f}?mode=ro" '.tables' 2>/dev/null | sed 's/^/    /' | head -10
        done
  done
done

hr "6. Фолбэк: база уведомлений macOS (нужен Full Disk Access для Терминала)"
NDB="$HOME/Library/Group Containers/group.com.apple.usernoted/db2/db"
if [ -r "$NDB" ]; then
  sqlite3 "file:${NDB}?mode=ro" "SELECT identifier FROM app ORDER BY identifier;" 2>/dev/null \
    | grep -iE "${PATTERN}|alfa|achat|chat" || echo "в уведомлениях нет приложения по подстроке"
else
  echo "нет доступа к $NDB: System Settings > Privacy & Security > Full Disk Access > Терминал"
fi

echo
echo "Дальше: путь к базе и запрос (id, ts, chat, author, text) в ~/.workos/achat.json,"
echo "проверка: python3 mac/achat_collector.py --dry-run"

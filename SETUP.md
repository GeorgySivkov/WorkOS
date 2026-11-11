# Как завести WorkOS

Ветка `build/all-phases` содержит код всех фаз плана. Включается он по фазам, как в плане:
следующая фаза только после недели спокойной работы предыдущей. Код для фазы 5 уже лежит
на дроплете, но спит, пока ты не включишь её одной командой.

Везде ниже «дроплет» это твой DO-дроплет с `/opt/workos`, «Мак» это ноутбук.
Команды на дроплете выполняются под root.

**Python.** На дроплете нужен 3.10 или новее: Ubuntu 22.04 даёт 3.10, 24.04 даёт 3.12, оба подходят
(`python3 --version`). На Маке ничего ставить не надо: `tools/google_auth.py` и `mac/achat_collector.py`
написаны на одной стандартной библиотеке и работают на системном `/usr/bin/python3` (3.9+).

---

## 0. Один раз: забрать код и поставить

**На Маке** (ветка уже лежит в `~/WorkOS`):
```bash
cd ~/WorkOS && git log --oneline -5 && git push -u origin build/all-phases
```
Посмотри дифф на GitHub, потом смёрджи в main (или работай с веткой).

**На дроплете:**
```bash
cd /opt/workos && git fetch && git checkout build/all-phases && git pull
apt-get update && apt-get install -y python3-venv     # остальное (ffmpeg, age, клиент Postgres) ставит install.sh
cp deploy/env/admin.example .env.admin && nano .env.admin   # doadmin + пароли новых ролей
bash deploy/install.sh                                 # пользователи, права, пакеты, своп, venv, systemd
WORKOS_ROLE=admin .venv/bin/python migrate.py          # миграции 002-012
WORKOS_ROLE=admin .venv/bin/python migrate.py --status # всё с галочками
```
`migrate.py` сам увидит, что схема из шага 0.3 уже стоит, и не будет её трогать.
Пароли ролей `workos_bot` и `workos_app` придумай сам: `openssl rand -base64 24`.

Что делает `install.sh` кроме пользователей и прав:
- **Клиент PostgreSQL под версию сервера.** Управляемый Postgres у DO новее клиента из Ubuntu, а `pg_dump`
  отказывается снимать дамп с сервера новее себя (бэкап молча не делался бы). Скрипт спрашивает у сервера
  версию (`SHOW server_version_num` под `.env.admin`) и ставит `postgresql-client-<версия>` из
  apt.postgresql.org (PGDG). Без `.env.admin` ставит 18; свою версию можно задать `PG_MAJOR=17 bash deploy/install.sh`.
  Проверка: `pg_dump --version` не меньше версии сервера.
- **Своп 2 ГБ** (`/swapfile`, swappiness 10), если свопа нет: у DO его нет по умолчанию, а на 1 ГБ модель,
  эмбеддинги и бот вместе не помещаются. Повторный запуск ничего не меняет; не трогать своп: `NO_SWAP=1`.
- **Секреты закрываются первыми.** Всё в корне `/opt/workos`, что похоже на секрет (`.env*`, `*.json`,
  `*.pem`, `*.key`, `*.save`, `*.bak`), сначала закрывается от группы и остальных (`chmod go-rwx`) и не попадает
  в общий chmod группы, так что окна, когда его читает группа, нет; потом `.env.<роль>` отдаются своему пользователю. В `certs/` файлы с приватным ключом только для root.

**Claude CLI** (боту и читателям нужна модель, как у JobBot):
```bash
npm install -g @anthropic-ai/claude-code   # если ещё не стоит
claude setup-token                          # на Маке, если токена нет; он кладётся в CLAUDE_CODE_OAUTH_TOKEN
```

Проверка прав (должны быть одни галочки). Файлы `.env.bot`, `.env.app`, `.env.executor` появятся
только в фазах 1 и 2, поэтому цикл пропускает те, которых ещё нет; повтори его в конце фазы 1 и фазы 2:
```bash
for r in reader bot executor app; do
  [ -f .env.$r ] || { echo "нет .env.$r, пропускаю"; continue; }
  sudo -u workos-${r/executor/exec} WORKOS_ROLE=$r .venv/bin/python workctl.py sql-check
done
```

---

## Фаза 1. Терминал: бот и мини-приложение

1. **Замер «до» (1.0).** Четыре цифры в `claude/baseline.md` с датой: часы в неделю на разбор входящих,
   время до первого ответа, сколько обещаний потерялось за месяц, сколько раз в день открываешь мессенджеры.
2. **Бот.** В @BotFather: `/newbot`, имя любое, токен в `.env.bot`. Там же в настройках бота
   включи темы в личке (Bot Settings → Threaded Mode). Свой user id узнай у @userinfobot.
   Напиши новому боту `/start`, иначе он не сможет написать первым.
3. **HTTPS для мини-приложения.** Tailscale на дроплете:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
   tailscale funnel --bg 8787
   tailscale funnel status          # адрес вида https://<имя>.<tailnet>.ts.net
   ```
4. **Окружения:**
   ```bash
   cp deploy/env/bot.example .env.bot && nano .env.bot   # токен, owner id, APP_URL = адрес из funnel
   cp deploy/env/app.example .env.app && nano .env.app   # TELEGRAM_BOT_ID = цифры до «:» в токене
   bash deploy/install.sh                                 # выставит владельцев и 600
   bash deploy/phase.sh 1
   ```
5. **Голос (вторая половина 1.2):**
   ```bash
   git clone https://github.com/ggml-org/whisper.cpp /opt/whisper.cpp && cd /opt/whisper.cpp
   cmake -B build && cmake --build build -j2 --config Release
   bash models/download-ggml-model.sh base-q5_1
   chmod -R a+rX /opt/whisper.cpp
   ```
6. **Готово, когда:**
   - в чате с ботом пять тем (Входящие, Решения, План, Система, Спросить), сообщение в любую он подтверждает в той же теме;
   - «позвонить в банк в пт в 10» во «Входящих» стало задачей, видно в мини-приложении (кнопка «Квесты»);
   - `sudo -u workos-bot cat /opt/workos/.env.executor` отвечает Permission denied;
   - `curl -s https://<адрес>/api/today` отвечает 401, а `/api/health` отвечает `{"ok": true}`;
   - после `reboot` бот поднялся сам;
   - проверка прав из шага 0 для reader, bot и app даёт одни галочки.
7. **Вечерний план до фазы 2 (мост 1.4):** вставляешь секцию «Завтра» из вечернего плана Cowork
   в тему «План», бот отвечает карточкой «В список · Выбрать · Не надо».

---

## Персонаж

1. Сетка заполнена 11.11: полный JSON с источниками Garmin, оценкой дня и наградами лежит в проекте
   PersonalOS как `claude/mandarat.json`. Скачай его, положи на дроплет в `/opt/workos/mandarat.json` и засей:
   ```bash
   WORKOS_ROLE=admin .venv/bin/python workctl.py seed-grid mandarat.json
   ```
   Повторный запуск безопасен: клетки обновятся, убранные станут неактивными, опыт не пропадёт.
   Награды и источники уже внутри JSON, флаги `--auto` и `--reward` нужны, только чтобы что-то поменять.
2. **Garmin (П.4):** `WITH_GARMIN=1 bash deploy/install.sh`. Впиши в `.env.reader` на время входа
   `GARMIN_EMAIL=...` и `GARMIN_PASSWORD=...` (строки есть в `deploy/env/reader.example`), затем один раз
   `sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python workctl.py garmin-login`
   (если включена MFA, он спросит код). Токены лягут в `GARMIN_TOKENS`, после этого пароль из `.env.reader` сотри.
   Клетки уже привязаны в JSON (сон, шаги, силовые, сноуборд, хайкинг, велосипед, прогулки).
3. **Оценка дня:** ползунок от -10 до +10 на «Сегодня» в мини-приложении, или `/day +3 заметка` в боте;
   после 00:30, если день не оценён, бот сам спросит «Как прошёл день?» с кнопками.
   Привычка «Оценка дня» и этап «Месяц со средней оценкой +3» засчитываются сами.
4. `bash deploy/phase.sh persona`
5. **Готово, когда:** отметка задачи в мини-приложении дала опыт, `/me` в теме «План» показывает те же цифры,
   первое повышение уровня пришло сообщением.

---

## Фаза 2. Дневной контур

1. **Google.** В console.cloud.google.com: проект, включить Gmail API и Calendar API, OAuth consent screen
   перевести в **In production** (иначе токен умрёт через 7 дней), Credentials → OAuth client ID → Desktop app,
   скачать `client_secret.json` на Мак. Токен записи (календарь и черновики):
   ```bash
   python3 tools/google_auth.py --client ~/Downloads/client_secret.json --scopes write
   ```
   → `GOOGLE_WRITE_REFRESH_TOKEN` в `.env.executor`. Скрипт на одной стандартной библиотеке: системный
   `python3` на Маке подходит, venv и `pip install` не нужны. `client_secret.json` на дроплет не копируй.
2. **Календари.** В личном Google создай календари «Инфатика», «Альфа», «План» (Другие календари → +).
   Их id (Настройки → Интеграция → Идентификатор) в `GCAL_MIRROR_MAP` и `GCAL_PLAN_CALENDAR` в `.env.executor`.
   Руками в них ничего не добавляй: зеркало удаляет всё, чего нет в источнике.
3. **iCal.** Google Календарь → Настройки → основной календарь → «Секретный адрес в формате iCal»
   → `ICAL_PERSONAL_URL`. То же для календаря Инфатики → `ICAL_INFATICA_URL`.
4. **Trello (чтение).** Ключ на trello.com/power-ups/admin, токен:
   `https://trello.com/1/authorize?expiration=never&scope=read&response_type=token&key=<KEY>`.
   Проверка: `sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -m collectors.trello whoami`.
5. **Plaud.** web.plaud.ai → DevTools → Network → любой запрос к api.plaud.ai → заголовок Authorization
   → `PLAUD_TOKEN`. API неофициальный, токен иногда придётся обновлять.
6. **Slack.** api.slack.com/apps → Create App в воркспейсе Инфатики → User Token Scopes:
   `channels:history`, `groups:history`, `search:read`, `users:read` → Install → `xoxp-...` в `SLACK_TOKEN`.
7. **Альфа (EAS, 2.4).**
   - DeviceId: `python3 -c "import secrets; print('WorkOS' + secrets.token_hex(8).upper())"` → `ALFA_EAS_DEVICE_ID`.
   - Сертификат: на Маке «Связка ключей» → Система/Системные корневые → «Russian Trusted Root CA»
     (и «Russian Trusted Sub CA», если есть) → Экспорт в .pem, склеить в
     `/opt/workos/certs/russian_trusted_root_ca.pem`. Проверку TLS не отключать.
   - Письмо в ИТ **до** первого подключения (см. ниже). Пока ИТ не одобрило устройство, источник
     `eas:alfa` не включай: `phase.sh 2` включает и юнит `workos-eas`, но при выключенном источнике он
     выходит с кодом 3 и systemd его не перезапускает. Включаешь источник (шаг 8) только после ответа ИТ,
     потом `systemctl restart workos-eas`.
   - Проба и первый прогон:
     ```bash
     sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -m collectors.eas --probe
     sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -m collectors.eas --once
     ```
8. **Включить источники:**
   ```sql
   UPDATE work.sources SET enabled = true, config = '{"board_id":"6a197760b7795b2a91550ec0"}' WHERE id = 'trello:george';
   UPDATE work.sources SET enabled = true WHERE id IN ('plaud','ical:personal','ical:infatica');
   UPDATE work.sources SET enabled = true WHERE id = 'eas:alfa';      -- только после одобрения ИТ
   UPDATE work.sources SET enabled = true, config = '{"channels":["C..."],"user_id":"U..."}' WHERE id = 'slack:infatica';
   ```
   (под админом: `set -a; . .env.admin; set +a; psql`). Потом `bash deploy/phase.sh 2`.
9. **Cowork-планировщик:** когда план с дроплета три вечера подряд приходит в 00:00 и не хуже нынешнего,
   выключи расписание вечерней задачи в Cowork (саму задачу оставь для разговоров с планом).
10. **Сторож:** healthchecks.io → новый check с периодом 10 минут → URL в `HEALTHCHECKS_URL` (`.env.bot`).
11. **Готово, когда:** вчерашний день целиком в базе (`workctl.py status`), перенесённая встреча Инфатики
    или Альфы через 15 минут переехала в личный календарь, план дня встал блоками в «План»,
    выключенный руками источник через час дал алерт в «Системе», а проверка прав из шага 0
    для всех четырёх ролей даёт одни галочки.

**Письмо в ИТ Альфы** (поправь под себя):
> Коллеги, привет. Подключаю к своему ящику MOSCOW\U_M2PH5 ещё один клиент ActiveSync, это мой
> персональный помощник для календаря и почты. Параметры устройства: DeviceType WorkOS, DeviceId <твой id>,
> User-Agent WorkOS/1.0. Одобрите его, пожалуйста, в списке устройств. Если политика требует PIN или
> шифрование, клиент честно ответит, что не поддерживает, поэтому нужно разрешение на устройство
> без полной поддержки политик. Ещё одно: названия, время и место встреч из календаря будут копироваться
> в мой личный Google Календарь, без участников и описаний. Если с этим есть вопросы, скажите, переключу на режим «занят».

---

## Фаза 3. Память

1. Gmail на чтение: `python3 tools/google_auth.py --client ... --scopes read-gmail` → `GMAIL_PERSONAL_REFRESH_TOKEN`
   в `.env.reader`, `UPDATE work.sources SET enabled = true WHERE id = 'gmail:personal';`
2. Модель эмбеддингов:
   ```bash
   .venv/bin/python tools/fetch_embed_model.py && chmod -R a+rX models
   sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -c "from core import embed; print(len(embed.embed(['проверка'])[0]))"   # 768
   ```
   Модель берёт 400-500 МБ при работе. На дроплете 1 ГБ это тесно: ночной `workctl.py embed` ограничен
   (MemoryMax 800 МБ в `deploy/systemd/workos@embed.service.d/`, не дольше 40 минут за ночь, первый бэкфилл
   за 90 дней доедет за несколько ночей), а все команды с моделью идут по одной через общий замок.
   Смотри `free -m` во время `workctl.py embed`. Если уходит в своп, подними дроплет до 2 ГБ (+6 $ в месяц)
   и тогда же можно включить `WORKOS_ASK_VECTOR=1` в `.env.bot` (по умолчанию выключено).
3. Проверь `WORKOS_SELF_EMAILS` в `.env.reader`: иначе ты станешь «человеком» на каждой встрече.
4. `bash deploy/phase.sh 3`. Карточки «Кто это?» по именам из встреч приходят во «Входящие».
5. **Готово, когда:** в теме «Спросить» на вопрос про июньские решения по SDK пришёл ответ со ссылками, и он верный;
   за 30 минут до встречи пришла карточка.

## Фаза 4. Извлечение
`bash deploy/phase.sh 4`. Обещания со сроком из встреч приходят карточками «В список».
Готово, когда на пяти встречах извлечённое совпало с твоим минимум в четырёх из пяти.

## Фаза 5. Действие
1. Trello с записью: токен `scope=read,write` → `TRELLO_WRITE_TOKEN` в `.env.executor`;
   `TRELLO_PROPOSE_LIST_ID` (куда предлагать карточки) в `.env.reader`.
2. Slack с записью: в том же приложении User Token Scope `chat:write` → `SLACK_WRITE_TOKEN` в `.env.executor`.
3. Gmail: исполнитель только создаёт черновики, токен тот же `GOOGLE_WRITE_REFRESH_TOKEN`.
   Ответы на письма Альфы приходят текстом в «Решения», отправляешь сам.
4. `bash deploy/phase.sh 5`.

## Фаза 6. Обучение
Заполни `style/infatica.md` и `style/alfa.md` своими реальными текстами (5-15 штук), потом `bash deploy/phase.sh 6`.
`/manual` в боте возвращает всё в ручное подтверждение, `/auto` снимает замок.

## Фаза 7. А-Чат
На Маке: открыть А-Чат, отправить себе сообщение, `bash mac/discover_achat.sh`. Найденный путь и запрос
в `~/.workos/achat.json` (пример в `mac/achat_collector.py`), проверка `python3 mac/achat_collector.py --dry-run`,
установка LaunchAgent из `deploy/launchd/` (команды в комментарии внутри plist).

**Полный доступ к диску.** База А-Чата лежит в `~/Library/Containers/...`, а macOS не даёт фоновым
процессам туда читать. Терминал у тебя, скорее всего, уже с доступом, поэтому `--dry-run` из него работает,
а LaunchAgent нет. Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску → «+»,
Cmd+Shift+G, `/usr/bin/python3`, включить. Если в `~/Library/Logs/workos-achat.log` всё ещё
«Operation not permitted», добавь так же путь из `python3 -c 'import sys; print(sys.executable)'`
(настоящий Python из Command Line Tools) и перезапусти агент:
`launchctl kickstart -k gui/$(id -u)/com.workos.achat`.

На дроплете `INGEST_TOKEN` и `INGEST_BIND` (tailnet-адрес),
`UPDATE work.sources SET enabled = true WHERE id = 'achat:alfa';`, `bash deploy/phase.sh 7`.

## Фаза 8. Бэкапы и аудит
1. `age-keygen -o ~/workos-age.key` **на Маке**, публичный ключ в `BACKUP_AGE_RECIPIENT` (`.env.admin`).
2. Проверь клиент: `pg_dump --version` не меньше версии сервера (`install.sh` ставит нужный из PGDG).
3. Первый бэкап руками, с открытым выводом: `bash deploy/backup.sh`. Он пишет во временный файл и
   переименовывает только после удачи всей цепочки, так что упавший `pg_dump` не оставит обрезанный `.age`.
   Удача отмечается в базе (`work.notices`, ключ `backup:<дата>`).
4. `bash deploy/phase.sh 8`. С первой отметки heartbeat следит за бэкапом: нет свежего дольше 26 часов,
   в «Систему» приходит алерт. Если выключишь фазу 8 насовсем, алерт придёт через сутки: убери отметки
   `DELETE FROM work.notices WHERE key LIKE 'backup:%';` под админом.
5. Через сутки проверь восстановление: скопируй ключ на дроплет на время проверки и
   `bash deploy/restore_check.sh <файл> <ключ>`, потом удали ключ. Скрипт создаёт `workos_restore_check`,
   ставит туда расширение `vector` (дамп по схемам его не содержит), восстанавливает с остановкой на первой
   ошибке и удаляет базу при любом исходе. Готово, когда он напечатал счётчики и «восстановление проверено».

---

## Обновление кода
```bash
cd /opt/workos && git pull && bash deploy/install.sh && WORKOS_ROLE=admin .venv/bin/python migrate.py
systemctl restart 'workos-*.service'
```

## Когда что-то не так
```bash
bash deploy/phase.sh status
journalctl -u workos-bot -n 100 --no-pager
journalctl -u 'workos@collect' --since today
sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python workctl.py status
```
- Модель не справилась с элементом три раза (встреча, ответ, карточки, триаж): он пропускается, а в
  «Систему» приходит одно сообщение с ошибкой. Повторить встречу: удалить её отметку
  (`DELETE FROM work.notices WHERE key = 'extract:<id>';` под админом).
- «пропускаю прогон, модель занята» в журнале таймера: команды с моделью идут по одной, этот прогон
  подождал замок и уступил, следующий таймер попробует снова. Часто: смотри, кто держит замок долго
  (`journalctl -u 'workos@*' --since -1h`).

## Что проверено, а что нет
- 269 автотестов на локальном Postgres (`python3 -m pytest -q`): права ролей, захват, карточки и хеш одобрения,
  мини-приложение, расстановка дня (включая переход часов 25.10), опыт, коллекторы и исполнители на поддельных API,
  разрешение имён, потолок попыток модели, настоящий `backup.sh` и `restore_check.sh` против тестовой базы.
- `install.sh` (PGDG, своп, права) и юниты systemd проверены только статически: первый прогон на дроплете
  смотри глазами.
- С живыми Telegram, Google, Exchange Альфы, Plaud и Slack код ещё не встречался. Первый запуск каждой
  фазы стоит делать с открытым `journalctl`.
- Plaud API неофициальный. В EAS таблицы токенов для Settings и части Provision записаны по документации
  без живой сверки: если `--probe` скажет «Exchange не понял запрос Provision», это туда.
- Модель эмбеддингов в тестах не загружалась (стоит подмена), реальный расход памяти измерь сам.

# Как завести WorkOS

Ветка `build/all-phases` содержит код всех фаз плана. Включается он по фазам, как в плане:
следующая фаза только после недели спокойной работы предыдущей. Код для фазы 5 уже лежит
на дроплете, но спит, пока ты не включишь её одной командой.

Везде ниже «дроплет» это твой DO-дроплет с `/opt/workos`, «Мак» это ноутбук.
Команды на дроплете выполняются под root.

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
apt-get install -y ffmpeg age postgresql-client       # голос, бэкапы, pg_dump
cp deploy/env/admin.example .env.admin && nano .env.admin   # doadmin + пароли новых ролей
bash deploy/install.sh                                 # пользователи, права, venv, systemd
WORKOS_ROLE=admin .venv/bin/python migrate.py          # миграции 002-008
WORKOS_ROLE=admin .venv/bin/python migrate.py --status # всё с галочками
```
`migrate.py` сам увидит, что схема из шага 0.3 уже стоит, и не будет её трогать.
Пароли ролей `workos_bot` и `workos_app` придумай сам: `openssl rand -base64 24`.

**Claude CLI** (боту и читателям нужна модель, как у JobBot):
```bash
npm install -g @anthropic-ai/claude-code   # если ещё не стоит
claude setup-token                          # на Маке, если токена нет; он кладётся в CLAUDE_CODE_OAUTH_TOKEN
```

Проверка прав (должны быть одни галочки):
```bash
for r in reader bot executor app; do sudo -u workos-${r/executor/exec} WORKOS_ROLE=$r .venv/bin/python workctl.py sql-check; done
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
   - в чате с ботом четыре темы, сообщение в любую он подтверждает в той же теме;
   - «позвонить в банк в пт в 10» во «Входящих» стало задачей, видно в мини-приложении (кнопка «Квесты»);
   - `sudo -u workos-bot cat /opt/workos/.env.executor` отвечает Permission denied;
   - `curl -s https://<адрес>/api/today` отвечает 401, а `/api/health` отвечает `{"ok": true}`;
   - после `reboot` бот поднялся сам.
7. **Вечерний план до фазы 2 (мост 1.4):** вставляешь секцию «Завтра» из вечернего плана Cowork
   в тему «План», бот отвечает карточкой «В список · Выбрать · Не надо».

---

## Персонаж

1. Заполни сетку на странице «Мандарат» (центр, 8 направлений, практики с типом).
   Скажи Claude, что заполнил: он выгрузит JSON. Дальше на дроплете:
   ```bash
   WORKOS_ROLE=admin .venv/bin/python workctl.py seed-grid mandarat.json \
       --reward character:8="вещь из хотелок" --reward character:10="ещё одна"
   ```
   Повторный запуск безопасен: клетки обновятся, убранные станут неактивными, опыт не пропадёт.
2. **Garmin (П.4):** `WITH_GARMIN=1 bash deploy/install.sh`, затем один раз
   `sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python workctl.py garmin-login`
   и привяжи клетки: `seed-grid mandarat.json --auto A1=garmin:sleep:7.5 --auto A2=garmin:activity`.
3. `bash deploy/phase.sh persona`
4. **Готово, когда:** отметка задачи в мини-приложении дала опыт, `/me` в теме «План» показывает те же цифры,
   первое повышение уровня пришло сообщением.

---

## Фаза 2. Дневной контур

1. **Google.** В console.cloud.google.com: проект, включить Gmail API и Calendar API, OAuth consent screen
   перевести в **In production** (иначе токен умрёт через 7 дней), Credentials → OAuth client ID → Desktop app,
   скачать `client_secret.json` на Мак. Токен записи (календарь и черновики):
   ```bash
   python3 tools/google_auth.py --client ~/Downloads/client_secret.json --scopes write
   ```
   → `GOOGLE_WRITE_REFRESH_TOKEN` в `.env.executor`.
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
   - Письмо в ИТ **до** первого подключения (см. ниже).
   - Проба и первый прогон:
     ```bash
     sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -m collectors.eas --probe
     sudo -u workos-reader WORKOS_ROLE=reader .venv/bin/python -m collectors.eas --once
     ```
8. **Включить источники:**
   ```sql
   UPDATE work.sources SET enabled = true, config = '{"board_id":"6a197760b7795b2a91550ec0"}' WHERE id = 'trello:george';
   UPDATE work.sources SET enabled = true WHERE id IN ('plaud','ical:personal','ical:infatica','eas:alfa');
   UPDATE work.sources SET enabled = true, config = '{"channels":["C..."],"user_id":"U..."}' WHERE id = 'slack:infatica';
   ```
   (под админом: `set -a; . .env.admin; set +a; psql`). Потом `bash deploy/phase.sh 2`.
9. **Cowork-планировщик:** когда план с дроплета три вечера подряд приходит в 00:00 и не хуже нынешнего,
   выключи расписание вечерней задачи в Cowork (саму задачу оставь для разговоров с планом).
10. **Сторож:** healthchecks.io → новый check с периодом 10 минут → URL в `HEALTHCHECKS_URL` (`.env.bot`).
11. **Готово, когда:** вчерашний день целиком в базе (`workctl.py status`), перенесённая встреча Инфатики
    или Альфы через 15 минут переехала в личный календарь, план дня встал блоками в «План»,
    а выключенный руками источник через час дал алерт в «Системе».

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
   Модель берёт 400-500 МБ при работе. На дроплете 1 ГБ это тесно: смотри `free -m` во время `workctl.py embed`.
   Если уходит в своп, подними дроплет до 2 ГБ (+6 $ в месяц) и тогда же можно включить `WORKOS_ASK_VECTOR=1` в `.env.bot`.
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
установка LaunchAgent из `deploy/launchd/`. На дроплете `INGEST_TOKEN` и `INGEST_BIND` (tailnet-адрес),
`UPDATE work.sources SET enabled = true WHERE id = 'achat:alfa';`, `bash deploy/phase.sh 7`.

## Фаза 8. Бэкапы и аудит
`age-keygen -o ~/workos-age.key` **на Маке**, публичный ключ в `BACKUP_AGE_RECIPIENT` (`.env.admin`),
`bash deploy/phase.sh 8`, через сутки проверь восстановление: `bash deploy/restore_check.sh <файл> <ключ>`.

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

## Что проверено, а что нет
- 185 автотестов на локальном Postgres: права ролей, захват, карточки и хеш одобрения, мини-приложение,
  расстановка дня (включая переход часов 25.10), опыт, коллекторы и исполнители на поддельных API.
- С живыми Telegram, Google, Exchange Альфы, Plaud и Slack код ещё не встречался. Первый запуск каждой
  фазы стоит делать с открытым `journalctl`.
- Plaud API неофициальный. В EAS таблицы токенов для Settings и части Provision записаны по документации
  без живой сверки: если `--probe` скажет «Exchange не понял запрос Provision», это туда.
- Модель эмбеддингов в тестах не загружалась (стоит подмена), реальный расход памяти измерь сам.

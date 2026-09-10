# RUNBOOK — хост JARVIS на Raspberry Pi

Что делать руками: поднять хост, завести внешние сервисы, снять бэкап,
восстановиться из него. Состояние этапов — `BUILD-PROGRESS.md`, поведение
системы — `SPEC.md`, причины решений — `DECISIONS.md`. Здесь только команды.

---

## Что где лежит

| Что | Где |
|---|---|
Плата | Raspberry Pi 4 Model B, 2 ГБ, домашняя сеть, `jarvis.local`
Пользователь | `jarvis`, вход **только по ключу**
Ключ доступа | `~/.ssh/jarvis_pi` на машине owner
Репозиторий на плате | `/home/jarvis/jarvis`
Секреты | `/home/jarvis/jarvis/.env`, права `600`, в git не попадает
Данные базы | том Docker `jarvis_db-data`
Локальные копии | том Docker `jarvis_db-backups`

Вход:

```bash
ssh -i ~/.ssh/jarvis_pi jarvis@jarvis.local
```

Если имя не отвечает — плата доступна по адресу из DHCP роутера. Найти её
можно по MAC-префиксу Raspberry Pi (`dc:a6:32`).

**Если локальная сеть не отвечает вовсе, а интернет работает** — смотреть на
VPN: клиент с включённым kill-switch блокирует домашние адреса. Признак —
`ping` до собственного роутера отвечает `General failure`.

---

## Повседневные команды

Выполняются на плате, из `/home/jarvis/jarvis`.

```bash
make up              # поднять стек (база и API; туннель - только с COMPOSE_PROFILES=tunnel)
make down            # остановить
make logs            # смотреть логи
make migrate-pi      # накатить миграции внутри контейнера api
make backup          # снять дамп и показать, что выгрузил бы (dry-run)
make backup-apply    # снять дамп, выгрузить в B2, отправить ping
make restore-check   # развернуть свежий дамп в отдельную базу и осмотреть
make sync-itmo-pi    # забрать расписание портала в зеркало (dry-run)
make sync-gcal-pi    # показать, что уедет в Google Calendar (dry-run)
```

Наружу по умолчанию не пишет ничего: реальная выгрузка — только `--apply`.

Проверка живости:

```bash
curl -s http://127.0.0.1:8000/health
```

---

## Первый запуск на чистой плате

Если плату приходится ставить заново — например, после смерти карты.

**1. Система.** Raspberry Pi OS Lite **64-bit** (не 32-битная: под неё не
выпускают образов Postgres, а образ API собран под `linux/arm64`). Пишется
Raspberry Pi Imager с ноутбука; в окне настроек задаются имя хоста `jarvis`,
пользователь `jarvis`, Wi-Fi, часовой пояс и **SSH только по ключу** —
публичный ключ вставляется в поле на вкладке Services.

Imager 2.x применяет эти настройки через cloud-init: на разделе `bootfs`
появляются `user-data`, `meta-data`, `network-config`. Файл `custom.toml`
той же версией **не читается** — проверять применение настроек надо по
cloud-init файлам.

**2. Беспарольный `sudo`** (cloud-init его не выдаёт):

```bash
echo "jarvis ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_jarvis-nopasswd
sudo chmod 0440 /etc/sudoers.d/010_jarvis-nopasswd
```

**3. Memory cgroup** — Docker без него не ограничивает память контейнеров.
Параметр отключает прошивка платы, в файле его не видно, поэтому дописывается
в конец **единственной строки** `cmdline.txt` (перевод строки внутри ломает
загрузку):

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
CUR=$(tr -d '\n' < /boot/firmware/cmdline.txt)
printf '%s cgroup_enable=memory cgroup_memory=1\n' "$CUR" | sudo tee /boot/firmware/cmdline.txt
sudo reboot
```

Проверка: `cat /sys/fs/cgroup/cgroup.controllers` содержит `memory`.

**4. Docker** из официального репозитория (в Debian свой пакет заметно старее):

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get -y install git curl ca-certificates gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker jarvis
sudo reboot
```

**5. Репозиторий и `.env`:**

```bash
git clone https://github.com/Viniron/PetProject.git ~/jarvis
cd ~/jarvis
cp .env.example .env
chmod 600 .env
```

Дальше в `.env` вписываются реальные значения — см. следующий раздел.
Пароль базы генерируется на месте:

```bash
openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 28
```

**6. Подъём и проба восстановления:**

```bash
make up
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis < infra/restore-probe.sql
```

---

## Внешние сервисы

Секреты вписываются в `.env` **на плате** и никуда больше не копируются.
Открыть файл: `nano ~/jarvis/.env`, сохранить `Ctrl+O`, выйти `Ctrl+X`.

### Google Calendar → `GOOGLE_SA_JSON`, `GOOGLE_CALENDAR_OWNER_EMAIL`

Service account в Google Cloud и почта owner, которой отдаются календари.
Пошагово — в разделе «Google Calendar» ниже: там же создание календарей
и первая запись расписания.

### Cloudflare Tunnel → `CF_TUNNEL_TOKEN`

**Отложено до Э7** решением owner: доступ извне нечему открывать, пока нет
интерфейса. Инструкция ниже остаётся готовой к тому моменту.

Нужен домен в Cloudflare: постоянного HTTPS-имени без него не бывает,
а бесплатные адреса `trycloudflare` меняются при каждом перезапуске. Сам
туннель бесплатен — платный только домен.

1. `dash.cloudflare.com` → Add a site → домен → перенести NS у регистратора.
2. Zero Trust → Networks → Tunnels → Create a tunnel → **Cloudflared**,
   имя `jarvis`.
3. Скопировать токен из показанной команды (длинная строка после `--token`).
4. Public Hostname: поддомен (например `jarvis`), домен, service
   `http://api:8000` — туннель и API в одной сети compose, поэтому обращение
   по имени сервиса.
5. Вписать токен в `.env`, затем `make up`.

Проверка: `docker compose logs cloudflared` содержит `Registered tunnel
connection`, и публичное имя отдаёт `/health`.

### Backblaze B2 → `B2_BUCKET`, `B2_KEY_ID`, `B2_APP_KEY`

1. B2 Cloud Storage → Create a Bucket, тип **Private**.
2. Application Keys → Add a New Application Key: доступ **только к этому
   бакету**, права `writeFiles`, `listFiles`, `listBuckets` —
   **`deleteFiles` не давать.** Смысл в ADR-020: сбойнувшая или
   скомпрометированная плата физически не должна уметь стереть историю копий.
   Удаляет только облако, правилами жизненного цикла.
3. `applicationKey` показывается один раз — сохранить сразу.
4. Lifecycle Rules на бакете: глубина 7 ежедневных / 4 недельных /
   6 месячных, по префиксам (`daily/` и далее).

**Дампы не шифруются** — решение owner, риск очерчен в ADR-020. Поэтому
`ISU_CRED_KEY` в бакете оказаться не должен никогда: он живёт только в `.env`.

### Сторож бэкапа → `HEARTBEAT_URL`

Подходит любой сервис «мёртвой руки», принимающий GET: **UptimeRobot**,
**Better Stack**, healthchecks.io. Требование одно: сторож живёт **вне дома**,
иначе он умрёт вместе с платой и промолчит именно тогда, когда нужен.

1. Создать монитор типа **Heartbeat** (не «HTTP» — тот сам стучится к вам,
   а нужен обратный: ждать стука от платы).
2. Период — **1 день**, допуск опоздания — **2 часа**.
3. Скопировать выданный URL.

Джоб отказывается делать `--apply` с пустым `HEARTBEAT_URL`: бэкап без
контроля — это бэкап, о поломке которого узнают в день восстановления.

---

### ИСУ → `ISU_LOGIN`, `ISU_PASSWORD`, `ISU_CRED_KEY`

Регистрировать ничего не надо — учётная запись уже есть. Нужны три строки
в `.env` **на плате**; в чат они не присылаются.

```bash
ISU_LOGIN=<логин ИСУ>
ISU_PASSWORD=<пароль ИСУ>
ISU_CRED_KEY=<вывод openssl rand -base64 32>
```

**Зачем третья строка.** Пароль ИСУ нужен приложению постоянно: без него не
выпустить новый refresh-токен, когда старый кончится. Значит, он лежит в базе.
База каждую ночь уезжает дампом в B2, и **дампы не шифруются** (ADR-020) —
открытым текстом пароль в дампе означал бы, что утечка бэкапа равна утечке
доступа к ИСУ. `ISU_CRED_KEY` — замок: пароль шифруется им перед записью,
в облако уезжает шифротекст, ключ остаётся в `.env`.

Отсюда единственное жёсткое правило: **ключ и дамп не должны оказаться
в одном месте.** В бакете B2 ключу делать нечего никогда.

Генерировать — на плате, той же командой, что и пароль базы:

```bash
openssl rand -base64 32
```

**Сохранить копию ключа в двух местах** (менеджер паролей и что-нибудь
офлайновое). Восстановить его неоткуда: в git его нет, в бэкапе его нет.
Потеря ключа не катастрофа — приложение попросит ввести пароль ИСУ заново;
катастрофа — потеря ключа вместе с забытым паролем.

Кавычки вокруг значений не ставить: `docker compose` их не снимает, и они
уедут внутрь пароля.

Проверка, что вписалось без опечаток (сами значения на экран не выводятся):

```bash
grep -c '^ISU_.*=' ~/jarvis/.env               # 3
grep '^ISU_' ~/jarvis/.env | grep -c CHANGE_ME  # 0
chmod 600 ~/jarvis/.env
```

Первый забор расписания и что он делает — раздел «Расписание из my.itmo.ru».

---

## Ночной бэкап по расписанию

Юниты лежат в репозитории и ставятся симлинками — правка в git сразу
доходит до systemd:

```bash
sudo ln -sf /home/jarvis/jarvis/infra/systemd/jarvis-backup.service /etc/systemd/system/
sudo ln -sf /home/jarvis/jarvis/infra/systemd/jarvis-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-backup.timer
```

Проверки:

```bash
systemctl list-timers jarvis-backup.timer   # когда следующий запуск
sudo systemctl start jarvis-backup.service  # прогнать сейчас
journalctl -u jarvis-backup.service -n 50   # что было в последний раз
```

`Persistent=true` в таймере означает, что пропущенный из-за выключенной платы
запуск выполнится при следующем включении — тот же принцип, что инвариант 11.

---

## Восстановление из бэкапа

Главная процедура файла. Пока она не проделана руками хотя бы раз, бэкапа
считай что нет (ADR-020).

**Быстрая проверка последней локальной копии** — не требует B2:

```bash
make restore-check
```

Разворачивает свежайший дамп в базу `jarvis_restore`, печатает список таблиц
и число строк в пробе. Живую базу не трогает.

**Полное восстановление из облака** — то, что понадобится при смерти карты:

1. Скачать дамп из бакета через веб-интерфейс B2 (ключ на плате читать не
   умеет намеренно) и положить на плату, например в `/tmp/restore.dump`.
2. Скопировать файл в том с копиями и развернуть:

```bash
docker cp /tmp/restore.dump jarvis-db-1:/tmp/restore.dump
docker exec -i jarvis-db-1 psql -U jarvis -d postgres -c "DROP DATABASE IF EXISTS jarvis_restore"
docker exec -i jarvis-db-1 psql -U jarvis -d postgres -c "CREATE DATABASE jarvis_restore"
docker exec -i jarvis-db-1 pg_restore -U jarvis -d jarvis_restore --exit-on-error /tmp/restore.dump
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis_restore -c '\dt'
```

3. Убедиться, что данные на месте: `\dt` показывает девять таблиц схемы v1
   (`settings`, `integration_tokens`, `itmo_lessons`, `calendar_events`,
   `day_flags`, `audit_log`, `job_runs`, `capture_drafts`, `capture_blobs`)
   плюс `alembic_version`.
4. Только после этого, если восстановление идёт вместо живой базы:
   остановить стек, переименовать базы, поднять снова.

Порядок именно такой: сначала развернуть рядом и посмотреть, потом
подменять. Восстановление поверх живой базы не оставляет пути назад.

---

## Схема базы и миграции

Схему создаёт только Alembic (инвариант хоста 3). Руками в базе структуру
не менять — иначе следующий `upgrade` встретит не то, что ожидает.

```bash
make migrate-pi      # накатить всё, чего ещё нет
```

**Чистая плата:** поднять стек, затем `make migrate-pi` — база получит схему
с нуля.

**После восстановления из дампа миграции применять не нужно:** `pg_dump -Fc`
несёт и структуру, и таблицу `alembic_version`. Прогнать `make migrate-pi`
всё же стоит — он либо не сделает ничего, либо докатит то, чего в старом дампе
не было, и это единственный способ заметить, что дамп старше кода.

**Обновление кода на плате.** Порядок обязателен именно такой:

```bash
cd ~/jarvis && git pull
make build          # без этого up -d оставит старый образ
make up
make migrate-pi     # если в обновлении есть миграции
```

`make build` пропускать нельзя: `up -d` поднимает уже собранный образ молча,
и новый код на плату просто не приезжает. Симптом пропуска - `alembic: not
found` или старое поведение API после «успешного» обновления.

**Если `git pull` отказывается** («Your local changes would be overwritten»),
значит в рабочей копии лежат файлы, доставленные мимо git. Разработка на плате
не ведётся, поэтому местные версии выбрасываются:

```bash
tar czf ~/jarvis-pre-pull-$(date +%F).tar.gz --exclude=.git -C ~ jarvis
git checkout -- .
git clean -fd apps infra docs
git pull
```

`git clean` без `-x` игнорируемые файлы не трогает, а пути ограничены тремя
каталогами - `.env` в корне остаётся на месте. Снимок в tar делается всё равно:
секунда времени против невосстановимой потери.

**Проверить, на какой версии база:**

```bash
docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.pi.yml   run --rm api alembic -c /app/apps/api/alembic.ini current
```

---

## Расписание из my.itmo.ru

Джоб забирает расписание портала в зеркало `itmo_lessons`. В Google из
зеркала пишет отдельный джоб — раздел «Google Calendar» ниже.

**По умолчанию ничего не записывает.** `sync-itmo` показывает дифф и выходит;
единственное, что он сохраняет в этом режиме, — токены доступа, добытые
входом. Без этого каждый ручной прогон логинился бы паролем заново.

```bash
cd ~/jarvis
make sync-itmo-pi          # дифф, в базу не пишет
make sync-itmo-pi-apply    # запись зеркала
```

**Что читать в выводе.** Строки `+` — пары, которых в зеркале не было,
`~` — изменившиеся, `-` — исчезнувшие из расписания портала. Первый прогон
показывает одни `+`; второй подряд — ноль изменений, и это главная проверка:
джоб идемпотентен, двойной запуск не задваивает расписание.

**Проверить, что легло в базу:**

```bash
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select lesson_date, starts_at, subject, room, fetched_at from itmo_lessons order by starts_at limit 10;"
```

Время в базе — **UTC** (инвариант 7). Пара, стоящая в расписании на 10:00,
здесь будет 07:00 — это не сдвиг, а хранение.

### Когда что-то пошло не так

Джоб падает громко и называет причину. Три сообщения, которые стоит узнавать:

- **`ISU_CRED_KEY пуст`** — не заполнена третья строка в `.env`. Шифровать
  пароль нечем, и класть его открытым текстом джоб отказывается.
- **`ITMO ID не принял логин и пароль`** — Keycloak показал форму снова.
  Почти всегда это действительно неверный пароль; ни логин, ни пароль
  в текст ошибки не попадают, поэтому лог можно пересылать.
- **`на странице входа ITMO ID не найден loginAction`** — портал сменил
  разметку страницы входа. Чинится не в `.env`, а правкой регулярного
  выражения в `apps/api/jarvis_api/integrations/itmo/auth.py`; это самое
  хрупкое место интеграции, и оно названо своим именем нарочно.
- **`формат ответа my.itmo.ru изменился`** — портал переименовал или убрал
  поле. Джоб не пишет разобранный мусор в базу; правится
  `integrations/itmo/schema.py`.

След каждого прогона — в базе:

```bash
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select at, status, detail from audit_log where kind='itmo_fetch' order by at desc limit 5;"
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select * from job_runs where job='sync_itmo' order by run_date desc limit 5;"
```

**Смена пароля ИСУ.** Пароль хранится в базе, а не читается из `.env` каждый
раз, поэтому правки одной строки в `.env` мало. После смены пароля на портале:

```bash
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "delete from integration_tokens where provider='itmo';"
```

Следующий прогон возьмёт новое значение из `.env` и положит его в базу заново.

---

## Google Calendar

Расписание из зеркала уезжает в календарь owner. Доступ — **service account**
(ADR-019): у него нет ни браузерного входа, ни срока жизни токена, ни
требования публичного HTTPS-имени. Календари он создаёт себе и отдаёт owner.

### Что завести в Google Cloud — один раз

1. [console.cloud.google.com](https://console.cloud.google.com) → создать
   проект (имя любое, например `jarvis`).
2. **APIs & Services → Library → Google Calendar API → Enable.** Без этого
   шага все запросы получают `403`, и выглядит это как проблема с ключом.
3. **IAM & Admin → Service Accounts → Create service account.** Имя любое,
   например `jarvis-calendar`. **Роли не нужны** — на шаге «Grant this
   service account access to project» ничего не выбирать: доступ к
   календарям даётся не ролями IAM, а тем, что аккаунт сам их создаёт.
4. Открыть созданный аккаунт → вкладка **Keys → Add key → Create new key →
   JSON**. Файл скачается сам; второй раз его не показывают.

### Ключ в `.env` на плате

JSON нужен **одной строкой**. Превратить скачанный файл:

```bash
python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))' key.json
```

Полученную строку вписать в `~/jarvis/.env` целиком, без кавычек вокруг:

```
GOOGLE_SA_JSON={"type":"service_account","project_id":"…"}
GOOGLE_CALENDAR_OWNER_EMAIL=почта-owner@gmail.com
```

Две вещи, о которых стоит знать заранее:

- **Кавычек вокруг значения ставить не нужно** — JSON начинается с `{`, и
  compose передаёт строку как есть. А вот символ `$` в значении compose
  попытался бы подставить как переменную; в ключах Google его не бывает
  (base64 плюс hex), но если однажды появится — его удваивают: `$$`.
- **Скачанный файл ключа после этого удалить.** В репозиторий он не должен
  попасть никогда (инвариант 6), и второй копии на диске быть не должно.

Дальше — перезапуск стека, чтобы переменные дошли до контейнера:

```bash
cd ~/jarvis && docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.pi.yml up -d
```

### Создать календари

```bash
make gcal-setup          # показать, что будет создано
make gcal-setup-apply    # создать и отдать owner
```

Создаётся три: `JARVIS · ИТМО`, `JARVIS · Занятия`, `JARVIS · События`.
Наполняется на этом этапе только первый — два других ждут курсов и захвата.
Их id ложатся в `settings`, а не в `.env`: они приезжают из Google и обязаны
вернуться вместе с базой при восстановлении из дампа.

**Что должен увидеть owner:** три новых календаря в списке слева в Google
Calendar. Если их нет — проверить почту в `GOOGLE_CALENDAR_OWNER_EMAIL`
и заглянуть в почтовый ящик: Google может прислать приглашение.

Повторный запуск ничего не создаёт: команда сверяется с самим Google, а не
со своей базой. Календарь, удалённый owner вручную, при следующем прогоне
будет заведён заново — id в базе от этого не спасает и не должен.

### Записать расписание

```bash
make sync-gcal-pi          # дифф, наружу не пишет ничего
make sync-gcal-pi-apply    # запись в календарь
```

**Что читать в выводе.** `+` — событие будет создано, `~` — обновлено,
`-` — удалено, `!` — не записано, с причиной. Первый прогон — одни `+`;
**второй подряд обязан дать нули по всем трём** — это и есть проверка
идемпотентности, ради которой существует реконсил (§11.2).

Проверить, что записано:

```bash
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select external_key, title, starts_at, sync_state, google_event_id from calendar_events order by starts_at limit 10;"
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select at, status, target, detail from audit_log where kind='calendar_write' order by at desc limit 5;"
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "select * from job_runs where job='push_gcal' order by run_date desc limit 5;"
```

Время в `calendar_events`, как и везде в базе, — UTC. Пара, стоящая в
расписании на 10:00, лежит как 07:00 и показывается в календаре как 10:00:
зона задана самому календарю при создании.

### Когда что-то пошло не так

- **`GOOGLE_SA_JSON пуст`** — переменная не дошла до контейнера. Сначала
  проверить `.env`, потом — что стек перезапущен после правки.
- **`GOOGLE_SA_JSON не разбирается как JSON`** — строка склеена не целиком
  или в неё попал перенос строки.
- **`календарь JARVIS · ИТМО не создан`** — не выполнен `make gcal-setup-apply`,
  либо он выполнялся против другой базы.
- **`Google отказал: … 403`** — два обычных случая: не включён Calendar API
  (шаг 2 выше) или ключ отозван в консоли. Прогон при этом прекращается
  целиком, а не перебирает все восемьдесят пар.
- **`Google недоступен`** — сеть или таймаут. Зеркало и журнал не тронуты,
  следующий прогон догонит: `sync-gcal` идемпотентен по построению.
- **Отдельные события с `!`** — они помечены `sync_state = failed` в
  `calendar_events`, причина в `last_error`. Следующий прогон допишет их сам,
  вмешательства не требуется.

**Событие, исправленное руками в Google, вернётся к прежнему виду.** Синк
односторонний (ADR-001): источник истины — расписание портала. Удалённое
руками событие создастся заново — по той же причине.

**Начать календарь с чистого листа:** удалить календарь `JARVIS · ИТМО`
в интерфейсе Google, затем очистить журнал и завести календарь заново.

```bash
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "delete from calendar_events where source='itmo';"
docker exec -i jarvis-db-1 psql -U jarvis -d jarvis -c \
  "update settings set gcal_itmo_id = null where id = 1;"
make gcal-setup-apply && make sync-gcal-pi-apply
```

---

## Плата переехала в другую сеть

Симптом: плата включена, но недоступна ни по `jarvis.local`, ни по прежнему
адресу, и сканирование новой сети её не находит. Причина почти всегда одна:
прописанные в ней сети не совпадают с доступными. Ничего не сломано — плата
просто не вышла в сеть.

Способы, от простого к муторному. Выбирается по тому, что под рукой.

### 1. Кабель Ethernet — если есть провод и порт

Воткнуть в роутер: плата получит адрес по DHCP независимо от Wi-Fi. Дальше
зайти по SSH (адрес искать в панели роутера или сканированием подсети) и
добавить сеть штатно:

```bash
sudo nmcli device wifi connect "<SSID>" password "<пароль>"
sudo nmcli connection modify "<SSID>" connection.autoconnect-priority 10
```

### 2. Точка доступа на телефоне с именем прежней сети

Создать на телефоне точку с **тем же SSID и тем же паролем**, что знает
плата. Она подключится, приняв её за домашнюю; ноутбук подключить туда же —
и дальше как в первом способе. Провода не нужны.

### 3. Карта в ноутбук — когда нет ни кабеля, ни телефона

Профили сетей NetworkManager лежат в `/etc/NetworkManager/system-connections/`,
то есть в ext4-разделе, которого Windows не видит. Но у Raspberry Pi OS есть
штатный способ выполнить скрипт при загрузке, задаваемый **с загрузочного
раздела** — того самого, что Windows видит как `bootfs`. Тот же механизм
использует Imager для своего `firstrun.sh`.

Порядок:

1. Выдернуть питание платы (корректно выключить нечем — SSH недоступен),
   вынуть microSD, вставить в ноутбук. Предложение Windows отформатировать
   второй раздел — **отклонить**.
2. Создать на `bootfs` файл `jarvis-net.sh`. **Только ASCII и только
   переводы строк LF**: файл правится Блокнотом, а `/bin/sh` не выполнит
   скрипт с CRLF в строке `#!`.

```sh
#!/bin/sh
set -e
BOOT=/boot/firmware
NMDIR=/etc/NetworkManager/system-connections
SSID='<имя новой сети>'
PSK='<пароль>'

UUID=$(cat /proc/sys/kernel/random/uuid)
PROFILE="$NMDIR/$SSID.nmconnection"
cat > "$PROFILE" <<PROFILE_EOF
[connection]
id=$SSID
uuid=$UUID
type=wifi
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=$SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$PSK

[ipv4]
method=auto

[ipv6]
addr-gen-mode=default
method=auto
PROFILE_EOF
chmod 600 "$PROFILE"
chown root:root "$PROFILE"

# Себя из cmdline.txt убрать обязательно: иначе скрипт пойдёт каждую
# загрузку, а на FAT32 останется лежать пароль от Wi-Fi.
sed -i 's/ systemd\.run=[^ ]*//g; s/ systemd\.run_success_action=[^ ]*//g; s/ systemd\.unit=[^ ]*//g' "$BOOT/cmdline.txt"
rm -f "$BOOT/jarvis-net.sh"
```

3. Дописать в конец **единственной строки** `cmdline.txt` (перевод строки
   внутри ломает загрузку):

```
systemd.run=/boot/firmware/jarvis-net.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
```

4. Извлечь карту, вернуть в плату, включить. Плата выполнит скрипт, уберёт
   его вместе с параметром из `cmdline.txt`, перезагрузится и подключится
   к новой сети.

### Чтобы это не повторялось

Держать в плате **несколько** профилей сразу — домашний, запасной и точку
доступа телефона. NetworkManager подключится к той, что доступна, а
`autoconnect-priority` задаёт предпочтение. Добавить сеть, находясь на плате:

```bash
# Проще всего — подключиться к сети, находясь в её зоне: профиль создастся сам.
sudo nmcli device wifi connect "<SSID>" password "<пароль>"

# Приоритет: чем больше число, тем охотнее плата выберет эту сеть.
sudo nmcli connection modify "<SSID>" connection.autoconnect-priority 10

# Посмотреть, какие сети уже прописаны.
nmcli connection show
```

Тогда переезд плата переживает сама: включили — нашла знакомую сеть — поднялась.

---

## Если что-то не работает

| Симптом | Куда смотреть |
|---|---|
`/health` не отвечает | `docker ps`, `make logs`; база `healthy`? API ждёт её по `depends_on` |
Контейнеры не поднялись после перезагрузки | `systemctl is-enabled docker`, политика `unless-stopped` в compose |
`make` падает на `POSTGRES_PASSWORD` | нет `.env` в корне репозитория: compose ищет его в каталоге compose-файла, поэтому цели передают `--env-file` |
Бэкап молчит, healthchecks шлёт письмо | `journalctl -u jarvis-backup.service`; ping уходит только после успешной выгрузки — значит не дошла она |
`pg_dump: server version mismatch` | образ бэкапа обязан быть той же мажорной версии, что база: оба на `postgres:17-alpine` |
Туннель не поднимается | токен в `.env`, `docker compose logs cloudflared` |
Нет места на карте | `df -h`, `docker system prune`, глубина копий `BACKUP_KEEP_LOCAL` |

**Носитель — расходник.** Пока система стоит на microSD, а не на USB-SSD
(отступление от `SPEC.md` §11.1, зафиксировано в `BUILD-PROGRESS.md`),
единственная защита истории занятий — офсайт-копия в B2. Проверять её
восстановление раз в месяц, а не раз в никогда.

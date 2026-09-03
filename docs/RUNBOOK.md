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
make backup          # снять дамп и показать, что выгрузил бы (dry-run)
make backup-apply    # снять дамп, выгрузить в B2, отправить ping
make restore-check   # развернуть свежий дамп в отдельную базу и осмотреть
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

3. Убедиться, что данные на месте (до Э2 — таблица `restore_probe`).
4. Только после этого, если восстановление идёт вместо живой базы:
   остановить стек, переименовать базы, поднять снова.

Порядок именно такой: сначала развернуть рядом и посмотреть, потом
подменять. Восстановление поверх живой базы не оставляет пути назад.

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

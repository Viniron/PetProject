# Единая точка входа для команд разработки.
#
# Цели одинаковы на машине owner, на Pi и в CI - расходится только путь
# к интерпретатору внутри venv, поэтому он и вынесен в переменную.
# Переопределяется снаружи: make test PY=python.

ifeq ($(OS),Windows_NT)
PY ?= apps/api/.venv/Scripts/python.exe
else
PY ?= apps/api/.venv/bin/python
endif

API := apps/api
WEB := apps/web
# Скрипты фронта запускаются из каталога пакета, поэтому цели работают
# из корня репозитория, как и остальные.
NPM := npm --prefix $(WEB)
COMPOSE := infra/docker-compose.yml
COMPOSE_PI := infra/docker-compose.pi.yml
COMPOSE_DEV := infra/docker-compose.dev.yml

# docker compose ищет .env в каталоге compose-файла, то есть в infra/, а
# лежит он в корне репозитория - поэтому --env-file указывается явно.
# Найдено на Э1 при первом подъёме стека на Pi.
#
# Если файла нет (CI), аргумент не добавляется вовсе и значения берутся из
# переменных окружения: иначе compose падает на отсутствующем файле.
ENV_FILE ?= .env
ENV_FILE_ARG := $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE),)

DC := docker compose $(ENV_FILE_ARG) -f $(COMPOSE)
DC_PI := docker compose $(ENV_FILE_ARG) -f $(COMPOSE) -f $(COMPOSE_PI)
# У машины разработки свой env-файл, и он коммитится: в нём нет секретов,
# зато dev-база отличается от prod именем, портом и паролем (инвариант хоста 6).
DC_DEV := docker compose --env-file infra/dev.env -f $(COMPOSE) -f $(COMPOSE_DEV)

# Alembic вызывается с явным -c: пути внутри ini заданы через %(here)s,
# поэтому цель работает из корня репозитория, а не только из apps/api.
ALEMBIC := $(PY) -m alembic -c $(API)/alembic.ini

.PHONY: help venv dev test test-docker lint format compose-check up down logs         db-up db-down build migrate migrate-pi revision contract         web-install web-dev web-build web-lint web-test web-client tunnel-check tunnel-check-pi llm-routes llm-routes-pi       sync-itmo sync-itmo-apply sync-itmo-pi sync-itmo-pi-apply         gcal-setup gcal-setup-apply gcal-setup-pi gcal-setup-pi-apply         sync-gcal sync-gcal-apply sync-gcal-pi sync-gcal-pi-apply sync-capture sync-capture-apply sync-capture-pi sync-capture-pi-apply capture-cleanup capture-cleanup-apply capture-cleanup-pi capture-cleanup-pi-apply         daily daily-apply daily-pi daily-pi-apply plan-today         finance-import finance-import-apply finance-import-pi finance-import-pi-apply         finance-taxonomy finance-taxonomy-apply finance-inherit finance-inherit-apply         finance-categorize finance-categorize-apply finance-categorize-pi finance-categorize-pi-apply \n        finance-offset finance-offset-apply finance-offset-unlink finance-offset-unlink-apply finance-offset-show \n        finance-balance finance-balance-pi finance-accounts finance-accounts-apply         backup backup-apply restore-check restore-check-apply

help:
	@echo "venv          - create apps/api/.venv and install dev extras"
	@echo "dev           - run API with reload on :8000"
	@echo "test          - pytest"
	@echo "test-docker   - pytest inside the api image, next to the test database"
	@echo "lint          - ruff + black --check + mypy --strict"
	@echo "format        - ruff --fix + black"
	@echo "compose-check - validate every compose overlay without starting it"
	@echo "contract      - dump the OpenAPI contract to packages/contracts (stage E6)"
	@echo "db-up         - start dev and test databases on the workstation (stage E2)"
	@echo "db-down       - stop them"
	@echo "build         - rebuild the api image after Dockerfile or dependency changes"
	@echo "up            - start the stack on the Pi (base + pi overlay)"
	@echo "down          - stop the stack"
	@echo "logs          - follow logs of the stack"
	@echo "backup        - pg_dump + report, dry-run (stage E1)"
	@echo "backup-apply  - pg_dump + upload to B2 + heartbeat ping (stage E1)"
	@echo "restore-check - restore the newest local dump into a scratch database and report"
	@echo "restore-check-apply - the same plus a heartbeat ping (stage E9)"
	@echo "migrate       - alembic upgrade head against DATABASE_URL (stage E2)"
	@echo "migrate-pi    - the same inside the api container on the Pi (stage E2)"
	@echo "revision      - autogenerate a migration: make revision m=\"what changed\" (stage E2)"
	@echo "sync-itmo       - my.itmo.ru -> mirror, dry-run (stage E3)"
	@echo "sync-itmo-apply - the same, writing the mirror (stage E3)"
	@echo "sync-itmo-pi    - dry-run inside the api container on the Pi (stage E3)"
	@echo "sync-itmo-pi-apply - the same, writing the mirror on the Pi (stage E3)"
	@echo "gcal-setup       - create the three JARVIS calendars, dry-run (stage E4)"
	@echo "gcal-setup-apply - the same, creating and sharing them (stage E4)"
	@echo "gcal-setup-pi    - the same inside the api container on the Pi (stage E4)"
	@echo "gcal-setup-pi-apply - the same, creating and sharing from the Pi (stage E4)"
	@echo "sync-gcal        - mirror -> Google Calendar, dry-run (stage E4)"
	@echo "sync-gcal-apply  - the same, writing to the calendar (stage E4)"
	@echo "sync-gcal-pi     - dry-run inside the api container on the Pi (stage E4)"
	@echo "sync-gcal-pi-apply - the same, writing to the calendar on the Pi (stage E4)"
	@echo "sync-capture     - confirmed capture drafts -> Google Calendar, dry-run (stage E8)"
	@echo "sync-capture-apply - the same, writing to the calendar (stage E8)"
	@echo "sync-capture-pi  - dry-run inside the api container on the Pi (stage E8)"
	@echo "sync-capture-pi-apply - the same, writing on the Pi (stage E8)"
	@echo "capture-cleanup  - abandoned capture drafts, dry-run (stage E8)"
	@echo "capture-cleanup-apply - the same, deleting them (stage E8)"
	@echo "capture-cleanup-pi - dry-run inside the api container on the Pi (stage E8)"
	@echo "capture-cleanup-pi-apply - the same, deleting them on the Pi (stage E8)"
	@echo "daily            - the daily chain: mirror + calendar, dry-run (stage E5)"
	@echo "daily-apply      - the same, doing the work (stage E5)"
	@echo "daily-pi         - dry-run inside the api container on the Pi (stage E5)"
	@echo "daily-pi-apply   - the same, doing the work on the Pi (stage E5)"
	@echo "plan-today    - course session planning, not implemented (courses track)"
	@echo "finance-import       - bank statements -> book, dry-run: make finance-import file=\"a.csv b.pdf\" (stage F3)"
	@echo "finance-import-apply - the same, writing the transactions (stage F3)"
	@echo "finance-import-pi    - dry-run inside the api container on the Pi, one file= (stage F3)"
	@echo "finance-import-pi-apply - the same, writing on the Pi (stage F3)"
	@echo "finance-taxonomy     - owner categories and rules from a JSON file, dry-run: make finance-taxonomy file=\"set.json\" (stage F4a)"
	@echo "finance-taxonomy-apply - the same, writing categories and rules (stage F4a)"
	@echo "finance-inherit      - copy a month category set forward: make finance-inherit from=2026-08 to=2026-09 (stage F4a)"
	@echo "finance-categorize   - apply the rules to the book, dry-run (stage F4a)"
	@echo "finance-categorize-apply - the same, writing kind and category (stage F4a)"
	@echo "finance-offset       - link an incoming payment to an expense, dry-run: make finance-offset income=12 expense=34 (stage F4b)"
	@echo "finance-offset-apply - the same, writing the link (stage F4b)"
	@echo "finance-offset-unlink - drop the link, dry-run: make finance-offset-unlink income=12 (stage F4b)"
	@echo "finance-offset-unlink-apply - the same, writing (stage F4b)"
	@echo "finance-offset-show  - show what an expense really cost: make finance-offset-show expense=34 (stage F4b)"
	@echo "finance-balance      - month balance, saved and left: make finance-balance month=2026-08 (stage F5)"
	@echo "finance-balance-pi   - the same inside the api container on the Pi (stage F5)"
	@echo "finance-accounts     - show account roles; with bank=/account=/role= a dry-run of the change (stage F5)"
	@echo "finance-accounts-apply - the same, writing the role (stage F5)"
	@echo "web-install   - install frontend dependencies from the lockfile (stage E7)"
	@echo "web-dev       - run the frontend with reload on :3000 (stage E7)"
	@echo "web-build     - export the frontend to apps/web/out (stage E7)"
	@echo "web-lint      - eslint + tsc --noEmit (stage E7)"
	@echo "web-test      - vitest (stage E7)"
	@echo "web-client    - regenerate the API client types from the contract (stage E7)"
	@echo "tunnel-check     - preflight before switching the tunnel on (stage E7)"
	@echo "tunnel-check-pi  - the same inside the running stack on the Pi (stage E7)"
	@echo "llm-routes       - print model assignment parsed from LLM_ROUTING (stage E12a)"
	@echo "llm-routes-pi    - the same inside the running stack on the Pi (stage E12a)"

venv:
	py -3.12 -m venv $(API)/.venv || python3.12 -m venv $(API)/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "$(API)[dev]"

# Планировщик на машине разработки выключен целевой переменной, а не
# infra/dev.env: эта цель поднимает uvicorn мимо compose, и env-файл до
# него не доезжает. Без строки ниже каждое сохранение файла при --reload
# перезапускало бы процесс, а вместе с ним - догоняющий запуск в живой
# ИСУ и живой Google.
dev: export SCHEDULER_ENABLED = false
dev:
	$(PY) -m uvicorn jarvis_api.main:app --reload --port 8000

test:
	$(PY) -m pytest $(API)

# Тот же прогон, но внутри образа. Нужен на машине owner: Smart App Control
# блокирует неподписанную DLL драйвера psycopg, и тесты с базой падают
# на импорте, а не на соединении. Внутри linux-образа политики нет, среда
# та же, что на Pi. `--build` намеренно: образ пересобирается при правке
# зависимостей, иначе прогон молча пойдёт на вчерашнем окружении.
test-docker:
	$(DC_DEV) --profile test run --rm --build api-test

# Контракт коммитится, поэтому перегенерация - отдельный ручной шаг, а не
# побочный эффект сборки: изменение контракта обязано быть видно в дифе.
# Сверку делает tests/test_contract.py, то есть обычный `make test`.
contract:
	$(PY) -m jarvis_api.contract

# Порядок неслучаен: сначала быстрый ruff, потом форматтер, потом самый
# медленный mypy. Падать дешевле на первом шаге.
lint:
	$(PY) -m ruff check $(API)
	$(PY) -m black --check $(API)
	$(PY) -m mypy --strict --config-file $(API)/pyproject.toml $(API)/jarvis_api $(API)/tests $(API)/alembic

format:
	$(PY) -m ruff check --fix $(API)
	$(PY) -m black $(API)

# Проверяются оба файла: overlay ломается ровно так же тихо, как база,
# а применяется только на Pi - то есть там, где отладка дороже всего.
compose-check:
	docker compose $(ENV_FILE_ARG) -f $(COMPOSE) config --quiet
	docker compose $(ENV_FILE_ARG) -f $(COMPOSE) -f $(COMPOSE_PI) config --quiet
	$(DC_DEV) config --quiet

# Пересборка образов api и web. Нужна после правки Dockerfile или зависимостей:
# `up -d` поднимает уже собранный образ и молча оставляет старый, поэтому
# новый код приезжает на плату только после этой цели.
build:
	$(DC_PI) build api web

up:
	$(DC_PI) up -d

down:
	$(DC_PI) down

logs:
	$(DC_PI) logs -f --tail=100

# Бэкап по умолчанию dry-run: снимает дамп, считает сумму, показывает, что
# выгрузил бы, и не отправляет наружу ни байта (CLAUDE.md).
backup:
	$(DC_PI) run --rm backup

backup-apply:
	$(DC_PI) run --rm backup --apply

# Проверка восстановления (Э1, разбор и расписание - Э9). Живую базу не
# трогает ни в одном режиме: дамп разворачивается в отдельную базу. --apply
# добавляет единственную запись наружу - ping сторожу, и так её зовёт таймер.
restore-check:
	./infra/restore-check.sh

restore-check-apply:
	./infra/restore-check.sh --apply

# Базы разработки. Тестовая эфемерная и живёт в tmpfs, dev-база - обычная:
# первую сносит каждый прогон тестов, вторую нет.
db-up:
	$(DC_DEV) up -d --wait db db-test

db-down:
	$(DC_DEV) down

# Схема накатывается только миграциями (инвариант хоста 3): create_all нет
# нигде, включая тесты, - иначе на Pi и в тестах оказались бы разные схемы.
migrate:
	$(ALEMBIC) upgrade head

# То же на Pi: там нет venv, зато есть образ с alembic внутри.
migrate-pi:
	$(DC_PI) run --rm api alembic -c /app/apps/api/alembic.ini upgrade head

# make revision m="что изменилось". Autogenerate сверяет модели с живой базой,
# поэтому DATABASE_URL должен указывать на неё, а не на пустую.
revision:
	$(ALEMBIC) revision --autogenerate -m "$(m)"

# Забор расписания. По умолчанию dry-run: показывает дифф к зеркалу и не
# пишет в базу ничего, кроме токенов, добытых входом (они - плата за доступ,
# а не результат работы; подробности в jobs/sync_itmo.py).
sync-itmo:
	$(PY) -m jarvis_api.jobs.sync_itmo

sync-itmo-apply:
	$(PY) -m jarvis_api.jobs.sync_itmo --apply

# То же на Pi: там нет venv, зато есть образ со всеми зависимостями.
# Умолчание такое же, как у остальных целей, - dry-run. Цель, пишущая
# в prod-базу по одному слову без флага, рано или поздно будет набрана
# по ошибке вместо соседней.
sync-itmo-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.sync_itmo

sync-itmo-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.sync_itmo --apply

# Календари JARVIS. Создание расшаривает их owner, то есть действие видно
# в чужом интерфейсе и отменяется руками - поэтому у него тот же порядок
# умолчаний, что у записи наружу: сначала показать, потом сделать.
gcal-setup:
	$(PY) -m jarvis_api.jobs.gcal_setup

gcal-setup-apply:
	$(PY) -m jarvis_api.jobs.gcal_setup --apply

# То же на плате. Нужны именно эти цели: ключ Google живёт в .env на Pi,
# venv там нет, и настройка выполняется внутри контейнера. Найдено на живом
# прогоне Э4 - без них команду приходилось набирать через docker compose руками.
gcal-setup-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.gcal_setup

gcal-setup-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.gcal_setup --apply

# Запись расписания в Google. Dry-run показывает дифф к календарю -
# что будет создано, обновлено и удалено, - и не отправляет наружу ничего.
sync-gcal:
	$(PY) -m jarvis_api.jobs.push_gcal

sync-gcal-apply:
	$(PY) -m jarvis_api.jobs.push_gcal --apply

# То же на Pi. Умолчание dry-run по той же причине, что у sync-itmo-pi:
# цель, пишущая наружу по одному слову без флага, рано или поздно будет
# набрана по ошибке вместо соседней.
sync-gcal-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.push_gcal

sync-gcal-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.push_gcal --apply

# --- Захват событий (Э8) ---------------------------------------------------
# Очередь подтверждённых черновиков в календарь `JARVIS · События`. Dry-run
# показывает, что стоит в очереди, и не отправляет наружу ничего. Обычно
# событие уезжает сразу при подтверждении (ADR-042), и очередь пуста -
# в ней остаётся только то, что не дошло с первой попытки.
sync-capture:
	$(PY) -m jarvis_api.jobs.push_capture

sync-capture-apply:
	$(PY) -m jarvis_api.jobs.push_capture --apply

sync-capture-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.push_capture

sync-capture-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.push_capture --apply

# Уборка брошенных черновиков (§9). Наружу не пишет, но удаляет данные
# owner - отсюда тот же dry-run по умолчанию, что у остальных целей.
capture-cleanup:
	$(PY) -m jarvis_api.jobs.capture_cleanup

capture-cleanup-apply:
	$(PY) -m jarvis_api.jobs.capture_cleanup --apply

capture-cleanup-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.capture_cleanup

capture-cleanup-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.capture_cleanup --apply

# Ежедневная цепочка целиком - то же, что каждые три часа делает планировщик
# внутри API. Нужна для ручного прогона и для проверки в dry-run перед тем,
# как включать расписание на плате.
daily:
	$(PY) -m jarvis_api.jobs.runner

daily-apply:
	$(PY) -m jarvis_api.jobs.runner --apply

daily-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.runner

daily-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.runner --apply

# Заглушка. Планирование занятия курса (SPEC §4) в календарный релиз не
# входит: время напоминания считается по манифесту курса - offset_minutes,
# fallback_time, latest_start, - а курсов установлено ноль. Выходит
# с ненулевым кодом намеренно: молчаливый успех несделанной работы хуже
# явной ошибки (инвариант 9 - падать громко).

plan-today:
	@echo "not implemented: needs a course manifest, see docs/BUILD-PROGRESS.md" && exit 1

# --- Финансовая книжка (Ф3) ------------------------------------------------
# Импорт выписок. По умолчанию dry-run: дифф по каждому файлу и ни одной
# записанной строки. Файлов за заход несколько, по одному с каждого банка
# (ADR-030), поэтому file= принимает список: file="tbank.csv ozon.pdf".
# Формат файла определяет адаптер банка, а не расширение (ADR-044):
# Т-Банк отдаёт CSV, Ozon Bank - PDF, цель у них одна.
#
# Проверка на пустой file= здесь, а не в argparse, только ради сообщения:
# `make finance-import` без аргумента - обычная опечатка, и она должна
# отвечать по-человечески, а не трассировкой.
ФАЙЛЫ = $(if $(file),,$(error укажи file="путь/к/выписке ..."))$(foreach ф,$(file),--file "$(ф)")

finance-import:
	$(PY) -m jarvis_api.jobs.finance_import $(ФАЙЛЫ)

finance-import-apply:
	$(PY) -m jarvis_api.jobs.finance_import $(ФАЙЛЫ) --apply

# То же на плате. Файл лежит на Pi (owner кладёт его туда scp), а внутрь
# контейнера попадает монтированием на один прогон: постоянного тома под
# входящие файлы нет и не нужно - сырьё живёт в базе, а не на диске
# (инвариант хоста 1). Отсюда ограничение: один файл за прогон.
DC_PI_IMPORT = $(DC_PI) run --rm -v "$(abspath $(file))":/in/$(notdir $(file)):ro api 	python -m jarvis_api.jobs.finance_import --file /in/$(notdir $(file))

finance-import-pi:
	$(DC_PI_IMPORT)

finance-import-pi-apply:
	$(DC_PI_IMPORT) --apply

# --- Разбор книжки (Ф4а) ---------------------------------------------------
# Набор категорий месяца и правила разбора - файлом от owner: они данные,
# а не код (инвариант 2), и в исходниках нет ни одного их названия.
НАБОР = $(if $(file),,$(error укажи file="путь/к/набору.json"))--file "$(file)"

finance-taxonomy:
	$(PY) -m jarvis_api.jobs.finance_taxonomy $(НАБОР)

finance-taxonomy-apply:
	$(PY) -m jarvis_api.jobs.finance_taxonomy $(НАБОР) --apply

# Наследование набора на новый месяц (§15.4). Отдельной целью, а не флагом
# импорта: месяц наследуется один раз, и случиться это должно по решению
# owner, а не побочным эффектом загрузки выписки.
МЕСЯЦЫ = $(if $(from),,$(error укажи from=ГГГГ-ММ))$(if $(to),,$(error укажи to=ГГГГ-ММ))--inherit-from $(from) --month $(to)

finance-inherit:
	$(PY) -m jarvis_api.jobs.finance_taxonomy $(МЕСЯЦЫ)

finance-inherit-apply:
	$(PY) -m jarvis_api.jobs.finance_taxonomy $(МЕСЯЦЫ) --apply

# Переразбор книжки. Нужен отдельно от импорта, потому что правила приезжают
# позже данных: owner разметил счёт или добавил родного - книжка обязана
# пересчитаться без перезагрузки выписок.
finance-categorize:
	$(PY) -m jarvis_api.jobs.finance_categorize

finance-categorize-apply:
	$(PY) -m jarvis_api.jobs.finance_categorize --apply

finance-categorize-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.finance_categorize

finance-categorize-pi-apply:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.finance_categorize --apply

# Гашение расхода поступлением (Ф4б, §15.5). Экрана для него ещё нет - он
# приезжает на Ф7, - а разбирать входящие переводы owner хочет раньше.
# Аргументы обязательны: `make finance-offset` без них - опечатка, и она
# должна останавливать make, а не запускать джоб, который сам не знает, что
# гасить.
ГАШЕНИЕ = $(if $(income),,$(error укажи income=<id поступления>))--income $(income)
РАСХОД = $(if $(expense),,$(error укажи expense=<id расхода>))--expense $(expense)
ПОКАЗАТЬ = $(if $(expense),,$(error укажи expense=<id расхода>))--show $(expense)

finance-offset:
	$(PY) -m jarvis_api.jobs.finance_offset $(ГАШЕНИЕ) $(РАСХОД)

finance-offset-apply:
	$(PY) -m jarvis_api.jobs.finance_offset $(ГАШЕНИЕ) $(РАСХОД) --apply

finance-offset-unlink:
	$(PY) -m jarvis_api.jobs.finance_offset $(ГАШЕНИЕ) --unlink

finance-offset-unlink-apply:
	$(PY) -m jarvis_api.jobs.finance_offset $(ГАШЕНИЕ) --unlink --apply

# Только чтение: ни `--apply`, ни записи. Отвечает на вопрос «сколько
# на самом деле стоил этот расход» - его же owner задаёт после привязки.
finance-offset-show:
	$(PY) -m jarvis_api.jobs.finance_offset $(ПОКАЗАТЬ)

# Сальдо месяца, «Отложено» и «Осталось» (Ф5, §15.5). Только чтение:
# сальдо нигде не хранится, оно пересчитывается из операций при каждом
# показе - хранимая копия разошлась бы с книжкой в первый же день, когда
# гашение задним числом пересчитает закрытый месяц.
#
# Без month= показывается текущий месяц в зоне owner, поэтому аргумент
# необязателен, в отличие от гашения: обзор без аргумента осмыслен.
МЕСЯЦ = $(if $(month),--month $(month),)

finance-balance:
	$(PY) -m jarvis_api.jobs.finance_balance $(МЕСЯЦ)

finance-balance-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.finance_balance $(МЕСЯЦ)

# Роли своих счетов (Ф5, §15.5). Счета заводит импорт с ролью unknown,
# роль ставит owner - по ней считается «Отложено» и по ней же разбор
# отличает перевод себе от перевода человеку (ADR-041).
#
# Без аргументов - показ. Разметка требует всех трёх сразу, и это
# проверяет джоб, а не make: частичные аргументы должны давать внятный
# отказ, а не молчаливый показ вместо записи.
СЧЁТ = $(if $(bank),--bank "$(bank)",) $(if $(account),--account "$(account)",) $(if $(role),--role $(role),)

finance-accounts:
	$(PY) -m jarvis_api.jobs.finance_accounts $(СЧЁТ)

finance-accounts-apply:
	$(PY) -m jarvis_api.jobs.finance_accounts $(СЧЁТ) --apply

# --- Фронт (Э7) ------------------------------------------------------------
# ci, а не install: ставится ровно то, что в package-lock.json. Иначе
# у owner, в образе и в CI оказываются разные версии одной зависимости,
# и «у меня работает» становится правдой в буквальном смысле.
web-install:
	$(NPM) ci

# Порт 3000, прокси на API по /api - настроено в next.config.ts. Рядом
# должен идти `make dev`: без API экран покажет деградацию, а не данные.
web-dev:
	$(NPM) run dev

# Статический экспорт в apps/web/out (ADR-020). Ту же команду выполняет
# сборочный слой infra/Dockerfile.web - расхождению взяться неоткуда.
web-build:
	$(NPM) run build

# Порядок как у Python-линта: сначала быстрый eslint, потом проверка типов.
web-lint:
	$(NPM) run lint
	$(NPM) run typecheck

web-test:
	$(NPM) run test

# Клиент генерируется из packages/contracts/openapi.json - тем же ручным
# шагом, что и сам контракт (make contract). Расхождение файла с контрактом
# роняет tests/contract.test.ts, то есть обычный make web-test.
web-client:
	$(NPM) run client

# Проверка перед включением туннеля: ADR-031 п. 3 «туннель не раньше
# аутентификации» прогоном, а не обещанием. Ничего не пишет, флага --apply
# у неё нет - отсюда и отсутствие пары целей, как у джобов.
#
# Умолчания адресов - loopback платы. Нужен поднятый стек: половина пунктов -
# живые запросы к API и к Caddy.
tunnel-check:
	$(PY) -m jarvis_api.jobs.tunnel_check

# exec, а не run: проверяется работающий процесс, а одноразовый контейнер
# api никого не слушает. Адрес Caddy - по имени сервиса, тем же путём,
# каким пойдёт запрос из туннеля.
tunnel-check-pi:
	$(DC_PI) exec api python -m jarvis_api.jobs.tunnel_check --api http://127.0.0.1:8000 --web http://web:8080

# Что назначено моделям. Ни строчки наружу и ни одного обращения к базе:
# отвечает на вопрос «доехал ли LLM_ROUTING до процесса и так ли он разобран»,
# который иначе проверяется первым живым вызовом. Опечатка в JSON и «задача
# не назначена» выглядят одинаково, а чинятся по-разному.
llm-routes:
	$(PY) -m jarvis_api.jobs.llm_routes

# exec, а не run: важно именно то окружение, которое видит работающий процесс.
# Одноразовый контейнер прочитал бы .env заново и показал бы не то, чем живёт
# API, - ровно та подмена, ради обнаружения которой цель и существует.
llm-routes-pi:
	$(DC_PI) exec api python -m jarvis_api.jobs.llm_routes

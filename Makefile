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

.PHONY: help venv dev test lint format compose-check up down logs         db-up db-down build migrate migrate-pi revision contract         web-install web-dev web-build web-lint web-test web-client tunnel-check tunnel-check-pi       sync-itmo sync-itmo-apply sync-itmo-pi sync-itmo-pi-apply         gcal-setup gcal-setup-apply gcal-setup-pi gcal-setup-pi-apply         sync-gcal sync-gcal-apply sync-gcal-pi sync-gcal-pi-apply         daily daily-apply daily-pi daily-pi-apply plan-today         backup backup-apply restore-check

help:
	@echo "venv          - create apps/api/.venv and install dev extras"
	@echo "dev           - run API with reload on :8000"
	@echo "test          - pytest"
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
	@echo "restore-check - restore the newest local dump into a scratch database (stage E1)"
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
	@echo "daily            - the daily chain: mirror + calendar, dry-run (stage E5)"
	@echo "daily-apply      - the same, doing the work (stage E5)"
	@echo "daily-pi         - dry-run inside the api container on the Pi (stage E5)"
	@echo "daily-pi-apply   - the same, doing the work on the Pi (stage E5)"
	@echo "plan-today    - course session planning, not implemented (courses track)"
	@echo "web-install   - install frontend dependencies from the lockfile (stage E7)"
	@echo "web-dev       - run the frontend with reload on :3000 (stage E7)"
	@echo "web-build     - export the frontend to apps/web/out (stage E7)"
	@echo "web-lint      - eslint + tsc --noEmit (stage E7)"
	@echo "web-test      - vitest (stage E7)"
	@echo "web-client    - regenerate the API client types from the contract (stage E7)"
	@echo "tunnel-check     - preflight before switching the tunnel on (stage E7)"
	@echo "tunnel-check-pi  - the same inside the running stack on the Pi (stage E7)"

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

restore-check:
	./infra/restore-check.sh

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
